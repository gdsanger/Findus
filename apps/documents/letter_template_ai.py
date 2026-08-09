"""KI-Assistent zum Entwerfen einer Brief-Vorlage (#1097) -- der
„skill-creator" für #1094.

Eine Ebene *über* der Brief-Generierung (#1095): hier entsteht nicht der
fertige Brief, sondern der Body der Vorlage -- die **Anleitung**
(Markdown: Ton, Aufbau, Vorgaben) und die **Platzhalter** samt
vorgeschlagener Findus-Quelle. Aus „Widerspruch gegen eine
Inkasso-Forderung, sachlich-bestimmt, mit Fristsetzung" wird das, was
man sonst von Hand tippt.

Ein `generate()`-Call pro Klick, synchron im Request (siehe
`letter_template_views.letter_template_draft`) -- das Ergebnis wird
nirgends gespeichert, es füllt nur die Formularfelder vor. Ein Worker-Job
bräuchte deshalb erst einen Ablageort samt Polling für ein Ergebnis, auf
das der Nutzer ohnehin wartet.

Der Quellen-Katalog im Prompt kommt aus der Registry
(`letter_bindings.source_choices`), nicht aus einer zweiten, hier
gepflegten Liste: eine später registrierte Quelle steht damit sofort im
Prompt *und* wird sofort akzeptiert. Umgekehrt ist alles, was das Modell
sonst als Quelle nennt, ein Halluzinat -- solche Zeilen fallen auf
`manual` zurück (der Nutzer trägt den Wert dann beim Erzeugen selbst
ein) und werden in `unknown_sources` gemeldet, statt still eine
kaputte Bindung anzulegen.

JSON-Robustheit wie in #1028 (`generate_json`: defensives Parsen ->
Repair -> ein Retry, Truncation ist ein Fehler). Was danach immer noch
nicht brauchbar ist -- auch eine formal gültige Antwort ohne Anleitung --
wird als `LetterTemplateDraftError` geworfen und im Formular angezeigt;
still verpuffen darf ein Knopfdruck nicht.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .letter_bindings import source_choices, source_label, validate_source
from .models import LetterTemplate, LetterTemplatePlaceholder
from .text_sanitize import clean_json

logger = logging.getLogger(__name__)

# Die Rückfall-Quelle für alles, was sich nicht binden lässt: „beim
# Erzeugen des Schreibens vom Nutzer ausfüllen lassen".
MANUAL_SOURCE = "manual"

# Was `_to_draft` aus der Antwort liest. Fehlt einer davon, wiederholt
# `generate_json` den Call, statt uns ein leeres Ergebnis unterzuschieben.
_REQUIRED_KEYS = ("anleitung", "platzhalter")

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})

_SYSTEM_PROMPT = (
    "Du bist Vorlagen-Autor in einem Dokumentenmanagementsystem. Aus einer "
    "kurzen Absichtsbeschreibung entwirfst du eine **Brief-Vorlage** -- "
    "also die Anleitung, nach der spaeter ein konkreter Brief geschrieben "
    "wird, NICHT den fertigen Brief selbst. Schreibe deshalb keinen "
    "Beispielbrief und keine Anrede aus, sondern Regeln.\n"
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein "
    "Markdown, keine Code-Fences, kein Text davor oder danach) mit genau "
    "diesen Schluesseln, in dieser Reihenfolge:\n"
    '- "anleitung": Markdown-Text mit den Abschnitten "## Ton", "## Aufbau" '
    'und "## Vorgaben". Ton: Stil und Haltung. Aufbau: nummerierte '
    "Abschnitte des Schreibens. Vorgaben: was zwingend rein muss, was nicht "
    "hineingehoert, wie mit fehlenden Angaben umzugehen ist. Verweise auf "
    # Vervierfachte Klammern: das hier ist eine `.format`-Vorlage, aus
    # `{{{{schluessel}}}}` wird die Platzhalter-Schreibweise {{schluessel}}.
    "Platzhalter im Text als {{{{schluessel}}}} -- genau die Schluessel, die "
    'du unter "platzhalter" auffuehrst.\n'
    '- "platzhalter": Liste von maximal {max_placeholders} Objekten mit:\n'
    '  - "key": Schluessel, nur Kleinbuchstaben/Ziffern/Unterstrich, '
    "beginnend mit einem Buchstaben (z. B. aktenzeichen, "
    "empfaenger_adresse).\n"
    '  - "label": kurze deutsche Bezeichnung.\n'
    '  - "quelle": GENAU einer der unten gelisteten Quellen-Schluessel. '
    "Erfinde keine weiteren; passt keine, nimm \"" + MANUAL_SOURCE + "\".\n"
    '  - "pflicht": true, wenn das Schreiben ohne diesen Wert nicht '
    "abgeschickt werden kann, sonst false.\n"
    '- "name": kurzer Name der Vorlage (max. 60 Zeichen).\n'
    '- "kategorie": eine Kategorie, bevorzugt aus: {categories}.\n'
    '- "beschreibung": ein Satz, wann man diese Vorlage benutzt.\n'
    "Alle Schluessel MUESSEN vorhanden sein; notfalls mit leerem Wert, aber "
    "nicht weggelassen. Schreibe auf Deutsch.\n"
    "Die Vorlage darf keine verbindliche Rechts- oder Steuerberatung "
    "enthalten: formuliere Vorgaben so, dass Fristen, Betraege und "
    "Rechtsfolgen vom Nutzer geprueft werden, und erfinde keine "
    "Paragraphen oder Fristen, die in der Beschreibung nicht vorkommen."
)


class LetterTemplateDraftError(Exception):
    """Der Entwurf kam nicht zustande -- Provider-Ausfall, unbrauchbares
    JSON oder eine Antwort ohne Anleitung. Die Meldung landet im Formular.
    """


@dataclass(frozen=True)
class PlaceholderSuggestion:
    """Ein vorgeschlagener Platzhalter -- dieselben vier Felder, die eine
    Zeile in `LetterTemplatePlaceholder` hat, aber noch nichts Gespeichertes.
    """

    key: str
    label: str
    source: str
    required: bool = False

    @property
    def source_label(self):
        return source_label(self.source)


@dataclass(frozen=True)
class LetterTemplateDraft:
    """Das Ergebnis eines Entwurfs-Calls. Reines Vorschlagsgut: die View
    schreibt es in die Formularfelder, gespeichert wird erst auf Knopfdruck.
    """

    instructions: str
    placeholders: tuple[PlaceholderSuggestion, ...]
    name: str = ""
    category: str = ""
    description: str = ""
    # Quellen, die das Modell genannt hat, die es aber nicht gibt -- die
    # betroffenen Zeilen stehen auf `manual`. Wird in der UI angezeigt.
    unknown_sources: tuple[str, ...] = ()
    model: str = ""
    version: str = ""


def _source_catalogue() -> str:
    """Die waehlbaren Quellen als Prompt-Block, direkt aus der Registry."""
    lines = []
    for group_label, options in source_choices():
        for value, option_label in options:
            lines.append(f"- {value}: {group_label} · {option_label}")
    return "\n".join(lines)


def _build_messages(*, intent: str, category: str, tone: str, max_placeholders: int):
    context = [
        "Verfuegbare Quellen-Schluessel fuer Platzhalter:",
        _source_catalogue(),
        "",
        f"Absicht der Vorlage: {intent.strip()}",
    ]
    if category.strip():
        context.append(f"Gewuenschte Kategorie: {category.strip()}")
    if tone.strip():
        context.append(f"Hinweise zu Ton/Stil: {tone.strip()}")
    return [
        Message(
            role="system",
            content=_SYSTEM_PROMPT.format(
                max_placeholders=max_placeholders,
                categories=", ".join(LetterTemplate.CATEGORY_SUGGESTIONS),
            ),
        ),
        Message(role="user", content="\n".join(context)),
    ]


def normalize_placeholder_key(value: object) -> str:
    """Modell-Freitext -> ein Schluessel, den `LetterTemplatePlaceholder`
    akzeptiert (`^[a-z][a-z0-9_]*$`), oder "" wenn nichts uebrig bleibt.

    Modelle liefern den Schluessel gern in der Schreibweise, in der er im
    Text steht (`{{Empfänger-Adresse}}`) -- Klammern weg, Umlaute
    ausgeschrieben (nicht ersatzlos gestrichen: „empfaenger" ist lesbar,
    „empfnger" nicht), alles Uebrige zu Unterstrichen.
    """
    text = str(value or "").strip().lower()
    text = text.replace("{{", " ").replace("}}", " ")
    text = text.translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"^[^a-z]+", "", text)
    return text[:64].strip("_")


def _normalize_source(value: object) -> tuple[str, str]:
    """`(Quelle, verworfener Modellwert)` -- eine Quelle, die die Registry
    nicht kennt, wird zu `manual` und meldet sich im zweiten Rueckgabewert.

    Bewusst kein Raten per Praefix-Aehnlichkeit: „document.aktenzeichen"
    sieht aus wie eine Quelle, ist aber keine, und eine falsch geratene
    Bindung fuellt spaeter stillschweigend den falschen Wert in den Brief.
    `manual` ist der ehrliche Rueckfall -- der Wert wird dann beim Erzeugen
    abgefragt.
    """
    source = str(value or "").strip().strip("{}").strip()
    if not source:
        return MANUAL_SOURCE, ""
    try:
        validate_source(source)
    except ValidationError:
        return MANUAL_SOURCE, source
    return source, ""


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "ja", "yes", "1", "pflicht"}


def _placeholder_suggestions(
    value: object, max_placeholders: int
) -> tuple[tuple[PlaceholderSuggestion, ...], tuple[str, ...]]:
    if not isinstance(value, list):
        return (), ()

    suggestions: list[PlaceholderSuggestion] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        key = normalize_placeholder_key(item.get("key"))
        if not key or not _KEY_PATTERN.match(key):
            # Kein brauchbarer Schluessel -> keine Zeile. Ein Platzhalter
            # ohne gueltigen Schluessel liesse sich ohnehin nicht speichern.
            logger.warning(
                "Brief-Vorlagen-Entwurf: Platzhalter-Vorschlag %r ohne "
                "brauchbaren Schluessel verworfen",
                item.get("key"),
            )
            continue
        if key in seen:
            # Doppelter Schluessel -- die Unique-Constraint der Vorlage
            # liesse ohnehin nur einen davon zu.
            logger.warning(
                "Brief-Vorlagen-Entwurf: doppelter Platzhalter-Schluessel %r "
                "verworfen",
                key,
            )
            continue
        seen.add(key)
        source, rejected = _normalize_source(item.get("quelle"))
        if rejected:
            unknown.append(rejected)
        label = str(item.get("label") or "").strip()
        suggestions.append(
            PlaceholderSuggestion(
                key=key,
                label=label[: LetterTemplatePlaceholder._meta.get_field("label").max_length],
                source=source,
                required=_as_bool(item.get("pflicht")),
            )
        )

    if len(suggestions) > max_placeholders:
        logger.warning(
            "Brief-Vorlagen-Entwurf: Modell lieferte %s Platzhalter, gekuerzt "
            "auf das Limit von %s",
            len(suggestions),
            max_placeholders,
        )
        suggestions = suggestions[:max_placeholders]

    return tuple(suggestions), tuple(dict.fromkeys(unknown))


def _to_draft(parsed: dict, max_placeholders: int, *, model: str, version: str):
    instructions = str(parsed.get("anleitung") or "").strip()
    if not instructions:
        # Formal vollstaendig, inhaltlich leer: der Nutzer haette einen
        # Knopf gedrueckt und nichts bekommen. Lieber ein sichtbarer Fehler.
        raise LetterTemplateDraftError(
            "Die KI hat keine Anleitung geliefert. Bitte die Beschreibung "
            "etwas konkreter formulieren und es erneut versuchen."
        )

    placeholders, unknown = _placeholder_suggestions(
        parsed.get("platzhalter"), max_placeholders
    )
    return LetterTemplateDraft(
        instructions=instructions,
        placeholders=placeholders,
        name=str(parsed.get("name") or "").strip()[
            : LetterTemplate._meta.get_field("name").max_length
        ],
        category=str(parsed.get("kategorie") or "").strip()[
            : LetterTemplate._meta.get_field("category").max_length
        ],
        description=str(parsed.get("beschreibung") or "").strip(),
        unknown_sources=unknown,
        model=model,
        version=version,
    )


def draft_letter_template(
    *,
    intent: str,
    category: str = "",
    tone: str = "",
    generation_provider: GenerationProvider | None = None,
) -> LetterTemplateDraft:
    """Entwirft Anleitung + Platzhalter aus einer kurzen Beschreibung.

    Wirft `LetterTemplateDraftError`, wenn kein brauchbarer Entwurf
    zustande kam -- inklusive Provider-Ausfall und unparsbarer Antwort.
    Der Aufrufer ist eine View, die den Fehler anzeigt; ein Knopfdruck
    ohne Reaktion waere hier das schlechtere Verhalten.
    """
    provider = generation_provider or get_generation_provider()
    max_placeholders = settings.FINDUS_LETTER_TEMPLATE_DRAFT_MAX_PLACEHOLDERS

    try:
        result = generate_json(
            provider,
            _build_messages(
                intent=intent,
                category=category,
                tone=tone,
                max_placeholders=max_placeholders,
            ),
            max_tokens=settings.FINDUS_LETTER_TEMPLATE_DRAFT_MAX_OUTPUT_TOKENS,
            required_keys=_REQUIRED_KEYS,
        )
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "Brief-Vorlagen-Entwurf fehlgeschlagen, roher Modell-Output "
                "(gekuerzt): %s",
                raw_text,
            )
        else:
            logger.exception("Brief-Vorlagen-Entwurf fehlgeschlagen")
        raise LetterTemplateDraftError(str(exc)) from exc

    return _to_draft(
        clean_json(result.data),
        max_placeholders,
        model=result.model,
        version=result.version,
    )
