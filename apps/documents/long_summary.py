"""Ausfuehrliche Zusammenfassung fuer Dokument und Vorgang (#1135): ein
zweites, laengeres KI-Artefakt neben der bestehenden Kurzzusammenfassung
(#1020) -- Fliesstext in Absaetzen statt drei knapper Saetze, gedacht, den
Inhalt verstehbar zu machen, ohne das Original zu lesen. Ausdruecklich
KEIN struktierter Datensatz fuer Handlungsempfehlungen -- die gibt es
bereits je Vorgang (#1093, `apps.documents.recommendations`) und wird hier
nicht dupliziert oder ersetzt: Ratschlaege stehen ausschliesslich als
Saetze im Fliesstext, nichts davon wird geparst oder weiterverarbeitet.

Nur auf Knopfdruck (nie beim Ingest -- die meisten Dokumente werden nie
wieder geoeffnet, sie alle vorab ausfuehrlich zusammenzufassen waere
bezahlter Text, den niemand liest), im Hintergrund, dauerhaft gespeichert
-- direkt auf `Document`/`Vorgang` (`LongSummaryMixin`, siehe models.py),
kein eigenes Run-Model wie `VorgangRecommendationRun`: es gibt hier keine
strukturierten Kind-Zeilen, nur Text plus Provenienz.

Timeout-/Retry-Schachtelung, JSON-Robustheit (die Antwort kommt als JSON
mit einem einzigen Freitext-Schluessel, um `generate_json`s
Reparatur-/Retry-Maschinerie mitzunutzen) und Fehlerbehandlung folgen
demselben Muster wie `recommendations.generate_vorgang_recommendations`
(#1134): `TimeoutException` wird aufgezeichnet und weitergereicht, ein
Django-Q-`hook` ist das Sicherheitsnetz fuer den Fall, dass der eigene
except-Block einen abgestuerzten Worker nicht mehr erreicht, und
`expire_*_if_stalled` ist der Poll-Pfad-Bewacher gegen einen Job, der
spurlos verschwindet.

Veraltung (#1135, Abschnitt "Veralterung sichtbar machen"): beim Dokument
ist die Basis ein einzelner Zeitstempel -- die juengste Aenderung an Text
*oder* Kommentaren (`_document_long_summary_basis`); beim Vorgang die
Menge der beruecksichtigten Dokument-IDs plus ihr juengster Kommentar-
Zeitstempel (`_vorgang_long_summary_basis`), verglichen wie bei
`recommendations.is_stale_for`: nur "es ist etwas dazugekommen", nie
automatisch neu erzeugt.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_q.exceptions import TimeoutException

from apps.ai.providers import (
    GenerationProvider,
    Message,
    Usage,
    capture_usage,
    generate_json,
    get_generation_provider,
)

from .comment_context import build_comment_context
from .models import Document, DocumentComment, Vorgang
from .recommendations import basis_documents, limit_documents
from .text_sanitize import clean_json

logger = logging.getLogger(__name__)

# Die Antwort ist ein JSON-Objekt mit einem einzigen Freitext-Schluessel --
# kein strukturiertes Schema wie bei Analyse/Empfehlungen, aber trotzdem
# ueber `generate_json` statt eines rohen `generate()`-Calls: so profitiert
# auch dieser Call vom Reparatur-/Retry-Verhalten bei abgeschnittener oder
# leicht kaputter Ausgabe (#1028/#1096), ohne die Maschinerie zweimal zu
# schreiben.
_REQUIRED_KEYS = ("zusammenfassung",)

_SKIPPED_KEY_FACTS = ("ai_model", "ai_model_version")

_DOCUMENT_SYSTEM_PROMPT = (
    "Du erstellst eine AUSFUEHRLICHE Zusammenfassung eines einzelnen "
    "Dokuments fuer ein Dokumentenmanagementsystem. Ziel: wer sie liest, "
    "hat den Inhalt verstanden, ohne das Original gelesen zu haben.\n"
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein "
    "Markdown-Codeblock, kein Fliesstext drumherum) mit genau diesem "
    "Schluessel:\n"
    '- "zusammenfassung": der vollstaendige Text als Markdown-Fliesstext, '
    "gegliedert in Absaetze. Laenge nach Bedarf, nicht nach fester Vorgabe "
    "-- ein kurzes Anschreiben braucht weniger als ein langer Vertrag.\n"
    "Nutze AUSSCHLIESSLICH die unten gelieferten Quellen (Dokumenttext, "
    "vorhandene Kurzzusammenfassung/Key-Facts, Notizen des Nutzers). Was "
    "dort nicht steht, wird nicht ergaenzt -- fehlende Information wird "
    "benannt, nicht ueberbrueckt.\n"
    "Hinweise und pruefenswerte naechste Schritte sind erwuenscht, aber "
    "ausschliesslich als Saetze im Fliesstext -- keine Aufzaehlung, keine "
    "Liste, keine eigene Ueberschrift dafuer.\n"
    "Kein Rechts- oder Steuerrat mit Verbindlichkeitsanspruch: ordne "
    "sachlich ein, formuliere pruefenswert, nicht anweisend.\n"
    "Ein Abschnitt \"Notizen des Nutzers\" ist KEIN Dokumentinhalt, "
    "sondern der Handlungsstand aus Sicht des Nutzers -- gib eine Notiz "
    "nie als Aussage des Absenders/Ausstellers wieder. Widerspricht sich "
    "eine Notiz mit dem Dokument (z. B. Zahlungsziel im Dokument vs. "
    "\"bereits bezahlt\" in der Notiz), gilt die Notiz -- sie ist der "
    "aktuellere Stand.\n"
    "Schreibe in der Sprache des Dokuments."
)

_VORGANG_SYSTEM_PROMPT = (
    "Du erstellst eine AUSFUEHRLICHE Zusammenfassung eines kompletten "
    "Vorgangs (Akte) in einem Dokumentenmanagementsystem: die Geschichte "
    "des Vorgangs -- worum es geht, was bisher passiert ist, wo er gerade "
    "steht.\n"
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein "
    "Markdown-Codeblock, kein Fliesstext drumherum) mit genau diesem "
    "Schluessel:\n"
    '- "zusammenfassung": der vollstaendige Text als Markdown-Fliesstext, '
    "gegliedert in Absaetze. Laenge nach Bedarf, nicht nach fester "
    "Vorgabe.\n"
    "Nutze AUSSCHLIESSLICH die unten gelieferten Quellen (Vorgangsdaten, "
    "die Dokumente mit ihren Kurz- und ggf. ausfuehrlichen "
    "Zusammenfassungen, Notizen des Nutzers). Was dort nicht steht, wird "
    "nicht ergaenzt -- fehlende Information wird benannt, nicht "
    "ueberbrueckt.\n"
    "Hinweise und pruefenswerte naechste Schritte sind erwuenscht, aber "
    "ausschliesslich als Saetze im Fliesstext -- keine Aufzaehlung, keine "
    "Liste, keine eigene Ueberschrift dafuer. Fuer eine strukturierte "
    "Liste konkreter naechster Schritte gibt es in diesem System bereits "
    "eine eigene Funktion (Handlungsempfehlungen) -- dupliziere sie hier "
    "nicht.\n"
    "Kein Rechts- oder Steuerrat mit Verbindlichkeitsanspruch: ordne "
    "sachlich ein, formuliere pruefenswert, nicht anweisend.\n"
    "Ein Abschnitt \"Notizen des Nutzers\" ist KEIN Dokumentinhalt, "
    "sondern der Handlungsstand aus Sicht des Nutzers -- gib eine Notiz "
    "nie als Aussage eines Absenders/Ausstellers wieder. Widerspricht "
    "sich eine Notiz mit einem Dokument, gilt die Notiz -- sie ist der "
    "aktuellere Stand.\n"
    "Schreibe in der Sprache der Dokumente."
)

_DOCUMENT_COMMENT_HEADING = (
    "Notizen des Nutzers (chronologisch; das sind Notizen UEBER das "
    "Dokument, kein Dokumentinhalt -- bei Widerspruch zwischen einer "
    "Notiz und dem Dokument gilt die Notiz, sie ist der aktuellere "
    "Stand):"
)

_VORGANG_COMMENT_HEADING = (
    "Notizen des Nutzers zu diesen Dokumenten (chronologisch; das sind "
    "Notizen UEBER die Dokumente, kein Dokumentinhalt -- bei Widerspruch "
    "zwischen einer Notiz und einem Dokument gilt die Notiz, sie ist der "
    "aktuellere Stand):"
)


# -- Dokumentebene -----------------------------------------------------------


def _document_key_facts_line(document: Document) -> str:
    key_facts = document.key_facts or {}
    if not isinstance(key_facts, dict):
        return ""
    parts = [
        f"{field}={value}"
        for field, value in key_facts.items()
        if field not in _SKIPPED_KEY_FACTS and str(value or "").strip()
    ]
    return "; ".join(parts)


def _document_text_for_prompt(document: Document) -> str:
    """Der volle `text_content`, bei sehr langen Dokumenten erkennbar
    gekuerzt (#1135) -- eine stille Kuerzung waere eine Luecke, die der
    Text selbst nicht ausweist.
    """
    max_chars = settings.FINDUS_LONG_SUMMARY_MAX_CHARS
    text = document.text_content or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... Text gekuerzt, das Dokument ist laenger ...]"


def build_document_long_summary_messages(document: Document) -> list[Message]:
    """Eigene, testbare Funktion (#1135): so laesst sich pruefen, was in
    den Prompt geht (Volltext, Kurzzusammenfassung/Key-Facts, Notizen des
    Nutzers als eigener Abschnitt), ohne ein Modell aufzurufen.
    """
    context = [
        f"Titel: {document.title}",
        f"Datum: {document.display_date.isoformat()}",
    ]
    if document.correspondent:
        context.append(f"Korrespondent: {document.correspondent.name}")
    if document.summary.strip():
        context.append(f"Vorhandene Kurzzusammenfassung: {document.summary.strip()}")
    key_facts_line = _document_key_facts_line(document)
    if key_facts_line:
        context.append(f"Key-Facts: {key_facts_line}")
    context.append(f"Dokumenttext:\n{_document_text_for_prompt(document)}")
    context.extend(
        build_comment_context(
            [document],
            settings.FINDUS_LONG_SUMMARY_MAX_COMMENTS,
            heading=_DOCUMENT_COMMENT_HEADING,
        )
    )
    return [
        Message(role="system", content=_DOCUMENT_SYSTEM_PROMPT),
        Message(role="user", content="\n\n".join(context)),
    ]


def _document_long_summary_basis(document: Document) -> dict:
    """"Worauf stuetzte sich die Erzeugung" fuer ein Dokument (#1135): ein
    einziger Zeitstempel -- die juengste Aenderung an Text (`updated_at`)
    *oder* Kommentaren. Absichtlich EIN Wert statt zwei getrennter Felder:
    fuer die Veraltet-Frage zaehlt nur "ist seither ueberhaupt etwas
    passiert", nicht welche der beiden Quellen es war.
    """
    latest_comment = document.comments.order_by("-updated_at").first()
    basis_at = document.updated_at
    if latest_comment is not None and latest_comment.updated_at > basis_at:
        basis_at = latest_comment.updated_at
    return {"basis_updated_at": basis_at.isoformat()}


def document_long_summary_is_stale(document: Document) -> bool:
    """Veraltet-Hinweis (#1135): hat sich Text oder Kommentare seit der
    Erzeugung geaendert? Vergleicht die aktuelle Basis
    (`_document_long_summary_basis`) gegen die gespeicherte -- dieselbe
    "es ist etwas dazugekommen"-Logik wie
    `recommendations.is_stale_for`, nur auf einen einzelnen Zeitstempel
    verdichtet.
    """
    if document.long_summary_status != Document.LongSummaryStatus.READY:
        return False
    if document.long_summary_generated_at is None:
        return False
    stored = document.long_summary_based_on.get("basis_updated_at")
    if not stored:
        return False
    stored_at = parse_datetime(stored)
    if stored_at is None:
        return False
    current = _document_long_summary_basis(document)["basis_updated_at"]
    current_at = parse_datetime(current)
    return current_at is not None and current_at > stored_at


def start_document_long_summary(document: Document) -> None:
    """Markiert das Dokument synchron beim Knopfdruck als `running` (analog
    `document_analysis_rerun`/`recommendations.start_recommendation_run`),
    damit die UI den Spinner ab dem Klick zeigt statt bis zum Anlaufen des
    Workers noch den alten Stand zu behaupten. Ein bereits vorhandener
    Text bleibt dabei absichtlich stehen -- er ist bis zum Eintreffen des
    neuen weiterhin die beste verfuegbare Auskunft.
    """
    document.long_summary_status = Document.LongSummaryStatus.RUNNING
    document.long_summary_error = ""
    document.long_summary_run_started_at = timezone.now()
    document.save(
        update_fields=[
            "long_summary_status",
            "long_summary_error",
            "long_summary_run_started_at",
        ]
    )


def expire_document_long_summary_if_stalled(document: Document) -> None:
    """Netz gegen einen Job, der spurlos verschwindet (Worker-Neustart,
    OOM-Kill), bevor `generate_document_long_summary()` den eigenen
    except-Block *oder* der Django-Q-`hook` erreicht -- dasselbe Prinzip
    wie `recommendations.expire_if_stalled`, hier gegen
    `long_summary_run_started_at` statt `updated_at` verglichen (siehe
    `LongSummaryMixin`-Docstring, warum `updated_at` dafuer nicht
    herhalten darf).
    """
    if document.long_summary_status != Document.LongSummaryStatus.RUNNING:
        return
    started = document.long_summary_run_started_at
    if started is None:
        return
    age_seconds = (timezone.now() - started).total_seconds()
    if age_seconds < settings.FINDUS_LONG_SUMMARY_POLL_TIMEOUT_SECONDS:
        return
    document.long_summary_status = Document.LongSummaryStatus.FAILED
    document.long_summary_error = (
        "Der Hintergrundjob hat sich nicht zurückgemeldet (Zeitüberschreitung "
        "beim Warten auf ein Ergebnis)."
    )
    document.save(update_fields=["long_summary_status", "long_summary_error"])


def generate_document_long_summary(
    document_id: int, *, generation_provider: GenerationProvider | None = None
) -> Document:
    """Erzeugt die ausfuehrliche Zusammenfassung eines Dokuments -- ein
    `generate()`-Call, niemals beim Ingest, nur auf Knopfdruck.

    Wirft nicht fuer einen Provider-Ausfall oder eine bis zuletzt
    unparsbare Antwort: die landet als `status="failed"` + `error` am
    Dokument und damit sichtbar in der UI -- der Nutzer hat auf einen
    Knopf gedrueckt und erwartet eine Antwort, anders als bei der
    fehlertoleranten Kurz-Analyse (#1020).

    `TimeoutException` (Django-Q, erbt von `SystemExit`, #1134) ist die
    eine Ausnahme davon: sie wird aufgezeichnet, dann aber weitergereicht,
    damit Django-Q den Worker-Prozess neu startet.
    """
    document = Document.objects.get(pk=document_id)

    started = time.monotonic()
    usages: list[Usage] = []
    attempts = 0
    try:
        with capture_usage() as usages:
            provider = generation_provider or get_generation_provider()
            result = generate_json(
                provider,
                build_document_long_summary_messages(document),
                max_tokens=settings.FINDUS_LONG_SUMMARY_MAX_OUTPUT_TOKENS,
                required_keys=_REQUIRED_KEYS,
            )
        attempts = result.attempts
        parsed = clean_json(result.data)
        text = str(parsed.get("zusammenfassung") or "").strip()

        document.long_summary = text
        document.long_summary_status = Document.LongSummaryStatus.READY
        document.long_summary_generated_at = timezone.now()
        document.long_summary_based_on = _document_long_summary_basis(document)
        document.long_summary_error = ""
        document.long_summary_ai_model = result.model
        document.long_summary_ai_model_version = result.version
        document.save(
            update_fields=[
                "long_summary",
                "long_summary_status",
                "long_summary_generated_at",
                "long_summary_based_on",
                "long_summary_error",
                "long_summary_ai_model",
                "long_summary_ai_model_version",
            ]
        )
    except TimeoutException:
        logger.exception(
            "Ausfuehrliche Zusammenfassung fuer Document %s: Task-Zeitbudget "
            "aufgebraucht, waehrend noch auf die KI-Antwort gewartet wurde",
            document_id,
        )
        document.long_summary_status = Document.LongSummaryStatus.FAILED
        document.long_summary_error = (
            "Zeitüberschreitung – die KI-Antwort kam nicht rechtzeitig zurück. "
            "Bitte erneut versuchen."
        )
        document.save(update_fields=["long_summary_status", "long_summary_error"])
        raise
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "Ausfuehrliche Zusammenfassung fehlgeschlagen fuer Document %s, "
                "roher Modell-Output (gekuerzt): %s",
                document_id,
                raw_text,
            )
        else:
            logger.exception(
                "Ausfuehrliche Zusammenfassung fehlgeschlagen fuer Document %s",
                document_id,
            )
        document.long_summary_status = Document.LongSummaryStatus.FAILED
        document.long_summary_error = str(exc)
        document.save(update_fields=["long_summary_status", "long_summary_error"])
    finally:
        duration_seconds = time.monotonic() - started
        prompt_tokens = sum(usage.prompt_tokens for usage in usages)
        completion_tokens = sum(usage.completion_tokens for usage in usages)
        logger.info(
            "Ausfuehrliche Zusammenfassung fuer Document %s: %.1fs Laufzeit, %s "
            "generate()-Aufruf(e), %s Prompt-/%s Completion-Tokens",
            document_id,
            duration_seconds,
            attempts,
            prompt_tokens,
            completion_tokens,
        )

    return document


# -- Vorgangsebene ------------------------------------------------------------


def _vorgang_document_block(
    document: Document, max_summary_chars: int, max_long_summary_chars: int
) -> str:
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
        f"Kurzzusammenfassung: {summary[:max_summary_chars]}"
        if summary
        else "Kurzzusammenfassung: (keine -- Dokument noch nicht KI-analysiert)"
    )
    long_summary = (document.long_summary or "").strip()
    if document.has_long_summary and long_summary:
        lines.append(
            f"Ausfuehrliche Zusammenfassung: {long_summary[:max_long_summary_chars]}"
        )
    return "\n".join(lines)


def build_vorgang_long_summary_messages(
    vorgang: Vorgang, documents: list[Document]
) -> list[Message]:
    """Eigene, testbare Funktion (#1135), analog
    `recommendations._build_messages`: Vorgangsdaten, die sichtbaren
    Dokumente mit Kurz- und ggf. ausfuehrlicher Zusammenfassung (NICHT die
    Volltexte -- das wuerde jeden Kontextrahmen sprengen), dann die
    Notizen des Nutzers als eigener Abschnitt.
    """
    max_summary_chars = settings.FINDUS_VORGANG_LONG_SUMMARY_MAX_SUMMARY_CHARS
    max_long_summary_chars = (
        settings.FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENT_LONG_SUMMARY_CHARS
    )
    context = [
        f"Vorgang: {vorgang.name}",
        f"Status: {vorgang.get_status_display()}",
    ]
    if vorgang.description.strip():
        context.append(f"Beschreibung: {vorgang.description.strip()}")
    context.append(f"Heutiges Datum: {timezone.localdate().isoformat()}")
    context.append("")
    context.append("Dokumente des Vorgangs (chronologisch, aeltestes zuerst):")
    context.extend(
        _vorgang_document_block(document, max_summary_chars, max_long_summary_chars)
        for document in documents
    )
    context.extend(
        build_comment_context(
            documents,
            settings.FINDUS_VORGANG_LONG_SUMMARY_MAX_COMMENTS,
            heading=_VORGANG_COMMENT_HEADING,
        )
    )
    return [
        Message(role="system", content=_VORGANG_SYSTEM_PROMPT),
        Message(role="user", content="\n\n".join(context)),
    ]


def _vorgang_long_summary_basis(considered: list[Document]) -> dict:
    """"Worauf stuetzte sich die Erzeugung" fuer einen Vorgang (#1135):
    Anzahl und juengstes Datum der einbezogenen (sichtbaren) Dokumente
    plus deren juengster Kommentar-Zeitstempel -- Letzteres, damit ein
    neuer Kommentar ohne neues Dokument ebenfalls als "seither hat sich
    etwas getan" erkannt wird (Abschnitt "Veralterung sichtbar machen").
    """
    considered_ids = sorted(document.pk for document in considered)
    latest_document_date = max(
        (document.display_date for document in considered), default=None
    )
    latest_comment = (
        DocumentComment.objects.filter(document_id__in=considered_ids)
        .order_by("-updated_at")
        .first()
    )
    return {
        "considered_document_ids": considered_ids,
        "document_count": len(considered_ids),
        "latest_document_date": (
            latest_document_date.isoformat() if latest_document_date else None
        ),
        "latest_comment_at": (
            latest_comment.updated_at.isoformat() if latest_comment else None
        ),
    }


def vorgang_long_summary_is_stale(vorgang: Vorgang, user) -> bool:
    """Veraltet-Hinweis (#1135), analog `recommendations.is_stale_for`:
    ist seither ein fuer `user` sichtbares Dokument dazugekommen, hat sich
    eines der beruecksichtigten geaendert, oder ist ein neuerer Kommentar
    zu einem der beruecksichtigten Dokumente entstanden?

    Bewusst nur "es ist etwas dazugekommen", nicht "etwas fehlt" -- ob ein
    damals beruecksichtigtes Dokument verschwunden oder fuer *diesen*
    Nutzer nur unsichtbar ist, laesst sich von hier aus nicht
    unterscheiden.
    """
    if vorgang.long_summary_status != Vorgang.LongSummaryStatus.READY:
        return False
    if vorgang.long_summary_generated_at is None:
        return False

    considered = set(vorgang.long_summary_based_on.get("considered_document_ids") or [])
    current = Document.objects.visible_to(user).filter(vorgaenge=vorgang)
    current_ids = set(current.values_list("pk", flat=True))
    if current_ids - considered:
        return True
    if current.filter(
        pk__in=considered, updated_at__gt=vorgang.long_summary_generated_at
    ).exists():
        return True
    return DocumentComment.objects.filter(
        document_id__in=considered, updated_at__gt=vorgang.long_summary_generated_at
    ).exists()


def vorgang_long_summary_new_document_count(vorgang: Vorgang, user) -> int:
    """Fuer den Veraltet-Hinweistext ("seitdem sind 2 Dokumente
    hinzugekommen") -- nur die tatsaechlich neu hinzugekommenen, nicht
    verschwundene (siehe `vorgang_long_summary_is_stale`-Docstring).
    """
    considered = set(vorgang.long_summary_based_on.get("considered_document_ids") or [])
    current_ids = set(
        Document.objects.visible_to(user).filter(vorgaenge=vorgang).values_list(
            "pk", flat=True
        )
    )
    return len(current_ids - considered)


def start_vorgang_long_summary(vorgang: Vorgang) -> None:
    """Wie `start_document_long_summary`, nur fuer den Vorgang."""
    vorgang.long_summary_status = Vorgang.LongSummaryStatus.RUNNING
    vorgang.long_summary_error = ""
    vorgang.long_summary_run_started_at = timezone.now()
    vorgang.save(
        update_fields=[
            "long_summary_status",
            "long_summary_error",
            "long_summary_run_started_at",
        ]
    )


def expire_vorgang_long_summary_if_stalled(vorgang: Vorgang) -> None:
    """Wie `expire_document_long_summary_if_stalled`, nur fuer den
    Vorgang.
    """
    if vorgang.long_summary_status != Vorgang.LongSummaryStatus.RUNNING:
        return
    started = vorgang.long_summary_run_started_at
    if started is None:
        return
    age_seconds = (timezone.now() - started).total_seconds()
    if age_seconds < settings.FINDUS_VORGANG_LONG_SUMMARY_POLL_TIMEOUT_SECONDS:
        return
    vorgang.long_summary_status = Vorgang.LongSummaryStatus.FAILED
    vorgang.long_summary_error = (
        "Der Hintergrundjob hat sich nicht zurückgemeldet (Zeitüberschreitung "
        "beim Warten auf ein Ergebnis)."
    )
    vorgang.save(update_fields=["long_summary_status", "long_summary_error"])


def generate_vorgang_long_summary(
    vorgang_id: int,
    user_id: int,
    *,
    generation_provider: GenerationProvider | None = None,
) -> Vorgang:
    """Erzeugt die ausfuehrliche Zusammenfassung eines Vorgangs --
    `visible_to`-gescoped auf `user_id` (der Worker hat keinen Request,
    aus dem sich das ableiten liesse), analog
    `recommendations.generate_vorgang_recommendations`.
    """
    vorgang = Vorgang.objects.get(pk=vorgang_id)

    started = time.monotonic()
    usages: list[Usage] = []
    attempts = 0
    try:
        user = get_user_model().objects.get(pk=user_id)
        considered = list(basis_documents(vorgang, user))
        used, truncated = limit_documents(
            considered, settings.FINDUS_VORGANG_LONG_SUMMARY_MAX_DOCUMENTS
        )
        if truncated:
            logger.warning(
                "Ausfuehrliche Zusammenfassung fuer Vorgang %s: Datenbasis auf "
                "die juengsten %s von %s sichtbaren Dokumenten begrenzt",
                vorgang_id,
                len(used),
                len(considered),
            )

        basis = _vorgang_long_summary_basis(considered)
        basis["truncated"] = truncated
        basis["used_count"] = len(used)

        if not used:
            # Kein sichtbares Dokument -> nichts, worueber zu berichten
            # waere. Fertig melden statt einen generate()-Call fuer eine
            # leere Datenbasis zu bezahlen; die UI sagt es dann selbst.
            vorgang.long_summary = ""
            vorgang.long_summary_status = Vorgang.LongSummaryStatus.READY
            vorgang.long_summary_generated_at = timezone.now()
            vorgang.long_summary_based_on = basis
            vorgang.long_summary_error = ""
            vorgang.save(
                update_fields=[
                    "long_summary",
                    "long_summary_status",
                    "long_summary_generated_at",
                    "long_summary_based_on",
                    "long_summary_error",
                ]
            )
            return vorgang

        with capture_usage() as usages:
            provider = generation_provider or get_generation_provider()
            result = generate_json(
                provider,
                build_vorgang_long_summary_messages(vorgang, used),
                max_tokens=settings.FINDUS_VORGANG_LONG_SUMMARY_MAX_OUTPUT_TOKENS,
                required_keys=_REQUIRED_KEYS,
            )
        attempts = result.attempts
        parsed = clean_json(result.data)
        text = str(parsed.get("zusammenfassung") or "").strip()

        vorgang.long_summary = text
        vorgang.long_summary_status = Vorgang.LongSummaryStatus.READY
        vorgang.long_summary_generated_at = timezone.now()
        vorgang.long_summary_based_on = basis
        vorgang.long_summary_error = ""
        vorgang.long_summary_ai_model = result.model
        vorgang.long_summary_ai_model_version = result.version
        vorgang.save(
            update_fields=[
                "long_summary",
                "long_summary_status",
                "long_summary_generated_at",
                "long_summary_based_on",
                "long_summary_error",
                "long_summary_ai_model",
                "long_summary_ai_model_version",
            ]
        )
    except TimeoutException:
        logger.exception(
            "Ausfuehrliche Zusammenfassung fuer Vorgang %s: Task-Zeitbudget "
            "aufgebraucht, waehrend noch auf die KI-Antwort gewartet wurde",
            vorgang_id,
        )
        vorgang.long_summary_status = Vorgang.LongSummaryStatus.FAILED
        vorgang.long_summary_error = (
            "Zeitüberschreitung – die KI-Antwort kam nicht rechtzeitig zurück. "
            "Bitte erneut versuchen."
        )
        vorgang.save(update_fields=["long_summary_status", "long_summary_error"])
        raise
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "Ausfuehrliche Zusammenfassung fehlgeschlagen fuer Vorgang %s, "
                "roher Modell-Output (gekuerzt): %s",
                vorgang_id,
                raw_text,
            )
        else:
            logger.exception(
                "Ausfuehrliche Zusammenfassung fehlgeschlagen fuer Vorgang %s",
                vorgang_id,
            )
        vorgang.long_summary_status = Vorgang.LongSummaryStatus.FAILED
        vorgang.long_summary_error = str(exc)
        vorgang.save(update_fields=["long_summary_status", "long_summary_error"])
    finally:
        duration_seconds = time.monotonic() - started
        prompt_tokens = sum(usage.prompt_tokens for usage in usages)
        completion_tokens = sum(usage.completion_tokens for usage in usages)
        logger.info(
            "Ausfuehrliche Zusammenfassung fuer Vorgang %s: %.1fs Laufzeit, %s "
            "generate()-Aufruf(e), %s Prompt-/%s Completion-Tokens",
            vorgang_id,
            duration_seconds,
            attempts,
            prompt_tokens,
            completion_tokens,
        )

    return vorgang
