"""Mail-Body-Aufbereitung fuer den Ingest (#1070): aus dem rohen (HTML-)
E-Mail-Text entstehen (a) bereinigter Klartext fuer Index/Embedding und
(b) ein lesbares PDF fuer die Ansicht -- plus die Substanz-Pruefung, die
entscheidet, ob der Body ueberhaupt gefuellt wird.

Bewusste Trennung Index- vs. Anzeige-Repraesentation (Anforderung #6):
`clean_body()` liefert den Klartext, aus dem sowohl `build_index_text()`
(Metadaten-Kopf + Body -> Embedding) als auch `render_body_pdf()`
(Metadaten-Kopf + Body -> PDF) speisen. Zitierte Verlaeufe und
Signaturen werden dabei *einmal* entfernt, damit dieselbe Antwortkette
nicht ueber jede Folge-Mail erneut in den Vektorraum wandert.

HTML wird mit BeautifulSoup entruempelt (Skripte/Styles, Tracking-Pixel,
versteckte Preheader, `blockquote`/`gmail_quote`-Verlaeufe), der Rest der
Zitat-/Signatur-Erkennung laeuft anschliessend zeilenbasiert auf dem
Klartext -- dieselben Muster greifen dann auch fuer reine Text-Mails.
Die Muster und die Wortschwelle sind als Konstanten/Setting justierbar
(uebernommen aus dem Prototyp).

PDF via fpdf2 (reines Python), nicht wkhtmltopdf: der Body ist zu diesem
Zeitpunkt bereits auf lesbaren Klartext reduziert, ein vollstaendiger
HTML-Renderer (headless-Browser-Binary im Container) waere dafuer
ueberdimensioniert und stuende quer zum Rest der Codebase (Stdlib/plain
statt schwergewichtiger Binaer-Abhaengigkeiten).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment
from django.conf import settings

# Elemente, deren Inhalt nie in den Klartext gehoert.
_STRIP_TAGS = ("script", "style", "head", "title", "noscript")

# Zitierte Verlaeufe im HTML (Client-spezifische Container). `gmail_quote`
# (Gmail), `moz-cite-prefix` (Thunderbird), `OutlookMessageHeader`/
# `divRplyFwdMsg` (Outlook/OWA) klammern jeweils die eingeklappte
# Vorgaenger-Mail -- alles darin ist Duplikat der urspruenglichen Mail.
_QUOTE_SELECTORS = (
    {"name": "blockquote"},
    {"attrs": {"class": "gmail_quote"}},
    {"attrs": {"class": "gmail_extra"}},
    {"attrs": {"class": "moz-cite-prefix"}},
    {"attrs": {"class": "OutlookMessageHeader"}},
    {"attrs": {"id": "divRplyFwdMsg"}},
)

# Preheader/versteckter Text ("display:none"-Schnipsel, die viele Newsletter
# als Inbox-Vorschau einschmuggeln) -- verzerren den Index, ohne im Body
# sichtbar zu sein.
_HIDDEN_STYLE_RE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0)", re.IGNORECASE
)

# Zeilen-Marker, ab denen der Rest der Mail Zitat/Signatur ist und
# abgeschnitten wird (nach dem HTML->Text-Schritt, greift auch fuer
# reine Text-Mails).
_SIGNATURE_LINE_RE = re.compile(r"^--\s*$")
_MOBILE_SIGNATURE_RE = re.compile(
    r"^(Gesendet von meinem|Von meinem .* gesendet|Sent from my|Get Outlook for)\b",
    re.IGNORECASE,
)
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^\s*-{2,}\s*(Urspr(ü|ue)ngliche Nachricht|Original Message|Weitergeleitete Nachricht|"
    r"Forwarded message)\s*-{2,}",
    re.IGNORECASE,
)
# "Am 01.01.2026 um 10:00 schrieb Anna:" / "On Mon, ... wrote:"
_QUOTE_INTRO_RE = re.compile(
    r"^\s*(Am\s.+\sschrieb\s.+:|On\s.+\swrote:)\s*$", re.IGNORECASE
)
# Outlook-Kopfblock: Zeile "Von:" gefolgt von "Gesendet:"/"An:" -- Beginn der
# eingeklappten Vorgaenger-Mail auch ohne Trenner-Strich.
_OUTLOOK_HEADER_RE = re.compile(
    r"^\s*(Von|From)\s*:\s*.+", re.IGNORECASE
)
_OUTLOOK_HEADER_FOLLOWUP_RE = re.compile(
    r"^\s*(Gesendet|Sent|An|To|Datum|Date)\s*:\s*.+", re.IGNORECASE
)


def _is_tracking_pixel(img) -> bool:
    """1x1-Bilder (Read-Receipt-/Tracking-Pixel) -- kein Body-Inhalt."""
    for attr in ("width", "height"):
        value = (img.get(attr) or "").strip().lower().rstrip("px")
        if value in ("0", "1"):
            return True
    style = img.get("style", "")
    return bool(re.search(r"(width|height)\s*:\s*[01]px", style, re.IGNORECASE))


def html_to_text(html: str) -> str:
    """Entruempelt HTML und liefert den sichtbaren Klartext.

    Reihenfolge: Skripte/Styles/Kommentare raus, dann Tracking-Pixel,
    versteckte Preheader und zitierte Verlaeufe entfernen, dann Bloecke/
    Zeilenumbrueche in echte Newlines uebersetzen und den Text ziehen.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for img in soup.find_all("img"):
        if _is_tracking_pixel(img):
            img.decompose()
    for element in soup.find_all(style=_HIDDEN_STYLE_RE):
        element.decompose()
    for element in soup.find_all(attrs={"hidden": True}):
        element.decompose()
    for selector in _QUOTE_SELECTORS:
        name = selector.get("name")
        attrs = selector.get("attrs", {})
        for element in soup.find_all(name, attrs=attrs):
            element.decompose()

    # Bloecke/<br> -> Zeilenumbrueche, damit get_text die Struktur nicht
    # zu einem einzigen Absatz verklebt.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4"]):
        block.append("\n")

    return soup.get_text()


