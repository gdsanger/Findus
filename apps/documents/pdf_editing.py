"""Seitenoperationen am PDF: drehen, löschen, aufteilen (#1155).

Reine Datei-/Arithmetik-Schicht der Scan-Korrektur -- kein Django-Model,
keine Storage-Zugriffe, keine Queue: `PdfEditPlan` beschreibt *was*
passieren soll, `iter_edited_parts` liefert die fertigen PDF-Teile. Wer
das Ergebnis wohin schreibt, entscheidet `apps.documents.pdf_edit`.

Bibliothek ist `pypdf` -- dieselbe, mit der die Extraktion schon
Seitenzahl und Text-Layer liest (`apps.documents.extraction`). Bewusst
keine zweite PDF-Bibliothek fürs Schreiben: zwei Bibliotheken bedeuten
zwei Fehlerbilder bei kaputten Dateien, und genau die sind hier der
Normalfall (dies ist die Reparaturfunktion für misslungene Scans).

Feste Reihenfolge, wenn mehrere Operationen zusammen kommen:
**erst drehen, dann löschen, dann aufteilen.** Ohne festgelegte
Reihenfolge bedeuten dieselben Eingaben je nach Umsetzung etwas anderes
-- eine Schnittmarke "vor Seite 5" wäre nach einer Löschung von Seite 3
plötzlich eine andere Stelle. Alle Seitenangaben eines Plans beziehen
sich deshalb durchgängig auf die **Originalnummerierung**; das Umrechnen
passiert genau einmal, in `split_page_groups`.

Drehungen landen als `/Rotate` dauerhaft in der Datei (nicht nur in der
Anzeige): jeder Rasterer -- pypdfium2 für die Vorschaubilder, poppler für
OCR/Vision -- rendert danach die gerade Seite, und genau darin liegt der
fachliche Nutzen (verdrehte Seiten sind eine häufige Ursache schlechter
Text- und Vision-Erkennung).
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO

logger = logging.getLogger(__name__)

#: Erlaubte Drehwinkel. 0 heißt "nicht drehen" und steht bewusst nicht
#: drin -- ein Plan trägt nur die Seiten, an denen sich etwas ändert.
ALLOWED_ROTATIONS = (90, 180, 270)

#: Ab dieser Größe wandert ein erzeugtes Teil-PDF auf die Platte statt in
#: den Arbeitsspeicher (`SpooledTemporaryFile`). Ein 300-Seiten-Scan darf
#: den Worker nicht sprengen, ein zweiseitiger Beleg soll nicht erst
#: durchs Dateisystem laufen.
_SPOOL_MAX_BYTES = 8 * 1024 * 1024


class PdfEditError(Exception):
    """Eine Datei, mit der sich nicht arbeiten lässt -- passwortgeschützt,
    beschädigt, kein PDF.

    Trägt bewusst eine für den Nutzer lesbare deutsche Meldung: sie wird
    unverändert in der Seitenansicht bzw. am fehlgeschlagenen Lauf
    angezeigt. Ein solcher Fall ist eine Auskunft, keine 500er-Seite.
    """


@dataclass(frozen=True)
class PdfInfo:
    """Was die Seitenansicht über eine Datei wissen muss, bevor sie
    irgendetwas anbietet."""

    page_count: int
    #: Digital signiert? Die Bearbeitung macht die Signatur ungültig --
    #: Hinweis im Bestätigungsschritt, aber ausdrücklich keine Sperre
    #: (ein misslungener Scan bleibt reparaturbedürftig, auch wenn ihn
    #: jemand signiert hat).
    has_signature: bool


@dataclass(frozen=True)
class PdfEditPlan:
    """Die bestätigte Eingabe der Seitenansicht, in Originalnummerierung.

    `rotations`: Seite -> Winkel (90/180/270). `deletions`: zu entfernende
    Seiten. `splits`: Seiten, **vor** denen ein neues Dokument beginnt
    (also 2..n; "vor Seite 1" wäre keine Teilung).
    """

    rotations: dict[int, int]
    deletions: tuple[int, ...]
    splits: tuple[int, ...]

    @classmethod
    def from_dict(cls, data: dict) -> PdfEditPlan:
        """Plan aus der JSON-Ablage am `DocumentPdfEditRun` -- die
        Schlüssel des Drehungs-Mappings sind dort Strings (JSON kennt keine
        Integer-Keys).
        """
        rotations = {
            int(page): int(angle) for page, angle in (data.get("rotations") or {}).items()
        }
        return cls(
            rotations=rotations,
            deletions=tuple(sorted(int(page) for page in data.get("deletions") or ())),
            splits=tuple(sorted(int(page) for page in data.get("splits") or ())),
        )

    def as_dict(self) -> dict:
        return {
            "rotations": {str(page): angle for page, angle in sorted(self.rotations.items())},
            "deletions": list(self.deletions),
            "splits": list(self.splits),
        }

    def as_form_data(self) -> list[tuple[str, str]]:
        """Der Plan als Formularfelder für den Bestätigungsschritt.

        Der zweite Schritt schickt exakt dieselben Feldnamen noch einmal an
        den Ausführen-Endpunkt, damit dort **dasselbe** Formular noch
        einmal validiert -- die Bestätigung ist eine Zwischenanzeige, kein
        Nebenkanal mit eigener Datenhaltung.
        """
        data = [("delete_pages", str(page)) for page in self.deletions]
        data += [(f"rotate_{page}", str(angle)) for page, angle in sorted(self.rotations.items())]
        data += [(f"split_before_{page}", "1") for page in self.splits]
        return data


def open_pdf(fileobj: IO[bytes]):
    """`PdfReader` für `fileobj` -- oder ein `PdfEditError` mit einer
    verständlichen Meldung.

    Passwortgeschützte Dateien werden abgelehnt statt mit leerem Passwort
    aufgebrochen: eine Datei, die nur ein Eigentümer-Passwort trägt, ließe
    sich zwar öffnen, aber das Ergebnis wäre eine entschützte Kopie -- eine
    Entscheidung, die niemand getroffen hat.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(fileobj)
        # Vor jedem Zugriff auf die Seiten: bei einer verschlüsselten Datei
        # scheitert `reader.pages` mit einem generischen Fehler, und die
        # Meldung würde "beschädigt" behaupten, wo "passwortgeschützt"
        # gemeint ist -- zwei sehr verschiedene Auskünfte für den Nutzer.
        encrypted = reader.is_encrypted
    except Exception as exc:  # pypdf wirft eine ganze Familie von Fehlern
        logger.info("PDF-Bearbeitung: Datei nicht lesbar (%s)", exc)
        raise PdfEditError(
            "Die PDF-Datei konnte nicht gelesen werden – sie ist beschädigt "
            "oder kein gültiges PDF."
        ) from exc

    if encrypted:
        raise PdfEditError(
            "Die PDF-Datei ist passwortgeschützt und lässt sich nicht bearbeiten. "
            "Bitte zuerst den Schutz entfernen und erneut hochladen."
        )

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        logger.info("PDF-Bearbeitung: Seiten nicht lesbar (%s)", exc)
        raise PdfEditError(
            "Die PDF-Datei konnte nicht gelesen werden – sie ist beschädigt "
            "oder kein gültiges PDF."
        ) from exc

    if page_count == 0:
        raise PdfEditError("Die PDF-Datei enthält keine Seiten.")
    return reader


