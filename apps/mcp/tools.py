"""MCP tools. Runs inside the same process as `apps.mcp.server`, after
`django.setup()`, so tools can freely use Django's ORM/services -- no
separate database layer for the MCP process.
"""

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from mcp.server.fastmcp import FastMCP

from .auth import get_mcp_user

mcp_app = FastMCP(
    "findus-mcp",
    instructions="Findus MCP service (stub). Shares Django models/services.",
    host=settings.MCP_HOST,
    port=settings.MCP_PORT,
)


@mcp_app.tool()
def ping() -> str:
    """Liveness check for the Findus MCP service."""
    return "pong"


def _check_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


@mcp_app.tool()
async def health() -> dict:
    """Health check, also verifies the shared Django/PostgreSQL connection."""
    await sync_to_async(_check_database, thread_sensitive=True)()
    return {"status": "ok", "database": "reachable"}


def _document_comments_payload(document_id):
    from apps.documents.models import Document

    document = Document.objects.visible_to(get_mcp_user()).filter(pk=document_id).first()
    if document is None:
        return {"error": "not_found"}

    return {
        "document_id": document.id,
        "document_title": document.title,
        "comments": [
            {
                "id": comment.id,
                "body": comment.body,
                "author": comment.author.get_username() if comment.author else None,
                "created_at": comment.created_at.isoformat(),
                "follow_up_date": (
                    comment.follow_up_date.isoformat() if comment.follow_up_date else None
                ),
                "remind": comment.remind,
                "reminded_at": comment.reminded_at.isoformat() if comment.reminded_at else None,
            }
            for comment in document.comments.select_related("author")
        ],
    }


@mcp_app.tool()
async def document_comments(document_id: int) -> dict:
    """Kommentar-Chronik eines Dokuments (#1125) -- Notizen/Wiedervorlagen

    über das Dokument, nicht dessen extrahierter Inhalt (der läuft über die
    Chunk-/Embedding-Suche, nicht hier). Läuft wie jeder MCP-Zugriff über
    `visible_to(get_mcp_user())`; ein für den MCP-User unsichtbares oder
    nicht existierendes Dokument liefert denselben expliziten
    `{"error": "not_found"}`, statt sich stillschweigend hinter einer leeren
    Kommentarliste zu verstecken -- die beiden Fälle sind sonst nicht zu
    unterscheiden.
    """
    return await sync_to_async(_document_comments_payload, thread_sensitive=True)(document_id)
