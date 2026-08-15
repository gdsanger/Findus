"""Gemeinsame Bausteine, um eine per stdlib `email` geparste Nachricht in
Absender/Body/Anhaenge zu zerlegen -- geteilt vom IMAP-Connector
(`connectors/mail_imap.py`) und dem `.eml`-Ingest (`service.ingest_eml_file`,
#1133), damit die beiden Wege nicht auseinanderlaufen (siehe CLAUDE.md).
Microsoft Graph (`connectors/mail_graph.py`) liefert dagegen JSON statt
RFC822-Bytes und behaelt deshalb seine eigenen Parsing-Bausteine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from io import BytesIO
from typing import IO, Optional


@dataclass(frozen=True)
class MailAttachment:
    """Ein Mail-Anhang, normalisiert ueber beide Backends (IMAP/Graph) --
    der Connector reicht nur Bytes + Herkunft, der Ingest kennt den Rest.

    `content_id`/`inline` speisen den Grampf-Filter (#1081): Signatur-/
    Deko-Bilder (Content-Disposition inline bzw. per `cid:` im Body
    referenziert) werden gar nicht erst als Unterdokument angelegt."""

    fileobj: IO[bytes]
    filename: str
    content_type: str = ""
    content_id: str = ""
    inline: bool = False


def sender_email_and_name(msg: EmailMessage) -> tuple[str, str]:
    name, address = parseaddr(msg.get("From", ""))
    return address, name


def body_content(msg: EmailMessage) -> tuple[Optional[str], Optional[str]]:
    """Return `(content, content_type)` of the mail's preferred body part
    (HTML vor Text -- die reichhaltigere Quelle fuer `mail_body.clean_body()`
    bzw. den Grampf-Filter, der die `cid:`-Referenzen daraus liest), or
    `(None, None)`."""
    body_part = msg.get_body(preferencelist=("html", "plain"))
    if body_part is None:
        return None, None
    content = body_part.get_content()
    if not content:
        return None, None
    return content, body_part.get_content_type()


def _attachment_payload(part) -> Optional[bytes]:
    """Bytes eines Anhang-Teils. `message/rfc822` (eine weitergeleitete Mail
    als Anhang) hat keine direkte Payload -- `get_payload(decode=True)`
    liefert `None`, weil die "Payload" eine verschachtelte `Message` ist,
    keine kodierten Bytes. Stattdessen die verschachtelte Nachricht selbst
    serialisieren (kein rekursives Auspacken -- sie bleibt ein Anhang)."""
    payload = part.get_payload(decode=True)
    if payload:
        return payload
    if part.get_content_type() == "message/rfc822":
        nested = part.get_payload()
        if isinstance(nested, list) and nested:
            try:
                return nested[0].as_bytes()
            except Exception:
                return None
    return None


def collect_attachments(msg: EmailMessage) -> list[MailAttachment]:
    """Echte Anhaenge -- `iter_attachments()` schliesst Inline-Bilder aus
    `multipart/related` (Body + eingebettete Bilder) per Definition bereits
    aus, weil es multipart-Teile und den erkannten Body-Teil ueberspringt.
    Der Grampf-Filter (#1081, `attachment_filter.filter_mail_attachments`)
    sortiert danach noch Signatur-/Deko-Bilder aus, die trotzdem als
    eigenstaendiger `attachment`-Teil vorliegen."""
    attachments: list[MailAttachment] = []
    for part in msg.iter_attachments():
        payload = _attachment_payload(part)
        if not payload:
            continue
        disposition = (part.get_content_disposition() or "").strip().lower()
        content_type = part.get_content_type()
        filename = part.get_filename() or (
            "email.eml" if content_type == "message/rfc822" else "attachment"
        )
        attachments.append(
            MailAttachment(
                fileobj=BytesIO(payload),
                filename=filename,
                content_type=content_type,
                content_id=part.get("Content-ID", "") or "",
                inline=disposition == "inline",
            )
        )
    return attachments


def parse_mail_date(raw: str) -> Optional[datetime]:
    """RFC-konform geparster `Date`-Header, oder `None` bei fehlendem oder
    unparsbarem Wert -- der Aufrufer entscheidet dann selbst ueber den
    Ingest-Zeitpunkt-Fallback (Anforderung #1133/#3)."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
