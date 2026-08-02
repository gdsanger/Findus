"""Chunking + embedding: the second half of document processing (#1010).

Text extraction populates `Document.text_content` (separate, earlier
step); `process_document()` turns that text into `Chunk` rows with
embeddings via the AI provider layer (apps.ai.providers, #1004). It is
also the Re-Index migration path (see `management/commands/
reindex_documents.py`): calling it again after an embedding model/
dimension change re-chunks and re-embeds a document from scratch, since
vectors from different models aren't comparable (Architektur.md,
"Lock-in-Hedges").
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.db import transaction

from apps.ai.providers import EmbeddingProvider, get_embedding_provider

from .chunking import chunk_text
from .models import Chunk, Document

logger = logging.getLogger(__name__)


def _embed_all(
    texts: list[str], provider: EmbeddingProvider, batch_size: int
) -> tuple[list[list[float]], str, str]:
    """Embed `texts` in batches of `batch_size` -- one big request per
    document could exceed a provider's request-size limit once a long
    document produces hundreds of chunks.
    """
    if not texts:
        return [], "", ""
    vectors: list[list[float]] = []
    model = version = ""
    for start in range(0, len(texts), batch_size):
        result = provider.embed(texts[start : start + batch_size])
        vectors.extend(result.vectors)
        model, version = result.model, result.version
    return vectors, model, version


def process_document(
    document_id: int, *, embedding_provider: Optional[EmbeddingProvider] = None
) -> Document:
    """Chunk `document.text_content`, embed each chunk, and replace the
    document's `Chunk` set. On success, `processing_status` becomes
    `ready`; on failure it becomes `failed` with `processing_error` set,
    and the exception is re-raised so Django-Q (or a management command
    loop) sees the task as failed too.

    Existing chunks are only replaced once new embeddings were computed
    successfully -- a failing embed call leaves a previously indexed
    document searchable rather than clobbering it.
    """
    document = Document.objects.get(pk=document_id)
    document.processing_status = Document.ProcessingStatus.PROCESSING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])

    try:
        chunk_contents = chunk_text(
            document.text_content,
            chunk_size=settings.FINDUS_CHUNK_SIZE_TOKENS,
            overlap=settings.FINDUS_CHUNK_OVERLAP_TOKENS,
        )
        provider = embedding_provider or get_embedding_provider()
        vectors, model, version = _embed_all(
            chunk_contents, provider, settings.FINDUS_CHUNK_EMBEDDING_BATCH_SIZE
        )

        with transaction.atomic():
            document.chunks.all().delete()
            Chunk.objects.bulk_create(
                Chunk(
                    document=document,
                    position=position,
                    content=content,
                    embedding=vector,
                    embedding_model=model,
                    embedding_model_version=version,
                )
                for position, (content, vector) in enumerate(zip(chunk_contents, vectors))
            )
            document.processing_status = Document.ProcessingStatus.READY
            document.save(update_fields=["processing_status", "updated_at"])
    except Exception as exc:
        logger.exception("Chunking/Embedding fehlgeschlagen fuer Document %s", document_id)
        document.processing_status = Document.ProcessingStatus.FAILED
        document.processing_error = str(exc)
        document.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise

    return document
