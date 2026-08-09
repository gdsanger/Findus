"""KI-Handlungsempfehlungen je Vorgang (#1093): "wo steht dieser Vorgang,
was waeren die naechsten Schritte" -- auf Knopfdruck, nie automatisch.

Reasoning ueber die *Sammlung*, nicht ueber ein Einzeldokument: Datenbasis
sind die je Dokument bereits vorhandenen Zusammenfassungen und Key-Facts
aus der KI-Analyse (#1020), chronologisch geordnet, plus Vorgang-Kontext
(Name/Status/Beschreibung) und je Dokument Datum/Richtung/Kontakt.
Bewusst **keine Volltexte** -- ein Vorgang mit 30 Dokumenten wuerde damit
jeden vernuenftigen Kontext- und Kostenrahmen sprengen, waehrend die
Zusammenfassungen genau die verdichtete Fassung sind, fuer die #1020
schon einmal bezahlt wurde. Ein `generate()`-Call pro Generierung.

Alles bleibt Vorschlag: das Ergebnis landet als
`VorgangRecommendationRun` + `VorgangRecommendation`-Zeilen im Status
`open`, und erst eine Nutzeraktion macht daraus eine `Task` (#1012) oder
verwirft sie. Es gibt hier bewusst keinen Pfad, der selbst eine Aufgabe
anlegt.

Provenienz statt Vertrauen: das Modell muss zu jeder Empfehlung die
Dokument-IDs nennen, auf die sie sich stuetzt; `_source_documents()`
verwirft jede ID, die nicht in der tatsaechlich mitgeschickten Liste
stand. Halluzinierte Quellen kommen so gar nicht erst in die UI, und was
angezeigt wird, ist nachpruefbar verlinkt.

`visible_to`-gescoped: die Datenbasis ist immer nur das, was der
ausloesende Nutzer ohnehin sehen darf (`user_id` wandert deshalb mit in
den Worker-Job) -- eine Empfehlung darf kein Dokument zitieren, das der
Nutzer nicht oeffnen kann.

JSON-Robustheit wie in #1028: `generate_json` (defensives Parsen ->
Repair -> ein Retry). Schlaegt es trotzdem fehl, wird das *sichtbar*
(`status="failed"` + `error` im Panel) statt still zu verpuffen -- anders
als bei der Dokument-Analyse gibt es hier kein "die Pipeline laeuft schon
weiter", der Nutzer hat auf einen Knopf gedrueckt und erwartet eine
Antwort.
"""

from __future__ import annotations

import datetime
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import DateField
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .models import Document, Vorgang, VorgangRecommendation, VorgangRecommendationRun
from .text_sanitize import clean_json

logger = logging.getLogger(__name__)

# Reine KI-Metadaten aus `Document.key_facts` -- fuer die Lagebeurteilung
# irrelevant, wuerden im Prompt nur Platz kosten.
_SKIPPED_KEY_FACTS = ("ai_model", "ai_model_version")

_SYSTEM_PROMPT = (
    "Du beurteilst einen kompletten Vorgang (Akte) in einem "
    "Dokumentenmanagementsystem und schlaegst pruefenswerte naechste "
    "Schritte vor. Du gibst KEINE verbindliche Rechts- oder "
    "Steuerberatung: formuliere jede Empfehlung als pruefenswerten "
    "Schritt, nicht als Anweisung, und rate im Zweifel dazu, eine "
    "Fachperson einzubeziehen.\n"
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein "
    "Markdown, kein Fliesstext drumherum) mit genau diesen Schluesseln:\n"
    '- "lage": 2-5 Saetze Fliesstext auf Deutsch, wo der Vorgang steht '
    "(Stand, offene Punkte, erkennbare Fristen).\n"
    '- "empfehlungen": Liste von maximal {max_items} Objekten mit:\n'
    '  - "titel": kurzer, handlungsorientierter Titel.\n'
    '  - "begruendung": 1-3 Saetze, warum dieser Schritt jetzt ansteht.\n'
    '  - "frist": vorgeschlagene Frist als "YYYY-MM-DD", sonst null.\n'
    '  - "prioritaet": "hoch", "mittel" oder "niedrig", sonst null.\n'
    '  - "quellen": Liste der Dokument-IDs (Zahlen), auf die sich die '
    "Empfehlung stuetzt -- ausschliesslich IDs aus der unten "
    "gelisteten Dokumentliste, keine erfundenen.\n"
    "Jede Empfehlung MUSS mindestens eine Quelle nennen. Erfinde keine "
    "Sachverhalte, die aus den Zusammenfassungen/Key-Facts nicht "
    "hervorgehen -- gib lieber weniger Empfehlungen aus. Ist nichts zu "
    "tun, gib eine leere Liste zurueck."
)


