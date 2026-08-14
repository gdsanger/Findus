"""Thumbnail-Rendering: `Document.original_file` -> `thumbnail`, ein
Vorschaubild der ersten Seite fuer die Kachelansicht (#1123).

Bewusst beim Ingest im Worker gerendert und als Datei neben dem Original
im Object Storage abgelegt -- nicht on-the-fly beim ersten Abruf: die
Dokumentuebersicht zeigt 20 Kacheln gleichzeitig, 20 synchrone Renderings
pro Seitenaufruf waeren der falsche Ort fuer die CPU-Last. Der Preis ist
etwas Storage plus der Backfill-Command `generate_thumbnails` fuer den
Bestand.

Renderbar sind PDF (erste Seite via `pypdfium2` -- reines Python-Wheel,
keine Poppler-Systemabhaengigkeit) und Bildformate (herunterskaliert via
Pillow). Alles andere (docx, xlsx, zip, eml, …) bekommt kein Thumbnail;
die UI zeigt dort einen Typ-Platzhalter. Ausgabe ist WebP (Fallback PNG,
falls die Pillow-Installation kein WebP kann), laengste Kante
`FINDUS_THUMBNAIL_MAX_EDGE` px -- ein Kachelbild, keine Leseansicht.

Fehlertoleranz wie bei der KI-Analyse (#1020): ein fehlgeschlagenes
Rendering darf die Pipeline nicht anhalten. `generate_thumbnail_for_document`
loggt und laeuft ohne Thumbnail weiter, `generate_thumbnail` selbst wirft
weiter, damit der Backfill-Command echte Fehler zaehlen kann.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.files.base import ContentFile

from .models import Document

logger = logging.getLogger(__name__)

_PDF_MIME = "application/pdf"


def _max_edge() -> int:
    return settings.FINDUS_THUMBNAIL_MAX_EDGE


def _render_pdf_first_page(data: bytes):
    """Rasterize the first page of a PDF to a Pillow image via pypdfium2.

    Renders straight at the target scale (target edge / longest page edge in
    points) instead of at a fixed high DPI and downscaling -- a thumbnail
    never needs more pixels than its longest edge, so this keeps the render
    cheap. `_encode` still caps the result, which also covers the clamp of
    the scale for unusually small/large pages.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        if len(pdf) == 0:
            return None
        page = pdf[0]
        width, height = page.get_size()
        longest = max(width, height) or 1
        # Clamp: never below a tiny positive scale (a giant page would round
        # to 0), never above 3x (a business-card-sized page shouldn't be
        # upscaled into a blurry mess).
        scale = min(max(_max_edge() / longest, 0.1), 3.0)
        return page.render(scale=scale).to_pil()
    finally:
        pdf.close()


def _open_image(data: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(data))


def _encode(image) -> tuple[bytes, str]:
    """Scale `image` down to the max edge and encode it, preferring WebP and
    falling back to PNG if this Pillow build lacks WebP. Returns the bytes
    and the matching file extension.
    """
    if image.mode not in ("RGB", "RGBA"):
        # WebP/PNG can't encode palette ("P") or CMYK directly; RGB(A) also
        # gives a sane thumbnail for greyscale/1-bit scans.
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    edge = _max_edge()
    image.thumbnail((edge, edge))  # in-place, preserves aspect ratio

    for fmt, ext in (("WEBP", "webp"), ("PNG", "png")):
        try:
            buffer = io.BytesIO()
            image.save(buffer, format=fmt)
            return buffer.getvalue(), ext
        except Exception:
            logger.warning("Thumbnail-Encoding als %s fehlgeschlagen, versuche Fallback", fmt)
    raise RuntimeError("Kein unterstuetztes Thumbnail-Format (WebP/PNG) verfuegbar.")


def render_thumbnail(data: bytes, mime_type: str) -> Optional[tuple[bytes, str]]:
    """Render thumbnail bytes for `data` of type `mime_type`, or None if the
    type isn't thumbnail-able / the source has no renderable first page.

    Pure function (no DB, no storage) so tests can exercise the rendering in
    isolation and both the ingest step and the backfill command share it.
    """
    if mime_type == _PDF_MIME:
        image = _render_pdf_first_page(data)
    elif mime_type.startswith("image/"):
        image = _open_image(data)
    else:
        return None
    if image is None:
        return None
    return _encode(image)


def generate_thumbnail(document: Document, *, force: bool = False) -> bool:
    """Render and store the first-page thumbnail for `document`.

    Returns True if a thumbnail was written, False if the step was skipped
    (no original, not thumbnail-able, empty render, or a thumbnail already
    exists and `force` is False -- idempotent for the backfill command).
    Raises on a genuine rendering/storage error, so the Django-Q task
    wrapper resp. the management command can log/count it; the
    fault-tolerant pipeline entry point below swallows that.
    """
    if not document.original_file or not document.is_thumbnailable:
        return False
    if document.thumbnail and not force:
        return False

    document.original_file.open("rb")
    try:
        data = document.original_file.read()
    finally:
        document.original_file.close()

    rendered = render_thumbnail(data, document.mime_type)
    if rendered is None:
        return False
    image_bytes, ext = rendered

    # Re-rendering (`--force`) must not leave the previous file orphaned in
    # storage -- Django never cleans up a replaced FileField on its own.
    if document.thumbnail:
        document.thumbnail.delete(save=False)

    stem = Path(document.original_filename).stem or f"document-{document.pk}"
    document.thumbnail.save(f"{stem}.{ext}", ContentFile(image_bytes), save=False)
    document.save(update_fields=["thumbnail", "updated_at"])
    return True


def generate_thumbnail_for_document(document_id: int, *, force: bool = False) -> None:
    """Fault-tolerant pipeline/queue entry point (#1123).

    A failed thumbnail must never stop ingest -- same principle as the
    KI-Analyse (#1020): log and carry on without a thumbnail rather than
    marking the document `failed`. Loads the document by id (the worker has
    only an id), so it is safe to call from a Django-Q task.
    """
    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        logger.warning("Thumbnail uebersprungen: Document %s existiert nicht mehr", document_id)
        return
    try:
        generate_thumbnail(document, force=force)
    except Exception:
        logger.exception("Thumbnail-Erzeugung fehlgeschlagen fuer Document %s", document_id)
