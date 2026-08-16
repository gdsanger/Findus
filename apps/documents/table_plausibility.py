"""Rein rechnerische Plausibilitaetspruefungen fuer die tabellarischen
Bereiche der KI-Vision-Markdown-Neuextraktion (#1152) -- Ergaenzung zur
reinen Vorkommens-Gegenprobe in `number_crosscheck.py`. Beide Pruefungen
laufen im Code, nicht im Prompt (CLAUDE.md, "Manuelle KI-Vision-
Neuextraktion"): ein Modell, das seine eigene Ausgabe validieren soll,
uebersieht denselben Fehler ein zweites Mal.

Zwei Fehlerbilder, zwei Pruefungen:

- Einheiten-Plausibilitaet (`check_unit_plausibility`): eine Tabellenzeile,
  deren Einheit vom Rest der Tabelle abweicht (z. B. `mg/dl` inmitten einer
  `U/l`-Tabelle), ist ein starkes Indiz fuer eine eingeschobene oder
  verrutschte Zeile -- genau das Muster aus #1152 (eine Fortsetzungszeile
  der Vorseite, in eine neue Tabelle gezogen).
- Zeilenweiser Text-Layer-Abgleich (`find_row_shifts`): ein reiner
  Vorkommens-Abgleich (siehe `number_crosscheck.find_unmatched_numbers`)
  uebersieht einen Zeilenversatz, weil dabei alle Zahlen vorhanden sind --
  nur an der falschen Stelle. Diese Pruefung verlangt zusaetzlich
  raeumliche Naehe: liegen die signifikanten Zahlen derselben Tabellenzeile
  im Text-Layer innerhalb eines kleinen Zeichenfensters beieinander? Eine
  exakte Layout-Rekonstruktion ist dafuer nicht noetig.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,/]*\d")
_NON_DIGIT_RE = re.compile(r"\D")
_MIN_SIGNIFICANT_DIGITS = 3
# Zweistellig genuegt fuer den Zeilen-Abgleich (anders als der dreistellige
# Schwellwert oben, der Seitenzahlen/Aufzaehlungspunkte im *gesamten*
# Dokument ausfiltert): ein Laborwert wie "50" ist innerhalb der schmalen
# Zeile, aus der er stammt, bereits ein aussagekraeftiges Signal.
_MIN_SIGNIFICANT_DIGITS_ROW = 2
_DEFAULT_PROXIMITY_WINDOW = 80
_SNIPPET_RADIUS = 25

_UNIT_HEADER_KEYWORDS = ("einheit", "unit")


@dataclass(frozen=True)
class MarkdownTable:
    header: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class UnitFlag:
    row: str
    unit: str
    expected_unit: str

    def as_dict(self) -> dict:
        return {"row": self.row, "unit": self.unit, "expected_unit": self.expected_unit}


@dataclass(frozen=True)
class RowShiftFlag:
    row: str
    numbers: list[str]
    reference_snippets: dict[str, str]

    def as_dict(self) -> dict:
        return {
            "row": self.row,
            "numbers": self.numbers,
            "reference_snippets": self.reference_snippets,
        }


def parse_markdown_tables(markdown: str) -> list[MarkdownTable]:
    """Extrahiert GFM-Pipe-Tabellen (Kopfzeile + `---`-Trennzeile + Zeilen)
    aus einem Markdown-Dokument. Bewusst einfach gehalten -- kein
    allgemeiner Markdown-Parser, sondern zugeschnitten auf das, was das
    Vision-Modell laut `_VISION_MARKDOWN_PROMPT` erzeugt (fuehrende und
    schliessende Pipe je Zeile). Eine leere Zelle bleibt als leerer String
    erhalten, damit ein fehlender Wert von einem tatsaechlich vorhandenen
    unterscheidbar bleibt -- kein Nachruecken der folgenden Zellen.
    """
    tables: list[MarkdownTable] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        header_match = _TABLE_ROW_RE.match(lines[index].strip())
        separator_match = (
            _TABLE_ROW_RE.match(lines[index + 1].strip()) if index + 1 < len(lines) else None
        )
        if header_match and separator_match and _is_separator_row(separator_match.group(1)):
            header = _split_row(header_match.group(1))
            row_index = index + 2
            rows: list[list[str]] = []
            while row_index < len(lines):
                row_match = _TABLE_ROW_RE.match(lines[row_index].strip())
                if not row_match:
                    break
                rows.append(_split_row(row_match.group(1)))
                row_index += 1
            tables.append(MarkdownTable(header=header, rows=rows))
            index = row_index
        else:
            index += 1
    return tables


def _is_separator_row(raw: str) -> bool:
    cells = [cell.strip() for cell in raw.split("|")]
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells if cell)


def _split_row(raw: str) -> list[str]:
    return [cell.strip() for cell in raw.split("|")]


def _row_label(row: list[str]) -> str:
    if row and row[0].strip():
        return row[0].strip()
    return " | ".join(cell for cell in row if cell.strip()) or "(leere Zeile)"


def _find_column_index(header: list[str], keywords: tuple[str, ...]) -> int | None:
    for index, name in enumerate(header):
        normalized = name.strip().lower()
        if any(keyword in normalized for keyword in keywords):
            return index
    return None


def check_unit_plausibility(tables: list[MarkdownTable]) -> list[UnitFlag]:
    """Flags rows whose unit deviates from the majority unit of their own
    table. Requires at least two non-empty units and a real majority (more
    than one row, but not every row) -- a table with no clear majority has
    nothing to compare a lone value against.
    """
    flags: list[UnitFlag] = []
    for table in tables:
        unit_index = _find_column_index(table.header, _UNIT_HEADER_KEYWORDS)
        if unit_index is None:
            continue
        units = [
            row[unit_index].strip()
            for row in table.rows
            if unit_index < len(row) and row[unit_index].strip()
        ]
        if len(units) < 2:
            continue
        counts: dict[str, int] = {}
        for unit in units:
            counts[unit] = counts.get(unit, 0) + 1
        majority_unit, majority_count = max(counts.items(), key=lambda item: item[1])
        if majority_count < 2 or majority_count == len(units):
            continue
        for row in table.rows:
            if unit_index >= len(row):
                continue
            unit = row[unit_index].strip()
            if unit and unit != majority_unit:
                flags.append(
                    UnitFlag(row=_row_label(row), unit=unit, expected_unit=majority_unit)
                )
    return flags


def _significant_numbers_in_row(row: list[str]) -> list[str]:
    row_text = " | ".join(row)
    return [
        token
        for token in _NUMBER_TOKEN_RE.findall(row_text)
        if len(_NON_DIGIT_RE.sub("", token)) >= _MIN_SIGNIFICANT_DIGITS_ROW
    ]


def _positions_in_reference(number: str, reference_text: str) -> list[int]:
    normalized_target = _NON_DIGIT_RE.sub("", number)
    return [
        match.start()
        for match in _NUMBER_TOKEN_RE.finditer(reference_text)
        if _NON_DIGIT_RE.sub("", match.group()) == normalized_target
    ]


def _has_close_combination(position_lists: list[list[int]], window: int) -> bool:
    for combo in itertools.product(*position_lists):
        if max(combo) - min(combo) <= window:
            return True
    return False


def _snippet(reference_text: str, position: int) -> str:
    start = max(0, position - _SNIPPET_RADIUS)
    end = min(len(reference_text), position + _SNIPPET_RADIUS)
    return reference_text[start:end].strip()


def find_row_shifts(
    tables: list[MarkdownTable],
    reference_text: str,
    *,
    window: int = _DEFAULT_PROXIMITY_WINDOW,
) -> list[RowShiftFlag]:
    """Zeilen, deren signifikante Zahlen zwar allesamt im Text-Layer
    vorkommen, dort aber nicht innerhalb von `window` Zeichen beieinander
    stehen -- der Fingerabdruck eines Zeilenversatzes (#1152). Eine Zeile,
    bei der schon eine einzelne Zahl im Text-Layer fehlt, wird hier
    uebersprungen: das ist der Fall von `number_crosscheck.
    find_unmatched_numbers`, nicht von einem Versatz.
    """
    flags: list[RowShiftFlag] = []
    for table in tables:
        for row in table.rows:
            numbers = _significant_numbers_in_row(row)
            if len(numbers) < 2:
                continue
            position_lists = [_positions_in_reference(number, reference_text) for number in numbers]
            if any(not positions for positions in position_lists):
                continue
            if _has_close_combination(position_lists, window):
                continue
            reference_snippets = {
                number: _snippet(reference_text, positions[0])
                for number, positions in zip(numbers, position_lists)
            }
            flags.append(
                RowShiftFlag(row=_row_label(row), numbers=numbers, reference_snippets=reference_snippets)
            )
    return flags