def _prompt_key_facts(document: Document) -> str:
    key_facts = document.key_facts or {}
    if not isinstance(key_facts, dict):
        return ""
    parts = [
        f"{field}={value}"
        for field, value in key_facts.items()
        if field not in _SKIPPED_KEY_FACTS and str(value or "").strip()
    ]
    return "; ".join(parts)


def _document_block(document: Document, max_summary_chars: int) -> str:
    """Ein Dokument, wie es im Prompt steht: Kopfzeile mit ID/Datum/
    Richtung/Kontakt, darunter Zusammenfassung und Key-Facts (#1020).

    Die ID in eckigen Klammern ist der Anker fuer die Quellenangabe --
    das Modell zitiert genau diese Zahl zurueck, siehe
    `_source_documents`.
    """
    header = f"[Dokument {document.pk}] {document.display_date.isoformat()}"
    if document.display_date_is_upload_fallback:
        header += " (Upload-Datum)"
    header += f" · {document.get_direction_display()}"
    if document.correspondent:
        header += f" · {document.correspondent.name}"
    header += f" · {document.title}"

    lines = [header]
    summary = (document.summary or "").strip()
    lines.append(
        f"Zusammenfassung: {summary[:max_summary_chars]}"
        if summary
        else "Zusammenfassung: (keine -- Dokument noch nicht KI-analysiert)"
    )
    key_facts = _prompt_key_facts(document)
    if key_facts:
        lines.append(f"Key-Facts: {key_facts}")
    return "\n".join(lines)


def _basis_documents(vorgang, user):
    """Die Dokumente des Vorgangs, die der Nutzer sehen darf -- chronologisch
    (aeltestes zuerst), damit das Modell den Verlauf in der Reihenfolge
    liest, in der er passiert ist.

    Sortiert ueber dieselbe `Coalesce`-Annotation wie
    `apps.documents.views.filtered_documents` (#1085), damit Dokumente
    ohne erkanntes `document_date` an ihrer chronologisch richtigen
    Stelle stehen statt pauschal am Rand.
    """
    return (
        Document.objects.visible_to(user)
        .filter(vorgaenge=vorgang)
        .select_related("correspondent")
        .annotate(
            effective_date=Coalesce(
                "document_date", TruncDate("created_at"), output_field=DateField()
            )
        )
        .order_by("effective_date", "created_at")
        .distinct()
    )


def _limited(documents: list[Document], limit: int) -> tuple[list[Document], bool]:
    """Bei sehr vielen Dokumenten die *juengsten* `limit` behalten und
    wieder chronologisch zurueckgeben.

    Die juengsten, weil die naechsten Schritte aus dem aktuellen Stand
    folgen -- die ersten Dokumente eines langen Vorgangs sind meist
    Vorgeschichte. Der zweite Rueckgabewert meldet, dass gekuerzt wurde;
    das landet in `based_on["truncated"]` und wird im Panel angezeigt
    (keine stille Kuerzung).
    """
    if len(documents) <= limit:
        return documents, False
    return documents[-limit:], True


def _build_messages(vorgang, documents: list[Document], max_items: int) -> list[Message]:
    max_summary_chars = settings.FINDUS_VORGANG_RECOMMENDATION_MAX_SUMMARY_CHARS
    context = [
        f"Vorgang: {vorgang.name}",
        f"Status: {vorgang.get_status_display()}",
    ]
    if vorgang.description.strip():
        context.append(f"Beschreibung: {vorgang.description.strip()}")
    context.append(f"Heutiges Datum: {timezone.localdate().isoformat()}")
    context.append("")
    context.append("Dokumente des Vorgangs (chronologisch, aeltestes zuerst):")
    context.extend(_document_block(document, max_summary_chars) for document in documents)
    return [
        Message(role="system", content=_SYSTEM_PROMPT.format(max_items=max_items)),
        Message(role="user", content="\n\n".join(context)),
    ]


