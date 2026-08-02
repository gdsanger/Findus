"""Central retrieval/query service for Document/Chunk.

See Architektur.md, "Sichtbarkeitsmodell" and the note on
"permission-aware Retrieval" in #994: this module is the only place in
the codebase allowed to query `Chunk` directly. Every entry point --
structured filtering and semantic (pgvector) search alike -- starts
from `Document.objects.visible_to(user)` before anything else touches
the data, so the ACL can't be forgotten at a call site the way a
scattered `Chunk.objects.filter(...)` could be. RAG and MCP tools are
meant to sit on top of `DocumentRetrievalService`, not query the models
themselves.

Hybrid search (Postgres full-text + vector) is a natural extension of
`search()`: add a `SearchVector`/`SearchRank` (or plain `icontains`)
filter on `text_content` alongside the `CosineDistance` annotation,
still scoped through `_filtered_documents()` first, and combine the two
scores before ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from django.db.models import QuerySet

from apps.ai.providers import EmbeddingProvider, get_embedding_provider
from pgvector.django import CosineDistance

from .models import Chunk, Document


@dataclass(frozen=True)
class DocumentHit:
    """One ranked search result.

    `score` is cosine similarity (1.0 = identical direction, 0.0 =
    orthogonal), i.e. `1 - CosineDistance`, so higher is always better.
    `snippet` is the content of the best-matching chunk -- the context
    the UI shows under the title, not the whole document text.
    """

    document: Document
    score: float
    snippet: str = ""


class DocumentRetrievalService:
    """Permission-aware entry point for reading Documents/Chunks.

    Construct one per request/user (`DocumentRetrievalService(request.user)`)
    and call `list_documents()` for structured-only browsing or
    `search()` for semantic search, optionally combined with the same
    structured filters.
    """

    def __init__(self, user, *, embedding_provider: Optional[EmbeddingProvider] = None):
        self.user = user
        self.embedding_provider = embedding_provider

    def _filtered_documents(
        self,
        *,
        correspondent=None,
        vorgang=None,
        tags: Optional[Iterable] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        status: Optional[str] = None,
    ) -> QuerySet[Document]:
        documents = (
            Document.objects.visible_to(self.user)
            .select_related("correspondent")
            .prefetch_related("vorgaenge", "tags")
        )
        if correspondent is not None:
            documents = documents.filter(correspondent=correspondent)
        if vorgang is not None:
            documents = documents.filter(vorgaenge=vorgang)
        if tags:
            documents = documents.filter(tags__in=tags)
        if date_from is not None:
            documents = documents.filter(created_at__date__gte=date_from)
        if date_to is not None:
            documents = documents.filter(created_at__date__lte=date_to)
        if status is not None:
            documents = documents.filter(processing_status=status)
        return documents.distinct()

    def list_documents(
        self,
        *,
        correspondent=None,
        vorgang=None,
        tags: Optional[Iterable] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        status: Optional[str] = None,
    ) -> QuerySet[Document]:
        """Structured browse -- no semantic query, just visibility + filters."""
        return self._filtered_documents(
            correspondent=correspondent,
            vorgang=vorgang,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        correspondent=None,
        vorgang=None,
        tags: Optional[Iterable] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        status: Optional[str] = None,
    ) -> list[DocumentHit]:
        """Embed `query` via the AI provider layer, rank visible documents
        by their best-matching chunk (cosine distance over
        `Chunk.embedding`), and apply the same structured filters as
        `list_documents()`.
        """
        visible_documents = self._filtered_documents(
            correspondent=correspondent,
            vorgang=vorgang,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            status=status,
        )

        provider = self.embedding_provider or get_embedding_provider()
        query_vector = provider.embed([query]).vectors[0]

        # Ordered by distance ascending across *all* chunks, so the first
        # row seen for a given document is necessarily its best-matching
        # chunk -- no separate aggregation query needed to also carry the
        # chunk's content along as the result snippet.
        chunk_rows = (
            Chunk.objects.filter(document__in=visible_documents)
            .annotate(distance=CosineDistance("embedding", query_vector))
            .order_by("distance")
            .values("document_id", "content", "distance")
        )

        best_row_by_document_id: dict[int, dict] = {}
        for row in chunk_rows:
            document_id = row["document_id"]
            if document_id in best_row_by_document_id:
                continue
            best_row_by_document_id[document_id] = row
            if len(best_row_by_document_id) >= limit:
                break

        documents_by_id = visible_documents.in_bulk(best_row_by_document_id.keys())
        return [
            DocumentHit(
                document=documents_by_id[document_id],
                score=1 - row["distance"],
                snippet=row["content"],
            )
            for document_id, row in best_row_by_document_id.items()
            if document_id in documents_by_id
        ]
