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

Kennungen/Referenznummern (#1099): derselbe Call gibt zusätzlich eine
*Liste* typisierter Kennungen aus (Aktenzeichen, Forderungs-/Kunden-/
Belegnummer, IBAN, "Ihr/Unser Zeichen" ...), die als `DocumentReference`
gespeichert werden -- bewusst eine Liste und kein weiteres Key-Fact-Feld:
ein Schreiben trägt regelmäßig mehrere davon gleichzeitig. Kein
zusätzlicher `generate()`-Call, nur ein weiterer Schlüssel in derselben
Antwort. Ein erneuter Lauf ersetzt nur die KI-extrahierten Zeilen; von
Hand nachgetragene/korrigierte bleiben stehen (siehe
`apps.documents.references`).

Dokumentdatum (#1141): derselbe Call liefert zusaetzlich eine *Liste*
typisierter Datumsangaben (`dates`); welche davon das `document_date` wird,
entscheidet nicht das Modell, sondern `apps.documents.document_dates` nach
einer festen Rangfolge -- Belegdatum vor Zeitraum-Ende vor Briefkopf vor
Erstell-/Druckdatum -- plus einer Plausibilitaetspruefung gegen den
Upload-Tag. Die Herkunft steht danach in
`metadata["document_date_source"]`, damit erkennbar bleibt, wie belastbar
das Datum ist; ein von Hand gesetztes Datum (`"manuell"`) ueberlebt jede
erneute Analyse.

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

import datetime
import logging
from typing import Optional

from django.conf import settings
from django.utils import timezone

from apps.ai.providers import GenerationProvider, Message, generate_json, get_generation_provider

from .document_dates import (
    AI_SOURCES,
    candidates_from_reply,
    period_bounds,
    resolve_document_date,
)
from .models import (
    Correspondent,
    Document,
    DocumentReference,
    SuggestionStatus,
    Tag,
    TagSuggestion,
    Vorgang,
    VorgangSuggestion,
    normalize_reference_value,
)
from .reference_matching import (
    auto_assign_from_references,
    learn_references_from_document,
)
from .references import normalize_role, normalize_type
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

# Die Zeitraum-Key-Facts (#1141) `period_start`/`period_end` stehen bewusst
# NICHT in dieser Liste: sie werden nicht als eigene Antwortfelder abgefragt,
# sondern aus der typisierten `dates`-Liste abgeleitet, aus der auch das
# Dokumentdatum stammt (siehe `_apply_analysis`). Zwei Wege zum selben Wert
# waeren zwei Wege, die auseinanderlaufen koennen.

# Die Kennungsarten für den Prompt kommen aus den Model-Choices, nicht aus
# einer zweiten, handgepflegten Liste (#1099): eine neue Kennungsart soll
# an genau einer Stelle ergänzt werden. `SONSTIGE` steht im Prompt separat
# als Auffangwert und fehlt deshalb hier.
_REFERENCE_TYPE_VALUES = [
    value
    for value in DocumentReference.Type.values
    if value != DocumentReference.Type.SONSTIGE
]

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
    '- "dates": Liste ALLER im Dokument vorkommenden Datumsangaben, je '
    'Eintrag "kind" und "value" (YYYY-MM-DD). "kind" ist einer von:\n'
    '  * "belegdatum" -- das ausgewiesene Datum des Dokuments selbst '
    "(Rechnungs-, Beleg-, Bescheid-, Vertragsdatum).\n"
    '  * "zeitraum_beginn" / "zeitraum_ende" -- Beginn und Ende eines '
    "Abrechnungs- oder Leistungszeitraums, z. B. \"Kontoauszug vom "
    '01.11.2023 bis 30.11.2023", Verbrauchs- oder Lohnabrechnung, '
    "Rechnungsabschluss. Beide Grenzen angeben, wenn beide dastehen.\n"
    '  * "briefkopf" -- das Datum im Briefkopf eines Anschreibens '
    '("Landshut, den 4. Maerz 2026").\n'
    '  * "erstellt" -- Erstell-, Druck- oder Abrufdatum ("Erstellt am", '
    '"Gedruckt am", "Ausgedruckt am", "Stand:").\n'
    "  Gib jede gefundene Angabe mit ihrer Art zurueck, auch wenn mehrere "
    "gleichzeitig vorkommen, und entscheide NICHT selbst, welche davon das "
    "maßgebliche Dokumentdatum ist -- diese Auswahl trifft das System. "
    "Zahlungsziele/Faelligkeiten gehoeren nicht in diese Liste (dafuer gibt "
    'es "due_date"), ebensowenig Datumsangaben, die zu einem anderen '
    'Schriftstueck gehoeren ("Ihr Schreiben vom ..."). Findest du keine '
    "Datumsangabe, gib eine leere Liste zurueck.\n"
    '- "direction": IMMER angeben, einer von "eingang", "ausgang", '
    '"intern", "unbekannt". Vergleiche Aussteller (sender_name/-ids) und '
    'Empfaenger (recipient_name/-ids) mit den unten gelisteten '
    '"Bestehende Kontakte" und deren [ICH SELBST]-Markierung: ist der '
    'Aussteller [ICH SELBST], ist es "ausgang" (ich verschicke); ist der '
    'Empfaenger [ICH SELBST], ist es "eingang" (ich empfange); sind beide '
    '[ICH SELBST], ist es "intern". Ist keine der beiden Parteien als '
    '[ICH SELBST] gelistet oder eindeutig zuzuordnen, antworte "unbekannt" '
    "-- rate nicht.\n"
    '- "sphere": IMMER angeben, einer von "geschaeftlich", "privat", '
    '"unbekannt". Klassifiziere das Dokument fachlich anhand der eigenen '
    "Seite (der unten als [ICH SELBST] gelisteten Partei, die als "
    "Aussteller oder Empfaenger beteiligt ist): ist diese eigene Identitaet "
    'als [MEINE FIRMA] markiert oder traegt sie eine USt-IdNr, ist es '
    '"geschaeftlich"; ist die beteiligte eigene Identitaet eine reine '
    'Privatperson (kein [MEINE FIRMA], keine USt-IdNr), ist es "privat". '
    "Auch USt-IdNr/Steuernummer der eigenen Seite im Dokumenttext sind ein "
    'Geschaeftlich-Signal. Laesst sich keine eigene Seite zuordnen oder ist '
    'es unklar, antworte "unbekannt" -- rate nicht.\n'
    '- "tax_relevance": IMMER angeben, einer von "ja", "nein", "vielleicht", '
    '"nicht_zutreffend". Bewerte AUSSCHLIESSLICH, ob dieser Beleg als '
    "PRIVATPERSON in der Einkommensteuererklaerung absetzbar ist -- also "
    "Werbungskosten, haushaltsnahe Dienstleistungen/Handwerkerleistungen "
    "(§35a EStG), Spenden, Versicherungs-/Vorsorgebeitraege, "
    "aussergewoehnliche Belastungen, Kinderbetreuung u. ae. Klar privat "
    'absetzbar -> "ja"; klar nicht absetzbar (Werbung, reiner Kontoauszug, '
    'Kassenbon ohne Bezug) -> "nein"; unsicher -> "vielleicht" (ehrlicher '
    "Hedge, lieber das als eine Fehleinschaetzung). WICHTIG: Es geht rein um "
    "die PRIVATE Absetzbarkeit. Ein geschaeftlicher/betrieblicher Beleg ist "
    'NICHT "ja", nur weil er als Betriebsausgabe absetzbar waere -- das ist '
    "ein anderes Thema. Ist das Dokument geschaeftlich (sphere = "
    '"geschaeftlich", eigene Seite [MEINE FIRMA]/mit USt-IdNr), antworte '
    '"nicht_zutreffend".\n'
    '- "tax_relevance_reason": kurze Begruendung in einem Satz zu '
    '"tax_relevance" (z. B. "Handwerkerrechnung mit ausgewiesenem Lohnanteil '
    '-> §35a EStG" oder "Reine Werbung, kein absetzbarer Aufwand"), sonst "".\n'
    '- "references": Liste ALLER im Dokument genannten Kennungen/'
    'Referenznummern, je Eintrag "type", "value", "role". Ein Dokument '
    "kann mehrere tragen (z. B. Aktenzeichen UND Forderungsnummer) -- "
    'dann mehrere Eintraege. "type" ist einer von: ' + ", ".join(
        f'"{value}"' for value in _REFERENCE_TYPE_VALUES
    ) + " -- passt keiner, "
    '"sonstige". "value" ist die Nummer/das Zeichen genau so, wie es im '
    "Dokument steht (ohne die Beschriftung davor, also \"123/45\" statt "
    '"Az. 123/45"). "role" ordnet zu, WEM die Kennung gehoert: "deins" = '
    'unsere/die des Empfaengers dieses Systems, "deren" = die der '
    "Gegenstelle. Bei einem Eingangsdokument ist \"Ihr Zeichen\" also "
    '"deins" und "Unser Zeichen" "deren", bei einem Ausgangsdokument '
    'umgekehrt. Unklar? Dann "" (leer) -- nicht raten. Erfinde keine '
    "Nummern: nur uebernehmen, was ausdruecklich als Kennung/Nummer/"
    "Zeichen ausgewiesen ist, keine Betraege, Datumsangaben, Telefon-/"
    "Steuernummern oder beliebige Ziffernfolgen aus dem Fliesstext.\n"
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
        # [MEINE FIRMA] verschärft [ICH SELBST] zur Gewerbe-/Firmen-Identität
        # (#1112) -- das primäre Signal, aus dem das Modell die Sphäre
        # (geschäftlich vs. privat) des Dokuments ableitet.
        if correspondent.is_self:
            marker = (
                " [ICH SELBST] [MEINE FIRMA]"
                if correspondent.is_own_business
                else " [ICH SELBST]"
            )
        else:
            marker = ""
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


def _uploaded_on(document: Document) -> Optional[datetime.date]:
    """Der Tag des Hochladens als lokales Datum -- Bezugspunkt der
    Plausibilitaetspruefung (#1141). Immer `localtime`, nie UTC: sonst faellt
    ein spaeter Abend-Upload je nach Serverzeitzone auf den Folgetag und die
    Pruefung greift nicht mehr.
    """
    if document.created_at is None:
        return None
    return timezone.localtime(document.created_at).date()


def _may_set_document_date(document: Document, metadata: dict) -> bool:
    """Darf die Analyse `document_date` (neu) setzen? (#1141)

    Ja, solange kein Datum gespeichert ist oder das gespeicherte aus einem
    frueheren Analyse-Lauf stammt (`document_date_source` ist eine
    KI-Herkunft). Nein bei `"manuell"` -- die Handkorrektur ist die
    Nutzerentscheidung, die kein Wartungslauf einkassieren darf -- und nein
    bei einem Datum ohne Herkunftsvermerk: das ist entweder Bestand von vor
    #1141 (die Migration stempelt dort, was nachweislich die KI gesetzt hat)
    oder stammt aus dem Ingest (E-Mail-`Date`-Header), und beides ist
    verlaesslicher als ein neuer Modell-Raterunde.
    """
    if document.document_date is None:
        return True
    return metadata.get("document_date_source") in AI_SOURCES


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


def _replace_references(document: Document, items: list) -> None:
    """Die KI-extrahierten Kennungen (#1099) dieses Dokuments neu setzen.

    Ersetzt ausschliesslich die eigenen (`Source.AI`) Zeilen: von Hand
    nachgetragene oder korrigierte Kennungen sind eine Nutzerentscheidung
    und ueberleben jeden Re-Run -- dasselbe Prinzip wie bei den bereits
    entschiedenen `TagSuggestion`s oben. Eine KI-Kennung, die eine
    manuelle Zeile dupliziert, faellt weg (die manuelle gewinnt, und die
    UniqueConstraint wuerde sie ohnehin abweisen).
    """
    manual_keys = set(
        document.references.filter(source=DocumentReference.Source.MANUAL).values_list(
            "type", "value_normalized"
        )
    )
    document.references.filter(source=DocumentReference.Source.AI).delete()

    max_length = DocumentReference._meta.get_field("value_raw").max_length
    seen = set(manual_keys)
    references = []
    for item in items:
        if not isinstance(item, dict):
            continue
        reference_type = normalize_type(item.get("type"))
        value_raw = str(item.get("value") or "").strip()[:max_length]
        value_normalized = normalize_reference_value(value_raw)
        if not value_normalized:
            continue
        key = (reference_type, value_normalized)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            DocumentReference(
                document=document,
                type=reference_type,
                value_raw=value_raw,
                value_normalized=value_normalized,
                role=normalize_role(item.get("role")),
                source=DocumentReference.Source.AI,
            )
        )
        if len(references) >= settings.FINDUS_ANALYSIS_MAX_REFERENCES:
            break
    DocumentReference.objects.bulk_create(references)


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


def _derive_sphere(
    sender: Optional[Correspondent], recipient: Optional[Correspondent]
) -> str:
    """Geschäftlich/privat aus den beteiligten Self-Identitäten (#1112) --
    das datenbankgestützte Gegenstück zu `_derive_direction`.

    Betrachtet nur die eigene(n) Seite(n) des Dokuments (`is_self`): trägt
    eine davon die Gewerbe-Markierung (`is_own_business`) oder eine USt-IdNr,
    ist das Dokument `geschaeftlich`; ist die eigene Seite eine reine
    Privatperson, `privat`. Ist keine der beiden Parteien `is_self` (die
    eigene Identität ist z. B. gar nicht als Kontakt hinterlegt), bleibt es
    `unbekannt` -- dann entscheidet der Modell-Vorschlag (`_resolve_sphere`).
    """
    self_sides = [c for c in (sender, recipient) if c and c.is_self]
    if not self_sides:
        return Document.Sphere.UNBEKANNT
    if any(c.is_own_business or c.vat_id.strip() for c in self_sides):
        return Document.Sphere.GESCHAEFTLICH
    return Document.Sphere.PRIVAT


def _normalize_sphere(value: object) -> str:
    text = str(value or "").strip().lower()
    valid_values = {choice.value for choice in Document.Sphere}
    return text if text in valid_values else Document.Sphere.UNBEKANNT


def _resolve_sphere(
    parsed_sphere: object,
    sender: Optional[Correspondent],
    recipient: Optional[Correspondent],
) -> str:
    """Sphäre (#1112): der DB-Abgleich über die `is_self`-Kontakte gewinnt,
    wann immer er greift -- er kennt `is_own_business`/USt-IdNr verlässlich.
    Nur wenn keine eigene Seite gematcht wurde (z. B. die eigene Identität
    steht im Dokument leicht anders als der gespeicherte `Correspondent`),
    fällt es auf den Vorschlag des Modells zurück, das dieselbe [MEINE
    FIRMA]-Markierung im Prompt-Kontext gesehen hat. Spiegelbildlich zu
    `_resolve_direction`.
    """
    derived = _derive_sphere(sender, recipient)
    if derived != Document.Sphere.UNBEKANNT:
        return derived
    return _normalize_sphere(parsed_sphere)


def _normalize_tax_relevance(value: object) -> str:
    text = str(value or "").strip().lower()
    valid_values = {choice.value for choice in Document.TaxRelevance}
    return text if text in valid_values else Document.TaxRelevance.UNBEKANNT


def _resolve_tax_relevance(parsed_value: object, effective_sphere: str) -> str:
    """Private ESt-Absetzbarkeit (#1113): geschaeftliche Belege sind hier
    IMMER `nicht_zutreffend` -- die betriebliche Absetzbarkeit ist ein
    anderes, spaeteres Merkmal, und der haeufigste Fehler waere, einen
    Gewerbe-Beleg faelschlich als privat "ja" zu markieren. Die (ggf. gerade
    abgeleitete) Sphaere entscheidet also, *ob* das Feld ueberhaupt greift:
    ist sie `geschaeftlich`, ueberschreibt das jeden Modell-Vorschlag; sonst
    zaehlt die private Einschaetzung des Modells (ja/nein/vielleicht).
    """
    if effective_sphere == Document.Sphere.GESCHAEFTLICH:
        return Document.TaxRelevance.NICHT_ZUTREFFEND
    return _normalize_tax_relevance(parsed_value)


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

    metadata = dict(document.metadata)
    metadata.pop("analysis_error", None)
    document.metadata = metadata

    update_fields = ["metadata", "summary", "key_facts", "updated_at"]
    document.summary = summary
    if title:
        document.title = title
        update_fields.append("title")

    # Dokumentdatum (#1141): erst die typisierten Datumsangaben einsammeln,
    # dann daraus sowohl den Zeitraum als auch das maßgebliche Datum
    # ableiten -- eine Quelle, zwei Ergebnisse.
    date_candidates = candidates_from_reply(
        parsed.get("dates"), model_date=key_facts_in.get("document_date")
    )
    period_start, period_end = period_bounds(date_candidates)
    if period_start is not None:
        key_facts["period_start"] = period_start.isoformat()
    if period_end is not None:
        key_facts["period_end"] = period_end.isoformat()
    resolved_date = resolve_document_date(
        date_candidates, uploaded_on=_uploaded_on(document)
    )

    # Dokumentdatum (#1085, geschaerft in #1141): frueher galt "nur
    # befuellen, solange leer" -- das schuetzte die Handkorrektur, machte
    # aber auch jedes von einem frueheren Lauf falsch gesetzte Datum
    # unkorrigierbar, obwohl genau das der Anlass von #1141 war (40
    # Kontoauszuege, alle auf den Upload-Tag datiert). Massgeblich ist
    # deshalb nicht mehr "ist das Feld leer?", sondern die Herkunft in
    # `metadata["document_date_source"]`: die Analyse ueberschreibt nur, was
    # sie selbst gesetzt hat. Ein von Hand gesetztes ("manuell") sowie ein
    # aus anderer Quelle stammendes Datum (E-Mail-Header beim EML-Ingest,
    # Bestand ohne Herkunftsvermerk) bleibt unangetastet.
    if _may_set_document_date(document, metadata) and resolved_date.date is not None:
        document.document_date = resolved_date.date
        metadata["document_date_source"] = resolved_date.kind
        update_fields.append("document_date")
        # Der Eingriff der Plausibilitaetspruefung wird protokolliert
        # (#1141) -- damit sich beurteilen laesst, wie oft die Regel
        # greift -- und bleibt am Dokument sichtbar, damit ein umgebogenes
        # Datum im Detail nachvollziehbar ist.
        if resolved_date.upload_conflict:
            rejected = resolved_date.rejected
            logger.info(
                "Dokumentdatum-Plausibilitaetspruefung griff fuer Document %s: "
                "%s (%s) lag am Upload-Tag, stattdessen %s (%s)",
                document.pk,
                rejected.date if rejected else "?",
                rejected.kind if rejected else "?",
                resolved_date.date,
                resolved_date.kind,
            )
            metadata["document_date_upload_conflict"] = True
        else:
            metadata.pop("document_date_upload_conflict", None)

    # `key_facts["document_date"]` traegt das *geltende* Datum des Dokuments,
    # nicht die Rohantwort des Modells: alles, was Key-Facts weiterverwendet
    # (Schreiben-Platzhalter, Empfehlungs- und Zusammenfassungs-Prompts),
    # soll dasselbe Datum sehen wie Timeline und Detail -- auch dann, wenn
    # das eine geschuetzte Handkorrektur ist und nicht die Wahl der Analyse.
    key_facts.pop("document_date", None)
    if document.document_date is not None:
        key_facts["document_date"] = document.document_date.isoformat()

    if key_facts:
        key_facts["ai_model"] = model
        key_facts["ai_model_version"] = version
    document.key_facts = key_facts

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

    # Sphäre (#1112): dasselbe "einmal befuellen, nie ungefragt
    # ueberschreiben"-Muster wie `direction`/`document_date` -- nur setzen,
    # solange sie noch `unbekannt` ist, damit eine erneute Analyse eine
    # bereits gesetzte (KI- oder von Hand gewaehlte) Sphaere nicht umwirft.
    # `metadata["sphere_source"]` haelt fest, dass der Wert ein noch nicht
    # bestaetigter KI-Vorschlag ist -- daran haengt das "KI"-Badge im Detail;
    # ein manuelles Speichern (document_meta) entfernt die Markierung.
    if document.sphere == Document.Sphere.UNBEKANNT:
        candidate_sphere = _resolve_sphere(
            parsed.get("sphere"), sender_match, recipient_match
        )
        if candidate_sphere != Document.Sphere.UNBEKANNT:
            document.sphere = candidate_sphere
            metadata["sphere_source"] = "ki"
            update_fields.append("sphere")

    # Private ESt-Absetzbarkeit (#1113): dasselbe "einmal befuellen, nie
    # ungefragt ueberschreiben"-Muster wie `sphere`/`direction` -- nur
    # setzen, solange sie noch `unbekannt` ist. `document.sphere` ist hier
    # bereits aktuell (oben ggf. gerade gesetzt), sodass ein geschaeftlicher
    # Beleg zuverlaessig `nicht_zutreffend` wird statt faelschlich "ja". Die
    # Begruendung wird mitgesetzt; fuer `nicht_zutreffend` bleibt sie leer
    # (die betriebliche Relevanz ist ein anderes Thema, keine private
    # Begruendung). `metadata["tax_relevance_source"] = "ki"` markiert den
    # noch unbestaetigten Vorschlag fuers Badge -- ein manuelles Speichern
    # (document_meta) entfernt die Markierung.
    if document.tax_relevance == Document.TaxRelevance.UNBEKANNT:
        candidate_tax = _resolve_tax_relevance(
            parsed.get("tax_relevance"), document.sphere
        )
        if candidate_tax != Document.TaxRelevance.UNBEKANNT:
            document.tax_relevance = candidate_tax
            document.tax_relevance_reason = (
                ""
                if candidate_tax == Document.TaxRelevance.NICHT_ZUTREFFEND
                else str(parsed.get("tax_relevance_reason") or "").strip()
            )
            metadata["tax_relevance_source"] = "ki"
            update_fields.append("tax_relevance")
            update_fields.append("tax_relevance_reason")

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

    _replace_references(document, parsed.get("references") or [])
    _replace_tag_suggestions(document, parsed.get("tag_suggestions") or [])
    _replace_vorgang_suggestions(document, parsed.get("vorgang_suggestions") or [])

    # Kennungen an ihr Zuhause (#1100), gleich nachdem sie feststehen: ist
    # das Dokument schon zugeordnet (Upload auf den Hub, Ordner-Import),
    # lernt der Vorgang/Kontakt hier seine Nummern -- ist es das nicht,
    # entscheidet der Abgleich gegen den bestehenden Kennungs-Bestand, ob
    # es einen Zuordnungs-Vorschlag gibt. `document.owner` ist der
    # Sichtbarkeits-Scope dafuer: der Worker hat keinen Request, aus dem er
    # einen Nutzer ableiten koennte, und der Besitzer ist der einzige, der
    # dieses Dokument sicher sehen darf.
    learn_references_from_document(document)
    auto_assign_from_references(document, document.owner)


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


def analyze_and_finalize(document_id: int) -> Document:
    """Run `analyze_document()` and -- unlike the ingest pipeline, which

    hands `processing_status` off to `process_document()` right
    afterwards (#1010) -- also move it to a terminal state: this is the
    last pipeline stage for a standalone re-analysis (management command
    or UI "Analyse erneut ausfuehren" button), so nothing else will ever
    get the document out of `analyzing` otherwise (#1029, #1035, #1063).
    """
    document = analyze_document(document_id)
    if "analysis_error" in document.metadata:
        document.processing_status = Document.ProcessingStatus.FAILED
    else:
        document.processing_status = Document.ProcessingStatus.READY
    document.save(update_fields=["processing_status", "updated_at"])
    return document
