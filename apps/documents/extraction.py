"""Extraction cascade: `Document.original_file` -> `text_content` + a
Markdown cache, the first half of document processing (#1009).

Fallback cascade, cheapest and most reliable stage first (see the
issue's "Eskalations-Trigger" and Architektur.md, "Vision & Zweck"):

  1. Text-layer -- PDF text layer / Office text, extracted directly.
  2. OCR -- scanned PDF pages or images that still carry print text.
  3. Vision AI -- `describe_image()` (apps.ai.providers, #1011): photos,
     diagrams, handwriting, forms, or anything stage 1/2 left empty or
     below the length/confidence threshold.

Escalation happens per PDF page, not per document (`_extract_pdf`), so a
mixed scan -- some pages born-digital, some photographed -- only pays
for OCR/vision on the pages that actually need it. `extraction_method`
records the most expensive stage any page actually used, since that's
the one a reader should trust least for the whole document.
`chunking.process_document()` (#1010) is the next pipeline step and
starts once this one has produced `text_content`.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings
from django.utils import timezone
from django_q.exceptions import TimeoutException

from apps.ai.providers import ImageInput, Usage, VisionProvider, capture_usage, get_vision_provider

from .mime import resolve_mime_type
from .models import Document
from .number_crosscheck import find_unmatched_numbers
from .text_sanitize import clean_text

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_EML_MIME = "message/rfc822"

_VISION_PROMPT = (
    "Transkribiere den gesamten lesbaren Text dieser Dokumentseite "
    "moeglichst wortgetreu. Zeigt die Seite stattdessen ein Foto, "
    "Diagramm, Formular oder Handschrift ohne durchgehend maschinenlesbaren "
    "Text, beschreibe ihren Inhalt praezise, damit er als durchsuchbarer "
    "Dokumenttext taugt."
)

# Picks the single `Document.extraction_method` for a multi-page PDF whose
# pages escalated to different stages: the most expensive stage any page
# actually used wins.
_METHOD_RANK = {
    Document.ExtractionMethod.TEXT_LAYER: 0,
    Document.ExtractionMethod.OCR: 1,
    Document.ExtractionMethod.VISION: 2,
}

VisionProviderFactory = Callable[[], VisionProvider]


@dataclass(frozen=True)
class _PageResult:
    text: str
    method: str


@dataclass(frozen=True)
class _OcrOutput:
    text: str
    confidence: float


def _ocr_image(image) -> _OcrOutput:
    """Run Tesseract OCR on a Pillow image and return its text plus the
    mean per-word confidence (0-100). Isolated in its own function so
    tests can patch it out: only the built container image (see
    Dockerfile) ships the `tesseract-ocr` binary this shells out to.
    """
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(
        image, lang=settings.FINDUS_EXTRACTION_OCR_LANGUAGES, output_type=Output.DICT
    )
    words: list[str] = []
    confidences: list[float] = []
    for text, conf in zip(data["text"], data["conf"]):
        if not text.strip():
            continue
        words.append(text)
        conf_value = float(conf)
        if conf_value >= 0:
            confidences.append(conf_value)
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return _OcrOutput(text=" ".join(words), confidence=confidence)


def _render_pdf_page(data: bytes, page_number: int, *, dpi: Optional[int] = None):
    """Rasterize one 1-based PDF page to a Pillow image via poppler (the
    `pdftoppm`/`pdftocairo` binaries pdf2image wraps) -- both the OCR and
    vision stages need pixels, not the PDF's internal text layer.

    `dpi` defaults to the cascade's own (deliberately low) setting; the
    manual vision re-extraction (#1143) passes its own, higher value --
    that DPI is chosen for a single last-resort description, not for
    reading dense text reliably.
    """
    from pdf2image import convert_from_bytes

    images = convert_from_bytes(
        data,
        first_page=page_number,
        last_page=page_number,
        dpi=dpi or settings.FINDUS_EXTRACTION_PDF_RENDER_DPI,
    )
    return images[0]


def _image_to_png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _is_text_sufficient(text: str) -> bool:
    return len(text.strip()) >= settings.FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE


def _is_ocr_sufficient(ocr: _OcrOutput) -> bool:
    return (
        len(ocr.text.strip()) >= settings.FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE
        and ocr.confidence >= settings.FINDUS_EXTRACTION_MIN_OCR_CONFIDENCE
    )


def _describe_with_vision(vision_provider: VisionProvider, image_bytes: bytes) -> str:
    result = vision_provider.describe_image(
        ImageInput(data=image_bytes, mime_type="image/png"), _VISION_PROMPT
    )
    return result.text


def _extract_page_via_cascade(
    *,
    text_layer: str,
    render: Callable[[], object],
    vision_provider_factory: VisionProviderFactory,
) -> _PageResult:
    """Shared escalation logic for one page/image: text-layer, else OCR on
    a rendered bitmap, else vision -- the one place the "only escalate if
    the cheaper stage was empty/weak" trigger lives, used by both the PDF
    and image extractors below.
    """
    if _is_text_sufficient(text_layer):
        return _PageResult(text=text_layer.strip(), method=Document.ExtractionMethod.TEXT_LAYER)

    image = render()
    ocr = _ocr_image(image)
    if _is_ocr_sufficient(ocr):
        return _PageResult(text=ocr.text.strip(), method=Document.ExtractionMethod.OCR)

    vision_provider = vision_provider_factory()
    text = _describe_with_vision(vision_provider, _image_to_png_bytes(image))
    return _PageResult(text=text.strip(), method=Document.ExtractionMethod.VISION)


def _extract_pdf(
    data: bytes, vision_provider_factory: VisionProviderFactory
) -> tuple[list[_PageResult], int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    results = [
        _extract_page_via_cascade(
            text_layer=page.extract_text() or "",
            render=lambda index=index: _render_pdf_page(data, index),
            vision_provider_factory=vision_provider_factory,
        )
        for index, page in enumerate(reader.pages, start=1)
    ]
    return results, page_count


def _extract_image(
    data: bytes, vision_provider_factory: VisionProviderFactory
) -> list[_PageResult]:
    from PIL import Image

    result = _extract_page_via_cascade(
        text_layer="",
        render=lambda: Image.open(io.BytesIO(data)),
        vision_provider_factory=vision_provider_factory,
    )
    return [result]


def _extract_docx(data: bytes) -> list[_PageResult]:
    import docx

    document = docx.Document(io.BytesIO(data))
    text = "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
    return [_PageResult(text=text, method=Document.ExtractionMethod.TEXT_LAYER)]


def _extract_plain_text(data: bytes) -> list[_PageResult]:
    text = data.decode("utf-8", errors="replace")
    return [_PageResult(text=text, method=Document.ExtractionMethod.TEXT_LAYER)]


def _extract_eml(data: bytes) -> list[_PageResult]:
    """Mail-Body -> Text (#1133). Vorrang hat `text/plain`; fehlt er, wird
    der `text/html`-Teil ueber `mail_body.html_to_text` in Klartext
    umgewandelt -- nie rohes HTML in `text_content`. Anders als
    `mail_body.clean_body()` (Ingest der IMAP/Graph-Mails, #1070) werden
    zitierte Vorgaenger-Mails/Signaturen *nicht* abgeschnitten: ein
    hochgeladenes `.eml` ist die Originaldatei, ihr Text soll vollstaendig
    durchsuchbar bleiben (Anforderung #4). Eine kaputte Nachricht liefert
    leeren Text statt einer Exception -- `email.message_from_bytes` mit
    `policy.default` ist bereits selbst defect-tolerant, dieser Fang ist
    nur das letzte Sicherheitsnetz."""
    import email
    import email.policy

    from apps.ingest.mail_body import html_to_text

    content, content_type = "", ""
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
        body_part = msg.get_body(preferencelist=("plain", "html"))
        if body_part is not None:
            content = body_part.get_content() or ""
            content_type = body_part.get_content_type()
    except Exception:
        logger.exception("Extraktion: EML-Body konnte nicht gelesen werden")

    text = html_to_text(content) if "html" in content_type.lower() else content
    return [_PageResult(text=text, method=Document.ExtractionMethod.TEXT_LAYER)]


def _dispatch(
    data: bytes, mime_type: str, vision_provider_factory: VisionProviderFactory
) -> tuple[list[_PageResult], int]:
    if mime_type == _PDF_MIME:
        return _extract_pdf(data, vision_provider_factory)
    if mime_type.startswith("image/"):
        return _extract_image(data, vision_provider_factory), 1
    if mime_type == _DOCX_MIME:
        return _extract_docx(data), 1
    if mime_type == _EML_MIME:
        return _extract_eml(data), 1
    if mime_type.startswith("text/"):
        return _extract_plain_text(data), 1
    raise ValueError(
        f"Extraktion: nicht unterstuetzter Dateityp '{mime_type}'. "
        "Unterstuetzt werden PDF, Bilder (PNG/JPG/TIFF), Word (DOCX), "
        "Textdateien und E-Mails (EML)."
    )


def _detect_language(text: str) -> str:
    # langdetect needs a handful of words to be reliable at all; below
    # that it either raises or returns noise, so treat it as unknown
    # rather than guessing.
    if len(text.strip()) < 20:
        return ""
    from langdetect import LangDetectException, detect

    try:
        return detect(text)
    except LangDetectException:
        return ""


def _resolve_method(results: list[_PageResult]) -> str:
    if not results:
        return Document.ExtractionMethod.TEXT_LAYER
    return max((r.method for r in results), key=lambda method: _METHOD_RANK[method])


def build_markdown(title: str, page_texts: list[str]) -> str:
    """Render extracted per-page text as the `Document.markdown` cache --
    a fast, low-fidelity view of the original that doesn't require
    re-rendering the source PDF/image on every page load.
    """
    lines = [f"# {title}", ""]
    if len(page_texts) > 1:
        for index, text in enumerate(page_texts, start=1):
            lines.append(f"## Seite {index}")
            lines.append("")
            lines.append(text.strip() or "_Kein Text erkannt._")
            lines.append("")
    else:
        body = page_texts[0].strip() if page_texts else ""
        lines.append(body or "_Kein Text erkannt._")
    return "\n".join(lines).strip() + "\n"


def extract_document(
    document_id: int, *, vision_provider: Optional[VisionProvider] = None
) -> Document:
    """Run the extraction cascade against `document.original_file`, and on
    success populate `text_content`/`markdown`/`extraction_method` plus
    `language`/`page_count` in `metadata`.

    `processing_status` becomes `extracting` while this runs. On success
    it is deliberately left at `extracting` -- `process_document()`
    (#1010) flips it to `embedding` the moment that task starts, so the
    two stages never disagree about which one is "current". On failure
    it becomes `failed` with `processing_error` set, and the exception is
    re-raised so Django-Q sees the task as failed too (mirrors
    `process_document()`'s own contract).
    """
    document = Document.objects.get(pk=document_id)
    document.processing_status = Document.ProcessingStatus.EXTRACTING
    document.processing_error = ""
    document.save(update_fields=["processing_status", "processing_error", "updated_at"])

    try:
        document.original_file.open("rb")
        try:
            data = document.original_file.read()
        finally:
            document.original_file.close()

        # MIME robust aus dem Inhalt bestimmen, nicht blind aus den beim
        # Ingest gespeicherten Metadaten (#1077): so wird ein als
        # `octet-stream` eingeliefertes PDF beim (Re-)Processing als
        # `application/pdf` erkannt statt abgelehnt. Der normalisierte Typ
        # wird zugleich in `metadata.mime_type` zurueckgeschrieben.
        mime_type = resolve_mime_type(
            data,
            filename=document.original_filename,
            declared=document.metadata.get("mime_type", ""),
        )

        vision_provider_factory: VisionProviderFactory = (
            (lambda: vision_provider) if vision_provider is not None else get_vision_provider
        )
        results, page_count = _dispatch(data, mime_type, vision_provider_factory)

        text_content = clean_text("\n\n".join(r.text for r in results if r.text).strip())

        document.text_content = text_content
        document.markdown = clean_text(build_markdown(document.title, [r.text for r in results]))
        document.extraction_method = _resolve_method(results)
        document.metadata = {
            **document.metadata,
            "mime_type": mime_type,
            "language": _detect_language(text_content),
            "page_count": page_count,
            # Extraktions-Provenienz fuer den "Inhalt"-Tab (#1142): wann
            # `text_content` zuletzt aus dem Original erzeugt wurde --
            # anders als `updated_at` bleibt das stabil, auch wenn eine
            # spaetere Analyse/Einbettung das Dokument erneut speichert.
            "extracted_at": timezone.now().isoformat(),
        }
        # Ein regulaerer (Re-)Extract ersetzt `text_content` unabhaengig
        # von einer frueheren manuellen KI-Vision-Neuextraktion (#1143) --
        # deren Provenienz-Felder muessen mit zurueckgesetzt werden, sonst
        # behauptet der "Inhalt"-Tab weiterhin einen Vision-Lauf fuer Text,
        # der laengst wieder ueberschrieben wurde. `content_format` faellt
        # dabei auf `text` zurueck (#1149): die Kaskade erzeugt kein
        # strukturerhaltendes Markdown, und `vision_reextraction_fingerprint`
        # zurueck auf leer macht einen spaeteren Klick auf "Mit KI-Vision neu
        # extrahieren" wieder zu einem echten Lauf statt eines Idempotenz-
        # Treffers gegen eine Datei, deren Inhalt inzwischen ueberschrieben ist.
        document.vision_reextraction_status = Document.VisionReextractionStatus.NONE
        document.vision_reextraction_error = ""
        document.vision_reextraction_completed_at = None
        document.vision_reextraction_pages_processed = None
        document.vision_reextraction_pages_total = None
        document.vision_reextraction_truncated = False
        document.vision_reextraction_pages_failed = None
        document.vision_reextraction_model = ""
        document.vision_reextraction_model_version = ""
        document.vision_reextraction_fingerprint = ""
        document.vision_reextraction_number_check = {}
        document.content_format = Document.ContentFormat.TEXT
        document.save(
            update_fields=[
                "text_content",
                "markdown",
                "extraction_method",
                "content_format",
                "metadata",
                "vision_reextraction_status",
                "vision_reextraction_error",
                "vision_reextraction_completed_at",
                "vision_reextraction_pages_processed",
                "vision_reextraction_pages_total",
                "vision_reextraction_truncated",
                "vision_reextraction_pages_failed",
                "vision_reextraction_model",
                "vision_reextraction_model_version",
                "vision_reextraction_fingerprint",
                "vision_reextraction_number_check",
                "updated_at",
            ]
        )
    except Exception as exc:
        logger.exception("Extraktion fehlgeschlagen fuer Document %s", document_id)
        document.processing_status = Document.ProcessingStatus.FAILED
        document.processing_error = str(exc)
        document.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise

    return document


# -- Manuelle KI-Vision-Neuextraktion (#1143, Markdown-Ausgabe #1149) -------
#
# Anders als die automatische Kaskade oben eskaliert dieser Weg NICHT je
# Seite (Text-Layer -> OCR -> Vision) -- er erzwingt Vision fuer jede Seite,
# unabhaengig davon, ob eine billigere Stufe ausgereicht haette. Gedacht als
# bewusst ausgeloester zweiter Anlauf fuer Dokumente, bei denen Text-Layer/
# OCR bereits verstuemmelten oder unvollstaendigen Text geliefert haben --
# teurer und langsamer, deshalb nie automatisch (siehe CLAUDE.md).
#
# Ausgabe-Kontrakt (#1149): das Modell liefert reine Abschrift als Markdown,
# keine Zusammenfassung/Interpretation. Tabellarische Bereiche werden zu
# Markdown-Tabellen (eine Zeile je fachlicher Zeile -- Bezeichnung, Wert,
# Referenz und Einheit bleiben zusammen, statt in vier getrennten Spalten-
# Listen zu zerfallen, der Grund fuer dieses Ticket ueberhaupt). Handschrift/
# Stempel/Markierungen stehen gesammelt in einem eigenen Abschnitt, nie im
# Fliesstext oder einer Tabelle vermischt. Unsichere Lesungen werden
# ausdruecklich als unsicher markiert statt geraten, leere/unlesbare Seiten
# als solche ausgewiesen statt erfunden oder wortlos uebersprungen. Das
# Ergebnis ersetzt `text_content` (nicht nur `document.markdown`, die
# unveraendert daneben bestehende Low-Fidelity-Vorschau) UND setzt
# `content_format = markdown` -- Struktur, die es nicht bis in die Chunks
# schafft, hilft dem Retrieval nicht (siehe `chunking.chunk_markdown`).
_VISION_MARKDOWN_PROMPT_VERSION = "1"

_VISION_MARKDOWN_PROMPT = (
    "Transkribiere den Inhalt dieser Dokumentseite als Markdown. Reine "
    "Abschrift: nichts zusammenfassen, kommentieren, interpretieren oder "
    "sinnvoll ergaenzen -- gib nur wieder, was auf der Seite steht.\n\n"
    "Regeln:\n"
    "- Tabellarische Bereiche (z. B. Kontoauszug, Rechnung, Laborbefund) "
    "als Markdown-Tabelle wiedergeben: eine Zeile je fachlicher Zeile, "
    "Bezeichnung, Wert, Referenzbereich und Einheit gehoeren in dieselbe "
    "Tabellenzeile, nicht in getrennte Listen.\n"
    "- Handschriftliche Vermerke, Stempel oder Markierungen NICHT in den "
    "Fliesstext oder eine Tabelle mischen, sondern gesammelt in einen "
    "eigenen Abschnitt mit der Ueberschrift 'Handschriftliche Vermerke'.\n"
    "- Unsichere Lesungen woertlich als '[unsicher: ...]' kennzeichnen "
    "statt zu raten.\n"
    "- Ist die Seite leer, ueberbelichtet oder unlesbar, schreibe genau "
    "das ('_Seite leer oder nicht lesbar._') statt Inhalt zu erfinden oder "
    "die Seite wortlos zu ueberspringen.\n"
    "- Keinen umschliessenden Codeblock um die Antwort setzen (keine ```)."
)

_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)

_PLACEHOLDER_PAGE_FAILED = "_Seite konnte technisch nicht verarbeitet werden._"


def _strip_code_fence(text: str) -> str:
    """Manche Vision-Modelle umschliessen ihre Markdown-Antwort trotz
    Anweisung mit einem Codeblock (```markdown ... ```). Unveraendert
    uebernommen wuerde `render_markdown` die ganze Seite als woertlichen
    Quelltext darstellen statt ihre Tabellen zu rendern -- also wird ein
    Codeblock, der die *gesamte* Antwort umschliesst, entfernt. Ein
    Codeblock, der nur einen Teil der Antwort umfasst, bleibt unangetastet
    (koennte fachlich gemeint sein, z. B. eine zitierte IBAN-Zeile).
    """
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text


def vision_markdown_fingerprint(document: Document) -> str:
    """Idempotenz-Schluessel fuer die Markdown-Neuextraktion (#1149):
    `sha256` der Originaldatei plus Prompt-Version. Bewusst ohne das
    verwendete Modell (siehe `Document.vision_reextraction_fingerprint`) --
    ein Modellwechsel soll nicht automatisch den ganzen Bestand neu durch
    Vision schicken.
    """
    return f"{document.sha256}:{_VISION_MARKDOWN_PROMPT_VERSION}"


def _page_count_for_vision(data: bytes, mime_type: str) -> int:
    if mime_type == _PDF_MIME:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    return 1


def _render_page_for_vision(data: bytes, mime_type: str, page_number: int):
    if mime_type == _PDF_MIME:
        return _render_pdf_page(
            data, page_number, dpi=settings.FINDUS_VISION_REEXTRACT_PDF_RENDER_DPI
        )
    from PIL import Image

    return Image.open(io.BytesIO(data))


def start_vision_reextraction(document: Document) -> None:
    """Markiert das Dokument synchron als `running` (analog
    `long_summary.start_document_long_summary`), damit die UI ab dem Klick
    den Spinner zeigt statt bis zum Anlaufen des Workers noch den alten
    Stand zu behaupten. `text_content` bleibt dabei unangetastet -- er ist
    bis zu einem erfolgreichen Ergebnis weiterhin die beste verfuegbare
    Auskunft.
    """
    document.vision_reextraction_status = Document.VisionReextractionStatus.RUNNING
    document.vision_reextraction_error = ""
    document.vision_reextraction_run_started_at = timezone.now()
    document.save(
        update_fields=[
            "vision_reextraction_status",
            "vision_reextraction_error",
            "vision_reextraction_run_started_at",
        ]
    )


def expire_vision_reextraction_if_stalled(document: Document) -> None:
    """Netz gegen einen Job, der spurlos verschwindet (Worker-Neustart,
    OOM-Kill), bevor `reextract_document_with_vision()` den eigenen
    except-Block *oder* der Django-Q-`hook`
    (`tasks.reextract_document_with_vision_hook`) erreicht -- dasselbe
    Prinzip wie `long_summary.expire_document_long_summary_if_stalled`.
    """
    if document.vision_reextraction_status != Document.VisionReextractionStatus.RUNNING:
        return
    started = document.vision_reextraction_run_started_at
    if started is None:
        return
    age_seconds = (timezone.now() - started).total_seconds()
    if age_seconds < settings.FINDUS_VISION_REEXTRACT_POLL_TIMEOUT_SECONDS:
        return
    document.vision_reextraction_status = Document.VisionReextractionStatus.FAILED
    document.vision_reextraction_error = (
        "Der Hintergrundjob hat sich nicht zurückgemeldet (Zeitüberschreitung "
        "beim Warten auf ein Ergebnis)."
    )
    document.save(update_fields=["vision_reextraction_status", "vision_reextraction_error"])


def _pdf_text_layer(data: bytes, pages_to_process: int) -> str:
    """Text-Layer der ersten `pages_to_process` Seiten eines PDFs (#1149) --
    dieselbe Quelle wie Stufe 1 der automatischen Kaskade, hier aber als
    Pruefer fuer die Vision-Transkription statt als deren Ersatz. Nur die
    tatsaechlich verarbeiteten Seiten zaehlen, nicht das ganze Dokument
    (bei einer Seitenobergrenze waeren die ueberzaehligen Seiten sonst
    faelschlich als "Zahl ohne Entsprechung" auffaellig).
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages[:pages_to_process])


def reextract_document_with_vision(
    document_id: int, *, vision_provider: Optional[VisionProvider] = None, force: bool = False
) -> Document:
    """Erzwingt die Vision-Stufe fuer jede Seite von `document.original_file`
    (#1143) und ersetzt `text_content`/`markdown`/`extraction_method` bei
    Erfolg mit strukturerhaltendem Markdown (#1149, siehe Kommentar oben an
    `_VISION_MARKDOWN_PROMPT`) -- ein bewusst ausgeloester, kostenpflichtiger
    zweiter Anlauf, keine automatische Eskalation.

    Idempotent: ist `force=False` (Default) und deckt sich
    `vision_markdown_fingerprint(document)` mit dem zuletzt gespeicherten
    `vision_reextraction_fingerprint`, macht der Lauf keinen Modellaufruf --
    dieselbe Datei wurde bereits vollstaendig erfolgreich transkribiert.
    `force=True` (der "erneut ausfuehren"-Weg, siehe
    `views.document_vision_reextract`) ueberspringt die Pruefung immer.

    Jede Seite ist einzeln fehlertolerant: schlaegt eine einzelne Seite
    technisch fehl (Provider-Fehler, Rendering), bekommt sie einen
    Platzhalter und der Lauf laeuft mit den uebrigen Seiten weiter -- ein
    Fehler auf einer Seite entwertet die anderen nicht. Erst wenn *jede*
    Seite fehlschlaegt, gilt der gesamte Lauf als gescheitert (derselbe
    Fehlerpfad wie bisher). Der Idempotenz-Fingerabdruck wird nur bei einem
    vollstaendigen Erfolg (keine Seitengrenze erreicht, keine Seite
    fehlgeschlagen) gesetzt, sonst bliebe ein unvollstaendiger Lauf ohne
    `force` fuer immer "aktuell".

    Anders als `extract_document()` legt ein Fehlschlag hier NICHT
    `processing_status` auf `failed` und ruehrt `text_content` nicht an:
    das Dokument war vor dem Klick auf diesen Button bereits fertig
    verarbeitet (sonst waere der Button nicht sichtbar, siehe
    `Document.supports_vision_reextraction`), ein Fehlschlag des
    Zusatzlaufs darf es nicht schlechter dastehen lassen als vorher.
    Terminaler Fehlerfall ist deshalb das eigene
    `vision_reextraction_status`-Feld -- genau die in CLAUDE.md
    ("Hintergrundjobs mit LLM-Aufruf") verlangte Wiederverwendung eines
    vorhandenen Statusfelds, nur eben des dediziert dafuer angelegten,
    nicht der Pipeline-weiten `processing_status`.

    `TimeoutException` (Django-Q, erbt von `SystemExit`) wird aufgezeichnet,
    dann aber weitergereicht, damit Django-Q den Worker-Prozess neu
    startet -- exakt wie bei `long_summary.generate_document_long_summary`.
    Sie entkommt bewusst dem `except Exception` je Seite (SystemExit ist
    keine Exception) und landet ungefangen hier unten. Jeder andere
    Fehler auf Lauf-Ebene (nicht unterstuetzter Dateityp, jede Seite
    gescheitert) wird aufgezeichnet, aber NICHT weitergereicht: der Nutzer
    hat auf einen Knopf gedrueckt und erwartet eine sichtbare Antwort,
    kein stillschweigend fehlgeschlagener Task.
    """
    document = Document.objects.get(pk=document_id)

    if not force and document.sha256:
        current_fingerprint = vision_markdown_fingerprint(document)
        if document.vision_reextraction_fingerprint == current_fingerprint:
            logger.info(
                "KI-Vision-Neuextraktion fuer Document %s uebersprungen: "
                "Datei und Prompt-Version unveraendert (sha256=%s), kein "
                "Modellaufruf",
                document_id,
                document.sha256,
            )
            document.vision_reextraction_status = Document.VisionReextractionStatus.READY
            document.vision_reextraction_error = ""
            document.save(
                update_fields=["vision_reextraction_status", "vision_reextraction_error"]
            )
            return document

    started = time.monotonic()
    usages: list[Usage] = []
    pages_processed = 0
    pages_total = 0
    try:
        document.original_file.open("rb")
        try:
            data = document.original_file.read()
        finally:
            document.original_file.close()

        mime_type = resolve_mime_type(
            data,
            filename=document.original_filename,
            declared=document.metadata.get("mime_type", ""),
        )
        if mime_type != _PDF_MIME and not mime_type.startswith("image/"):
            raise ValueError(
                f"KI-Vision-Neuextraktion: nicht unterstuetzter Dateityp "
                f"'{mime_type}'. Unterstuetzt werden PDF und Bilder."
            )

        pages_total = _page_count_for_vision(data, mime_type)
        max_pages = settings.FINDUS_VISION_REEXTRACT_MAX_PAGES
        pages_to_process = min(pages_total, max_pages)
        truncated = pages_total > max_pages

        provider_factory: VisionProviderFactory = (
            (lambda: vision_provider) if vision_provider is not None else get_vision_provider
        )

        page_texts: list[str] = []
        result_model = ""
        result_version = ""
        pages_failed = 0
        last_page_error: Optional[Exception] = None
        with capture_usage() as usages:
            active_provider = provider_factory()
            for page_number in range(1, pages_to_process + 1):
                try:
                    image = _render_page_for_vision(data, mime_type, page_number)
                    result = active_provider.describe_image(
                        ImageInput(data=_image_to_png_bytes(image), mime_type="image/png"),
                        _VISION_MARKDOWN_PROMPT,
                    )
                    page_texts.append(_strip_code_fence(result.text.strip()))
                    result_model, result_version = result.model, result.version
                except Exception as exc:
                    logger.exception(
                        "KI-Vision-Neuextraktion fuer Document %s: Seite %s "
                        "fehlgeschlagen, wird als Platzhalter markiert",
                        document_id,
                        page_number,
                    )
                    page_texts.append(_PLACEHOLDER_PAGE_FAILED)
                    pages_failed += 1
                    last_page_error = exc
                pages_processed += 1

        if pages_failed == pages_to_process and last_page_error is not None:
            # Keine Seite hat ein brauchbares Ergebnis geliefert -- der
            # gesamte Lauf gilt als gescheitert, mit der zuletzt
            # aufgetretenen Fehlermeldung (dieselbe, die vor #1149 ohne
            # Seiten-Fehlertoleranz direkt propagiert waere).
            raise last_page_error

        content_markdown = clean_text(build_markdown(document.title, page_texts))

        number_check = {"performed": False, "unmatched": []}
        if mime_type == _PDF_MIME:
            reference_text = _pdf_text_layer(data, pages_to_process)
            if _is_text_sufficient(reference_text):
                number_check = {
                    "performed": True,
                    "unmatched": find_unmatched_numbers(content_markdown, reference_text),
                }

        document.text_content = content_markdown
        document.markdown = content_markdown
        document.content_format = Document.ContentFormat.MARKDOWN
        document.extraction_method = Document.ExtractionMethod.VISION
        document.metadata = {
            **document.metadata,
            "mime_type": mime_type,
            "language": _detect_language(document.text_content),
            "page_count": pages_total,
            "extracted_at": timezone.now().isoformat(),
        }
        document.vision_reextraction_status = Document.VisionReextractionStatus.READY
        document.vision_reextraction_error = ""
        document.vision_reextraction_completed_at = timezone.now()
        document.vision_reextraction_pages_processed = pages_processed
        document.vision_reextraction_pages_total = pages_total
        document.vision_reextraction_truncated = truncated
        document.vision_reextraction_pages_failed = pages_failed
        document.vision_reextraction_model = result_model
        document.vision_reextraction_model_version = result_version
        document.vision_reextraction_number_check = number_check
        document.vision_reextraction_fingerprint = (
            vision_markdown_fingerprint(document) if not truncated and pages_failed == 0 else ""
        )
        document.save(
            update_fields=[
                "text_content",
                "markdown",
                "content_format",
                "extraction_method",
                "metadata",
                "vision_reextraction_status",
                "vision_reextraction_error",
                "vision_reextraction_completed_at",
                "vision_reextraction_pages_processed",
                "vision_reextraction_pages_total",
                "vision_reextraction_truncated",
                "vision_reextraction_pages_failed",
                "vision_reextraction_model",
                "vision_reextraction_model_version",
                "vision_reextraction_number_check",
                "vision_reextraction_fingerprint",
                "updated_at",
            ]
        )
    except TimeoutException:
        logger.exception(
            "KI-Vision-Neuextraktion fuer Document %s: Task-Zeitbudget "
            "aufgebraucht, waehrend noch auf die KI-Antwort gewartet wurde",
            document_id,
        )
        document.vision_reextraction_status = Document.VisionReextractionStatus.FAILED
        document.vision_reextraction_error = (
            "Zeitüberschreitung – die KI-Antwort kam nicht rechtzeitig zurück. "
            "Bitte erneut versuchen."
        )
        document.save(
            update_fields=["vision_reextraction_status", "vision_reextraction_error"]
        )
        raise
    except Exception as exc:
        logger.exception("KI-Vision-Neuextraktion fehlgeschlagen fuer Document %s", document_id)
        document.vision_reextraction_status = Document.VisionReextractionStatus.FAILED
        document.vision_reextraction_error = str(exc)
        document.save(
            update_fields=["vision_reextraction_status", "vision_reextraction_error"]
        )
    finally:
        duration_seconds = time.monotonic() - started
        prompt_tokens = sum(usage.prompt_tokens for usage in usages)
        completion_tokens = sum(usage.completion_tokens for usage in usages)
        image_tokens = sum(usage.image_tokens for usage in usages)
        logger.info(
            "KI-Vision-Neuextraktion fuer Document %s: %.1fs Laufzeit, %s/%s "
            "Seiten verarbeitet, %s Prompt-/%s Completion-/%s Bild-Tokens",
            document_id,
            duration_seconds,
            pages_processed,
            pages_total,
            prompt_tokens,
            completion_tokens,
            image_tokens,
        )

    return document
