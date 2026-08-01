"""MCP tools. Runs inside the same process as `apps.mcp.server`, after
`django.setup()`, so tools can freely use Django's ORM/services -- no
separate database layer for the MCP process.
"""

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from mcp.server.fastmcp import FastMCP

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
