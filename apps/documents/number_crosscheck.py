"""Zahlen-Gegenprobe fuer die KI-Vision-Markdown-Extraktion (#1149).

Wo ein PDF einen brauchbaren Text-Layer besitzt, ist der die billigste
verfuegbare Kontrolle fuer die vom Vision-Modell transkribierten Zahlen
(Betraege, Datumsangaben, Kontonummern): kostet keinen zusaetzlichen
Modellaufruf und faengt genau den teuren Fehler -- ein falsch gelesener
Wert in einer ansonsten sauber erkannten Tabelle.

Der Abgleich normalisiert grosszuegig (Tausendertrennzeichen, Komma/Punkt,
Waehrungszeichen, Leerzeichen werden verworfen, nur die Ziffernfolge
zaehlt), damit unterschiedliche Schreibweisen desselben Werts nicht als
Abweichung gemeldet werden -- eine buchstabengetreue Pruefung waere mehr
Fehlalarm als Nutzen. Zahlen mit weniger als drei Ziffern (Seiten-
nummern, Aufzaehlungspunkte, einzelne Mengenangaben) werden ignoriert,
weil sie in praktisch jedem Fliesstext ohne inhaltlichen Bezug auftauchen.
"""

from __future__ import annotations

import re

_NUMBER_TOKEN_RE = re.compile(r"\d[\d.,/]*\d")
_NON_DIGIT_RE = re.compile(r"\D")
_MIN_SIGNIFICANT_DIGITS = 3


def _normalize(token: str) -> str:
    return _NON_DIGIT_RE.sub("", token)


def _significant_numbers(text: str) -> set[str]:
    numbers = set()
    for token in _NUMBER_TOKEN_RE.findall(text):
        normalized = _normalize(token)
        if len(normalized) >= _MIN_SIGNIFICANT_DIGITS:
            numbers.add(normalized)
    return numbers


def find_unmatched_numbers(candidate_text: str, reference_text: str) -> list[str]:
    """Zahlen aus `candidate_text` (die Vision-Markdown-Transkription), die
    normalisiert in keiner Zahl aus `reference_text` (der PDF-Text-Layer)
    vorkommen. Reihenfolge und Schreibweise wie im Original, dedupliziert
    -- fuer eine lesbare Anzeige ("Prüfhinweis: 2 Zahlen ohne Entsprechung
    im Text-Layer"), nicht fuer einen maschinellen Weitervergleich.
    """
    reference_numbers = _significant_numbers(reference_text)
    seen: set[str] = set()
    unmatched: list[str] = []
    for token in _NUMBER_TOKEN_RE.findall(candidate_text):
        normalized = _normalize(token)
        if len(normalized) < _MIN_SIGNIFICANT_DIGITS:
            continue
        if normalized in reference_numbers or normalized in seen:
            continue
        seen.add(normalized)
        unmatched.append(token)
    return unmatched