def _has_signature(reader) -> bool:
    """Trägt das Dokument eine digitale Signatur? Erkannt am `/AcroForm`
    des Katalogs (`/SigFlags`-Bit bzw. ein Feld vom Typ `/Sig`).

    Bewusst großzügig gefangen: eine exotisch gebaute Datei soll den
    Bestätigungsschritt nicht sprengen -- im Zweifel fehlt der Hinweis,
    statt dass die Seite stehen bleibt.
    """
    try:
        acro_form = reader.trailer["/Root"].get("/AcroForm")
        if acro_form is None:
            return False
        acro_form = acro_form.get_object()
        if int(acro_form.get("/SigFlags", 0)) & 1:
            return True
        for field in acro_form.get("/Fields", []) or []:
            if field.get_object().get("/FT") == "/Sig":
                return True
    except Exception:
        logger.debug("PDF-Bearbeitung: Signaturprüfung nicht möglich", exc_info=True)
    return False


def inspect_pdf(fileobj: IO[bytes]) -> PdfInfo:
    """Seitenzahl + Signatur-Merkmal, ohne irgendetwas zu verändern."""
    reader = open_pdf(fileobj)
    return PdfInfo(page_count=len(reader.pages), has_signature=_has_signature(reader))


def split_page_groups(page_count: int, plan: PdfEditPlan) -> list[list[int]]:
    """Welche Originalseiten in welches Teil-Dokument wandern.

    Die eine Stelle, an der die festgelegte Reihenfolge tatsächlich
    stattfindet (drehen ist seitenlokal und spielt hier keine Rolle):
    erst fallen die gelöschten Seiten weg, dann wirken die Schnittmarken
    auf den Rest. Eine Marke, die auf eine gelöschte Seite zeigt, geht
    deshalb nicht verloren -- sie schneidet vor der nächsten übrig
    gebliebenen Seite. Eine Marke vor der ersten übrig gebliebenen Seite
    verfällt: ein Teil ohne Seiten wäre kein Dokument.

    Reine Arithmetik, damit die Reihenfolge ohne PDF testbar ist.
    """
    kept = [page for page in range(1, page_count + 1) if page not in plan.deletions]
    groups: list[list[int]] = []
    current: list[int] = []
    for page in kept:
        if current and any(current[-1] < marker <= page for marker in plan.splits):
            groups.append(current)
            current = []
        current.append(page)
    if current:
        groups.append(current)
    return groups


