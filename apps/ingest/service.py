"""Shared ingest contract: file + Herkunfts-Metadaten -> `Document` + Enqueue.

`ingest_file` is the one place that knows how to turn *any* incoming file
into a `Document` -- dedup, storage, visibility, enqueue. A connector (the
folder watcher in `connectors/folder.py`, the mail connectors) only has to
know how to *fetch* a file and its provenance; it never touches `Document`
or the storage/queue plumbing directly.

`ingest_mail` sits one level above `ingest_file` for the mail case (#1070):
es macht den aufbereiteten Mail-Body zum **Leitdokument** (`kind=mail_body`)
und hängt die Anhänge als **Unterdokumente** (`child_role=mail_attachment`,
`parent` = Leitdokument) über `ingest_file` darunter. Beide Mail-Connectoren
(IMAP + Graph) liefern nur noch Betreff/Absender/Body/Anhänge und rufen
`ingest_mail` -- die gesamte Body-Aufbereitung/Substanz-Logik lebt hier an
einer Stelle.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import IO, Any, Literal, Optional

from django.conf import settings
from django.core.files import File

from apps.accounts.models import Department
from apps.documents.models import Correspondent, Document

from .mail_body import (
    PdfRenderError,
    build_body_html_document,
    build_index_text,
    prepare_body,
    render_pdf_from_html,
)

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE = 1024 * 1024

OnDuplicate = Literal["skip", "link"]


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool
    duplicate: bool


@dataclass(frozen=True)
class MailAttachment:
    """Ein Mail-Anhang, so wie ihn ein Connector aus IMAP/Graph liefert --
    die reinen Bytes plus Name/MIME, entkoppelt vom jeweiligen Protokoll."""

    filename: str
    content: bytes
    content_type: str = ""


@dataclass(frozen=True)
class MailIngestResult:
    lead: Document
    lead_created: bool
    attachments: list[IngestResult] = field(default_factory=list)


def _hash_and_size(fileobj: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: fileobj.read(_HASH_CHUNK_SIZE), b""):
        digest.update(chunk)
        size += len(chunk)
    fileobj.seek(0)
    return digest.hexdigest(), size


def _handle_duplicate(
    existing: Document,
    *,
    on_duplicate: OnDuplicate,
    source: str,
    filename: str,
    origin_metadata: Optional[dict],
) -> IngestResult:
    logger.info(
        "Ingest: Duplikat sha256=%s erkannt (source=%s, filename=%s, on_duplicate=%s)",
        existing.sha256,
        source,
        filename,
        on_duplicate,
    )
    if on_duplicate == "link":
        occurrences = existing.metadata.setdefault("duplicate_occurrences", [])
        occurrences.append(
            {"source": source, "filename": filename, **(origin_metadata or {})}
        )
        existing.save(update_fields=["metadata"])
    return IngestResult(document=existing, created=False, duplicate=True)


def _enqueue_processing(document_id: int) -> str:
    from django_q.tasks import async_task

    from apps.documents.tasks import extract_document_task

    return async_task(extract_document_task, document_id)


def _enqueue_analysis(document_id: int) -> str:
    """Wie `_enqueue_processing`, aber *ohne* die Extraktionsstufe -- für das
    Mail-Leitdokument (#1070), dessen `text_content` (Index-Text) schon fertig
    aufbereitet vorliegt. Startet direkt bei der KI-Analyse, die anschließend
    wie üblich ans Chunking/Embedding übergibt; das generierte Body-PDF erneut
    durch die OCR-Kaskade zu jagen wäre nur teurer Umweg zum selben Text.
    """
    from django_q.tasks import async_task

    from apps.documents.tasks import analyze_document_task

    return async_task(analyze_document_task, document_id)


def ingest_file(
    fileobj: IO[bytes],
    *,
    filename: str,
    source: str,
    title: Optional[str] = None,
    department: Optional[Department] = None,
    owner: Optional[Any] = None,
    visibility: Optional[str] = None,
    content_type: str = "",
    origin_metadata: Optional[dict] = None,
    on_duplicate: OnDuplicate = "skip",
    correspondent: Optional[Correspondent] = None,
    parent: Optional[Document] = None,
    child_role: str = "",
) -> IngestResult:
    """Ingest `fileobj` as a new `Document`, or recognize it as a duplicate.

    `source` must be one of `Document.Source`. `origin_metadata` is
    connector-specific provenance (e.g. the watched folder path, or a
    mail message id) merged into `Document.metadata`. `correspondent`
    (e.g. resolved from a mail sender address) is attached the same way
    `department` is -- only to a newly created `Document`, never onto an
    existing one recognized as a duplicate. `parent`/`child_role` place the
    new `Document` in the hierarchy (#1069) -- e.g. a mail attachment under
    its mail-body lead document (`child_role=mail_attachment`).

    Dedup is global by `sha256` (see Architektur.md: one container per
    customer, so a single archive-wide dedup scope is correct). On a
    duplicate hit, `on_duplicate="skip"` (default) leaves the existing
    document untouched; `on_duplicate="link"` additionally records the
    new occurrence's provenance on it -- neither path re-imports the
    file or writes a second blob to storage.
    """

    sha256, size = _hash_and_size(fileobj)

    existing = Document.objects.filter(sha256=sha256).first()
    if existing is not None:
        return _handle_duplicate(
            existing,
            on_duplicate=on_duplicate,
            source=source,
            filename=filename,
            origin_metadata=origin_metadata,
        )

    resolved_content_type = content_type or mimetypes.guess_type(filename)[0] or (
        "application/octet-stream"
    )
    metadata = dict(origin_metadata or {})
    metadata.update(
        {
            "original_filename": filename,
            "mime_type": resolved_content_type,
            "size": size,
        }
    )

    document = Document(
        title=title or filename,
        source=source,
        processing_status=Document.ProcessingStatus.PENDING,
        sha256=sha256,
        metadata=metadata,
        owner=owner,
        visibility=visibility or Document.Visibility.DEPARTMENT,
        correspondent=correspondent,
        parent=parent,
        child_role=child_role,
    )
    document.original_file.save(filename, File(fileobj), save=False)
    document.save()
    if department is not None:
        document.departments.add(department)

    _enqueue_processing(document.id)
    logger.info(
        "Ingest: Document %s angelegt (source=%s, filename=%s, sha256=%s)",
        document.id,
        source,
        filename,
        sha256,
    )
    return IngestResult(document=document, created=True, duplicate=False)


def _format_sender(email: str, name: str) -> str:
    email = (email or "").strip()
    name = (name or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _mail_pdf_filename(subject: str) -> str:
    safe = re.sub(r"[^\w\-. ]", "_", subject or "").strip()[:80] or "mail"
    return f"{safe}.pdf"


def _find_existing_lead(message_id: str) -> Optional[Document]:
    """Dedup des Leitdokuments über die Message-ID (#1070) -- der Anhang-Dedup
    läuft weiterhin per sha256 in `ingest_file`. Nur bei nicht-leerer
    Message-ID, sonst würden mehrere Mails ohne Message-ID kollabieren."""
    if not message_id:
        return None
    return Document.objects.filter(
        kind=Document.Kind.MAIL_BODY, metadata__message_id=message_id
    ).first()


def _create_lead_document(
    *,
    subject: str,
    sender: str,
    date: str,
    body: Optional[str],
    body_content_type: str,
    metadata_base: dict,
    department: Optional[Department],
    owner: Optional[Any],
    visibility: Optional[str],
    correspondent: Optional[Correspondent],
    fill_body: bool,
) -> Document:
    """Legt das Mail-Leitdokument (`kind=mail_body`) an.

    Der Body wird immer aufbereitet und auf Substanz geprüft (nicht nur bei
    Mails ohne Anhang): trägt er substanziellen Inhalt (Wortzahl >=
    `FINDUS_MAIL_BODY_MIN_WORDS`), entstehen Index-Text (Embedding) und ein
    PDF (Ansicht); sonst bleibt das Leitdokument eine dünne Hülle (nur
    Metadaten), an der die Anhänge trotzdem hängen.
    """
    body_result = prepare_body(body, body_content_type) if fill_body else None
    min_words = getattr(settings, "FINDUS_MAIL_BODY_MIN_WORDS", 5)
    has_substance = bool(body_result and body_result.word_count >= min_words)

    metadata = dict(metadata_base)
    metadata["mail_body_word_count"] = body_result.word_count if body_result else 0

    document = Document(
        title=subject or "(ohne Betreff)",
        source=Document.Source.MAIL,
        kind=Document.Kind.MAIL_BODY,
        owner=owner,
        visibility=visibility or Document.Visibility.DEPARTMENT,
        correspondent=correspondent,
    )

    if has_substance:
        document.text_content = build_index_text(
            subject=subject, sender=sender, date=date, body_text=body_result.text
        )
        metadata["mail_body_generated"] = True
        metadata["mail_body_from_html"] = "html" in (body_content_type or "").lower()
        filename = _mail_pdf_filename(subject)
        pdf_bytes = _render_body_pdf(
            subject=subject, sender=sender, date=date, body_html=body_result.html
        )
        if pdf_bytes is not None:
            metadata.update(
                {
                    "original_filename": filename,
                    "mime_type": "application/pdf",
                    "size": len(pdf_bytes),
                }
            )
            document.sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            document.metadata = metadata
            document.processing_status = Document.ProcessingStatus.PENDING
            document.original_file.save(filename, File(BytesIO(pdf_bytes)), save=False)
            document.save()
            _finalize_lead_departments(document, department)
            _enqueue_analysis(document.id)
            return document
        # PDF-Rendering nicht verfügbar: Index-Text trotzdem erhalten, nur
        # ohne PDF-Ansicht -- besser eine indizierte Mail ohne Vorschau als
        # gar keine.
        metadata["mail_body_pdf_error"] = True
        document.metadata = metadata
        document.processing_status = Document.ProcessingStatus.PENDING
        document.save()
        _finalize_lead_departments(document, department)
        _enqueue_analysis(document.id)
        return document

    # Dünne Hülle: nur Metadaten, kein Body-Text/PDF/Embedding.
    metadata["mail_body_substanceless"] = True
    document.metadata = metadata
    document.processing_status = Document.ProcessingStatus.READY
    document.save()
    _finalize_lead_departments(document, department)
    return document


def _finalize_lead_departments(
    document: Document, department: Optional[Department]
) -> None:
    if department is not None:
        document.departments.add(department)


def _render_body_pdf(
    *, subject: str, sender: str, date: str, body_html: str
) -> Optional[bytes]:
    html_document = build_body_html_document(
        subject=subject, sender=sender, date=date, body_html=body_html
    )
    try:
        return render_pdf_from_html(html_document)
    except PdfRenderError:
        logger.warning(
            "Ingest-Mail: PDF-Rendering des Bodys fehlgeschlagen, indexiere nur Text",
            exc_info=True,
        )
        return None


def ingest_mail(
    *,
    message_id: str,
    subject: str,
    sender_email: str,
    sender_name: str = "",
    date: str = "",
    body: Optional[str] = None,
    body_content_type: str = "text/plain",
    attachments: Optional[list[MailAttachment]] = None,
    department: Optional[Department] = None,
    owner: Optional[Any] = None,
    visibility: Optional[str] = None,
    on_duplicate: OnDuplicate = "skip",
    correspondent: Optional[Correspondent] = None,
    fill_body: bool = True,
) -> MailIngestResult:
    """Ingest a whole mail as a lead document (`kind=mail_body`) plus its
    attachments as child documents (`child_role=mail_attachment`), #1070.

    The lead is deduplicated by `message_id`; attachments keep the global
    sha256 dedup of `ingest_file`. `fill_body=False` (from the connector's
    `ingest_body` config flag) forces the lead to stay a metadata-only
    shell regardless of the body's substance.
    """
    attachments = attachments or []
    metadata_base = {
        "message_id": message_id,
        "mail_from": sender_email,
        "mail_subject": subject,
        "mail_date": date,
    }

    existing = _find_existing_lead(message_id)
    if existing is not None:
        logger.info(
            "Ingest-Mail: Leitdokument für Message-ID %s existiert bereits (Document %s)",
            message_id,
            existing.id,
        )
        return MailIngestResult(lead=existing, lead_created=False)

    sender = _format_sender(sender_email, sender_name)
    lead = _create_lead_document(
        subject=subject,
        sender=sender,
        date=date,
        body=body,
        body_content_type=body_content_type,
        metadata_base=metadata_base,
        department=department,
        owner=owner,
        visibility=visibility,
        correspondent=correspondent,
        fill_body=fill_body,
    )
    logger.info(
        "Ingest-Mail: Leitdokument %s angelegt (message_id=%s, hülle=%s)",
        lead.id,
        message_id,
        lead.is_body_shell,
    )

    attachment_results: list[IngestResult] = []
    for attachment in attachments:
        if not attachment.content:
            continue
        result = ingest_file(
            BytesIO(attachment.content),
            filename=attachment.filename or "attachment",
            source=Document.Source.MAIL,
            department=department,
            owner=owner,
            visibility=visibility,
            content_type=attachment.content_type,
            origin_metadata=metadata_base,
            on_duplicate=on_duplicate,
            correspondent=correspondent,
            parent=lead,
            child_role=Document.ChildRole.MAIL_ATTACHMENT,
        )
        attachment_results.append(result)
        logger.info(
            "Ingest-Mail: Anhang %s -> Document %s (created=%s, duplicate=%s, parent=%s)",
            attachment.filename,
            result.document.id,
            result.created,
            result.duplicate,
            lead.id,
        )

    return MailIngestResult(
        lead=lead, lead_created=True, attachments=attachment_results
    )
