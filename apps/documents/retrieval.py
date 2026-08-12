"""Central retrieval/query service for Document/Chunk.

See Architektur.md, "Sichtbarkeitsmodell" and the note on
"permission-aware Retrieval" in #994: this module is the only place in
the codebase allowed to query `Chunk` directly. Every entry point --
structured filtering and semantic (pgvector) search alike -- starts
from `Document.objects.visible_to(user)` before anything else touches
the data, so the ACL can't be forgotten at a call site the way a
scattered `Chunk.objects.filter(...)` could be. RAG and MCP tools are
meant to sit on top of `DocumentRetrievalService`, not query the models
themselves. That includes `similar_documents()` (#1088): "ähnliche
Dokumente" is the same permission-aware kNN, only with a document
instead of a typed query as its starting point.

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

from django.conf import settings
from django.db.models import Q, QuerySet

from apps.ai.providers import EmbeddingProvider, get_embedding_provider
from pgvector.django import CosineDistance

from .models import Chunk, Document, tax_relevance_filter_q


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
        direction: Optional[str] = None,
        sphere: Optional[str] = None,
        tax_relevance: Optional[str] = None,
        action_status: Optional[str] = None,
    ) -> QuerySet[Document]:
        documents = (
            Document.objects.visible_to(self.user)
            .select_related("correspondent", "parent")
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
        if direction is not None:
            documents = documents.filter(direction=direction)
        if sphere is not None:
            documents = documents.filter(sphere=sphere)
        if tax_relevance is not None:
            tax_q = tax_relevance_filter_q(tax_relevance)
            if tax_q is not None:
                documents = documents.filter(tax_q)
        if action_status is not None:
            documents = documents.filter(action_status=action_status)
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
        direction: Optional[str] = None,
        sphere: Optional[str] = None,
        tax_relevance: Optional[str] = None,
        action_status: Optional[str] = None,
    ) -> QuerySet[Document]:
        """Structured browse -- no semantic query, just visibility + filters.

        Roots only (#1069): browsing surfaces Leitdokumente, Unterdokumente
        stay collapsed underneath (`Document.children`) -- unlike
        `search()`, which must still return a matching Unterdokument as its
        own hit.
        """
        return self._filtered_documents(
            correspondent=correspondent,
            vorgang=vorgang,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            status=status,
            direction=direction,
            sphere=sphere,
            tax_relevance=tax_relevance,
            action_status=action_status,
        ).roots()

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
        direction: Optional[str] = None,
        sphere: Optional[str] = None,
        tax_relevance: Optional[str] = None,
        action_status: Optional[str] = None,
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
            direction=direction,
            sphere=sphere,
            tax_relevance=tax_relevance,
            action_status=action_status,
        )

        provider = self.embedding_provider or get_embedding_provider()
        query_vector = provider.embed([query]).vectors[0]

        return self._rank_by_chunk_similarity(query_vector, visible_documents, limit=limit)

    def similar_documents(
        self,
        document,
        *,
        limit: Optional[int] = None,
        min_score: Optional[float] = None,
        exclude_ids: Optional[Iterable[int]] = None,
    ) -> list[DocumentHit]:
        """Top-N verwandte Dokumente zu `document` (#1088) -- reine
        Vektor-Ähnlichkeit über die vorhandenen Embeddings, kein
        zusätzlicher LLM-Call.

        Als Query dient ein *Dokument-Repräsentativ-Embedding*: der
        Mittelwert der eigenen Chunk-Vektoren. Das ist bewusst nicht "bestes
        Chunk-Match gegen bestes Chunk-Match" -- ein einzelner
        Boilerplate-Chunk (Adressblock, Fußzeile, AGB-Absatz) matcht sonst
        quer durchs Archiv und produziert genau das Rauschen, das dieser
        Block vermeiden soll. Der Mittelwert bildet stattdessen das Thema
        des ganzen Dokuments ab. Er wird bei jedem Aufruf aus den eigenen
        Chunks gerechnet statt gespeichert: das ist ein indizierter
        Lookup auf einer Handvoll Zeilen, während eine persistierte
        Spalte ein weiteres Feld wäre, das nach jedem Re-Embedding
        (#1010) und jeder Modell-Umstellung nachgezogen werden müsste.
        Gegen die *Kandidaten*-Chunks (nicht deren Mittel) wird gemessen,
        weil nur so der HNSW-Index auf `Chunk.embedding` greift.

        Aggregation auf Dokument-Ebene = bestes Chunk-Match je Dokument,
        wie bei `search()`; mehrere Chunks desselben Dokuments fallen damit
        zu einem Treffer zusammen. Ausgeschlossen werden das Dokument
        selbst sowie sein direkter Baum-Kontext (Leitdokument und
        Unterdokumente, #1069) -- die stehen im Detail ohnehin schon in
        ihrem eigenen Block und wären hier reine Wiederholung.
        `exclude_ids` reicht die bereits manuell verknüpften Dokumente
        durch (`DocumentLink`), damit derselbe Treffer nicht zweimal im
        Block auftaucht.
        """
        limit = settings.FINDUS_SIMILAR_DOCUMENTS_LIMIT if limit is None else limit
        if min_score is None:
            min_score = settings.FINDUS_SIMILAR_DOCUMENTS_MIN_SCORE
        if limit <= 0:
            return []

        own_chunks = list(
            Chunk.objects.filter(document=document).values_list(
                "embedding", "embedding_model", "embedding_model_version"
            )
        )
        representative_vector = _mean_vector(embedding for embedding, _, _ in own_chunks)
        if representative_vector is None:
            # Noch nicht indiziert (oder Text-los) -- ohne Embedding gibt es
            # nichts zu vergleichen; der Aufrufer zeigt dafür einen Hinweis
            # statt einer leeren Trefferliste.
            return []

        excluded_ids = {document.pk, document.parent_id, *(exclude_ids or ())}
        excluded_ids.update(document.subtree().values_list("pk", flat=True))
        excluded_ids.discard(None)

        candidate_documents = self._filtered_documents().exclude(pk__in=excluded_ids)
        return self._rank_by_chunk_similarity(
            representative_vector,
            candidate_documents,
            limit=limit,
            min_score=min_score,
            chunks=Chunk.objects.filter(
                _same_embedding_model_filter(
                    {(model, version) for _, model, version in own_chunks}
                )
            ),
            max_candidate_chunks=settings.FINDUS_SIMILAR_DOCUMENTS_CANDIDATE_CHUNKS,
        )

    def _rank_by_chunk_similarity(
        self,
        query_vector,
        documents: QuerySet[Document],
        *,
        limit: int,
        min_score: Optional[float] = None,
        chunks: Optional[QuerySet[Chunk]] = None,
        max_candidate_chunks: Optional[int] = None,
    ) -> list[DocumentHit]:
        """Rank `documents` by their best-matching chunk against
        `query_vector` -- the shared core of `search()` and
        `similar_documents()`, which differ only in where the query vector
        comes from.

        Ordered by distance ascending across *all* candidate chunks, so the
        first row seen for a given document is necessarily its best-matching
        chunk -- no separate aggregation query needed to also carry the
        chunk's content along as the result snippet.
        """
        chunk_rows = (
            (chunks if chunks is not None else Chunk.objects.all())
            .filter(document__in=documents)
            .annotate(distance=CosineDistance("embedding", query_vector))
        )
        if min_score is not None:
            # Als Distanz-Filter, nicht nachtraeglich in Python: so faellt
            # alles unterhalb des Schwellwerts schon in der DB weg.
            chunk_rows = chunk_rows.filter(distance__lte=1 - min_score)
        chunk_rows = chunk_rows.order_by("distance").values(
            "document_id", "content", "distance"
        )
        if max_candidate_chunks is not None:
            chunk_rows = chunk_rows[:max_candidate_chunks]

        best_row_by_document_id: dict[int, dict] = {}
        for row in chunk_rows:
            document_id = row["document_id"]
            if document_id in best_row_by_document_id:
                continue
            best_row_by_document_id[document_id] = row
            if len(best_row_by_document_id) >= limit:
                break

        documents_by_id = documents.in_bulk(best_row_by_document_id.keys())
        return [
            DocumentHit(
                document=documents_by_id[document_id],
                score=1 - row["distance"],
                snippet=row["content"],
            )
            for document_id, row in best_row_by_document_id.items()
            if document_id in documents_by_id
        ]


def _same_embedding_model_filter(model_versions) -> Q:
    """Restrict candidate chunks to the embedding model(s)+version(s) the
    source document itself was embedded with.

    Vektoren verschiedener Modelle/Versionen sind nicht vergleichbar (siehe
    Architektur.md, "Lock-in-Hedges") -- ohne diesen Filter lieferte ein
    halb durchgelaufener Re-Index zufällige Distanzen statt Ähnlichkeit.
    Normalerweise ist das genau ein Paar; die Schleife deckt nur den
    Übergangsfall eines gemischt indizierten Dokuments ab.
    """
    model_filter = Q()
    for model, version in model_versions:
        model_filter |= Q(embedding_model=model, embedding_model_version=version)
    return model_filter


def _mean_vector(vectors) -> Optional[list[float]]:
    """Component-wise mean of the given embeddings, or None if there are
    none -- the Dokument-Repräsentativ-Embedding of `similar_documents()`.

    Kosinus-Ähnlichkeit ist skaleninvariant, die Division durch die Anzahl
    ändert das Ranking also nicht; sie hält den Vektor nur im selben
    Wertebereich wie die Chunk-Vektoren und damit lesbar beim Debuggen.
    """
    total: list[float] = []
    count = 0
    for vector in vectors:
        if not total:
            total = [0.0] * len(vector)
        for index, value in enumerate(vector):
            total[index] += float(value)
        count += 1
    if not count:
        return None
    return [value / count for value in total]
