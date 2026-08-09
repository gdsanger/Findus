"""Microsoft-Graph-Postfach-Connector: liest ungelesene Mails aus einem
per Graph API erreichbaren Postfach und übergibt sie an den gemeinsamen
Ingest-Kontrakt (`apps.ingest.service.ingest_mail`). Jede Mail wird zu
*einem* Leitdokument aus dem Body (`kind=mail_body`), die Anhänge hängen
als Unterdokumente (`child_role=mail_attachment`) darunter (#1070 --
nutzt die Dokument-Hierarchie aus #1069). Der Body wird immer aufbereitet
(HTML -> Klartext + PDF) und auf Substanz geprüft; ohne substanziellen
Inhalt bleibt das Leitdokument eine dünne Hülle (nur Metadaten), die
Anhänge hängen trotzdem daran.

Nutzt den client-credentials Token-Fetch aus `apps.mail.backends.graph`
(`GraphTokenClient`) -- dieselbe App-Registrierung wie der Mail-Versand,
nur mit zusätzlich erteilter Mail.Read-Berechtigung (application) für
dieses Postfach.

Idempotenz über `isRead`: die Arbeitsmenge ist `isRead eq false`, nach
erfolgreichem Ingest wird die Mail per `PATCH isRead=true` markiert.
Kein Verschieben in einen Zielordner -- das würde nur einen weiteren
API-Call und die Existenz/Anlage dieses Ordners voraussetzen, ohne einen
zusätzlichen Nutzen gegenüber dem Read-Flag zu bringen. Schlägt die
Verarbeitung einer Mail fehl, bleibt sie ungelesen und wird beim
nächsten Poll erneut versucht (Fehlertoleranz, andere Mails im selben
Durchlauf laufen weiter).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests
from django.conf import settings

from apps.accounts.models import Department
from apps.documents.models import Document
from apps.documents.services import find_or_create_correspondent_by_email
from apps.ingest.service import MailAttachment, ingest_mail
from apps.mail.backends.graph import GraphTokenClient

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MESSAGE_SELECT_FIELDS = (
    "id,subject,from,toRecipients,receivedDateTime,hasAttachments,body"
)


@dataclass(frozen=True)
class GraphMailbox:
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str
    folder: str = "inbox"
    department: Optional[str] = None
    visibility: str = Document.Visibility.DEPARTMENT
    on_duplicate: str = "skip"
    ingest_body: bool = True
    timeout: float = 30.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.5


def get_graph_mailboxes() -> list[GraphMailbox]:
    config = getattr(settings, "FINDUS_INGEST_MAIL_SOURCES", {}).get("graph") or {}
    if not config.get("enabled") or not config.get("mailbox"):
        return []
    return [
        GraphMailbox(
            tenant_id=config["tenant_id"],
            client_id=config["client_id"],
            client_secret=config["client_secret"],
            mailbox=config["mailbox"],
            folder=config.get("folder") or "inbox",
            department=config.get("department"),
            visibility=config.get("visibility") or Document.Visibility.DEPARTMENT,
            on_duplicate=config.get("on_duplicate") or "skip",
            ingest_body=config.get("ingest_body", True),
        )
    ]


def _resolve_department(name: Optional[str]) -> Optional[Department]:
    if not name:
        return None
    department, _ = Department.objects.get_or_create(name=name)
    return department


def _token_client(mailbox: GraphMailbox) -> GraphTokenClient:
    return GraphTokenClient(
        tenant_id=mailbox.tenant_id,
        client_id=mailbox.client_id,
        client_secret=mailbox.client_secret,
        timeout=mailbox.timeout,
        max_retries=mailbox.max_retries,
        retry_backoff_seconds=mailbox.retry_backoff_seconds,
    )


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fetch_unread_messages(mailbox: GraphMailbox, token: str) -> list[dict]:
    url = (
        f"{GRAPH_BASE_URL}/users/{mailbox.mailbox}/mailFolders/{mailbox.folder}/messages"
        f"?$filter=isRead eq false&$select={MESSAGE_SELECT_FIELDS}"
    )
    response = requests.get(url, headers=_headers(token), timeout=mailbox.timeout)
    response.raise_for_status()
    return response.json().get("value", [])


def _fetch_attachments(mailbox: GraphMailbox, token: str, message_id: str) -> list[dict]:
    url = f"{GRAPH_BASE_URL}/users/{mailbox.mailbox}/messages/{message_id}/attachments"
    response = requests.get(url, headers=_headers(token), timeout=mailbox.timeout)
    response.raise_for_status()
    return response.json().get("value", [])


def _mark_as_read(mailbox: GraphMailbox, token: str, message_id: str) -> None:
    url = f"{GRAPH_BASE_URL}/users/{mailbox.mailbox}/messages/{message_id}"
    response = requests.patch(
        url, headers=_headers(token), json={"isRead": True}, timeout=mailbox.timeout
    )
    response.raise_for_status()


def _sender_email_and_name(message: dict) -> tuple[str, str]:
    sender = (message.get("from") or {}).get("emailAddress") or {}
    return sender.get("address", ""), sender.get("name", "")


def _recipients(message: dict) -> str:
    addresses = [
        ((recipient or {}).get("emailAddress") or {}).get("address", "")
        for recipient in message.get("toRecipients") or []
    ]
    return ", ".join(address for address in addresses if address)


def _collect_attachments(mailbox, token, message) -> list[MailAttachment]:
    if not message.get("hasAttachments"):
        return []
    attachments: list[MailAttachment] = []
    for attachment in _fetch_attachments(mailbox, token, message["id"]):
        if attachment.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue
        content = attachment.get("contentBytes")
        if not content:
            continue
        attachments.append(
            MailAttachment(
                fileobj=BytesIO(base64.b64decode(content)),
                filename=attachment.get("name") or "attachment",
                content_type=attachment.get("contentType", ""),
                content_id=attachment.get("contentId") or "",
                inline=bool(attachment.get("isInline")),
            )
        )
    return attachments


def _body_content(message: dict) -> tuple[str, str]:
    body = message.get("body") or {}
    content = body.get("content") or ""
    content_type = "text/html" if (body.get("contentType") or "text") == "html" else "text/plain"
    return content, content_type


def scan_mailbox(mailbox: GraphMailbox) -> None:
    """One polling pass over a single configured Graph mailbox.

    Every message is handled independently: a failure on one message is
    logged and it's left unread for the next poll, the scan continues
    with the rest (Fehlertoleranz aus den Anforderungen).
    """

    department = _resolve_department(mailbox.department)
    token_client = _token_client(mailbox)
    token = token_client.get_token()

    for message in _fetch_unread_messages(mailbox, token):
        message_id = message["id"]
        try:
            sender_email, sender_name = _sender_email_and_name(message)
            correspondent = find_or_create_correspondent_by_email(sender_email, sender_name)
            body, body_content_type = _body_content(message)
            mail_metadata = {
                "message_id": message_id,
                "mail_from": sender_email,
                "mail_to": _recipients(message),
                "mail_subject": message.get("subject", ""),
                "mail_date": message.get("receivedDateTime", ""),
            }

            result = ingest_mail(
                body=body,
                body_content_type=body_content_type,
                attachments=_collect_attachments(mailbox, token, message),
                mail_metadata=mail_metadata,
                department=department,
                visibility=mailbox.visibility,
                on_duplicate=mailbox.on_duplicate,
                correspondent=correspondent,
                fill_body=mailbox.ingest_body,
            )
            logger.info(
                "Ingest-Graph-Postfach: Mail -> Leitdokument %s "
                "(created=%s, duplicate=%s, anhaenge=%s)",
                result.leitdokument.id,
                result.created,
                result.duplicate,
                len(result.attachments),
            )

            _mark_as_read(mailbox, token, message_id)
        except Exception:
            logger.exception(
                "Ingest-Graph-Postfach: Verarbeitung fehlgeschlagen für Message %s", message_id
            )


def scan_all_graph_mailboxes() -> None:
    for mailbox in get_graph_mailboxes():
        try:
            scan_mailbox(mailbox)
        except Exception:
            logger.exception(
                "Ingest-Graph-Postfach: Poll fehlgeschlagen für Postfach %s", mailbox.mailbox
            )
