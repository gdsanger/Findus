"""Shared ingest contract: file + Herkunfts-Metadaten -> `Document` + Enqueue.

`ingest_file` is the one place that knows how to turn *any* incoming file
into a `Document` -- dedup, storage, visibility, enqueue. A connector (the
folder watcher in `connectors/folder.py`, later the mail connector) only
has to know how to *fetch* a file and its provenance; it never touches
`Document` or the storage/queue plumbing directly.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import IO, Any, Literal, Optional

from django.core.files import File

from apps.accounts.models import Department
from apps.documents.mime import resolve_mime_type
from apps.documents.models import Correspondent, Document
from apps.documents.text_sanitize import clean_text
from apps.ingest.attachment_filter import filter_mail_attachments
from apps.ingest.mail_body import (
    build_index_text,
    clean_body,
    has_substance,
    render_body_pdf,
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


@dataclass(frozen=True)
class MailIngestResult:
    leitdokument: Document
    created: bool
    duplicate: bool
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


def _enqueue_body_processing(document_id: int) -> str:
    """Enqueue der Pipeline fuer ein Body-Leitdokument (#1070): Analyse ->
    Embedding, *ohne* Extraktion. Der Index-Text (Metadaten-Kopf +
    bereinigter Body) steht schon in `text_content` -- die Extraktions-
    Kaskade wuerde ihn nur durch aus dem generierten PDF zurueckgelesenen
    Text ersetzen. `analyze_document_task` verkettet selbst weiter zum
    Embedding."""
    from django_q.tasks import async_task

    from apps.documents.tasks import analyze_document_task

    return async_task(analyze_document_task, document_id)


def _safe_pdf_filename(subject: str) -> str:
    safe_subject = re.sub(r"[^\w\-. ]", "_", subject or "email").strip()[:80] or "email"
    return f"{safe_subject}.pdf"


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
    existing one recognized as a duplicate.

    Dedup is global by `sha256` (see Architektur.md: one container per
    customer, so a single archive-wide dedup scope is correct). On a
    duplicate hit, `on_duplicate="skip"` (default) leaves the existing
    document untouched; `on_duplicate="link"` additionally records the
    new occurrence's provenance on it -- neither path re-imports the
    file or writes a second blob to storage.

    `parent`/`child_role` hang a newly created `Document` under a
    Leitdokument (#1069/#1070) -- used for mail attachments
    (`child_role="mail_attachment"`). A duplicate hit is never re-parented;
    the existing document keeps whatever tree position it already had.
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

    # MIME zentral aus dem Inhalt bestimmen (#1077) -- die Client-/Upload-
    # Angabe `content_type` ist bei Scannern haeufig `octet-stream`. Der
    # Header genuegt fuer die Magic-Bytes-/libmagic-Erkennung.
    header = fileobj.read(_HASH_CHUNK_SIZE)
    fileobj.seek(0)
    resolved_content_type = resolve_mime_type(header, filename=filename, declared=content_type)
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


def _create_mail_leitdokument(
    *,
    subject: str,
    mail_metadata: dict,
    cleaned_body: str,
    source: str,
    department: Optional[Department],
    owner: Optional[Any],
    visibility: Optional[str],
    correspondent: Optional[Correspondent],
) -> tuple[Document, bool]:
    """Legt das Body-Leitdokument einer Mail an (#1070).

    Mit substanziellem Body (`cleaned_body` nicht leer): Index-Text
    (Metadaten-Kopf + Body) in `text_content`, generiertes PDF als
    `original_file`, Enqueue Analyse->Embedding. Ohne: duenne Huelle --
    nur Metadaten, kein Body-Text/PDF/Embedding. Die Anhaenge haengt der
    Aufrufer in beiden Faellen als Unterdokumente darunter.

    Zweiter Rueckgabewert: ob ein Body-PDF gefuellt wurde (True) oder es
    eine Huelle blieb (False) -- nur fuers Logging/Tests.
    """
    metadata = dict(mail_metadata)
    title = (subject or "(ohne Betreff)")[:255]

    document = Document(
        title=title,
        kind=Document.Kind.MAIL_BODY,
        source=source,
        owner=owner,
        visibility=visibility or Document.Visibility.DEPARTMENT,
        correspondent=correspondent,
    )

    filled = bool(cleaned_body)
    if filled:
        index_text = clean_text(build_index_text(mail_metadata, cleaned_body))
        pdf_bytes = render_body_pdf(mail_metadata, cleaned_body)
        filename = _safe_pdf_filename(subject)
        metadata.update(
            {
                "original_filename": filename,
                "mime_type": "application/pdf",
                "size": len(pdf_bytes),
                # Kennzeichnung "aus Body erzeugt" (Anforderung #4) -- neben
                # dem Badge, das `kind=mail_body` im Detail rendert.
                "body_source": "generated_from_mail",
            }
        )
        document.text_content = index_text
        document.markdown = index_text
        document.extraction_method = Document.ExtractionMethod.TEXT_LAYER
        document.sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        document.metadata = metadata
        document.processing_status = Document.ProcessingStatus.PENDING
        document.original_file.save(filename, File(BytesIO(pdf_bytes)), save=False)
        document.save()
    else:
        metadata["body_source"] = "thin_shell"
        document.metadata = metadata
        document.processing_status = Document.ProcessingStatus.READY
        document.save()

    if department is not None:
        document.departments.add(department)

    if filled:
        _enqueue_body_processing(document.id)

    return document, filled


def ingest_mail(
    *,
    body: Optional[str],
    body_content_type: str,
    attachments: list[MailAttachment],
    mail_metadata: dict,
    source: str = Document.Source.MAIL,
    department: Optional[Department] = None,
    owner: Optional[Any] = None,
    visibility: Optional[str] = None,
    on_duplicate: OnDuplicate = "skip",
    correspondent: Optional[Correspondent] = None,
    fill_body: bool = True,
) -> MailIngestResult:
    """Ingest a whole mail as one Leitdokument (Body) + n Unterdokumente
    (Anhaenge), #1070.

    `mail_metadata` carries the provenance both backends normalise the same
    way (`message_id`, `mail_from`, `mail_to`, `mail_subject`, `mail_date`).
    The Body wird immer aufbereitet und auf Substanz geprueft (auch bei
    Mails *mit* Anhang) -- der Substanz-Check (`mail_body.has_substance`,
    Schwelle `FINDUS_MAIL_BODY_MIN_WORDS`) entscheidet nur, ob das
    Leitdokument einen gefuellten Body (Klartext/PDF/Embedding) bekommt
    oder eine duenne Huelle bleibt; die Anhaenge haengen so oder so daran.

    Dedup des Leitdokuments ueber die Message-ID: eine bereits erfasste
    Mail wird nicht ein zweites Mal angelegt (Idempotenz-Netz neben dem
    Seen/Read-Flag der Connectoren). `fill_body=False` (Mailbox-Schalter
    `ingest_body`) erzwingt die Huelle auch bei substanziellem Body.
    """
    message_id = (mail_metadata.get("message_id") or "").strip()
    if message_id:
        existing = (
            Document.objects.filter(
                kind=Document.Kind.MAIL_BODY, metadata__message_id=message_id
            )
            .roots()
            .first()
        )
        if existing is not None:
            logger.info(
                "Ingest-Mail: Duplikat message_id=%s erkannt -> Document %s",
                message_id,
                existing.id,
            )
            return MailIngestResult(
                leitdokument=existing, created=False, duplicate=True, attachments=[]
            )

    # Grampf-Filter (#1081): Signatur-/Deko-Bilder (Social-Logos, winzige
    # Inline-Grafiken, Tracking-Pixel) raus, bevor sie zu Unterdokumenten
    # werden. Braucht den Body fuer die `cid:`-Referenzen; Nicht-Bilder
    # (PDF/Office/echte Belege) bleiben unangetastet.
    attachments = filter_mail_attachments(attachments, body=body)

    subject = mail_metadata.get("mail_subject") or ""
    cleaned = clean_body(body or "", body_content_type or "")
    substantial = fill_body and bool(cleaned) and has_substance(cleaned)

    leitdokument, filled = _create_mail_leitdokument(
        subject=subject,
        mail_metadata=mail_metadata,
        cleaned_body=cleaned if substantial else "",
        source=source,
        department=department,
        owner=owner,
        visibility=visibility,
        correspondent=correspondent,
    )
    logger.info(
        "Ingest-Mail: Leitdokument %s angelegt (kind=mail_body, body_gefuellt=%s, "
        "anhaenge=%s, message_id=%s)",
        leitdokument.id,
        filled,
        len(attachments),
        message_id,
    )

    attachment_results: list[IngestResult] = []
    for attachment in attachments:
        result = ingest_file(
            attachment.fileobj,
            filename=attachment.filename,
            source=source,
            department=department,
            owner=owner,
            visibility=visibility or leitdokument.visibility,
            content_type=attachment.content_type,
            origin_metadata=dict(mail_metadata),
            on_duplicate=on_duplicate,
            correspondent=correspondent,
            parent=leitdokument,
            child_role=Document.ChildRole.MAIL_ATTACHMENT,
        )
        attachment_results.append(result)

    return MailIngestResult(
        leitdokument=leitdokument,
        created=True,
        duplicate=False,
        attachments=attachment_results,
    )
