"""IMAP-Postfach-Connector: pollt ein Postfach per IMAP4, holt ungelesene
Mails und übergibt Anhänge (+ optional den Mailbody) an den gemeinsamen
Ingest-Kontrakt (`apps.ingest.service`).

Reines `imaplib`/`email` (Stdlib) statt einer zusätzlichen Abhängigkeit --
passt zum Rest der Codebase (z. B. `apps.ai.providers`, `apps.mail.backends`:
plain HTTP/Stdlib statt Vendor-SDK, siehe deren Docstrings).

`BODY.PEEK[]` statt `BODY[]` beim Fetch, damit das Abrufen selbst eine
Mail nicht schon als gelesen markiert -- `\\Seen` wird erst nach
erfolgreichem Ingest gesetzt. Das ist zugleich der Idempotenz-Marker
(Arbeitsmenge ist immer nur `UNSEEN`) und die Fehlertoleranz: schlägt die
Verarbeitung einer Mail fehl, bleibt sie ungelesen und wird beim
nächsten Poll erneut versucht, der Durchlauf läuft mit den übrigen
Mails weiter.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from io import BytesIO
from typing import Optional

from django.conf import settings

from apps.accounts.models import Department
from apps.documents.models import Document
from apps.documents.services import find_or_create_correspondent_by_email
from apps.ingest.service import ingest_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImapMailbox:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    folder: str = "INBOX"
    department: Optional[str] = None
    visibility: str = Document.Visibility.DEPARTMENT
    on_duplicate: str = "skip"
    ingest_body: bool = True


def get_imap_mailboxes() -> list[ImapMailbox]:
    config = getattr(settings, "FINDUS_INGEST_MAIL_SOURCES", {}).get("imap") or {}
    if not config.get("enabled") or not config.get("host"):
        return []
    return [
        ImapMailbox(
            host=config["host"],
            port=int(config.get("port") or 993),
            username=config.get("username") or "",
            password=config.get("password") or "",
            use_ssl=bool(config.get("use_ssl", True)),
            folder=config.get("folder") or "INBOX",
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


def _connect(mailbox: ImapMailbox) -> imaplib.IMAP4:
    connection_cls = imaplib.IMAP4_SSL if mailbox.use_ssl else imaplib.IMAP4
    connection = connection_cls(mailbox.host, mailbox.port)
    connection.login(mailbox.username, mailbox.password)
    connection.select(mailbox.folder)
    return connection


def _unseen_ids(connection: imaplib.IMAP4) -> list[bytes]:
    status, data = connection.search(None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_message(connection: imaplib.IMAP4, message_id: bytes) -> Optional[EmailMessage]:
    status, data = connection.fetch(message_id, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1], policy=email.policy.default)


def _mark_seen(connection: imaplib.IMAP4, message_id: bytes) -> None:
    connection.store(message_id, "+FLAGS", "\\Seen")


def _sender_email_and_name(msg: EmailMessage) -> tuple[str, str]:
    name, address = parseaddr(msg.get("From", ""))
    return address, name


def _safe_filename(subject: str, extension: str) -> str:
    safe_subject = re.sub(r"[^\w\-. ]", "_", subject or "email").strip()[:80] or "email"
    return f"{safe_subject}.{extension}"


def _assign_correspondent(result, correspondent) -> None:
    if correspondent is None or not result.created:
        return
    result.document.correspondent = correspondent
    result.document.save(update_fields=["correspondent"])


def _ingest_attachments(msg, mailbox, department, correspondent, origin_metadata) -> None:
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        result = ingest_file(
            BytesIO(payload),
            filename=part.get_filename() or "attachment",
            source=Document.Source.MAIL,
            department=department,
            visibility=mailbox.visibility,
            content_type=part.get_content_type(),
            origin_metadata=origin_metadata,
            on_duplicate=mailbox.on_duplicate,
            correspondent=correspondent,
        )
        _assign_correspondent(result, correspondent)
        logger.info(
            "Ingest-IMAP-Postfach: %s -> Document %s (created=%s, duplicate=%s)",
            part.get_filename(),
            result.document.id,
            result.created,
            result.duplicate,
        )


def _ingest_body(msg, mailbox, department, correspondent, origin_metadata) -> None:
    body_part = msg.get_body(preferencelist=("html", "plain"))
    if body_part is None:
        return
    content = body_part.get_content()
    if not content:
        return
    content_type = body_part.get_content_type()
    extension = "html" if content_type == "text/html" else "txt"
    subject = origin_metadata.get("subject", "")
    result = ingest_file(
        BytesIO(content.encode("utf-8")),
        filename=_safe_filename(subject, extension),
        source=Document.Source.MAIL,
        title=subject or None,
        department=department,
        visibility=mailbox.visibility,
        content_type=content_type,
        origin_metadata=origin_metadata,
        on_duplicate=mailbox.on_duplicate,
        correspondent=correspondent,
    )
    _assign_correspondent(result, correspondent)
    logger.info(
        "Ingest-IMAP-Postfach: Mailbody -> Document %s (created=%s, duplicate=%s)",
        result.document.id,
        result.created,
        result.duplicate,
    )


def scan_mailbox(mailbox: ImapMailbox) -> None:
    """One polling pass over a single configured IMAP mailbox."""

    department = _resolve_department(mailbox.department)
    connection = _connect(mailbox)
    try:
        for message_id in _unseen_ids(connection):
            try:
                msg = _fetch_message(connection, message_id)
                if msg is None:
                    continue

                sender_email, sender_name = _sender_email_and_name(msg)
                correspondent = find_or_create_correspondent_by_email(sender_email, sender_name)
                origin_metadata = {
                    "message_id": msg.get("Message-ID", ""),
                    "sender": sender_email,
                    "subject": msg.get("Subject", ""),
                    "received_at": msg.get("Date", ""),
                }

                _ingest_attachments(msg, mailbox, department, correspondent, origin_metadata)
                if mailbox.ingest_body:
                    _ingest_body(msg, mailbox, department, correspondent, origin_metadata)

                _mark_seen(connection, message_id)
            except Exception:
                logger.exception(
                    "Ingest-IMAP-Postfach: Verarbeitung fehlgeschlagen für Message-Id %s",
                    message_id,
                )
    finally:
        try:
            connection.logout()
        except Exception:
            logger.warning("Ingest-IMAP-Postfach: Logout fehlgeschlagen", exc_info=True)


def scan_all_imap_mailboxes() -> None:
    for mailbox in get_imap_mailboxes():
        try:
            scan_mailbox(mailbox)
        except Exception:
            logger.exception(
                "Ingest-IMAP-Postfach: Poll fehlgeschlagen für Postfach %s", mailbox.host
            )