def _truncate_at_quote(text: str) -> str:
    """Schneidet Zitat-Verlauf/Signatur ab dem ersten Marker ab.

    Alles ab dem ersten Signatur-/Zitat-Marker gehoert zur Signatur oder
    zur zitierten Vorgaenger-Mail und wuerde den Index mit Duplikaten aus
    der Antwortkette fluten -- also verwerfen.
    """
    lines = text.splitlines()
    kept: list[str] = []
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        stripped = line.strip()
        if (
            _SIGNATURE_LINE_RE.match(line)
            or _MOBILE_SIGNATURE_RE.match(stripped)
            or _ORIGINAL_MESSAGE_RE.match(stripped)
            or _QUOTE_INTRO_RE.match(stripped)
        ):
            break
        if _OUTLOOK_HEADER_RE.match(stripped):
            following = "\n".join(lines[index + 1 : index + 4])
            if _OUTLOOK_HEADER_FOLLOWUP_RE.search(following):
                break
        if stripped.startswith(">"):
            # Zitierte Zeile (Text-Mail-Verlauf) -- ueberspringen, nicht
            # zwingend Ende, aber nie Teil der Antwort.
            continue
        kept.append(line)
    return "\n".join(kept)


def _collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_body(raw: str, content_type: str) -> str:
    """Roher Body (HTML oder Text) -> bereinigter Klartext.

    `content_type` ist der MIME-Typ des Body-Parts (z. B. `text/html`
    oder `text/plain`); nur bei HTML laeuft der BeautifulSoup-Schritt.
    """
    if not raw:
        return ""
    text = html_to_text(raw) if "html" in (content_type or "").lower() else raw
    text = _truncate_at_quote(text)
    return _collapse_blank_lines(text)


