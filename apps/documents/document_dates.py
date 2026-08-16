"""Das maßgebliche Dokumentdatum aus den Datumsangaben der KI-Analyse

ableiten (#1141) -- die Entscheidung liegt bewusst hier im Code und nicht im
Prompt.

Ausgangslage war ein Stapel Kontoauszüge, die alle das Upload-Datum trugen:
im Fuß steht „Erstellt am: 15.08.2026" (der Tag, an dem jemand die PDF aus
dem Online-Banking gezogen hat), und genau das hat das Modell als
Dokumentdatum geliefert. Fachlich maßgeblich ist aber das Ende des
Abrechnungszeitraums („Kontoauszug vom 01.11.2023 bis 30.11.2023"). Ein
Archiv, in dem alle Auszüge auf denselben Tag datieren, verliert genau die
Chronologie, für die die Timeline (#1087) gebaut wurde.

Die Aufgabenteilung ist deshalb:

- **Das Modell liest, es entscheidet nicht.** Der Analyse-Prompt (#1020)
  fragt eine *Liste* aller gefundenen Datumsangaben mit ihrer jeweiligen Art
  ab (``dates``: ``belegdatum``/``zeitraum_beginn``/``zeitraum_ende``/
  ``briefkopf``/``erstellt``), nicht ein einzelnes „das ist es".
- **Der Code wählt** nach der festen Rangfolge in ``_PRIORITY``. Damit ist die
  Auswahl testbar und übersteht einen Modellwechsel, statt bei jedem neuen
  Modell neu ausgewürfelt zu werden.

`erstellt` steht bewusst ganz unten: dieses Datum sagt, wann jemand die Datei
erzeugt hat, nicht, worauf sich das Dokument bezieht. `zeitraum_beginn` taucht
in der Rangfolge gar nicht auf -- ein Zeitraum datiert auf sein Ende, sein
Beginn ist nie das Dokumentdatum (er wird trotzdem als Key-Fact erfasst, siehe
`period_bounds`).

Die Plausibilitätsprüfung (`resolve_document_date`) gehört ebenfalls hierher
und nicht in den Prompt: sie braucht das Upload-Datum, das dem Modell gar
nicht vorliegt. Sie nimmt in Kauf, dass ein Beleg, der tatsächlich am
Upload-Tag datiert *und* zusätzlich einen Zeitraum ausweist (die am selben Tag
eingescannte Rechnung über den Vormonat), auf den Zeitraum umgebogen wird --
bei einem Archiv ist „datiert ausgerechnet heute" die seltene Ausnahme, ein
falsch datierter Altbeleg-Stapel der reale Fall. Das Ergebnis ist über
`ResolvedDocumentDate.upload_conflict` erkennbar, wird protokolliert und lässt
sich von Hand korrigieren -- eine Handkorrektur überlebt danach jede erneute
Analyse (siehe `analysis._apply_analysis`).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Iterable, Optional

from django.conf import settings


class DateKind:
    """Die Arten von Datumsangaben, die die Analyse unterscheidet.

    Kein `TextChoices`: das sind keine Model-Feldwerte, sondern Schlüssel
    zwischen Prompt, Ableitung und `metadata["document_date_source"]`.
    """

    BELEG = "belegdatum"
    ZEITRAUM_BEGINN = "zeitraum_beginn"
    ZEITRAUM_ENDE = "zeitraum_ende"
    BRIEFKOPF = "briefkopf"
    MODELL = "modell"
    ERSTELLT = "erstellt"


# Von Hand gesetzt (`document_meta`) -- kein KI-Kind, sondern die
# Gegenmarkierung: sie schützt das Datum vor jeder erneuten Analyse.
MANUAL_SOURCE = "manuell"

# Die Rangfolge aus #1141. `ZEITRAUM_BEGINN` fehlt hier absichtlich (siehe
# Modul-Docstring); `MODELL` ist die Selbsteinschätzung des Modells aus
# `key_facts.document_date` -- besser als das reine Druckdatum, aber
# schwächer als jede ausdrücklich benannte Quelle.
_PRIORITY = (
    DateKind.BELEG,
    DateKind.ZEITRAUM_ENDE,
    DateKind.BRIEFKOPF,
    DateKind.MODELL,
    DateKind.ERSTELLT,
)

# Alle Arten, die die KI-Analyse selbst vergeben kann -- die Menge, an der
# `_apply_analysis` erkennt, dass ein gespeichertes Datum aus einem früheren
# Analyse-Lauf stammt und daher neu bestimmt werden darf.
AI_SOURCES = frozenset((*_PRIORITY, DateKind.ZEITRAUM_BEGINN))

SOURCE_LABELS = {
    DateKind.BELEG: "Belegdatum im Dokument",
    DateKind.ZEITRAUM_ENDE: "Ende des Abrechnungszeitraums",
    DateKind.BRIEFKOPF: "Datum im Briefkopf",
    DateKind.MODELL: "KI-Einschätzung ohne benannte Quelle",
    DateKind.ERSTELLT: "Erstell-/Druckdatum (unsichere Quelle)",
    MANUAL_SOURCE: "Von Hand gesetzt",
}

# Großzügige Synonymliste: das Modell hält sich meist, aber nicht immer an die
# im Prompt genannten Schlüssel. Was hier nicht steht, wird verworfen statt
# geraten -- eine Datumsangabe unbekannter Art ist als Anker wertlos
# („Ihr Schreiben vom …", ein Zahlungsziel im Fließtext), und
# `key_facts.document_date` bleibt als Auffangnetz ohnehin erhalten.
_KIND_ALIASES = {
    "belegdatum": DateKind.BELEG,
    "beleg": DateKind.BELEG,
    "rechnungsdatum": DateKind.BELEG,
    "rechnung": DateKind.BELEG,
    "bescheiddatum": DateKind.BELEG,
    "bescheid": DateKind.BELEG,
    "dokumentdatum": DateKind.BELEG,
    "vertragsdatum": DateKind.BELEG,
    "quittungsdatum": DateKind.BELEG,
    "zeitraum_beginn": DateKind.ZEITRAUM_BEGINN,
    "zeitraumbeginn": DateKind.ZEITRAUM_BEGINN,
    "zeitraum_von": DateKind.ZEITRAUM_BEGINN,
    "periode_beginn": DateKind.ZEITRAUM_BEGINN,
    "leistungszeitraum_beginn": DateKind.ZEITRAUM_BEGINN,
    "abrechnungszeitraum_beginn": DateKind.ZEITRAUM_BEGINN,
    "zeitraum_ende": DateKind.ZEITRAUM_ENDE,
    "zeitraumende": DateKind.ZEITRAUM_ENDE,
    "zeitraum_bis": DateKind.ZEITRAUM_ENDE,
    "periode_ende": DateKind.ZEITRAUM_ENDE,
    "leistungszeitraum_ende": DateKind.ZEITRAUM_ENDE,
    "abrechnungszeitraum_ende": DateKind.ZEITRAUM_ENDE,
    "briefkopf": DateKind.BRIEFKOPF,
    "briefkopfdatum": DateKind.BRIEFKOPF,
    "briefdatum": DateKind.BRIEFKOPF,
    "schreiben_vom": DateKind.BRIEFKOPF,
    "erstellt": DateKind.ERSTELLT,
    "erstellt_am": DateKind.ERSTELLT,
    "erstelldatum": DateKind.ERSTELLT,
    "druckdatum": DateKind.ERSTELLT,
    "gedruckt": DateKind.ERSTELLT,
    "gedruckt_am": DateKind.ERSTELLT,
    "ausgedruckt": DateKind.ERSTELLT,
    "ausgedruckt_am": DateKind.ERSTELLT,
    "abrufdatum": DateKind.ERSTELLT,
    "abgerufen_am": DateKind.ERSTELLT,
    "stand": DateKind.ERSTELLT,
}


@dataclass(frozen=True)
class DateCandidate:
    """Eine vom Modell gelesene Datumsangabe samt ihrer Art."""

    kind: str
    date: datetime.date


@dataclass(frozen=True)
class ResolvedDocumentDate:
    """Das Ergebnis der Ableitung: Datum, Herkunft und -- falls die
    Plausibilitätsprüfung eingegriffen hat -- die verworfene Wahl.
    """

    date: Optional[datetime.date] = None
    kind: str = ""
    upload_conflict: bool = False
    rejected: Optional[DateCandidate] = None


def parse_date(value: object) -> Optional[datetime.date]:
    """Eine Datumsangabe aus einer Modellantwort in ein `date` überführen.

    Eine LLM-Antwort ist freier Text, kein validiertes Feld: ein fehlender
    oder unparsbarer Wert (leerer String, Prosa, falsches Format) muss zu
    `None` werden, statt zu werfen und den Rest der Analyse mitzureißen.
    Neben dem im Prompt verlangten ISO-Format wird auch das deutsche
    `TT.MM.JJJJ` akzeptiert -- genau die Schreibweise, die in den betroffenen
    Dokumenten steht und die ein Modell gerne unverändert durchreicht.
    """
    text = str(value or "").strip()
    if not text:
        return None
    for parser in (
        datetime.date.fromisoformat,
        lambda raw: datetime.datetime.strptime(raw, "%d.%m.%Y").date(),
    ):
        try:
            return parser(text)
        except ValueError:
            continue
    return None


def normalize_kind(value: object) -> str:
    """Die Art einer Datumsangabe auf einen `DateKind` normalisieren; ein
    unbekannter Wert wird zu `""` und die Angabe damit verworfen.
    """
    text = str(value or "").strip().lower()
    normalized = "".join(char if char.isalnum() else "_" for char in text)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return _KIND_ALIASES.get(normalized.strip("_"), "")


def candidates_from_reply(items: object, *, model_date: object = None) -> list[DateCandidate]:
    """Die `dates`-Liste einer Analyse-Antwort in Kandidaten übersetzen.

    `model_date` ist `key_facts.document_date`, also die Selbsteinschätzung
    des Modells: sie kommt als `MODELL`-Kandidat dazu, damit eine Antwort
    ohne (oder mit unbrauchbarer) `dates`-Liste weiterhin ein Datum liefert
    -- Kompatibilität zum Verhalten vor #1141.
    """
    candidates: list[DateCandidate] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = normalize_kind(item.get("kind") or item.get("type"))
        date = parse_date(item.get("value") or item.get("date"))
        if not kind or date is None:
            continue
        candidates.append(DateCandidate(kind=kind, date=date))

    parsed_model_date = parse_date(model_date)
    if parsed_model_date is not None:
        candidates.append(DateCandidate(kind=DateKind.MODELL, date=parsed_model_date))
    return candidates


def period_bounds(
    candidates: Iterable[DateCandidate],
) -> tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Beginn und Ende des Abrechnungs-/Leistungszeitraums.

    Nennt ein Dokument mehrere Zeitraumgrenzen (mehrseitige Abrechnung, je
    Seite eine Kopfzeile), spannt der äußerste Rahmen den Zeitraum auf:
    frühester Beginn, spätestes Ende.
    """
    candidates = list(candidates)
    starts = [c.date for c in candidates if c.kind == DateKind.ZEITRAUM_BEGINN]
    ends = [c.date for c in candidates if c.kind == DateKind.ZEITRAUM_ENDE]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _ranked(candidates: Iterable[DateCandidate]) -> list[DateCandidate]:
    """Je Art höchstens einen Kandidaten, sortiert nach `_PRIORITY`.

    Bei mehreren Angaben derselben Art gewinnt beim Zeitraum-Ende das
    späteste und beim Zeitraum-Beginn das früheste Datum (derselbe äußere
    Rahmen wie in `period_bounds`), sonst die erste Nennung -- deterministisch
    statt „was zufällig zuerst im JSON stand".
    """
    by_kind: dict[str, DateCandidate] = {}
    for candidate in candidates:
        if candidate.kind not in _PRIORITY:
            continue
        current = by_kind.get(candidate.kind)
        if current is None:
            by_kind[candidate.kind] = candidate
        elif candidate.kind == DateKind.ZEITRAUM_ENDE and candidate.date > current.date:
            by_kind[candidate.kind] = candidate
    return [by_kind[kind] for kind in _PRIORITY if kind in by_kind]


