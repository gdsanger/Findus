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
import mimetypes
from dataclasses import dataclass
from typing import IO, Any, Literal, Optional

from django.core.files import File

from apps.accounts.models import Department
from apps.documents.models import Correspondent, Document

logger = logging.getLogger(__name__)

_HASH_CHUNK_SIZE = 1024 * 1024

OnDuplicate = Literal["skip", "link"]


@dataclass(frozen=True)
class IngestResult:
    document: Document
    created: bool
    duplicate: bool


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
