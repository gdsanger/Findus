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

Dokumentrichtung (#1030, geschaerft in #1048): the same `generate()` call
also extracts the *recipient*, not just the sender/Aussteller -- both are
matched read-only against `Correspondent` (preferring USt-IdNr/IBAN over
name, see `apps.documents.services.match_correspondent_by_ids`) to derive
`Document.direction` from `Correspondent.is_self`. Since #1048 the prompt
also lists the existing `Correspondent`s (name + Kennungen + `is_self`) as
context and asks the model to output `direction` itself -- a fallback for
when the DB match misses (e.g. the own identity is worded slightly
differently in the document than the stored `is_self` record). Whichever
side is *not* `is_self` (the Gegenstelle) becomes `Document.correspondent`
-- never the `is_self` side, so an Ausgangsrechnung no longer ends up
filed under "me" as its own contact. The `is_self` side is only ever
matched, never created, so the KI can't invent a duplicate "Ich"-identity.

Tag-Dimension/-Wert (#1034): `Tag`/`TagSuggestion` keep `name` and
`dimension` as separate fields on purpose (unique on the pair, see
`Tag.Meta.constraints`), but a KI reply sometimes ignores the prompt and
crams both into `name` as "Dimension:Wert" while *also* filling
`dimension` -- doubling the dimension in every display built from the
two fields. `_normalize_tag_fields` splits that back apart before a
`TagSuggestion` is ever created, so `name` never carries a `dimension`
prefix.
"""

from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .models import Correspondent, Document, SuggestionStatus, Tag, TagSuggestion, Vorgang, VorgangSuggestion
from .services import find_correspondent, find_or_create_correspondent
from .text_sanitize import clean_json

logger = logging.getLogger(__name__)

_KEY_FACT_FIELDS = (
    "sender_name",
    "sender_email",
    "sender_vat_id",
    "sender_iban",
    "recipient_name",
    "recipient_email",
    "recipient_vat_id",
    "recipient_iban",
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
    '"sender_vat_id" (USt-IdNr des Ausstellers/Absenders), "sender_iban", '
    '"recipient_name", "recipient_email", "recipient_vat_id" '
    "(USt-IdNr des Empfaengers -- bei einer Rechnung der Rechnungsempfaenger, "
    'nicht der Rechnungssteller), "recipient_iban", "document_date" '
    '(YYYY-MM-DD), "document_type" (z. B. Rechnung, Vertrag, Mahnung), '
    '"amount", "currency", "due_date" (YYYY-MM-DD) -- jeweils der erkannte '
    "Wert als String, sonst null.\n"
    '- "direction": IMMER angeben, einer von "eingang", "ausgang", '
    '"intern", "unbekannt". Vergleiche Aussteller (sender_name/-ids) und '
    'Empfaenger (recipient_name/-ids) mit den unten gelisteten '
    '"Bestehende Kontakte" und deren [ICH SELBST]-Markierung: ist der '
    'Aussteller [ICH SELBST], ist es "ausgang" (ich verschicke); ist der '
    'Empfaenger [ICH SELBST], ist es "eingang" (ich empfange); sind beide '
    '[ICH SELBST], ist es "intern". Ist keine der beiden Parteien als '
    '[ICH SELBST] gelistet oder eindeutig zuzuordnen, antworte "unbekannt" '
    "-- rate nicht.\n"
    '- "tag_suggestions": Liste von Objekten "name", "dimension", '
    '"confidence" (0-1) -- bevorzugt bestehende Tags/Dimensionen '
    "wiederverwenden, nicht wahllos neue erfinden. \"name\" ist "
    'IMMER nur der reine Wert, NIE "Dimension:Wert". Die Dimension '
    'gehoert ausschliesslich in das Feld "dimension". Richtig: '
    '{"name": "Eingangsrechnung", "dimension": "Dokumenttyp"}. Falsch: '
    '{"name": "Dokumenttyp:Eingangsrechnung", "dimension": "Dokumenttyp"}.\n'
    '- "vorgang_suggestions": Liste von Objekten "name", "confidence" '
    "(0-1) -- bevorzugt einen bestehenden Vorgang, sonst ein neuer, "
    "praegnanter Name.\n"
    "Bevorzuge fuer sender_name/recipient_name/Tags/Vorgaenge immer den "
    "exakten Namen aus den untenstehenden Bestehende-Kontakte/Tags/"
    "Vorgaenge-Listen, wenn er zum Dokument passt -- lege keinen neuen "
    "Eintrag an, wenn ein bestehender ersichtlich derselbe ist.\n"
    "Erfinde keine Werte, die im Text nicht belegt sind -- nutze null bzw. "
    "eine leere Liste statt zu raten."
)


def _correspondent_context_lines() -> list[str]:
    correspondents = Correspondent.objects.order_by("-is_self", "name")[
        : settings.FINDUS_ANALYSIS_MAX_CONTACTS
    ]
    lines = []
    for correspondent in correspondents:
        ids = ", ".join(
            filter(None, [correspondent.vat_id, correspondent.iban, correspondent.email])
        )
        marker = " [ICH SELBST]" if correspondent.is_self else ""
        suffix = f" ({ids})" if ids else ""
        lines.append(f"{correspondent.name}{marker}{suffix}")
    return lines


def _context_message(document: Document) -> str:
    existing_tags = sorted(
        {f"{tag.dimension}:{tag.name}" if tag.dimension else tag.name for tag in Tag.objects.all()}
    )
    existing_vorgaenge = sorted(Vorgang.objects.values_list("name", flat=True))
    existing_contacts = _correspondent_context_lines()
    text = document.text_content[: settings.FINDUS_ANALYSIS_MAX_CHARS]
    return (
        f"Bestehende Kontakte: {', '.join(existing_contacts) or '(keine)'}\n"
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


def _normalize_tag_fields(name: str, dimension: str) -> tuple[str, str]:
    """Defend against a KI reply that -- despite the prompt (#1034) --

    still crams the dimension into the name as "Dimension:Wert". Split
    on the first ":" and keep only the bare value as the name; an
    already-populated `dimension` field wins over the prefix (the
    prefix is redundant in that case, not a second opinion to merge).
    """
    name = name.strip()
    dimension = dimension.strip()
    prefix, sep, rest = name.partition(":")
    if sep and rest.strip():
        name = rest.strip()
        dimension = dimension or prefix.strip()
    return name, dimension


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
        name, dimension = _normalize_tag_fields(
            str(item.get("name") or ""), str(item.get("dimension") or "")
        )
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        suggestions.append(
            TagSuggestion(
                document=document,
                name=name,
                dimension=dimension,
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


def _derive_direction(
    sender: Optional[Correspondent], recipient: Optional[Correspondent]
) -> str:
    """Eingang/Ausgang/Intern aus den `is_self`-Flags von Aussteller und

    Empfaenger (#1030) -- Empfaenger ist "ich" -> Eingang (ich muss
    zahlen/reagieren), Aussteller ist "ich" -> Ausgang, beide -> Intern.
    Ist keiner der beiden `is_self`, bleibt es bei "unbekannt". Dient als
    aus der Datenbank abgeleitete, von der Modellantwort unabhaengige
    Bestaetigung/Fallback fuer `_normalize_direction` (#1048).
    """

    sender_is_self = bool(sender and sender.is_self)
    recipient_is_self = bool(recipient and recipient.is_self)
    if sender_is_self and recipient_is_self:
        return Document.Direction.INTERN
    if recipient_is_self:
        return Document.Direction.EINGANG
    if sender_is_self:
        return Document.Direction.AUSGANG
    return Document.Direction.UNBEKANNT


def _normalize_direction(value: object) -> str:
    text = str(value or "").strip().lower()
    valid_values = {choice.value for choice in Document.Direction}
    return text if text in valid_values else Document.Direction.UNBEKANNT


def _resolve_direction(
    parsed_direction: object,
    sender: Optional[Correspondent],
    recipient: Optional[Correspondent],
) -> str:
    """Richtung (#1048): das Modell kennt jetzt die `is_self`-Kontakte aus

    dem Prompt-Kontext und gibt "direction" direkt aus -- das faengt auch
    Faelle auf, in denen die eigene Identitaet im Dokument leicht anders
    geschrieben ist als der gespeicherte `Correspondent` (z. B. "Software
    Entwicklung Angermeier" im Text vs. "Christian Angermeier" als
    `is_self`-Datensatz), wo ein rein datenbankbasierter Abgleich
    scheitert. Der DB-Abgleich (`_derive_direction`) bleibt die
    verlaesslichere Quelle, wann immer er tatsaechlich einen `is_self`-
    Treffer liefert, und gewinnt daher zuerst.
    """

    derived = _derive_direction(sender, recipient)
    if derived != Document.Direction.UNBEKANNT:
        return derived
    return _normalize_direction(parsed_direction)


def _apply_analysis(document: Document, parsed: dict, *, model: str, version: str) -> None:
    parsed = clean_json(parsed)
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

    sender_kwargs = {
        "name": key_facts_in.get("sender_name") or "",
        "email": key_facts_in.get("sender_email") or "",
        "vat_id": key_facts_in.get("sender_vat_id") or "",
        "iban": key_facts_in.get("sender_iban") or "",
    }
    recipient_kwargs = {
        "name": key_facts_in.get("recipient_name") or "",
        "email": key_facts_in.get("recipient_email") or "",
        "vat_id": key_facts_in.get("recipient_vat_id") or "",
        "iban": key_facts_in.get("recipient_iban") or "",
    }

    # Read-only Matching zuerst (fuer beide Seiten) -- ob/welche Seite
    # tatsaechlich `is_self` ist, muss feststehen, bevor eine Seite als
    # Gegenstelle angelegt wird (#1048); ein bereits gesetzter Kontakt gilt
    # weiter als "Aussteller-Seite" fuer die Richtungs-Ableitung, wie schon
    # vor #1048.
    sender_match = (
        document.correspondent if document.correspondent_id is not None
        else find_correspondent(**sender_kwargs)
    )
    recipient_match = find_correspondent(**recipient_kwargs)

    candidate_direction = _resolve_direction(
        parsed.get("direction"), sender_match, recipient_match
    )
    if (
        document.direction == Document.Direction.UNBEKANNT
        and candidate_direction != Document.Direction.UNBEKANNT
    ):
        document.direction = candidate_direction
        update_fields.append("direction")

    if document.correspondent_id is None:
        # Kontakt = Gegenstelle, nie eine eigene Identitaet (#1048): bei
        # Ausgang der Empfaenger, bei Eingang der Aussteller -- die jeweils
        # andere Seite bleibt unangetastet (Dedup: keine `is_self`-Dublette
        # anlegen). Bei "intern" gibt es keine Gegenstelle. Ohne feststellbare
        # Richtung ("unbekannt", z. B. keine eigene Identitaet hinterlegt)
        # bleibt es beim bisherigen Verhalten: Aussteller als plausibelste
        # Gegenstelle.
        if document.direction == Document.Direction.AUSGANG:
            counterpart = find_or_create_correspondent(**recipient_kwargs)
        elif document.direction == Document.Direction.INTERN:
            counterpart = None
        else:
            counterpart = find_or_create_correspondent(**sender_kwargs)

        if counterpart is not None and not counterpart.is_self:
            document.correspondent = counterpart
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