def _parse_due_date(value: object):
    """Wie `analysis._parse_document_date`: eine Modellantwort ist freier
    Text, kein validiertes Feld -- ein leerer/unpassender Wert wird zu
    `None`, statt die ganze Empfehlung an einem `ValueError` scheitern zu
    lassen.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _normalize_priority(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in VorgangRecommendation.Priority.values else ""


def _source_documents(value: object, allowed_ids: set[int]) -> list[int]:
    """Die genannten Quell-IDs, gefiltert auf die tatsaechlich
    mitgeschickten Dokumente.

    Das ist die Halluzinations-Bremse: eine ID, die nie im Prompt stand
    (erfunden, oder aus einem Dokument ausserhalb des `visible_to`-Scopes
    geraten), wird verworfen statt verlinkt.
    """
    if not isinstance(value, list):
        return []
    source_ids = []
    for item in value:
        try:
            document_id = int(item)
        except (TypeError, ValueError):
            continue
        if document_id in allowed_ids and document_id not in source_ids:
            source_ids.append(document_id)
    return source_ids


def _based_on(considered: list[Document], used: list[Document], truncated: bool) -> dict:
    latest = max((document.display_date for document in considered), default=None)
    return {
        "document_ids": [document.pk for document in used],
        "considered_document_ids": sorted(document.pk for document in considered),
        "document_count": len(considered),
        "used_count": len(used),
        "truncated": truncated,
        "latest_document_date": latest.isoformat() if latest else None,
    }


@transaction.atomic
def _apply_result(run, parsed: dict, used: list[Document], *, model: str, version: str) -> None:
    """Ersetzt Lage + Empfehlungen des Laufs in einer Transaktion.

    Die alten Empfehlungen fallen erst hier -- waehrend der Generierung
    bleiben sie stehen, damit "neu generieren" die Anzeige nicht fuer die
    Dauer des Worker-Laufs leerraeumt.
    """
    parsed = clean_json(parsed)
    max_items = settings.FINDUS_VORGANG_RECOMMENDATION_MAX_ITEMS
    allowed_ids = {document.pk for document in used}

    items = parsed.get("empfehlungen")
    if not isinstance(items, list):
        items = []
    if len(items) > max_items:
        logger.warning(
            "Handlungsempfehlungen fuer Vorgang %s: Modell lieferte %s Empfehlungen, "
            "gekuerzt auf das Limit von %s",
            run.vorgang_id,
            len(items),
            max_items,
        )
        items = items[:max_items]

    run.recommendations.all().delete()

    order = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("titel") or "").strip()
        if not title:
            continue
        recommendation = VorgangRecommendation.objects.create(
            run=run,
            order=order,
            title=title[:255],
            rationale=str(item.get("begruendung") or "").strip(),
            due_date=_parse_due_date(item.get("frist")),
            priority=_normalize_priority(item.get("prioritaet")),
        )
        recommendation.documents.set(_source_documents(item.get("quellen"), allowed_ids))
        order += 1

    run.situation = str(parsed.get("lage") or "").strip()
    run.status = VorgangRecommendationRun.Status.READY
    run.generated_at = timezone.now()
    run.error = ""
    run.ai_model = model
    run.ai_model_version = version
    run.save(
        update_fields=[
            "situation",
            "status",
            "generated_at",
            "error",
            "ai_model",
            "ai_model_version",
            "updated_at",
        ]
    )


def start_recommendation_run(vorgang) -> VorgangRecommendationRun:
    """Markiert den (ggf. neu angelegten) Lauf als `running`, synchron beim
    Knopfdruck -- so zeigt das Panel den Spinner ab dem Klick, statt bis
    zum Anlaufen des Workers noch den alten Stand zu behaupten (dasselbe
    Muster wie `document_analysis_rerun`, #1063).

    Ein bereits vorhandenes Ergebnis bleibt dabei absichtlich stehen: es
    ist bis zum Eintreffen des neuen weiterhin die beste verfuegbare
    Auskunft.
    """
    run, _ = VorgangRecommendationRun.objects.get_or_create(vorgang=vorgang)
    run.status = VorgangRecommendationRun.Status.RUNNING
    run.error = ""
    run.save(update_fields=["status", "error", "updated_at"])
    return run


def generate_vorgang_recommendations(
    vorgang_id: int,
    user_id: int,
    *,
    generation_provider: GenerationProvider | None = None,
) -> VorgangRecommendationRun:
    """Erzeugt die Handlungsempfehlungen fuer einen Vorgang -- ein
    `generate()`-Call, `visible_to`-gescoped auf `user_id`.

    Wirft nicht: ein Provider-Ausfall oder eine bis zuletzt unparsbare
    Antwort landet als `status="failed"` + `error` am Lauf und damit
    sichtbar im Panel. Genau das ist der Unterschied zu #1020 -- dort ist
    die Analyse Beiwerk einer laufenden Pipeline, hier ist sie das
    angeforderte Ergebnis.
    """
    vorgang = Vorgang.objects.get(pk=vorgang_id)
    run, _ = VorgangRecommendationRun.objects.get_or_create(vorgang=vorgang)

    try:
        user = get_user_model().objects.get(pk=user_id)
        considered = list(_basis_documents(vorgang, user))
        used, truncated = _limited(
            considered, settings.FINDUS_VORGANG_RECOMMENDATION_MAX_DOCUMENTS
        )
        if truncated:
            logger.warning(
                "Handlungsempfehlungen fuer Vorgang %s: Datenbasis auf die juengsten "
                "%s von %s sichtbaren Dokumenten begrenzt",
                vorgang_id,
                len(used),
                len(considered),
            )

        run.based_on = _based_on(considered, used, truncated)

        if not used:
            # Kein sichtbares Dokument -> nichts, worueber zu urteilen waere.
            # Fertig melden statt einen generate()-Call fuer eine leere
            # Datenbasis zu bezahlen; das Panel sagt es dann selbst.
            run.situation = ""
            run.recommendations.all().delete()
            run.status = VorgangRecommendationRun.Status.READY
            run.generated_at = timezone.now()
            run.error = ""
            run.save(
                update_fields=[
                    "based_on",
                    "situation",
                    "status",
                    "generated_at",
                    "error",
                    "updated_at",
                ]
            )
            return run

        provider = generation_provider or get_generation_provider()
        result = generate_json(
            provider,
            _build_messages(
                vorgang, used, settings.FINDUS_VORGANG_RECOMMENDATION_MAX_ITEMS
            ),
        )
        run.save(update_fields=["based_on", "updated_at"])
        _apply_result(run, result.data, used, model=result.model, version=result.version)
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "Handlungsempfehlungen fehlgeschlagen fuer Vorgang %s, roher "
                "Modell-Output (gekuerzt): %s",
                vorgang_id,
                raw_text,
            )
        else:
            logger.exception("Handlungsempfehlungen fehlgeschlagen fuer Vorgang %s", vorgang_id)
        run.status = VorgangRecommendationRun.Status.FAILED
        run.error = str(exc)
        run.save(update_fields=["status", "error", "updated_at"])

    return run


def is_stale_for(run, user) -> bool:
    """"Veraltet"-Hinweis (#1093): hat sich die Dokumentbasis seit der
    Generierung geaendert?

    Verglichen wird gegen `based_on["considered_document_ids"]`, also den
    Bestand zum Zeitpunkt der Generierung -- nicht gegen die (evtl.
    gekuerzte) Prompt-Liste, sonst waere ein gekuerzter Lauf sofort
    "veraltet".

    Bewusst nur "es ist etwas dazugekommen oder etwas hat sich geaendert",
    nicht "etwas fehlt": ob ein damals beruecksichtigtes Dokument
    verschwunden ist oder fuer *diesen* Nutzer nur unsichtbar, laesst
    sich von hier aus nicht unterscheiden -- ein Kollege mit engerem
    Sichtbarkeitsbereich bekaeme sonst dauerhaft einen falschen
    Veraltet-Hinweis.
    """
    if run is None or run.status != VorgangRecommendationRun.Status.READY:
        return False
    if run.generated_at is None:
        return False

    considered = set(run.based_on.get("considered_document_ids") or [])
    current = Document.objects.visible_to(user).filter(vorgaenge=run.vorgang)
    current_ids = set(current.values_list("pk", flat=True))
    if current_ids - considered:
        return True
    return current.filter(pk__in=considered, updated_at__gt=run.generated_at).exists()
