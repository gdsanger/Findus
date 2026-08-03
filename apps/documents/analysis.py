"""KI-Analyse & Anreicherung (#1020): the "second brain" step after

extraction. `extract_document()` (#1009) turns a file into
`Document.text_content`; this module turns that text into a title,
a readable summary, structured key-facts (sender/date/type/amount),
an auto-matched/created `Correspondent`, and tag/Vorgang *suggestions*
the user accepts or rejects (`TagSuggestion`/`VorgangSuggestion`) --
never applied automatically, so the KI never "müllt selbstständig zu".

One `generate()` call per document, on the already-extracted text --
no additional vision call, cost-conscious per the issue. Unlike
`extraction`/`processing`, a failing analysis does NOT fail the
pipeline: it's enrichment on top of an already-searchable document, not
a precondition for it, so `analyze_document()` never raises -- a
provider outage or a malformed response is recorded in
`Document.metadata["analysis_error"]` and the pipeline moves on to
embedding regardless (see `apps.documents.tasks.analyze_document_task`).

JSON parsing goes through `apps.ai.providers.generate_json` (bug fix,
#1028): LLMs occasionally return near-valid JSON (missing/trailing
comma, prose or code-fences around the payload), and strict
`json.loads` on that used to leave `summary`/`key_facts` empty with an
`analysis_error` even though the model had, for all practical purposes,
answered correctly. `generate_json` repairs/retries before giving up,
so `analysis_error` now only fires on a genuine provider failure or a
reply that isn't recoverable JSON at all -- still costing at most one
extra `generate()` call, only in that failure case.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .models import Document, SuggestionStatus, Tag, TagSuggestion, Vorgang, VorgangSuggestion
from .services import find_or_create_correspondent

logger = logging.getLogger(__name__)

_KEY_FACT_FIELDS = (
    "sender_name",
    "sender_email",
    "document_date",
    "document_type",
    "amount",
    "currency",
    "due_date",
)

_SYSTEM_PROMPT = (
    "Du analysierst eingehende Dokumente fuer ein Dokumentenmanagementsystem. "
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein Markdown, "
    "kein Fliesstext drumherum) mit genau diesen Schluesseln:\n"
    '- "title": kurzer, aussagekraeftiger Titel.\n'
    '- "summary": 2-4 Saetze lesbare Zusammenfassung auf Deutsch.\n'
    '- "key_facts": Objekt mit "sender_name", "sender_email", '
    '"document_date" (YYYY-MM-DD), "document_type" (z. B. Rechnung, '
    'Vertrag, Mahnung), "amount", "currency", "due_date" (YYYY-MM-DD) -- '
    "jeweils der erkannte Wert als String, sonst null.\n"
    '- "tag_suggestions": Liste von Objekten "name", "dimension", '
    '"confidence" (0-1) -- bevorzugt bestehende Tags/Dimensionen '
    "wiederverwenden, nicht wahllos neue erfinden.\n"
    '- "vorgang_suggestions": Liste von Objekten "name", "confidence" '
    "(0-1) -- bevorzugt einen bestehenden Vorgang, sonst ein neuer, "
    "praegnanter Name.\n"
    "Erfinde keine Werte, die im Text nicht belegt sind -- nutze null bzw. "
    "eine leere Liste statt zu raten."
)


def _context_message(document: Document) -> str:
    existing_tags = sorted(
        {f"{tag.dimension}:{tag.name}" if tag.dimension else tag.name for tag in Tag.objects.all()}
    )
    existing_vorgaenge = sorted(Vorgang.objects.values_list("name", flat=True))
    text = document.text_content[: settings.FINDUS_ANALYSIS_MAX_CHARS]
    return (
        f"Bestehende Tags: {', '.join(existing_tags) or '(keine)'}\n"
        f"Bestehende Vorgaenge: {', '.join(existing_vorgaenge) or '(keine)'}\n\n"
        f"Dokumenttext:\n{text}"
    )


def _build_messages(document: Document) -> list[Message]:
    return [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=_context_message(document)),
    ]


def _clamp_confidence(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _replace_tag_suggestions(document: Document, items: list) -> None:
    decided_names = {
        name.lower()
        for name in document.tag_suggestions.exclude(status=SuggestionStatus.PENDING).values_list(
            "name", flat=True
        )
    }
    document.tag_suggestions.filter(status=SuggestionStatus.PENDING).delete()

    seen = set(decided_names)
    suggestions = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        suggestions.append(
            TagSuggestion(
                document=document,
                name=name,
                dimension=str(item.get("dimension") or "").strip(),
                confidence=_clamp_confidence(item.get("confidence")),
            )
        )
    TagSuggestion.objects.bulk_create(suggestions)


def _replace_vorgang_suggestions(document: Document, items: list) -> None:
    decided_names = {
        name.lower()
        for name in document.vorgang_suggestions.exclude(
            status=SuggestionStatus.PENDING
        ).values_list("name", flat=True)
    }
    document.vorgang_suggestions.filter(status=SuggestionStatus.PENDING).delete()

    seen = set(decided_names)
    suggestions = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        suggestions.append(
            VorgangSuggestion(
                document=document,
                name=name,
                confidence=_clamp_confidence(item.get("confidence")),
            )
        )
    VorgangSuggestion.objects.bulk_create(suggestions)


def _apply_analysis(document: Document, parsed: dict, *, model: str, version: str) -> None:
    title = str(parsed.get("title") or "").strip()
    summary = str(parsed.get("summary") or "").strip()
    key_facts_in = parsed.get("key_facts") or {}
    if not isinstance(key_facts_in, dict):
        key_facts_in = {}

    key_facts = {
        field: str(key_facts_in[field]).strip()
        for field in _KEY_FACT_FIELDS
        if str(key_facts_in.get(field) or "").strip()
    }
    if key_facts:
        key_facts["ai_model"] = model
        key_facts["ai_model_version"] = version

    metadata = dict(document.metadata)
    metadata.pop("analysis_error", None)
    document.metadata = metadata

    update_fields = ["metadata", "summary", "key_facts", "updated_at"]
    document.summary = summary
    document.key_facts = key_facts
    if title:
        document.title = title
        update_fields.append("title")

    if document.correspondent_id is None:
        correspondent = find_or_create_correspondent(
            name=key_facts_in.get("sender_name") or "",
            email=key_facts_in.get("sender_email") or "",
        )
        if correspondent is not None:
            document.correspondent = correspondent
            update_fields.append("correspondent")

    document.save(update_fields=update_fields)

    _replace_tag_suggestions(document, parsed.get("tag_suggestions") or [])
    _replace_vorgang_suggestions(document, parsed.get("vorgang_suggestions") or [])


def analyze_document(
    document_id: int, *, generation_provider: Optional[GenerationProvider] = None
) -> Document:
    """Run the KI-Analyse for `document` (`text_content` must already be

    populated by the extraction cascade). `processing_status` becomes
    `analyzing` while this runs and -- on success just like on failure --
    is deliberately left there: `process_document()` (#1010) flips it to
    `embedding` the moment that stage starts, mirroring how `extraction`
    hands off to this stage.

    Never raises: a provider error or an unparseable response is
    recorded in `metadata["analysis_error"]` instead, so a KI hiccup
    doesn't keep an already-extracted document from becoming searchable.
    """
    document = Document.objects.get(pk=document_id)
    document.processing_status = Document.ProcessingStatus.ANALYZING
    document.save(update_fields=["processing_status", "updated_at"])

    try:
        provider = generation_provider or get_generation_provider()
        result = generate_json(provider, _build_messages(document))
        _apply_analysis(document, result.data, model=result.model, version=result.version)
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "KI-Analyse fehlgeschlagen fuer Document %s, roher Modell-Output (gekuerzt): %s",
                document_id,
                raw_text,
            )
        else:
            logger.exception("KI-Analyse fehlgeschlagen fuer Document %s", document_id)
        document.metadata = {**document.metadata, "analysis_error": str(exc)}
        document.save(update_fields=["metadata", "updated_at"])

    return document
