"""Shared ingest contract: file + Herkunfts-Metadaten -> `Document` + Enqueue.

`ingest_file` is the one place that knows how to turn *any* incoming file
into a `Document` -- dedup, storage, visibility, enqueue. A connector (the
folder watcher in `connectors/folder.py`, later the mail connector) only
has to know how to *fetch* a file and its provenance; it never touches
`Document` or the storage/queue plumbing directly.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import IO, Any, Literal, Optional

from django.core.files import File
from django.utils import timezone

from apps.accounts.models import Department
from apps.documents.mime import resolve_mime_type
from apps.documents.models import Correspondent, Document
from apps.documents.services import find_or_create_correspondent_by_email
from apps.documents.text_sanitize import clean_text
from apps.ingest.attachment_filter import filter_mail_attachments
from apps.ingest.mail_body import (
    build_index_text,
    clean_body,
    has_substance,
    render_body_pdf,
)
from apps.ingest.mail_parse import (
    MailAttachment,
    body_content,
    collect_attachments,
    parse_mail_date,
    sender_email_and_name,
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


def sniff_mime_type(fileobj: IO[bytes], *, filename: str, content_type: str = "") -> str:
    """Peek at the first chunk of `fileobj` to resolve its MIME type without
    consuming the stream (#1077 `resolve_mime_type`, content-first) -- lets
    a connector decide *before* ingest which path applies (#1133): `.eml`
    mail-envelopes (`message/rfc822`) via `ingest_eml_file`, everything else
    via plain `ingest_file` (which resolves the same MIME type again once
    it has already deduped -- cheap, and keeps both functions
    self-contained rather than threading the result through)."""
    header = fileobj.read(_HASH_CHUNK_SIZE)
    fileobj.seek(0)
    return resolve_mime_type(header, filename=filename, declared=content_type)


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
                # Extraktions-Provenienz (#1142), wie beim Kaskaden-Pfad in
                # `apps.documents.extraction` -- hier bekannt statt gemessen,
                # weil der Text direkt aus der Mail gebaut wird.
                "extracted_at": timezone.now().isoformat(),
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

    Bewusst *nicht* auf `kind=mail_body` eingeschraenkt: dieselbe Mail kann
    auch als `.eml` hochgeladen/abgelegt worden sein (`ingest_eml_file`,
    #1133) -- die traegt dieselbe Message-ID in `metadata`, aber
    `kind=document` (sie behaelt das Original, statt ein Body-PDF zu
    generieren). Beide Wege muessen sich gegenseitig erkennen, sonst
    laeuft die Mail zweimal ins Archiv, je nachdem welcher Weg zuerst war.
    """
    message_id = (mail_metadata.get("message_id") or "").strip()
    if message_id:
        existing = Document.objects.filter(metadata__message_id=message_id).roots().first()
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


def _fallback_eml_title(sender_name: str, sender_email_addr: str, parsed_date) -> str:
    """Ersatztitel fuer eine `.eml` ohne (nutzbaren) Betreff (#1133) -- nie
    ein leerer Titel. `parsed_date` ist bereits das Ergebnis von
    `mail_parse.parse_mail_date`; fehlt es, zaehlt der Ingest-Zeitpunkt."""
    who = (sender_name or sender_email_addr or "unbekannt").strip()
    when = parsed_date or timezone.now()
    if timezone.is_aware(when):
        when = timezone.localtime(when)
    return f"E-Mail von {who} vom {when.strftime('%d.%m.%Y')}"[:255]


def _eml_document_date(parsed_date):
    """`Document.document_date` aus dem geparsten `Date`-Header, in der
    lokalen Zeitzone (CLAUDE.md: nie UTC) -- oder `None`, wenn der Header
    fehlte/unparsbar war. `None` lassen (statt den Ingest-Zeitpunkt
    einzutragen) ist hier bewusst: `Document.display_date` faellt fuer ein
    leeres `document_date` ohnehin schon auf `created_at` zurueck (siehe
    Model-Property) -- genau der gewuenschte Ingest-Zeitpunkt-Fallback,
    ohne ihn ein zweites Mal zu kodieren."""
    if parsed_date is None:
        return None
    if timezone.is_aware(parsed_date):
        return timezone.localtime(parsed_date).date()
    return parsed_date.date()


def ingest_eml_file(
    fileobj: IO[bytes],
    *,
    filename: str,
    source: str,
    department: Optional[Department] = None,
    owner: Optional[Any] = None,
    visibility: Optional[str] = None,
    origin_metadata: Optional[dict] = None,
    on_duplicate: OnDuplicate = "skip",
    correspondent: Optional[Correspondent] = None,
) -> IngestResult:
    """Ingest a `.eml` file as a Document that keeps the raw message as its
    original file, plus its attachments as Unterdokumente (#1133).

    Anders als `ingest_mail` (IMAP/Graph, kein "Original" -- dort wird ein
    PDF *aus* dem Body gerendert): eine hochgeladene/abgelegte `.eml` ist
    selbst die Originaldatei und bleibt es (Anforderung "das Original
    bleibt als Datei erhalten"). Deshalb laeuft der Leitdokument-Anlage
    hier ueber das normale `ingest_file` (sha256-Dedup, Storage, Enqueue
    der Extraktions-Kaskade -- `apps.documents.extraction` liest
    `message/rfc822` genau wie jeden anderen Typ), nicht ueber
    `_create_mail_leitdokument`s PDF-Generierung.

    Wiederverwendet dieselbe Header-/Body-/Anhang-Auswertung wie der
    IMAP-Connector (`apps.ingest.mail_parse`), damit die beiden Wege nicht
    auseinanderlaufen. Jeder Auswertungsschritt faengt seine eigenen
    Fehler ab und faellt auf "nichts erkannt" zurueck -- eine kaputte
    Mail bekommt trotzdem ein Document, mit so viel Kontext wie sich lesen
    liess, nie eine durchschlagende Exception.

    Dedup zusaetzlich zum sha256-Vergleich in `ingest_file` ueber die
    `Message-ID`: dieselbe Mail, einmal per Postfach-Ueberwachung geholt
    (`ingest_mail`) und einmal als `.eml` abgelegt, hat unterschiedliche
    Bytes, traegt aber dieselbe Message-ID in `metadata`.
    """
    raw = fileobj.read()
    fileobj.seek(0)

    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        logger.exception("Ingest-EML: Nachricht konnte nicht geparst werden (%s)", filename)
        msg = email.message_from_bytes(b"", policy=email.policy.default)

    try:
        sender_email_addr, sender_name = sender_email_and_name(msg)
    except Exception:
        logger.exception("Ingest-EML: Absender-Header nicht lesbar (%s)", filename)
        sender_email_addr, sender_name = "", ""

    subject = (msg.get("Subject", "") or "").strip()
    message_id = (msg.get("Message-ID", "") or "").strip()
    mail_date_raw = msg.get("Date", "") or ""
    parsed_date = parse_mail_date(mail_date_raw)
    if not parsed_date:
        logger.info(
            "Ingest-EML: %s (%s) -- Fallback auf Ingest-Zeitpunkt (%s)",
            "Date-Header unparsbar" if mail_date_raw else "kein Date-Header",
            mail_date_raw or "-",
            filename,
        )

    mail_metadata = dict(origin_metadata or {})
    mail_metadata.update(
        {
            "message_id": message_id,
            "mail_from": sender_email_addr,
            "mail_to": msg.get("To", "") or "",
            "mail_cc": msg.get("Cc", "") or "",
            "mail_subject": subject,
            "mail_date": mail_date_raw,
        }
    )

    if message_id:
        existing = Document.objects.filter(metadata__message_id=message_id).roots().first()
        if existing is not None:
            logger.info(
                "Ingest-EML: Duplikat message_id=%s erkannt -> Document %s",
                message_id,
                existing.id,
            )
            return IngestResult(document=existing, created=False, duplicate=True)

    resolved_correspondent = correspondent or find_or_create_correspondent_by_email(
        sender_email_addr, sender_name
    )

    title = subject or _fallback_eml_title(sender_name, sender_email_addr, parsed_date)

    result = ingest_file(
        BytesIO(raw),
        filename=filename,
        source=source,
        title=title,
        department=department,
        owner=owner,
        visibility=visibility,
        content_type="message/rfc822",
        origin_metadata=mail_metadata,
        on_duplicate=on_duplicate,
        correspondent=resolved_correspondent,
    )
    if not result.created:
        # sha256-Duplikat (keine Message-ID oder keine getroffen) -- wie
        # jeder andere Duplikat-Treffer nicht neu verkindern (siehe
        # `ingest_file`-Docstring).
        return result

    document = result.document
    update_fields = ["direction"]
    document.direction = (
        Document.Direction.AUSGANG
        if resolved_correspondent is not None and resolved_correspondent.is_self
        else Document.Direction.EINGANG
    )
    document_date = _eml_document_date(parsed_date)
    if document_date is not None:
        document.document_date = document_date
        update_fields.append("document_date")
    document.save(update_fields=update_fields)

    try:
        cid_body, _cid_body_content_type = body_content(msg)
    except Exception:
        logger.exception(
            "Ingest-EML: Body-Auswertung fuer den Grampf-Filter fehlgeschlagen (%s)", filename
        )
        cid_body = None

    try:
        attachments = collect_attachments(msg)
    except Exception:
        logger.exception("Ingest-EML: Anhang-Erkennung fehlgeschlagen (%s)", filename)
        attachments = []

    attachments = filter_mail_attachments(attachments, body=cid_body)
    for attachment in attachments:
        ingest_file(
            attachment.fileobj,
            filename=attachment.filename,
            source=source,
            department=department,
            owner=owner,
            visibility=visibility or document.visibility,
            content_type=attachment.content_type,
            origin_metadata=dict(mail_metadata),
            on_duplicate=on_duplicate,
            correspondent=resolved_correspondent,
            parent=document,
            child_role=Document.ChildRole.MAIL_ATTACHMENT,
        )

    logger.info(
        "Ingest-EML: Document %s angelegt (message_id=%s, anhaenge=%s)",
        document.id,
        message_id or "-",
        len(attachments),
    )
    return result
