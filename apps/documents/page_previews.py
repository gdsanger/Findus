"""Seitenvorschauen für die Scan-Korrektur (#1155): jede PDF-Seite als
kleines Bild, plus der Hinweis "auf dieser Seite steht nichts Verwertbares".

Gerastert wird mit `pypdfium2` -- dieselbe Engine, die schon das
Kachel-Thumbnail rendert (`apps.documents.thumbnails`), und aus demselben
Grund: ein reines Python-Wheel, das ohne poppler/tesseract im Container
auskommt. Auch die Kodierung (WebP, Fallback PNG) kommt von dort; hier
wird lediglich eine größere Kantenlänge gewählt, weil man einer 120px-
Kachel nicht ansieht, ob die Seite leer oder nur blass ist.

Die Bilder sind ein reiner Cache (`DocumentPagePreview`): einmal erzeugt,
bis sich die Originaldatei ändert. Bei vielen Seiten läuft die Erzeugung
im Hintergrund und die Ansicht lädt nach -- deshalb schreibt
`generate_page_previews` Seite für Seite, statt erst am Ende alles auf
einmal: eine fertige Seite soll sofort sichtbar sein.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError

from .models import Document, DocumentPagePreview
from .thumbnails import encode_image

logger = logging.getLogger(__name__)

#: Wie die beiden Extraktionswege eine Seite ohne Inhalt benennen -- die
#: Kaskade (`extraction.build_markdown`) und die KI-Vision-Neuextraktion
#: (`extraction._VISION_MARKDOWN_PROMPT`). Beide Marker gelten als
#: "Seite ohne verwertbaren Inhalt"; der Platzhalter für eine technisch
#: fehlgeschlagene Seite ausdrücklich NICHT -- dass eine Seite nicht
#: verarbeitet werden konnte, heißt nicht, dass sie leer ist.
_EMPTY_PAGE_MARKERS = ("_Kein Text erkannt._", "_Seite leer oder nicht lesbar._")
_FAILED_PAGE_MARKER = "_Seite konnte technisch nicht verarbeitet werden._"

_PAGE_HEADING_RE = re.compile(r"^## Seite (\d+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PagePreview:
    """Eine Zeile der Seitenansicht -- Bild (oder noch keins) plus die
    Leerseiten-Kennzeichnung."""

    page_number: int
    has_image: bool
    looks_empty: bool


def _max_edge() -> int:
    return settings.FINDUS_PAGE_PREVIEW_MAX_EDGE


def _render_page(pdf, index: int):
    """Eine 0-basierte Seite als Pillow-Bild. Rendert direkt auf die
    Zielkante statt hoch und wieder herunter (wie beim Thumbnail) --
    pdfium wendet dabei die `/Rotate`-Angabe der Seite an, eine gedrehte
    Seite wird also gedreht angezeigt.
    """
    page = pdf[index]
    width, height = page.get_size()
    longest = max(width, height) or 1
    scale = min(max(_max_edge() / longest, 0.1), 3.0)
    return page.render(scale=scale).to_pil()


def generate_page_previews(document_id: int, *, force: bool = False) -> int:
    """Rastert die fehlenden Seitenbilder von `document` und legt sie ab.

    Gibt die Zahl der neu erzeugten Bilder zurück. Idempotent: bereits
    vorhandene Seiten werden übersprungen (`force=True` erzeugt alles neu,
    z. B. nach einer Änderung an der Datei -- der reguläre Weg dafür ist
    aber `discard_page_previews`, siehe dort).

    Bewusst fehlertolerant je Seite, wie das Thumbnail (#1123): eine Seite,
    die sich nicht rastern lässt, bleibt ohne Bild und die Ansicht zeigt
    dort einen Platzhalter -- der Rest der Seitenansicht bleibt bedienbar.
    Ein Fehler beim Öffnen der Datei selbst wird geloggt und beendet den
    Lauf still; die Seitenansicht selbst prüft die Datei ohnehin schon und
    meldet einen echten Defekt dort verständlich (`pdf_editing.open_pdf`).
    """
    import pypdfium2 as pdfium

    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        logger.warning("Seitenvorschau übersprungen: Document %s existiert nicht mehr", document_id)
        return 0
    if not document.supports_page_editing:
        return 0

    if force:
        discard_page_previews(document)

    existing = set(document.page_previews.values_list("page_number", flat=True))

    document.original_file.open("rb")
    try:
        data = document.original_file.read()
    finally:
        document.original_file.close()

    created = 0
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        logger.exception("Seitenvorschau: PDF von Document %s nicht lesbar", document_id)
        return 0
    try:
        for index in range(len(pdf)):
            page_number = index + 1
            if page_number in existing:
                continue
            try:
                image_bytes, ext = encode_image(_render_page(pdf, index), max_edge=_max_edge())
            except Exception:
                logger.exception(
                    "Seitenvorschau: Seite %s von Document %s konnte nicht gerastert werden",
                    page_number,
                    document_id,
                )
                continue
            preview = DocumentPagePreview(document=document, page_number=page_number)
            preview.image.save(
                f"document-{document.pk}-seite-{page_number}.{ext}",
                ContentFile(image_bytes),
                save=False,
            )
            try:
                preview.save()
            except IntegrityError:
                # Zwei parallel angestoßene Läufe (zwei Aufrufe der
                # Seitenansicht kurz hintereinander) treffen sich hier --
                # die Unique-Constraint entscheidet, der Verlierer räumt
                # seine Datei weg statt sie verwaist liegen zu lassen.
                logger.info(
                    "Seitenvorschau: Seite %s von Document %s war bereits vorhanden",
                    page_number,
                    document_id,
                )
                preview.image.delete(save=False)
                continue
            created += 1
    finally:
        pdf.close()

    logger.info("Seitenvorschau: %s neue Seitenbilder fuer Document %s", created, document_id)
    return created


def discard_page_previews(document: Document) -> None:
    """Wirft alle Seitenbilder eines Dokuments weg -- Dateien **und**
    Zeilen.

    Zu rufen, sobald sich die Originaldatei geändert hat (die Bilder zeigen
    dann Seiten, die es so nicht mehr gibt) und bevor ein Dokument gelöscht
    wird: Django räumt `FileField`-Inhalte nie von selbst auf, genauso wie
    beim `thumbnail` (#1123).
    """
    for preview in document.page_previews.all():
        if preview.image:
            preview.image.delete(save=False)
        preview.delete()


def _page_sections(markdown: str) -> dict[int, str]:
    """Zerlegt den Markdown-Cache eines Dokuments in seine Seiten.

    `extraction.build_markdown` schreibt je Seite eine `## Seite N`-
    Überschrift -- außer bei einem einseitigen Dokument, dort steht der
    Text ohne Überschrift unter dem Titel. Beide Formen werden hier
    gelesen, sonst wäre ausgerechnet die einseitige Datei nie "leer".
    """
    if not markdown:
        return {}
    matches = list(_PAGE_HEADING_RE.finditer(markdown))
    if not matches:
        body = markdown.split("\n", 1)[1] if markdown.startswith("# ") else markdown
        return {1: body.strip()}
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[int(match.group(1))] = markdown[match.end() : end].strip()
    return sections


def _looks_empty(section: str) -> bool:
    if _FAILED_PAGE_MARKER in section:
        return False
    text = section
    for marker in _EMPTY_PAGE_MARKERS:
        text = text.replace(marker, "")
    return len(text.strip()) < settings.FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE


def page_previews_for(document: Document, *, page_count: int) -> list[PagePreview]:
    """Die Zeilen der Seitenansicht: je Seite, ob ihr Bild schon da ist und
    ob sie "ohne verwertbaren Inhalt" aussieht.

    Die Leerseiten-Kennzeichnung ist ein **Hinweis**, keine Vorauswahl: eine
    Seite mit nur einem handschriftlichen Vermerk oder einem Stempel sieht
    für die Texterkennung leer aus und ist es nicht. Die Entscheidung
    bleibt beim Nutzer (siehe CLAUDE.md).
    """
    available = set(document.page_previews.values_list("page_number", flat=True))
    sections = _page_sections(document.markdown)
    return [
        PagePreview(
            page_number=page_number,
            has_image=page_number in available,
            # Ohne jeden extrahierten Text ist gar nichts bekannt -- dann
            # wird auch nichts gekennzeichnet. "Keine Angabe" als "leer"
            # anzuzeigen wäre eine Behauptung, die die Daten nicht hergeben.
            looks_empty=bool(sections) and _looks_empty(sections.get(page_number, "")),
        )
        for page_number in range(1, page_count + 1)
    ]
