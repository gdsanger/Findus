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
import time
from dataclasses import dataclass
from typing import Callable, Optional

from django.conf import settings
from django.utils import timezone
from django_q.exceptions import TimeoutException

from apps.ai.providers import ImageInput, Usage, VisionProvider, capture_usage, get_vision_provider

from .mime import resolve_mime_type
from .models import Document
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
        # der laengst wieder ueberschrieben wurde.
        document.vision_reextraction_status = Document.VisionReextractionStatus.NONE
        document.vision_reextraction_error = ""
        document.vision_reextraction_completed_at = None
        document.vision_reextraction_pages_processed = None
        document.vision_reextraction_pages_total = None
        document.vision_reextraction_truncated = False
        document.save(
            update_fields=[
                "text_content",
                "markdown",
                "extraction_method",
                "metadata",
                "vision_reextraction_status",
                "vision_reextraction_error",
                "vision_reextraction_completed_at",
                "vision_reextraction_pages_processed",
                "vision_reextraction_pages_total",
                "vision_reextraction_truncated",
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


# -- Manuelle KI-Vision-Neuextraktion (#1143) --------------------------------
#
# Anders als die automatische Kaskade oben eskaliert dieser Weg NICHT je
# Seite (Text-Layer -> OCR -> Vision) -- er erzwingt Vision fuer jede Seite,
# unabhaengig davon, ob eine billigere Stufe ausgereicht haette. Gedacht als
# bewusst ausgeloester zweiter Anlauf fuer Dokumente, bei denen Text-Layer/
# OCR bereits verstuemmelten oder unvollstaendigen Text geliefert haben --
# teurer und langsamer, deshalb nie automatisch (siehe CLAUDE.md).


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


def reextract_document_with_vision(
    document_id: int, *, vision_provider: Optional[VisionProvider] = None
) -> Document:
    """Erzwingt die Vision-Stufe fuer jede Seite von `document.original_file`
    (#1143) und ersetzt `text_content`/`markdown`/`extraction_method` bei
    Erfolg -- ein bewusst ausgeloester, kostenpflichtiger zweiter Anlauf,
    keine automatische Eskalation.

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
    Jeder andere Fehler wird aufgezeichnet, aber NICHT weitergereicht: der
    Nutzer hat auf einen Knopf gedrueckt und erwartet eine sichtbare
    Antwort, kein stillschweigend fehlgeschlagener Task.
    """
    document = Document.objects.get(pk=document_id)

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
        with capture_usage() as usages:
            active_provider = provider_factory()
            for page_number in range(1, pages_to_process + 1):
                image = _render_page_for_vision(data, mime_type, page_number)
                text = _describe_with_vision(active_provider, _image_to_png_bytes(image))
                page_texts.append(text.strip())
                pages_processed += 1

        if len(page_texts) > 1:
            # Seitengrenzen bleiben im Fliesstext kenntlich (#1143), damit
            # Absaetze nicht ueber Seiten hinweg verkleben -- anders als
            # `build_markdown()`s "## Seite N"-Ueberschriften, die nur fuer
            # die Markdown-Ansicht gedacht sind, nicht fuer `text_content`.
            text_content = "\n\n".join(
                f"--- Seite {index} ---\n\n{text}" for index, text in enumerate(page_texts, start=1)
            )
        else:
            text_content = page_texts[0] if page_texts else ""

        document.text_content = clean_text(text_content.strip())
        document.markdown = clean_text(build_markdown(document.title, page_texts))
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
        document.save(
            update_fields=[
                "text_content",
                "markdown",
                "extraction_method",
                "metadata",
                "vision_reextraction_status",
                "vision_reextraction_error",
                "vision_reextraction_completed_at",
                "vision_reextraction_pages_processed",
                "vision_reextraction_pages_total",
                "vision_reextraction_truncated",
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