def has_substance(cleaned_text: str, *, min_words: int | None = None) -> bool:
    """Traegt der bereinigte Body genug Inhalt, um gefuellt zu werden?

    Schwelle ist die konfigurierbare Mindest-Wortzahl
    (`FINDUS_MAIL_BODY_MIN_WORDS`). Darunter bleibt das Leitdokument eine
    duenne Huelle (nur Metadaten, kein Body-Text/PDF/Embedding).
    """
    if min_words is None:
        min_words = getattr(settings, "FINDUS_MAIL_BODY_MIN_WORDS", 8)
    return len(cleaned_text.split()) >= min_words


def _header_lines(metadata: dict) -> list[tuple[str, str]]:
    """Metadaten-Kopf als (Label, Wert)-Paare -- Basis fuer Index-Text und
    PDF-Kopf, damit beide denselben Kopf zeigen."""
    pairs = [
        ("Betreff", metadata.get("mail_subject", "")),
        ("Von", metadata.get("mail_from", "")),
        ("An", metadata.get("mail_to", "")),
        ("Datum", metadata.get("mail_date", "")),
    ]
    return [(label, str(value)) for label, value in pairs if value]


def build_index_text(metadata: dict, body_text: str) -> str:
    """Metadaten-Kopf (Betreff/Von/An/Datum) + bereinigter Body -> Index-Text.

    Der Kopf macht die semantische Suche treffsicherer (Anforderung #3):
    Absender/Betreff sind oft der eigentliche Suchbegriff.
    """
    head = "\n".join(f"{label}: {value}" for label, value in _header_lines(metadata))
    return f"{head}\n\n{body_text}".strip() if head else body_text.strip()


def _pdf_safe(text: str) -> str:
    """fpdf2-Kernschriften koennen nur cp1252 (siehe `core_fonts_encoding`
    unten) -- nicht abbildbare Zeichen (Emoji, nicht-lateinische Schrift)
    durch '?' ersetzen, statt beim PDF-Bau zu crashen. Umlaute, Euro und
    typografische Zeichen (Gedankenstrich, Anfuehrungszeichen) bleiben; die
    volle Unicode-Fassung steckt ohnehin im Index-Text, nicht im PDF."""
    return text.encode("cp1252", errors="replace").decode("cp1252")


def render_body_pdf(metadata: dict, body_text: str) -> bytes:
    """Bereinigter Body -> lesbares PDF (Metadaten-Kopf + Body).

    Verhaelt sich danach wie jedes andere Dokument (Inline-Vorschau,
    gleiche Pipeline). Die Kennzeichnung "aus E-Mail-Text erzeugt" steht
    als Fusszeile im PDF; die Rolle traegt zusaetzlich das Document
    (`kind=mail_body`, Badge im Detail).
    """
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    # Jede multi_cell soll auf einer eigenen Zeile am linken Rand fortsetzen
    # -- ohne das laesst fpdf2 den Cursor am rechten Rand stehen, sodass die
    # naechste multi_cell(0, ...) keine Breite mehr uebrig haette.
    def line(text: str, height: float = 6) -> None:
        pdf.multi_cell(0, height, _pdf_safe(text) or " ", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf = FPDF(format="A4")
    # cp1252 statt der fpdf2-Vorgabe latin-1: deckt zusaetzlich Euro,
    # Gedankenstriche und typografische Anfuehrungszeichen ab, die in
    # echten Mails haeufig vorkommen (siehe `_pdf_safe`).
    pdf.core_fonts_encoding = "cp1252"
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    subject = metadata.get("mail_subject") or "E-Mail"
    pdf.set_font("Helvetica", style="B", size=14)
    line(subject, height=8)
    pdf.ln(2)

    pdf.set_font("Helvetica", size=10)
    for label, value in _header_lines(metadata):
        if label == "Betreff":
            continue
        line(f"{label}: {value}")
    pdf.ln(2)

    pdf.set_draw_color(200, 200, 200)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", size=11)
    for paragraph in body_text.split("\n"):
        line(paragraph)

    pdf.set_y(-15)
    pdf.set_font("Helvetica", style="I", size=8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 6, _pdf_safe("Automatisch aus dem E-Mail-Text erzeugt (Findus)."))

    output = pdf.output()
    return bytes(output) if not isinstance(output, (bytes, bytearray)) else bytes(output)
