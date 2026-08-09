"""KI-Formulierung eines Briefs aus Vorlage + Kontext (#1095).

Die Ebene *unter* dem Vorlagen-Assistenten (#1097): dort entsteht die
Anleitung, hier der konkrete Brief. Ein `generate()`-Call pro Entwurf,
async im Django-Q-Worker -- anders als beim Vorlagen-Entwurf wartet
niemand mit offenem Formular darauf, und der Prompt ist deutlich größer
(Anleitung + Bindungen + Kontext des beantworteten Dokuments).

Was die KI liefert, ist **nur der Fließtext plus Betreff**. Anschrift,
Datumszeile, Grußformel und Signatur setzt das Layout
(`apps.documents.letter_render`) -- ein Modell, das den kompletten Brief
schreibt, erfindet dabei zuverlässig eine zweite Adresse und ein zweites
Datum, die dann neben den echten stehen. `_strip_layout_echo` räumt es
nachträglich weg, wenn das Modell die Grußformel trotz Ansage doch
mitschreibt.

Datenbasis ist bewusst nicht der Volltext des beantworteten Dokuments,
sondern seine Zusammenfassung und die Key-Facts (#1020) -- dieselbe
Entscheidung wie bei den Handlungsempfehlungen (#1093): dafür wurde
einmal bezahlt, sie sind verdichtet, und ein 40-seitiger Bescheid würde
sonst jeden Kostenrahmen sprengen. Fehlt die Zusammenfassung (Dokument
noch nicht analysiert), geht ersatzweise ein begrenzter Ausschnitt des
extrahierten Texts mit -- sonst stünde die KI vor einem leeren Kontext.

`visible_to`-gescoped: die `user_id` wandert in den Worker-Job, und der
Kontext wird gegen genau diesen Scope noch einmal geprüft -- ein Brief
darf sich nicht auf ein Dokument stützen, das der Auslösende nicht öffnen
darf.

Robustheit wie #1028/#1096: `generate_json` mit `required_keys`, eine am
Output-Limit abgeschnittene Antwort ist ein Fehler mit Retry. Was danach
nicht brauchbar ist, wird `status="failed"` + `error` am Entwurf und damit
im Panel sichtbar -- ein Knopfdruck darf nicht still verpuffen.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .letter_bindings import build_context, resolve_placeholders
from .models import Document, LetterDraft
from .text_sanitize import clean_json, clean_text

logger = logging.getLogger(__name__)

# Reine KI-Metadaten in `Document.key_facts` -- im Prompt nur Ballast
# (identisch zu `recommendations._SKIPPED_KEY_FACTS`).
_SKIPPED_KEY_FACTS = ("ai_model", "ai_model_version")

_REQUIRED_KEYS = ("betreff", "text")

_SYSTEM_PROMPT = (
    "Du formulierst als Assistent in einem Dokumentenmanagementsystem den "
    "Text eines deutschen Geschaeftsbriefs. Du schreibst einen ENTWURF, den "
    "ein Mensch vor dem Versand prueft und freigibt.\n"
    "Antworte AUSSCHLIESSLICH mit einem einzigen JSON-Objekt (kein "
    "Markdown, keine Code-Fences, kein Text davor oder danach) mit genau "
    "diesen Schluesseln, in dieser Reihenfolge:\n"
    '- "betreff": eine praegnante Betreffzeile (max. 120 Zeichen, ohne das '
    'Wort "Betreff").\n'
    '- "text": der Brieftext als Fliesstext, Absaetze durch Leerzeilen '
    "getrennt. Beginne mit der Anrede.\n"
    "Der Text enthaelt NICHT: Briefkopf, Absenderadresse, Empfaengeradresse, "
    "Datumszeile, Betreffzeile, Grussformel, Unterschrift oder Signatur -- "
    "das setzt das Brieflayout automatisch darum herum. Schreibst du sie "
    "trotzdem, stehen sie doppelt im Brief.\n"
    "Halte dich an die Anleitung der Vorlage (Ton, Aufbau, Vorgaben). "
    "Verwende die unten angegebenen Werte woertlich; ist ein Wert nicht "
    "angegeben, erfinde ihn NICHT -- schreibe stattdessen einen "
    "Platzhalter in eckigen Klammern (z. B. [Aktenzeichen bitte "
    "ergaenzen]), damit die Luecke beim Pruefen auffaellt.\n"
    "Erfinde keine Sachverhalte, Betraege, Fristen oder Paragraphen, die "
    "sich nicht aus dem Kontext ergeben. Du gibst keine verbindliche "
    "Rechts- oder Steuerberatung."
)

# Wird das Modell trotz Ansage doch geschwaetzig, endet der Text mit
# Grussformel und Unterschrift -- beides setzt das Layout schon.
_CLOSING_ECHO_RE = re.compile(
    r"^(mit freundlichen gr(ü|ue)ssen|mit freundlichen gr(ü|u)ßen|"
    r"freundliche gr(ü|ue|u)(ss|ß)e|hochachtungsvoll|beste gr(ü|ue|u)(ss|ß)e|"
    r"viele gr(ü|ue|u)(ss|ß)e)\b",
    re.IGNORECASE,
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


class LetterGenerationError(Exception):
    """Der Entwurf kam nicht zustande. Die Meldung landet am Entwurf und
    damit sichtbar im Review-Panel.
    """


def _placeholder_lines(template, values) -> list[str]:
    """Die Bindungen als „Schlüssel (Bezeichnung): Wert"-Zeilen.

    Auch die *leeren* stehen hier -- explizit als „(nicht bekannt)".
    Weggelassen wüsste das Modell nicht, dass es den Wert nicht hat, und
    füllte die Lücke mit etwas Plausiblem.
    """
    lines = []
    for placeholder in template.placeholders.all():
        value = (values.get(placeholder.key) or "").strip()
        marker = " [PFLICHT]" if placeholder.required else ""
        lines.append(
            f"- {{{{{placeholder.key}}}}} ({placeholder.display_label}){marker}: "
            f"{value or '(nicht bekannt)'}"
        )
    return lines


def _key_fact_line(document: Document) -> str:
    key_facts = document.key_facts or {}
    if not isinstance(key_facts, dict):
        return ""
    parts = [
        f"{name}={value}"
        for name, value in key_facts.items()
        if name not in _SKIPPED_KEY_FACTS and str(value or "").strip()
    ]
    return "; ".join(parts)


def _source_document_block(document: Document) -> list[str]:
    """Das beantwortete Dokument als Prompt-Block: Kopf, Zusammenfassung,
    Key-Facts -- und nur ersatzweise ein Textausschnitt.
    """
    lines = [
        "Beantwortetes Dokument:",
        f"- Titel: {document.title}",
        f"- Datum: {document.display_date.isoformat()}",
        f"- Richtung: {document.get_direction_display()}",
    ]
    if document.correspondent:
        lines.append(f"- Kontakt: {document.correspondent.name}")

    summary = (document.summary or "").strip()
    max_chars = settings.FINDUS_LETTER_CONTEXT_MAX_CHARS
    if summary:
        lines.append(f"- Zusammenfassung: {summary[:max_chars]}")
    else:
        excerpt = (document.text_content or "").strip()
        if excerpt:
            lines.append(f"- Textauszug (noch keine KI-Zusammenfassung): {excerpt[:max_chars]}")

    key_facts = _key_fact_line(document)
    if key_facts:
        lines.append(f"- Key-Facts: {key_facts}")
    return lines


def build_messages(draft, *, source_document=None) -> list[Message]:
    """Der komplette Prompt für einen Entwurf.

    `source_document` wird übergeben statt aus dem Entwurf gelesen: der
    Aufrufer hat es schon durch `visible_to` gezogen, und diese Funktion
    soll keine zweite, ungescopte Meinung dazu haben.
    """
    template = draft.template
    context = [f"Vorlage: {draft.template_name or (template.name if template else '')}"]
    if template is not None and template.description.strip():
        context.append(f"Zweck der Vorlage: {template.description.strip()}")

    instructions = (template.instructions if template else "").strip()
    context.append("")
    context.append("Anleitung der Vorlage:")
    context.append(instructions or "(keine Anleitung hinterlegt -- schreibe einen sachlichen Geschäftsbrief.)")

    if template is not None:
        lines = _placeholder_lines(template, draft.placeholder_values or {})
        if lines:
            context.append("")
            context.append(
                "Werte fuer die Platzhalter der Vorlage (im Text als "
                "{{schluessel}} referenziert):"
            )
            context.extend(lines)

    context.append("")
    context.append(f"Absender (Briefkopf, steht schon im Layout): {draft.sender_block or '(unbekannt)'}")
    context.append(f"Empfaenger (Anschrift, steht schon im Layout): {draft.recipient_block or '(unbekannt)'}")
    context.append(f"Grussformel (steht schon im Layout): {draft.layout_value('closing') or '(keine)'}")
    context.append(f"Heutiges Datum: {timezone.localdate().isoformat()}")

    if draft.vorgang is not None:
        context.append("")
        context.append(f"Vorgang: {draft.vorgang.name} ({draft.vorgang.get_status_display()})")
        if draft.vorgang.description.strip():
            context.append(f"Beschreibung des Vorgangs: {draft.vorgang.description.strip()}")

    if source_document is not None:
        context.append("")
        context.extend(_source_document_block(source_document))

    if draft.notes.strip():
        context.append("")
        context.append(f"Zusaetzliche Hinweise des Nutzers (haben Vorrang vor Stil-Vorgaben der Vorlage): {draft.notes.strip()}")

    return [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content="\n".join(context)),
    ]


def _strip_layout_echo(text: str, closing: str) -> str:
    """Schneidet eine vom Modell mitgeschriebene Grußformel samt allem
    Nachfolgenden ab.

    Nur am *Ende* und nur bei einer der bekannten Formeln: mitten im Text
    ist „mit freundlichen Grüßen" ein Zitat, keine Grußformel, und ein zu
    eifriger Filter würde den Brief verstümmeln.
    """
    lines = text.rstrip().splitlines()
    closing_normalized = (closing or "").strip().lower()
    for index in range(len(lines) - 1, -1, -1):
        candidate = lines[index].strip().lower().rstrip(",")
        if not candidate:
            continue
        if _CLOSING_ECHO_RE.match(candidate) or (
            closing_normalized and candidate == closing_normalized.rstrip(",")
        ):
            return "\n".join(lines[:index]).rstrip()
        # Nur die letzten Zeilen prüfen: darunter darf nur noch die
        # Unterschrift/Signatur stehen, nicht der halbe Brief.
        if len(lines) - index > 4:
            break
    return text.rstrip()


def apply_placeholder_values(text: str, values: dict) -> str:
    """Ersetzt verbliebene ``{{schluessel}}`` durch ihren gebundenen Wert.

    Sicherheitsnetz: das Modell übernimmt die Platzhalter-Schreibweise aus
    der Anleitung manchmal wörtlich in den Brief. Ein Schlüssel ohne Wert
    (und ein unbekannter) bleibt bewusst stehen -- im Review sichtbar
    stehen zu bleiben ist besser als still zu verschwinden.
    """

    def replace(match):
        value = (values or {}).get(match.group(1)) or ""
        return value.strip() or match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text or "")


def _apply_reply(draft, parsed: dict, *, model: str, version: str) -> None:
    parsed = clean_json(parsed)
    values = draft.placeholder_values or {}

    subject = apply_placeholder_values(str(parsed.get("betreff") or "").strip(), values)
    body = apply_placeholder_values(str(parsed.get("text") or "").strip(), values)
    body = _strip_layout_echo(body, draft.layout_value("closing") or "")

    if not body:
        raise LetterGenerationError(
            "Die KI hat keinen Brieftext geliefert. Bitte die Hinweise "
            "konkreter fassen und erneut generieren."
        )

    draft.subject = clean_text(subject)[:255] or draft.subject
    draft.body_text = body
    draft.status = LetterDraft.Status.READY
    draft.error = ""
    draft.ai_model = model
    draft.ai_model_version = version
    draft.save(
        update_fields=[
            "subject",
            "body_text",
            "status",
            "error",
            "ai_model",
            "ai_model_version",
            "updated_at",
        ]
    )


def refresh_placeholder_values(draft, *, source_document=None) -> dict:
    """Löst die Bindungen der Vorlage gegen den aktuellen Findus-Bestand auf
    und schreibt sie an den Entwurf.

    Läuft vor jeder Generierung, nicht nur beim Anlegen: „neu generieren"
    soll die inzwischen korrigierte Adresse verwenden. Die manuellen
    Eingaben des Nutzers gehen dabei als Kontext mit ein und überleben so
    jeden Lauf.
    """
    if draft.template is None:
        draft.placeholder_values = {}
    else:
        context = build_context(
            document=source_document,
            vorgang=draft.vorgang,
            kontakt=draft.recipient,
            manual_values=draft.manual_values or {},
        )
        draft.placeholder_values = resolve_placeholders(draft.template, context)
    draft.save(update_fields=["placeholder_values", "updated_at"])
    return draft.placeholder_values


def generate_letter_draft(
    draft_id: int,
    user_id: int,
    *,
    generation_provider: GenerationProvider | None = None,
) -> LetterDraft:
    """Formuliert den Brieftext für einen Entwurf -- ein `generate()`-Call.

    Wirft nicht: ein Provider-Ausfall oder eine unbrauchbare Antwort landet
    als `status="failed"` + `error` am Entwurf und damit sichtbar im
    Review-Panel. Danach ist der Entwurf trotzdem bearbeitbar -- der Nutzer
    kann den Text selbst schreiben, statt in einer Sackgasse zu stehen.
    """
    draft = LetterDraft.objects.get(pk=draft_id)

    try:
        user = get_user_model().objects.get(pk=user_id)
        source_document = None
        if draft.source_document_id is not None:
            # Erneut gegen `visible_to` gezogen: zwischen Anlegen und
            # Worker-Lauf kann das Dokument privat gestellt worden sein,
            # und sein Inhalt darf dann nicht in den Prompt (und über den
            # Brieftext wieder heraus).
            source_document = (
                Document.objects.visible_to(user)
                .select_related("correspondent")
                .filter(pk=draft.source_document_id)
                .first()
            )
            if source_document is None:
                logger.warning(
                    "Brief-Entwurf %s: Bezugsdokument %s ist fuer Nutzer %s nicht "
                    "sichtbar -- Generierung laeuft ohne dessen Kontext",
                    draft.pk,
                    draft.source_document_id,
                    user_id,
                )

        refresh_placeholder_values(draft, source_document=source_document)

        result = generate_json(
            generation_provider or get_generation_provider(),
            build_messages(draft, source_document=source_document),
            max_tokens=settings.FINDUS_LETTER_MAX_OUTPUT_TOKENS,
            required_keys=_REQUIRED_KEYS,
        )
        _apply_reply(draft, result.data, model=result.model, version=result.version)
    except Exception as exc:
        raw_text = getattr(exc, "raw_text", None)
        if raw_text is not None:
            logger.exception(
                "Brief-Entwurf %s fehlgeschlagen, roher Modell-Output (gekuerzt): %s",
                draft_id,
                raw_text,
            )
        else:
            logger.exception("Brief-Entwurf %s fehlgeschlagen", draft_id)
        draft.status = LetterDraft.Status.FAILED
        draft.error = str(exc)
        draft.save(update_fields=["status", "error", "updated_at"])
        return draft

    # Rendern erst nach erfolgreicher Formulierung: Word/PDF eines leeren
    # Entwurfs waeren zwei Dateien, die niemand haben will.
    _render_quietly(draft)
    return draft


def _render_quietly(draft) -> None:
    """Rendert Word + PDF und macht ein Scheitern sichtbar, ohne den schon
    fertigen Text zu verlieren.

    Der Text ist der teure Teil (ein KI-Call); geht das Rendern schief,
    bleibt der Entwurf `ready` und bearbeitbar, und die Meldung sagt, was
    fehlt -- statt den Lauf komplett als gescheitert auszuweisen.
    """
    from .letter_render import render_draft_files

    try:
        render_draft_files(draft)
    except Exception as exc:
        logger.exception("Brief-Entwurf %s: Rendering fehlgeschlagen", draft.pk)
        draft.error = f"Text erzeugt, aber Word/PDF-Rendering fehlgeschlagen: {exc}"
        draft.save(update_fields=["error", "updated_at"])
