"""Mail-Body-Aufbereitung (#1070): aus dem rohen (HTML-)Body einer Mail
entstehen zwei getrennte Repräsentationen --

  * **Index-Text** (`build_index_text`): Metadaten-Kopf + bereinigter
    Klartext, der ins Embedding geht (semantische Suche).
  * **Ansicht** (`build_body_html_document` + `render_pdf_from_html`): ein
    sauberes, lesbares PDF, das wie jedes andere Dokument im Object
    Storage liegt und inline vorschaubar ist.

Bewusst getrennt, weil beide unterschiedliche Anforderungen haben: der
Index will *einmaligen*, entrümpelten Klartext ohne Zitat-Ketten (sonst
landet derselbe Verlauf über eine Antwortkette n-fach im Vektorraum), die
Ansicht will lesbares Layout.

Das Entrümpeln (`clean_email_html` / `strip_quotes_and_signature`) folgt
dem vorhandenen Prototyp: Skripte/Styles, Tracking-Pixel (1×1-Bilder),
versteckte Preheader (`display:none`), zitierte Verläufe (`blockquote`,
`gmail_quote`, Outlook-Header-Blöcke, „Am … schrieb …:",
„-----Ursprüngliche Nachricht-----") und Signaturen (RFC-Trenner „-- ",
„Gesendet von meinem …") fliegen raus. Die Muster stehen als Konstanten
oben und sind bewusst justierbar.

BeautifulSoup mit dem Stdlib-Parser `html.parser` (kein zusätzliches
`lxml`), passend zum „Stdlib statt Vendor-Dependency"-Stil der Codebase
(vgl. `apps.ai.providers`, `apps.mail.backends`). Das PDF entsteht per
`wkhtmltopdf`-Subprozess -- dasselbe Muster wie die OCR-Kaskade, die
`tesseract`/`poppler` als im Container installierte Binaries aufruft
(siehe Dockerfile), nicht als pip-Abhängigkeit.
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)

# Elemente, die nie Inhalt tragen -- komplett entfernen (inkl. Kinder).
_STRIP_TAGS = ("script", "style", "head", "title", "meta", "link", "noscript")

# CSS-Marker für versteckte Preheader / unsichtbaren Text.
_HIDDEN_STYLE_RE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0|font-size\s*:\s*0)",
    re.IGNORECASE,
)

# Container zitierter Verläufe, die Mail-Clients konsistent auszeichnen.
_QUOTE_CLASS_RE = re.compile(
    r"(gmail_quote|yahoo_quoted|moz-cite-prefix|gmail_extra)", re.IGNORECASE
)
_QUOTE_ID_RE = re.compile(r"(divRplyFwdMsg|appendonsend)", re.IGNORECASE)

# Zeilen, ab denen der Rest der Mail zitierter Verlauf ist. Ab dem ersten
# Treffer wird alles Folgende verworfen (die Zitat-Kette gehört nicht in
# den Index, sonst Duplikate über Antwortketten).
_QUOTE_HEADER_PATTERNS = (
    re.compile(r"^\s*-{2,}\s*(ursprüngliche nachricht|original message)\s*-{2,}", re.IGNORECASE),
    # Outlook-Header-Block: "Von:" bzw. "From:" leitet den zitierten Kopf ein.
    re.compile(r"^\s*(von|from)\s*:\s*.+", re.IGNORECASE),
    re.compile(r"^\s*(gesendet|sent)\s*:\s*.+", re.IGNORECASE),
    # "Am 01.02.2026 um 10:00 schrieb Max:" / "On ... wrote:"
    re.compile(r"^\s*am\s+.+\s+schrieb.*:\s*$", re.IGNORECASE),
    re.compile(r"^\s*on\s+.+\s+wrote\s*:\s*$", re.IGNORECASE),
    # Zitierte Zeilen ("> ...").
    re.compile(r"^\s*>+"),
)

# Zeilen, ab denen der Rest der Mail Signatur ist (Trenner + typische
# Handy-Footer). Alles ab dem Treffer wird verworfen.
_SIGNATURE_PATTERNS = (
    # RFC-3676-Signatur-Trenner: exakt "-- " (mit oder ohne Trailing-Space).
    re.compile(r"^--\s*$"),
    re.compile(r"^\s*(gesendet|sent)\s+(von|from)\s+mein.*", re.IGNORECASE),
    re.compile(r"^\s*get\s+outlook\s+for\s+", re.IGNORECASE),
)


@dataclass(frozen=True)
class BodyResult:
    """Ergebnis der Aufbereitung eines Mail-Bodys.

    `text` ist der entrümpelte Klartext (für Index/Substanz-Check), `html`
    das bereinigte HTML-Fragment (für die PDF-Ansicht), `word_count` die
    Wortzahl von `text` (Basis des Substanz-Checks).
    """

    text: str
    html: str
    word_count: int


def _is_tracking_pixel(tag) -> bool:
    """1×1-(oder 0-)Pixel-Bild -- klassischer Tracking-Pixel ohne Inhalt."""
    if tag.name != "img":
        return False
    dims = []
    for attr in ("width", "height"):
        value = (tag.get(attr) or "").strip().lower().rstrip("px")
        if value:
            dims.append(value)
    if dims and all(d in ("0", "1") for d in dims):
        return True
    style = (tag.get("style") or "").lower()
    return bool(re.search(r"(width|height)\s*:\s*[01]px", style))


def clean_email_html(raw_html: str) -> str:
    """Entrümpeltes HTML-Fragment aus einem rohen HTML-Body.

    Entfernt Skripte/Styles, Tracking-Pixel, versteckte Preheader und
    zitierte Verläufe. Signaturen werden nicht hier, sondern auf der
    Textseite (`strip_quotes_and_signature`) gekappt -- im HTML sind sie
    zu uneinheitlich ausgezeichnet, um sie verlässlich am DOM zu greifen.
    """
    soup = BeautifulSoup(raw_html or "", "html.parser")

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()

    for tag in list(soup.find_all(True)):
        if not tag.parent:  # bereits mit einem Vorfahren entfernt
            continue
        if _is_tracking_pixel(tag):
            tag.decompose()
            continue
        if _HIDDEN_STYLE_RE.search(tag.get("style") or ""):
            tag.decompose()
            continue
        classes = " ".join(tag.get("class") or [])
        if _QUOTE_CLASS_RE.search(classes) or _QUOTE_ID_RE.search(tag.get("id") or ""):
            tag.decompose()
            continue
        if tag.name == "blockquote":
            tag.decompose()

    body = soup.body or soup
    return body.decode_contents().strip()


def _html_to_text(fragment_html: str) -> str:
    soup = BeautifulSoup(fragment_html or "", "html.parser")
    # <br>/Block-Elemente in Zeilenumbrüche übersetzen, damit der Klartext
    # nicht zu einer einzigen Zeile kollabiert.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    return soup.get_text()


def strip_quotes_and_signature(text: str) -> str:
    """Kappt zitierten Verlauf und Signatur aus einem Klartext-Body.

    Sucht die *früheste* Zeile, ab der der Rest Zitat/Signatur ist, und
    verwirft alles ab dort -- so landet eine Antwortkette nur einmal (der
    oberste, neue Teil) im Index statt n-fach über die Kette.
    """
    lines = text.splitlines()
    cut = len(lines)
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in _QUOTE_HEADER_PATTERNS):
            cut = index
            break
        if any(pattern.match(line) for pattern in _SIGNATURE_PATTERNS):
            cut = index
            break
    kept = lines[:cut]
    # Leerraum normalisieren: geräteweite Umbrüche/Whitespace zusammenfassen,
    # führende/abschließende Leerzeilen entfernen.
    normalized = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in kept]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(normalized))
    return collapsed.strip()


def _count_words(text: str) -> int:
    return len(re.findall(r"\w+", text, flags=re.UNICODE))


def prepare_body(raw: Optional[str], content_type: str) -> BodyResult:
    """Bereitet einen rohen Mail-Body auf: `content_type` ist der MIME-Typ
    des Bodys (`text/html` oder `text/plain`). Liefert Klartext + HTML +
    Wortzahl. Ein leerer Body ergibt ein leeres Ergebnis (`word_count=0`).
    """
    raw = raw or ""
    if "html" in (content_type or "").lower():
        cleaned_html = clean_email_html(raw)
        text = strip_quotes_and_signature(_html_to_text(cleaned_html))
    else:
        text = strip_quotes_and_signature(raw)
        cleaned_html = _text_to_html(text)
    return BodyResult(text=text, html=cleaned_html, word_count=_count_words(text))


def _text_to_html(text: str) -> str:
    """Klartext -> minimales HTML-Fragment (für die PDF-Ansicht eines
    text/plain-Bodys): jede Zeile escaped, Absätze als <p>."""
    paragraphs = [block.strip() for block in re.split(r"\n{2,}", text) if block.strip()]
    return "".join(
        "<p>" + "<br>".join(html.escape(line) for line in block.splitlines()) + "</p>"
        for block in paragraphs
    )


def build_index_text(
    *, subject: str, sender: str, date: str, body_text: str
) -> str:
    """Metadaten-Kopf (Betreff/Von/Datum) + bereinigter Body-Klartext.

    Der Kopf steht bewusst im Embedding-Text: er macht die semantische
    Suche treffsicherer ("Mail von X zum Thema Y").
    """
    header_lines = []
    if subject:
        header_lines.append(f"Betreff: {subject}")
    if sender:
        header_lines.append(f"Von: {sender}")
    if date:
        header_lines.append(f"Datum: {date}")
    header = "\n".join(header_lines)
    if header and body_text:
        return f"{header}\n\n{body_text}"
    return header or body_text


_PDF_STYLE = """
body { font-family: sans-serif; font-size: 12pt; color: #1a1a1a; margin: 2cm; }
.findus-mail-header { border-bottom: 1px solid #ccc; padding-bottom: 8px;
    margin-bottom: 16px; color: #444; font-size: 10pt; }
.findus-mail-header dt { font-weight: bold; }
.findus-mail-body { line-height: 1.5; }
"""


def build_body_html_document(
    *, subject: str, sender: str, date: str, body_html: str
) -> str:
    """Vollständiges HTML-Dokument (Metadaten-Kopf + bereinigter Body) als
    Vorlage für das PDF-Rendering."""
    header_rows = ""
    for label, value in (("Betreff", subject), ("Von", sender), ("Datum", date)):
        if value:
            header_rows += f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{_PDF_STYLE}</style></head><body>"
        f"<dl class='findus-mail-header'>{header_rows}</dl>"
        f"<div class='findus-mail-body'>{body_html}</div>"
        "</body></html>"
    )


class PdfRenderError(RuntimeError):
    """wkhtmltopdf war nicht erreichbar oder ist fehlgeschlagen."""


def render_pdf_from_html(html_document: str, *, timeout: float = 30.0) -> bytes:
    """Rendert ein HTML-Dokument per `wkhtmltopdf` zu PDF-Bytes.

    Isoliert in einer eigenen Funktion, damit Tests sie patchen können --
    nur das gebaute Container-Image (siehe Dockerfile) bringt das
    `wkhtmltopdf`-Binary mit, gegen das hier per Subprozess gefahren wird
    (`-` als In-/Output = stdin/stdout, kein Temp-File nötig).
    """
    try:
        process = subprocess.run(
            ["wkhtmltopdf", "--quiet", "--encoding", "utf-8", "-", "-"],
            input=html_document.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # Binary nicht installiert
        raise PdfRenderError("wkhtmltopdf ist nicht installiert") from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfRenderError("wkhtmltopdf-Timeout") from exc
    if process.returncode != 0 or not process.stdout:
        raise PdfRenderError(
            f"wkhtmltopdf fehlgeschlagen (rc={process.returncode}): "
            f"{process.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return process.stdout