def iter_edited_parts(
    fileobj: IO[bytes], plan: PdfEditPlan
) -> Iterator[tuple[list[int], IO[bytes]]]:
    """Wendet `plan` auf `fileobj` an und liefert die Teile nacheinander.

    Je Teil ein `(Originalseiten, Datei)`-Paar; die Datei steht auf
    Position 0 und ist ein `SpooledTemporaryFile` -- ein großer Scan liegt
    also nicht als Ganzes im Speicher, und der Aufrufer kann jedes Teil
    einzeln wegschreiben und wieder schließen, statt alle gleichzeitig zu
    halten.

    Ohne Schnittmarken ist es genau ein Teil: dann *ersetzt* das Ergebnis
    das Original (siehe `apps.documents.pdf_edit`), es entsteht kein neues
    Dokument.
    """
    from pypdf import PdfWriter

    reader = open_pdf(fileobj)
    groups = split_page_groups(len(reader.pages), plan)
    if not groups:
        raise PdfEditError(
            "Nach der Bearbeitung bliebe keine Seite übrig – ein Dokument "
            "ohne Seiten ist kein Dokument."
        )

    for group in groups:
        writer = PdfWriter()
        for page_number in group:
            page = reader.pages[page_number - 1]
            angle = plan.rotations.get(page_number)
            if angle:
                # pypdf rechnet den Winkel auf ein evtl. schon vorhandenes
                # `/Rotate` auf und normalisiert -- genau das gewünschte
                # "um 90° weiterdrehen", nicht "auf 90° setzen".
                page.rotate(angle)
            writer.add_page(page)
        buffer = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES, suffix=".pdf")
        try:
            writer.write(buffer)
        finally:
            writer.close()
        buffer.seek(0)
        yield group, buffer


def part_filename(original_filename: str, index: int, total: int) -> str:
    """Dateiname eines Teils: "scan.pdf" -> "scan-teil-2.pdf".

    Bei nur einem Teil bleibt der Name unverändert -- dort ersetzt das
    Ergebnis ja dieselbe Datei, ein "-teil-1" wäre schlicht falsch.
    """
    name = original_filename or "dokument.pdf"
    if total <= 1:
        return name
    stem, _, suffix = name.rpartition(".")
    if not stem:
        stem, suffix = name, "pdf"
    return f"{stem}-teil-{index}.{suffix or 'pdf'}"


def _page_list(pages: tuple[int, ...] | list[int]) -> str:
    """"3", "3 und 7", "3, 5 und 7" -- Aufzählung für den
    Bestätigungstext. Deutsche Zeichensetzung gehört an *eine* Stelle,
    nicht in drei Templates."""
    values = [str(page) for page in pages]
    if len(values) <= 1:
        return "".join(values)
    return f"{', '.join(values[:-1])} und {values[-1]}"


def _page_phrase(pages: tuple[int, ...] | list[int]) -> str:
    """"Seite 3 wird" / "Seiten 3 und 7 werden" -- Subjekt samt Numerus für
    die Sätze des Bestätigungsschritts."""
    if len(pages) == 1:
        return f"Seite {_page_list(pages)} wird"
    return f"Seiten {_page_list(pages)} werden"


def summarize_plan(plan: PdfEditPlan, *, page_count: int) -> list[str]:
    """Was der Plan bewirken wird, in Worten -- die Grundlage des
    Bestätigungsschritts ("Seiten 3 und 7 werden entfernt, Seite 5 wird um
    90° gedreht, das Dokument wird in 3 Dokumente aufgeteilt, das Original
    wird gelöscht.").

    Erzeugt in der Reihenfolge, in der die Operationen auch angewandt
    werden -- die Anzeige soll nicht suggerieren, es sei egal.
    """
    lines: list[str] = []
    for angle in ALLOWED_ROTATIONS:
        pages = tuple(sorted(page for page, value in plan.rotations.items() if value == angle))
        if pages:
            lines.append(f"{_page_phrase(pages)} um {angle}° gedreht")
    if plan.deletions:
        lines.append(f"{_page_phrase(plan.deletions)} entfernt")

    groups = split_page_groups(page_count, plan)
    if len(groups) > 1:
        lines.append(f"das Dokument wird in {len(groups)} Dokumente aufgeteilt")
        lines.append("das Original wird gelöscht")
    return lines
