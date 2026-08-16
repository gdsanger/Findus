"""Voraussetzungs-Prüfungen für Tests, die Systemwerkzeuge brauchen (#1145).

Ein Teil der Suite hängt an Programmen, die *nicht* per pip kommen, sondern
aus Systempaketen (siehe Dockerfile): `tesseract` für die OCR-Stufe der
Extraktions-Kaskade, `pdftoppm`/`pdfinfo` (poppler-utils) für das Rastern von
PDF-Seiten. Dazu kommt die System-MIME-Datenbank, aus der `mimetypes` seine
Zuordnungen liest.

Ein Test, der ohne so ein Werkzeug gar nicht laufen kann, ist in einer
Umgebung ohne dieses Werkzeug kein Fehlschlag — er ist **nicht anwendbar**.
Der Unterschied ist teuer: Eine rote Suite, deren Rot nichts mit der aktuellen
Änderung zu tun hat, zwingt zum zweiten vollständigen Lauf als Gegenbeweis,
und das bei jedem Ticket erneut. Übersprungen mit sichtbarem Grund trennt
„nicht anwendbar" von „kaputt", ohne Prüfumfang aufzugeben: Wo die Werkzeuge
da sind, laufen dieselben Tests unverändert.

Zwei Regeln für den Gebrauch:

- **Eng fassen.** Der Dekorator gehört an den Test, der das Werkzeug wirklich
  braucht — an eine Klasse nur, wenn das für jeden ihrer Tests gilt. Eine zu
  weit gefasste Bedingung lässt Prüfumfang unbemerkt verschwinden, und das ist
  schlimmer als der Fehlschlag, den sie vermeiden soll.
- **Grund nennen.** Jeder Überspring-Text nennt das fehlende Werkzeug beim
  Namen, damit im Protokoll steht, *warum* etwas nicht lief.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import unittest

from django.conf import settings

# Binaries hinter den Python-Bindings: pytesseract und pdf2image sind reine
# Wrapper, die auf diese Programme im PATH zeigen.
OCR_BINARY = "tesseract"
PDF_RASTER_BINARIES = ("pdftoppm", "pdfinfo")


def missing_binaries(*names: str) -> list[str]:
    """Die von `names`, die im PATH nicht auffindbar sind."""
    return [name for name in names if shutil.which(name) is None]


def requires_binaries(*names: str, purpose: str, package: str = ""):
    """Überspringt Test/Testklasse, solange eines der Binaries fehlt.

    `purpose` beschreibt, was übersprungen wird, `package` nennt optional das
    Systempaket, das es nachliefert -- beides landet im Überspring-Text.
    """
    missing = missing_binaries(*names)
    hint = f" (Paket {package})" if package else ""
    return unittest.skipIf(
        missing,
        f"{', '.join(missing)}{hint} nicht installiert — {purpose} übersprungen",
    )


def missing_ocr_languages() -> list[str]:
    """Die in `FINDUS_EXTRACTION_OCR_LANGUAGES` konfigurierten Sprachen, für
    die keine tessdata-Datei installiert ist.

    Ohne Binary ist die Frage gegenstandslos (leere Liste) -- dann greift
    bereits die Binary-Prüfung.
    """
    if shutil.which(OCR_BINARY) is None:
        return []
    import pytesseract

    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception:
        return []
    configured = settings.FINDUS_EXTRACTION_OCR_LANGUAGES.split("+")
    return [lang for lang in configured if lang not in available]


def requires_ocr(purpose: str = "OCR-Test"):
    """Überspringt, wenn `tesseract` fehlt -- oder wenn es zwar da ist, aber
    ohne die konfigurierten Sprachdaten: eine fehlende `deu.traineddata` löst
    denselben Fehlschlag aus wie ein fehlendes Binary und ist genauso wenig
    ein Befund über den geänderten Code.
    """
    if missing_binaries(OCR_BINARY):
        reason = f"{OCR_BINARY} nicht installiert — {purpose} übersprungen"
    elif languages := missing_ocr_languages():
        reason = (
            f"{OCR_BINARY}-Sprachdaten fehlen ({', '.join(languages)}) — "
            f"{purpose} übersprungen"
        )
    else:
        reason = ""
    return unittest.skipIf(bool(reason), reason)


def requires_pdf_rasterizer(purpose: str = "PDF-Raster-Test"):
    """Überspringt, wenn poppler fehlt. Das Rastern einer PDF-Seite ist die
    Vorstufe *jeder* Eskalation auf OCR oder Vision -- ohne Bild kommt die
    Kaskade dort gar nicht erst an, auch wenn `_ocr_image` gemockt ist.
    """
    return requires_binaries(
        *PDF_RASTER_BINARIES, purpose=purpose, package="poppler-utils"
    )


def requires_system_mime_type(filename: str, expected: str):
    """Überspringt, wenn die System-MIME-Datenbank `filename` nicht auf
    `expected` abbildet.

    Bewusst je Zuordnung statt pauschal: `apps.documents.mime` führt für die
    von der Extraktion unterstützten Typen eine eigene Tabelle und ist auf die
    Systemtabelle gar nicht angewiesen. Nur die Tests zum *Fallback* auf
    `mimetypes` hängen an einem konkreten Eintrag -- und nur der wird geprüft.
    """
    guessed = mimetypes.guess_type(filename)[0]
    suffix = os.path.splitext(filename)[1] or filename
    return unittest.skipUnless(
        guessed == expected,
        f"System-MIME-Datenbank kennt '{suffix}' nicht als '{expected}' "
        f"(liefert {guessed!r}) — Test übersprungen",
    )
