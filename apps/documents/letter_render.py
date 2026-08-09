"""Rendering eines Brief-Entwurfs ins Vorlagen-Layout (#1095): **Word**
(.docx) als editierbarer Master und **PDF** als Druck-/Sende-Artefakt.

Beide Ausgaben entstehen aus derselben `LetterContent` -- dem einmal
zusammengebauten Brief aus Briefkopf, Absender-/Empfängerblock,
Datumszeile, Betreff, Text, Grußformel und Signatur. Genau deshalb gibt es
diese Zwischenstufe: zwei Renderer, die sich ihre Bestandteile jeweils
selbst aus dem Entwurf zusammensuchen, wären zwei Gelegenheiten, das
Layout unterschiedlich zu interpretieren.

**PDF direkt gerendert (fpdf2), nicht aus dem docx konvertiert.** Die
Alternative -- LibreOffice headless als Konverter -- bräuchte ein ~400 MB
schweres Office-Paket im Container samt Subprozess-Aufruf, Timeout- und
Profil-Handling, nur um ein Layout zu reproduzieren, das wir hier ohnehin
selbst beschreiben. Der Preis für den direkten Weg ist, dass beide
Renderer gepflegt werden müssen; das ist überschaubar, weil sie aus
derselben Struktur lesen und die Layout-Entscheidungen (Ränder,
Positionen) hier oben als Konstanten stehen. fpdf2 ist zudem schon für die
Mail-Body-PDFs im Einsatz (#1070), es kommt also keine Abhängigkeit dazu.

Layout-Grundlage ist der deutsche Geschäftsbrief (DIN-5008-nah, siehe
`DEFAULT_LETTER_LAYOUT`): Anschriftfeld links oben im festen Abstand,
Absender-Kurzzeile darüber, Datumszeile rechts, Betreff fett, danach der
Text. Die Werte sind Millimeter-Konstanten statt einer Layout-Engine --
mehr braucht ein einseitiger Geschäftsbrief nicht, und alles, was eine
Vorlage daran ändern darf, kommt aus ihrem `layout`-JSON.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from io import BytesIO

from django.utils.formats import date_format

logger = logging.getLogger(__name__)

# Seitenmaße/Ränder in Millimetern (A4, DIN-5008-nah).
PAGE_MARGIN_LEFT_MM = 25
PAGE_MARGIN_RIGHT_MM = 20
PAGE_MARGIN_TOP_MM = 20
PAGE_MARGIN_BOTTOM_MM = 20
# Oberkante Anschriftfeld: DIN 5008 Form A. Der Block landet damit im
# Fenster eines C5/C6-Umschlags.
ADDRESS_FIELD_TOP_MM = 45
LOGO_WIDTH_MM = 40

_BODY_FONT_SIZE = 11
_SMALL_FONT_SIZE = 8


@dataclass(frozen=True)
class LetterContent:
    """Der fertig zusammengesetzte Brief -- alles, was auf das Papier
    kommt, in der Reihenfolge, in der es dort steht.

    Reine Daten, keine Model-Referenz: die Renderer sollen nicht wissen,
    ob der Brief aus einem `LetterDraft`, einer Vorschau oder einem Test
    kommt.
    """

    subject: str = ""
    body: str = ""
    letterhead: str = ""
    sender_line: str = ""
    sender_block: str = ""
    recipient_block: str = ""
    date_line: str = ""
    closing: str = ""
    signature: str = ""
    logo: bytes | None = None
    logo_name: str = ""


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _sender_line(sender_block: str) -> str:
    """Absender-Kurzzeile über dem Anschriftfeld: der mehrzeilige
    Absenderblock in einer Zeile, durch „· " getrennt.

    DIN 5008 sieht sie klein und einzeilig vor -- sie ist die Rücksendeangabe
    im Sichtfenster, nicht der Absenderblock selbst.
    """
    return " · ".join(_lines(sender_block))


def build_content(draft) -> LetterContent:
    """`LetterDraft` -> `LetterContent`, ausschließlich aus dem Snapshot.

    Liest bewusst nichts aus `draft.template` nach: das Layout wurde beim
    Anlegen eingefroren (siehe `LetterDraft`), und ein Brief darf sich
    nicht ändern, weil jemand die Vorlage bearbeitet hat. Nur das Logo
    hängt noch an der Vorlage -- eine Bilddatei je Entwurf zu kopieren
    wäre Speicher für einen Fall (Logo-Wechsel zwischen Entwurf und
    Freigabe), den niemand hat.
    """
    letterhead = draft.layout_value("letterhead") or ""
    sender_block = draft.sender_block if draft.layout_value("show_sender_block") else ""
    recipient_block = (
        draft.recipient_block if draft.layout_value("show_recipient_block") else ""
    )

    date_line = ""
    if draft.layout_value("show_date") and draft.letter_date:
        place = (draft.layout_value("date_place") or "").strip()
        formatted = date_format(draft.letter_date, "DATE_FORMAT")
        date_line = f"{place}, {formatted}" if place else formatted

    logo_bytes, logo_name = _logo_bytes(draft)

    return LetterContent(
        subject=draft.subject if draft.layout_value("show_subject") else "",
        body=draft.body_text or "",
        letterhead=letterhead,
        sender_line=_sender_line(sender_block),
        sender_block=sender_block,
        recipient_block=recipient_block,
        date_line=date_line,
        closing=(draft.layout_value("closing") or "").strip(),
        signature=draft.signature or "",
        logo=logo_bytes,
        logo_name=logo_name,
    )


def _logo_bytes(draft) -> tuple[bytes | None, str]:
    """Das Vorlagen-Logo als Bytes, oder `(None, "")`.

    Defensiv: das Logo liegt im Object-Storage (S3/MinIO), und ein Brief
    darf nicht daran scheitern, dass die Datei dort weg oder der Bucket
    kurz nicht erreichbar ist -- dann kommt er eben ohne Logo.
    """
    template = draft.template
    if template is None or not template.logo:
        return None, ""
    try:
        with template.logo.open("rb") as handle:
            return handle.read(), template.logo.name.rsplit("/", 1)[-1]
    except Exception:
        logger.warning(
            "Brief-Entwurf %s: Logo der Vorlage %s konnte nicht geladen werden",
            draft.pk,
            template.pk,
            exc_info=True,
        )
        return None, ""


def _content_blocks(content: LetterContent) -> list[str]:
    """Betreff/Text/Grußformel/Signatur als Absatzfolge -- die Reihenfolge,
    die beide Renderer und der Klartext teilen.
    """
    blocks = []
    if content.subject.strip():
        blocks.append(content.subject.strip())
    body = (content.body or "").strip()
    if body:
        blocks.extend(paragraph.strip() for paragraph in re.split(r"\n\s*\n", body))
    if content.closing:
        blocks.append(content.closing)
    if content.signature.strip():
        blocks.append(content.signature.strip())
    return [block for block in blocks if block]


def plain_text(content: LetterContent) -> str:
    """Der Brief als Klartext -- was als `text_content` in das abgelegte
    Dokument geht und damit in Index und Suche landet.

    Enthält Kopf und Anschrift: nach einem Brief wird genauso oft über den
    Empfänger gesucht wie über den Text.
    """
    parts = [
        content.letterhead,
        content.sender_block,
        content.recipient_block,
        content.date_line,
    ]
    parts.extend(_content_blocks(content))
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


# -- Word ------------------------------------------------------------------


def render_docx(content: LetterContent) -> bytes:
    """Word-Master (.docx) über python-docx.

    Word ist das *editierbare* Artefakt: was hier entsteht, soll jemand
    ohne Findus weiterschreiben können. Deshalb echte Absätze statt eines
    Textrahmen-Layouts -- ein pixelgenauer Nachbau des PDFs wäre in Word
    unbearbeitbar.
    """
    from docx import Document as DocxDocument
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt

    document = DocxDocument()
    section = document.sections[0]
    section.left_margin = Mm(PAGE_MARGIN_LEFT_MM)
    section.right_margin = Mm(PAGE_MARGIN_RIGHT_MM)
    section.top_margin = Mm(PAGE_MARGIN_TOP_MM)
    section.bottom_margin = Mm(PAGE_MARGIN_BOTTOM_MM)

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(_BODY_FONT_SIZE)

    def paragraph(text="", *, size=None, bold=False, align=None, space_after=6):
        item = document.add_paragraph()
        item.paragraph_format.space_after = Pt(space_after)
        if align is not None:
            item.alignment = align
        for index, line in enumerate((text or "").split("\n")):
            run = item.add_run(line)
            run.bold = bold
            if size is not None:
                run.font.size = Pt(size)
            if index < len((text or "").split("\n")) - 1:
                run.add_break()
        return item

    if content.logo:
        logo_paragraph = document.add_paragraph()
        logo_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        try:
            logo_paragraph.add_run().add_picture(BytesIO(content.logo), width=Mm(LOGO_WIDTH_MM))
        except Exception:
            # Ein Bildformat, das python-docx nicht kennt, darf den Brief
            # nicht kosten -- der Absatz bleibt dann eben leer.
            logger.warning("Brief-Rendering: Logo konnte nicht eingebettet werden", exc_info=True)

    if content.letterhead.strip():
        paragraph(content.letterhead.strip(), bold=True, space_after=12)

    if content.sender_line:
        paragraph(content.sender_line, size=_SMALL_FONT_SIZE, space_after=2)

    if content.recipient_block.strip():
        paragraph(content.recipient_block.strip(), space_after=18)

    if content.date_line:
        paragraph(content.date_line, align=WD_ALIGN_PARAGRAPH.RIGHT, space_after=18)

    if content.subject.strip():
        paragraph(content.subject.strip(), bold=True, space_after=12)

    for block in re.split(r"\n\s*\n", (content.body or "").strip()):
        if block.strip():
            paragraph(block.strip(), space_after=10)

    if content.closing:
        paragraph(content.closing, space_after=18)

    if content.signature.strip():
        paragraph(content.signature.strip(), space_after=0)

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# -- PDF -------------------------------------------------------------------


def _pdf_safe(text: str) -> str:
    """fpdf2-Kernschriften können nur cp1252 -- nicht abbildbare Zeichen
    (Emoji, nicht-lateinische Schrift) durch '?' ersetzen, statt beim
    PDF-Bau zu crashen. Umlaute, Euro und typografische Zeichen bleiben.

    Dieselbe Einschränkung wie bei den Mail-Body-PDFs (#1070); bewusst hier
    noch einmal formuliert, statt `apps.ingest` etwas zu entleihen --
    `apps.ingest` hängt von `apps.documents` ab, nicht umgekehrt.
    """
    return (text or "").encode("cp1252", errors="replace").decode("cp1252")


def render_pdf(content: LetterContent) -> bytes:
    """Druck-/Sendefassung (PDF) über fpdf2, direkt aus `LetterContent`.

    Positioniert Absenderzeile und Anschriftfeld absolut (DIN-5008-nah),
    danach läuft der Text im normalen Fluss mit automatischem Seitenumbruch.
    """
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(format="A4")
    pdf.core_fonts_encoding = "cp1252"
    pdf.set_margins(PAGE_MARGIN_LEFT_MM, PAGE_MARGIN_TOP_MM, PAGE_MARGIN_RIGHT_MM)
    pdf.set_auto_page_break(auto=True, margin=PAGE_MARGIN_BOTTOM_MM)
    pdf.add_page()

    def line(text, *, height=5.5):
        pdf.multi_cell(0, height, _pdf_safe(text) or " ", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if content.logo:
        try:
            pdf.image(
                BytesIO(content.logo),
                x=pdf.w - PAGE_MARGIN_RIGHT_MM - LOGO_WIDTH_MM,
                y=PAGE_MARGIN_TOP_MM,
                w=LOGO_WIDTH_MM,
            )
        except Exception:
            logger.warning("Brief-Rendering: Logo konnte nicht ins PDF gesetzt werden", exc_info=True)

    if content.letterhead.strip():
        pdf.set_font("Helvetica", style="B", size=12)
        line(content.letterhead.strip(), height=6)

    # Anschriftfeld: feste Oberkante, damit der Empfänger im Fensterumschlag
    # sichtbar ist -- unabhängig davon, wie hoch der Briefkopf ausgefallen ist.
    pdf.set_y(max(pdf.get_y(), ADDRESS_FIELD_TOP_MM - 8))
    if content.sender_line:
        pdf.set_font("Helvetica", size=_SMALL_FONT_SIZE)
        line(content.sender_line, height=4)

    pdf.set_y(max(pdf.get_y(), ADDRESS_FIELD_TOP_MM))
    pdf.set_font("Helvetica", size=_BODY_FONT_SIZE)
    if content.recipient_block.strip():
        line(content.recipient_block.strip())

    if content.date_line:
        pdf.ln(8)
        pdf.cell(0, 5.5, _pdf_safe(content.date_line), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    if content.subject.strip():
        pdf.set_font("Helvetica", style="B", size=_BODY_FONT_SIZE)
        line(content.subject.strip(), height=6)
        pdf.ln(4)

    pdf.set_font("Helvetica", size=_BODY_FONT_SIZE)
    for block in re.split(r"\n\s*\n", (content.body or "").strip()):
        if not block.strip():
            continue
        line(block.strip())
        pdf.ln(3)

    if content.closing:
        pdf.ln(4)
        line(content.closing)

    if content.signature.strip():
        pdf.ln(8)
        line(content.signature.strip())

    output = pdf.output()
    return bytes(output)


# -- Ablage am Entwurf -----------------------------------------------------


def file_slug(content: LetterContent, draft) -> str:
    """Dateiname-Stamm für beide Ausgaben: „brief-<betreff>-<id>".

    Die Entwurfs-ID hängt dran, damit zwei Briefe mit demselben Betreff im
    Downloads-Ordner unterscheidbar bleiben.
    """
    slug = re.sub(r"[^\w\- ]", "", content.subject or "").strip().replace(" ", "-")[:60]
    return f"brief-{slug}-{draft.pk}".replace("--", "-").strip("-").lower()


def render_draft_files(draft) -> LetterContent:
    """Rendert Word + PDF und legt beide am Entwurf ab.

    Ersetzt vorhandene Dateien bei jedem Aufruf -- „Text ändern und neu
    rendern" darf keine Halde alter Fassungen im Object-Storage
    hinterlassen, und die einzige Fassung, die zählt, ist die, die der
    Nutzer gerade vor sich sieht.
    """
    from django.core.files.base import ContentFile

    content = build_content(draft)
    slug = file_slug(content, draft)

    for field_name, suffix, data in (
        ("docx_file", "docx", render_docx(content)),
        ("pdf_file", "pdf", render_pdf(content)),
    ):
        file_field = getattr(draft, field_name)
        if file_field:
            file_field.delete(save=False)
        file_field.save(f"{slug}.{suffix}", ContentFile(data), save=False)

    draft.save(update_fields=["docx_file", "pdf_file", "updated_at"])
    return content