def _is_near(date: datetime.date, reference: datetime.date, tolerance_days: int) -> bool:
    return abs((date - reference).days) <= tolerance_days


def resolve_document_date(
    candidates: Iterable[DateCandidate],
    *,
    uploaded_on: Optional[datetime.date] = None,
    tolerance_days: Optional[int] = None,
) -> ResolvedDocumentDate:
    """Das maßgebliche Dokumentdatum nach der Rangfolge aus #1141 wählen.

    Zwei Schritte, beide im Code und nicht im Prompt:

    1. Rangfolge: Belegdatum vor Zeitraum-Ende vor Briefkopf vor der
       Selbsteinschätzung des Modells vor dem Erstell-/Druckdatum.
    2. Plausibilitätsprüfung gegen den Upload-Tag: fällt die Wahl auf ein
       Datum am (oder dicht am) Tag des Hochladens, obwohl eine andere Quelle
       ein davon abweichendes Datum nennt, gewinnt die andere Quelle. Das
       fängt genau den Fall ab, in dem das Modell das Abrufdatum eines
       Kontoauszugs für dessen Belegdatum hält -- die Rangfolge allein hilft
       da nicht, weil die Fehlwahl bereits ganz oben steht.
    """
    if tolerance_days is None:
        tolerance_days = settings.FINDUS_DOCUMENT_DATE_UPLOAD_TOLERANCE_DAYS

    ranked = _ranked(candidates)
    if not ranked:
        return ResolvedDocumentDate()

    best = ranked[0]
    if uploaded_on is not None and _is_near(best.date, uploaded_on, tolerance_days):
        for alternative in ranked[1:]:
            if not _is_near(alternative.date, uploaded_on, tolerance_days):
                return ResolvedDocumentDate(
                    date=alternative.date,
                    kind=alternative.kind,
                    upload_conflict=True,
                    rejected=best,
                )
    return ResolvedDocumentDate(date=best.date, kind=best.kind)
