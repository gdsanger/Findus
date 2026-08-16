import datetime
import io
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai.providers.fake import FakeVisionProvider
from config.test_requirements import requires_ocr, requires_pdf_rasterizer

from . import vision_markdown
from .extraction import (
    _OcrOutput,
    _ocr_image,
    build_markdown,
    extract_document,
    extract_vision_markdown,
    reextract_document_with_vision,
)
from .models import Document

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-extraction-media-")

_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _make_pdf(page_texts: list[str | None]) -> bytes:
    """Minimal single/multi-page PDF, one Helvetica text line per page (or
    a blank content stream when `text` is None), for extraction tests --
    avoids pulling in a PDF-generation library just for test fixtures.
    """
    page_objs = []
    content_objs = []
    font_obj_num = 3 + len(page_texts)
    for i, text in enumerate(page_texts):
        content_num = font_obj_num + 1 + i
        stream = b"" if text is None else f"BT /F1 18 Tf 20 700 Td ({text}) Tj ET".encode()
        content_objs.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        page_objs.append(
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
            f"/MediaBox [0 0 612 792] /Contents {content_num} 0 R >>".encode()
        )

    kids = " ".join(f"{3 + i} 0 R" for i in range(len(page_texts)))
    all_objs = (
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode(),
        ]
        + page_objs
        + [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        + content_objs
    )

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for idx, obj in enumerate(all_objs, start=1):
        offsets.append(buf.tell())
        buf.write(f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {len(all_objs) + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(
        f"trailer\n<< /Size {len(all_objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF".encode()
    )
    return buf.getvalue()


_GERMAN_PARAGRAPH = (
    "Dies ist ein deutscher Testsatz mit ausreichend vielen Woertern, "
    "damit die Spracherkennung ein zuverlaessiges Ergebnis liefert."
)


def _make_document(*, filename: str, data: bytes, mime_type: str) -> Document:
    document = Document.objects.create(title="Testdokument", metadata={"mime_type": mime_type})
    document.original_file.save(filename, io.BytesIO(data), save=True)
    return document


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractDocumentTextLayerTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_pdf_with_text_layer_is_extracted_directly(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH])
        document = _make_document(filename="doc.pdf", data=pdf_bytes, mime_type="application/pdf")

        with patch("apps.documents.extraction._ocr_image") as mock_ocr:
            vision_provider = FakeVisionProvider()
            result = extract_document(document.id, vision_provider=vision_provider)

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.TEXT_LAYER)
        self.assertIn("deutscher Testsatz", result.text_content)
        self.assertEqual(result.metadata["page_count"], 1)
        self.assertEqual(result.metadata["language"], "de")
        self.assertEqual(result.processing_status, Document.ProcessingStatus.EXTRACTING)
        self.assertEqual(result.processing_error, "")
        mock_ocr.assert_not_called()
        self.assertEqual(vision_provider.calls, [])

    def test_plain_text_is_extracted_directly(self):
        document = _make_document(
            filename="note.txt", data=_GERMAN_PARAGRAPH.encode("utf-8"), mime_type="text/plain"
        )

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.TEXT_LAYER)
        self.assertEqual(result.text_content, _GERMAN_PARAGRAPH)
        self.assertEqual(result.metadata["page_count"], 1)

    def test_docx_is_extracted_directly(self):
        import docx

        buffer = io.BytesIO()
        docx_document = docx.Document()
        docx_document.add_paragraph(_GERMAN_PARAGRAPH)
        docx_document.save(buffer)
        mime_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        document = _make_document(filename="doc.docx", data=buffer.getvalue(), mime_type=mime_type)

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.TEXT_LAYER)
        self.assertIn("deutscher Testsatz", result.text_content)

    def test_eml_prefers_plain_text_over_html(self):
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = "Hallo"
        message["From"] = "anna@example.com"
        message.set_content(_GERMAN_PARAGRAPH)
        message.add_alternative(f"<p>{_GERMAN_PARAGRAPH}</p><b>fett</b>", subtype="html")
        document = _make_document(
            filename="mail.eml", data=message.as_bytes(), mime_type="message/rfc822"
        )

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.TEXT_LAYER)
        self.assertEqual(result.text_content.strip(), _GERMAN_PARAGRAPH)
        self.assertNotIn("<b>", result.text_content)

    def test_eml_without_plain_text_converts_html_to_readable_text(self):
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = "Nur HTML"
        message["From"] = "anna@example.com"
        message.set_content(
            f"<html><body><p>{_GERMAN_PARAGRAPH}</p></body></html>",
            subtype="html",
        )
        document = _make_document(
            filename="mail.eml", data=message.as_bytes(), mime_type="message/rfc822"
        )

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertIn("deutscher Testsatz", result.text_content)
        self.assertNotIn("<p>", result.text_content)
        self.assertNotIn("<html>", result.text_content)

    def test_eml_keeps_quoted_history_unlike_mail_body_cleaning(self):
        """Anders als `mail_body.clean_body()` (Ingest der IMAP/Graph-Mails)
        schneidet die `.eml`-Extraktion zitierte Vorgaengermails NICHT ab
        (Anforderung #1133/#4) -- die Originaldatei soll vollstaendig
        durchsuchbar bleiben."""
        from email.message import EmailMessage

        body = f"{_GERMAN_PARAGRAPH}\n\n> Am 01.01.2026 schrieb Anna:\n> alter Verlauf hier drin"
        message = EmailMessage()
        message["Subject"] = "Antwort"
        message["From"] = "anna@example.com"
        message.set_content(body)
        document = _make_document(
            filename="mail.eml", data=message.as_bytes(), mime_type="message/rfc822"
        )

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertIn("alter Verlauf hier drin", result.text_content)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
@override_settings(FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE=20, FINDUS_EXTRACTION_MIN_OCR_CONFIDENCE=60)
class ExtractDocumentOcrTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    @requires_pdf_rasterizer("OCR-Eskalation eines gescannten PDF")
    def test_scanned_pdf_escalates_to_ocr_when_text_layer_is_empty(self):
        pdf_bytes = _make_pdf([None])
        document = _make_document(filename="scan.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider()

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text=_GERMAN_PARAGRAPH, confidence=85.0),
        ) as mock_ocr:
            result = extract_document(document.id, vision_provider=vision_provider)

        mock_ocr.assert_called_once()
        self.assertEqual(result.extraction_method, Document.ExtractionMethod.OCR)
        self.assertEqual(result.text_content, _GERMAN_PARAGRAPH)
        self.assertEqual(result.metadata["page_count"], 1)
        self.assertEqual(result.metadata["language"], "de")
        self.assertEqual(vision_provider.calls, [])

    def test_image_with_sufficient_ocr_does_not_escalate_to_vision(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="scan.png", data=buffer.getvalue(), mime_type="image/png"
        )
        vision_provider = FakeVisionProvider()

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text=_GERMAN_PARAGRAPH, confidence=90.0),
        ) as mock_ocr:
            result = extract_document(document.id, vision_provider=vision_provider)

        mock_ocr.assert_called_once()
        self.assertEqual(result.extraction_method, Document.ExtractionMethod.OCR)
        self.assertEqual(result.text_content, _GERMAN_PARAGRAPH)
        self.assertEqual(vision_provider.calls, [])

    @requires_pdf_rasterizer("Vision-Eskalation bei schwacher OCR-Konfidenz")
    def test_weak_ocr_confidence_escalates_to_vision(self):
        pdf_bytes = _make_pdf([None])
        document = _make_document(filename="scan.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Vision-Transkript des Scans.")

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text="ab", confidence=10.0),
        ):
            result = extract_document(document.id, vision_provider=vision_provider)

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(result.text_content, "Vision-Transkript des Scans.")
        self.assertEqual(len(vision_provider.calls), 1)

    @requires_pdf_rasterizer("Vision-Eskalation bei zu kurzem OCR-Text")
    def test_weak_ocr_text_length_escalates_to_vision_even_with_high_confidence(self):
        pdf_bytes = _make_pdf([None])
        document = _make_document(filename="scan.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Vision-Transkript.")

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text="ok", confidence=99.0),
        ):
            result = extract_document(document.id, vision_provider=vision_provider)

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(len(vision_provider.calls), 1)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractDocumentVisionTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_image_without_text_gets_a_usable_vision_description(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="blue").save(buffer, format="JPEG")
        document = _make_document(
            filename="photo.jpg", data=buffer.getvalue(), mime_type="image/jpeg"
        )
        vision_provider = FakeVisionProvider(reply="Ein Foto eines Buerogebaeudes mit Hausnummer 12.")

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text="", confidence=0.0),
        ):
            result = extract_document(document.id, vision_provider=vision_provider)

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(result.text_content, "Ein Foto eines Buerogebaeudes mit Hausnummer 12.")
        self.assertEqual(len(vision_provider.calls), 1)
        image, prompt = vision_provider.calls[0]
        self.assertEqual(image.mime_type, "image/png")
        self.assertTrue(prompt)

    @requires_pdf_rasterizer("gemischtes mehrseitiges PDF")
    def test_multi_page_pdf_reports_the_most_expensive_stage_used(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, None])
        document = _make_document(filename="mixed.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Beschreibung der zweiten Seite.")

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text="", confidence=0.0),
        ):
            result = extract_document(document.id, vision_provider=vision_provider)

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(result.metadata["page_count"], 2)
        self.assertIn("deutscher Testsatz", result.text_content)
        self.assertIn("Beschreibung der zweiten Seite.", result.text_content)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractDocumentFailureTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_unsupported_mime_type_marks_failed(self):
        document = _make_document(
            filename="archive.zip", data=b"PK\x03\x04", mime_type="application/zip"
        )

        with self.assertRaises(ValueError):
            extract_document(document.id, vision_provider=FakeVisionProvider())

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)
        self.assertIn("application/zip", document.processing_error)

    def test_pdf_with_octet_stream_mime_is_recognised_and_extracted(self):
        # Scanner/Upload-Wege melden fuer echte PDFs oft den generischen
        # `application/octet-stream` (#1077). Der Inhalt (%PDF) muss den
        # gespeicherten Typ ueberstimmen, statt abgelehnt zu werden -- auch
        # bei Dateinamen mit Leerzeichen ("001 1.pdf").
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH])
        document = _make_document(
            filename="20260708130035_001 1.pdf",
            data=pdf_bytes,
            mime_type="application/octet-stream",
        )

        with patch("apps.documents.extraction._ocr_image") as mock_ocr:
            result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(result.extraction_method, Document.ExtractionMethod.TEXT_LAYER)
        self.assertIn("deutscher Testsatz", result.text_content)
        self.assertEqual(result.processing_status, Document.ProcessingStatus.EXTRACTING)
        self.assertEqual(result.processing_error, "")
        # Der normalisierte Typ wird zurueckgeschrieben, nicht der octet-stream-Wert.
        self.assertEqual(result.metadata["mime_type"], "application/pdf")
        mock_ocr.assert_not_called()

    def test_truly_unsupported_file_has_clear_error(self):
        document = _make_document(
            filename="daten.bin", data=b"\x00\x01\x02\x03rohdaten", mime_type=""
        )

        with self.assertRaises(ValueError):
            extract_document(document.id, vision_provider=FakeVisionProvider())

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)
        self.assertIn("nicht unterstuetzter Dateityp", document.processing_error)

    @requires_pdf_rasterizer("Fehlerpfad des Vision-Providers")
    def test_vision_provider_failure_marks_failed_and_reraises(self):
        pdf_bytes = _make_pdf([None])
        document = _make_document(filename="scan.pdf", data=pdf_bytes, mime_type="application/pdf")

        class _RaisingVisionProvider:
            def describe_image(self, image, prompt):
                raise RuntimeError("vision boom")

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text="", confidence=0.0),
        ):
            with self.assertRaisesMessage(RuntimeError, "vision boom"):
                extract_document(document.id, vision_provider=_RaisingVisionProvider())

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)
        self.assertIn("vision boom", document.processing_error)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractDocumentNulByteTests(TestCase):
    """#1061: some scanners/PDF generators emit NUL bytes in the text layer,
    which Postgres `text` columns reject outright (`psycopg.DataError`).
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_text_with_embedded_nul_bytes_is_saved_successfully(self):
        text_with_nul = _GERMAN_PARAGRAPH + "\x00 nach dem Nullbyte\x00."
        document = _make_document(
            filename="note.txt", data=text_with_nul.encode("utf-8"), mime_type="text/plain"
        )

        result = extract_document(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(result.processing_status, Document.ProcessingStatus.EXTRACTING)
        self.assertNotIn("\x00", result.text_content)
        self.assertNotIn("\x00", result.markdown)
        self.assertIn("nach dem Nullbyte", result.text_content)


@requires_ocr("Direkttest der OCR-Stufe")
class OcrImageTests(SimpleTestCase):
    """`_ocr_image` ist die einzige Stelle, die wirklich das `tesseract`-Binary
    aufruft -- in allen Kaskaden-Tests oben ist sie gemockt, damit die
    Eskalationslogik ohne Systemwerkzeug pruefbar bleibt. Damit die Anbindung
    an das Binary trotzdem abgedeckt ist, prueft diese Klasse sie direkt; ohne
    `tesseract` bzw. ohne die konfigurierten Sprachdaten wird sie mit
    sichtbarem Grund uebersprungen (#1145).
    """

    @staticmethod
    def _image_with_text(text: str):
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (900, 200), "white")
        ImageDraw.Draw(image).text(
            (20, 60), text, fill="black", font=ImageFont.load_default(size=64)
        )
        return image

    def test_printed_text_is_recognised_with_a_confidence(self):
        result = _ocr_image(self._image_with_text("Rechnung 2026"))

        self.assertIn("Rechnung", result.text)
        self.assertGreater(result.confidence, 0.0)

    def test_blank_page_yields_no_text_and_zero_confidence(self):
        from PIL import Image

        result = _ocr_image(Image.new("RGB", (400, 200), "white"))

        self.assertEqual(result.text, "")
        self.assertEqual(result.confidence, 0.0)


class BuildMarkdownTests(TestCase):
    def test_single_page_has_no_page_headings(self):
        markdown = build_markdown("Rechnung 123", ["Inhalt der Rechnung."])

        self.assertEqual(markdown, "# Rechnung 123\n\nInhalt der Rechnung.\n")

    def test_multi_page_gets_a_heading_per_page(self):
        markdown = build_markdown("Vertrag", ["Seite eins Text", "Seite zwei Text"])

        self.assertIn("## Seite 1", markdown)
        self.assertIn("## Seite 2", markdown)
        self.assertIn("Seite eins Text", markdown)
        self.assertIn("Seite zwei Text", markdown)

    def test_blank_page_gets_a_placeholder(self):
        markdown = build_markdown("Leer", [""])

        self.assertIn("_Kein Text erkannt._", markdown)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ReextractDocumentWithVisionTests(TestCase):
    """Erzwungene KI-Vision-Extraktion (#1143): Vision fuer jede Seite,
    unabhaengig davon, ob Text-Layer/OCR ausgereicht haetten -- siehe
    CLAUDE.md "KI-Vision-Extraktion nach Markdown".
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_image_is_replaced_by_the_vision_transcript(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="scan.png", data=buffer.getvalue(), mime_type="image/png"
        )
        document.text_content = "Alter, verstuemmelter OCR-Text."
        document.processing_status = Document.ProcessingStatus.READY
        document.save()
        vision_provider = FakeVisionProvider(reply="Sauberes Vision-Transkript.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(result.text_content, "Sauberes Vision-Transkript.")
        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )
        self.assertIsNotNone(result.vision_reextraction_completed_at)
        self.assertEqual(result.vision_reextraction_pages_processed, 1)
        self.assertEqual(result.vision_reextraction_pages_total, 1)
        self.assertFalse(result.vision_reextraction_truncated)
        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(len(vision_provider.calls), 1)

    @requires_pdf_rasterizer("mehrseitige KI-Vision-Extraktion")
    def test_multi_page_pdf_joins_pages_with_visible_page_markers(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="mixed.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Transkript.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(result.vision_reextraction_pages_processed, 2)
        self.assertEqual(result.vision_reextraction_pages_total, 2)
        self.assertIn("--- Seite 1 ---", result.text_content)
        self.assertIn("--- Seite 2 ---", result.text_content)
        self.assertEqual(len(vision_provider.calls), 2)
        # Text-Layer wird bewusst NICHT beruecksichtigt -- jede Seite geht
        # trotz vorhandenem Text-Layer durch Vision.
        image, prompt = vision_provider.calls[0]
        self.assertEqual(image.mime_type, "image/png")
        self.assertTrue(prompt)

    @requires_pdf_rasterizer("Seitenobergrenze der KI-Vision-Extraktion")
    @override_settings(FINDUS_VISION_REEXTRACT_MAX_PAGES=1)
    def test_page_limit_truncates_and_is_reported(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="lang.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Transkript.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(len(vision_provider.calls), 1)
        self.assertEqual(result.vision_reextraction_pages_processed, 1)
        self.assertEqual(result.vision_reextraction_pages_total, 3)
        self.assertTrue(result.vision_reextraction_truncated)
        self.assertEqual(result.metadata["page_count"], 3)

    def test_unsupported_mime_type_fails_without_touching_processing_status(self):
        document = _make_document(
            filename="archiv.zip", data=b"PK\x03\x04", mime_type="application/zip"
        )
        document.text_content = "Unveraendert."
        document.processing_status = Document.ProcessingStatus.READY
        document.save()

        result = reextract_document_with_vision(document.id, vision_provider=FakeVisionProvider())

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertIn("application/zip", result.vision_reextraction_error)
        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(result.text_content, "Unveraendert.")

    def test_manual_document_date_is_untouched(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="scan.png", data=buffer.getvalue(), mime_type="image/png"
        )
        manual_date = datetime.date(2025, 1, 15)
        document.document_date = manual_date
        document.metadata = {**document.metadata, "document_date_source": "manuell"}
        document.save()

        result = reextract_document_with_vision(
            document.id, vision_provider=FakeVisionProvider(reply="Transkript.")
        )

        self.assertEqual(result.document_date, manual_date)
        self.assertEqual(result.metadata["document_date_source"], "manuell")


class _ScriptedVisionProvider:
    """Vision-Provider mit Drehbuch: je Aufruf entweder eine Antwort oder
    eine Exception.

    `FakeVisionProvider` liefert fuer jeden Aufruf dieselbe Antwort und kann
    deshalb nicht ausdruecken, worum es bei der Seiten-Toleranz (#1148) geht:
    Seite 2 faellt aus, Seite 1 und 3 nicht.
    """

    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def describe_image(self, image, prompt):
        from apps.ai.providers.base import VisionResult

        self.calls.append((image, prompt))
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return VisionResult(text=step, model="scripted-vision", version="1")


_LAB_TABLE = (
    "### Laborbefund\n"
    "\n"
    "| Bezeichnung | Ergebnis | Referenzbereich | Einheit |\n"
    "| --- | --- | --- | --- |\n"
    "| Haemoglobin | 13,4 | 12,0-16,0 | g/dl |\n"
    "| Leukozyten | 9,1 | 4,0-10,0 | /nl |\n"
    "\n"
    "### Handschriftliche Vermerke\n"
    "\n"
    "- Neben 'Leukozyten' eingekreist, dazu handschriftlich: 'Kontrolle in 4 Wochen'\n"
)


def _png_document(filename="scan.png"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
    return _make_document(filename=filename, data=buffer.getvalue(), mime_type="image/png")


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class VisionMarkdownExtractionTests(TestCase):
    """KI-Vision-Extraktion nach Markdown (#1148): der erzwungene Lauf gibt
    strukturerhaltendes Markdown zurueck -- Tabellen bleiben Tabellen -- und
    legt es sowohl in `markdown` (Ansicht) als auch in `text_content` (Index)
    ab.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_table_rows_stay_intact_in_text_and_markdown(self):
        document = _png_document()
        provider = _ScriptedVisionProvider([_LAB_TABLE])

        result = reextract_document_with_vision(document.id, vision_provider=provider)

        # Der eigentliche Zweck des Features: Wert, Referenzbereich und
        # Einheit stehen in *einer* Zeile, nicht in vier getrennten Listen.
        self.assertIn("| Haemoglobin | 13,4 | 12,0-16,0 | g/dl |", result.text_content)
        self.assertIn("| Haemoglobin | 13,4 | 12,0-16,0 | g/dl |", result.markdown)
        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )

    def test_handwriting_stays_in_its_own_section(self):
        document = _png_document()

        result = reextract_document_with_vision(
            document.id, vision_provider=_ScriptedVisionProvider([_LAB_TABLE])
        )

        heading_at = result.text_content.index("### Handschriftliche Vermerke")
        table_at = result.text_content.index("| Haemoglobin")
        self.assertLess(table_at, heading_at)
        self.assertIn("Kontrolle in 4 Wochen", result.text_content[heading_at:])

    def test_the_markdown_contract_prompt_is_used(self):
        document = _png_document()
        provider = _ScriptedVisionProvider([_LAB_TABLE])

        reextract_document_with_vision(document.id, vision_provider=provider)

        _image, prompt = provider.calls[0]
        self.assertEqual(prompt, vision_markdown.PAGE_PROMPT)

    def test_a_fenced_answer_is_unwrapped_before_saving(self):
        document = _png_document()
        provider = _ScriptedVisionProvider(["```markdown\n| A | B |\n| --- | --- |\n```"])

        result = reextract_document_with_vision(document.id, vision_provider=provider)

        self.assertTrue(result.text_content.startswith("| A | B |"))
        self.assertNotIn("```", result.text_content)

    def test_an_unreadable_page_is_reported_instead_of_invented(self):
        document = _png_document()
        marker = f"{vision_markdown.UNREADABLE_PAGE_MARKER} (ueberbelichtet)"
        provider = _ScriptedVisionProvider([marker])

        result = reextract_document_with_vision(document.id, vision_provider=provider)

        self.assertEqual(result.text_content, marker)
        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )

    def test_an_empty_answer_fails_the_run_and_keeps_the_previous_text(self):
        document = _png_document()
        document.text_content = "Alter OCR-Text."
        document.processing_status = Document.ProcessingStatus.READY
        document.save()

        result = reextract_document_with_vision(
            document.id, vision_provider=_ScriptedVisionProvider(["   "])
        )

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertEqual(result.text_content, "Alter OCR-Text.")
        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)

    def test_provider_failure_keeps_the_document_usable(self):
        from apps.ai.providers.base import ProviderError

        document = _png_document()
        document.text_content = "Alter OCR-Text."
        document.processing_status = Document.ProcessingStatus.READY
        document.save()

        result = reextract_document_with_vision(
            document.id,
            vision_provider=_ScriptedVisionProvider([ProviderError("provider down")]),
        )

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertNotEqual(result.vision_reextraction_error, "")
        self.assertEqual(result.text_content, "Alter OCR-Text.")

    @requires_pdf_rasterizer("seitenweise KI-Vision-Extraktion nach Markdown")
    def test_one_failing_page_does_not_devalue_the_others(self):
        from apps.ai.providers.base import ProviderError

        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(
            filename="befund.pdf", data=pdf_bytes, mime_type="application/pdf"
        )
        provider = _ScriptedVisionProvider(
            ["### Seite eins", ProviderError("timeout"), "### Seite drei"]
        )

        result = reextract_document_with_vision(document.id, vision_provider=provider)

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )
        self.assertIn("### Seite eins", result.text_content)
        self.assertIn("### Seite drei", result.text_content)
        # Die ausgefallene Seite bleibt als benannte Luecke an ihrer Stelle
        # stehen -- sonst verschoebe sich die Seitenzaehlung.
        self.assertIn("--- Seite 2 ---", result.text_content)
        self.assertIn("nicht verarbeitet werden", result.text_content)
        self.assertLess(
            result.text_content.index("--- Seite 2 ---"),
            result.text_content.index("--- Seite 3 ---"),
        )
        self.assertEqual(result.vision_reextraction_pages_processed, 2)
        self.assertEqual(result.vision_reextraction_pages_total, 3)

    def test_the_original_file_is_left_untouched(self):
        """Die Markdown-Fassung ist ein abgeleitetes Artefakt (#1148) -- das
        Original bleibt fuehrend und unveraendert herunterladbar.
        """
        document = _png_document()
        document.original_file.open("rb")
        try:
            before = document.original_file.read()
        finally:
            document.original_file.close()

        result = reextract_document_with_vision(
            document.id, vision_provider=_ScriptedVisionProvider([_LAB_TABLE])
        )

        result.original_file.open("rb")
        try:
            after = result.original_file.read()
        finally:
            result.original_file.close()
        self.assertEqual(before, after)
        self.assertEqual(result.sha256, document.sha256)

    def test_the_markdown_reaches_the_index(self):
        """Die erhaltene Struktur ist genau dann etwas wert, wenn sie auch
        in den Chunks steht, aus denen das Retrieval antwortet -- deshalb
        landet das Markdown in `text_content` und nicht nur im
        Markdown-Cache.
        """
        from apps.ai.providers.fake import FakeEmbeddingProvider

        from .processing import process_document

        document = _png_document()
        reextract_document_with_vision(
            document.id, vision_provider=_ScriptedVisionProvider([_LAB_TABLE])
        )

        process_document(
            document.id,
            embedding_provider=FakeEmbeddingProvider(dimensions=settings.FINDUS_EMBEDDING_DIMENSIONS),
        )

        chunk_contents = [chunk.content for chunk in document.chunks.all()]
        self.assertTrue(
            any("| Haemoglobin | 13,4 | 12,0-16,0 | g/dl |" in content for content in chunk_contents),
            f"Tabellenzeile in keinem Chunk gefunden: {chunk_contents}",
        )

    @requires_pdf_rasterizer("KI-Vision-Extraktion mit durchgehend fehlerhaften Seiten")
    def test_every_page_failing_marks_the_run_failed(self):
        from apps.ai.providers.base import ProviderError

        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="kaputt.pdf", data=pdf_bytes, mime_type="application/pdf")
        document.text_content = "Alter OCR-Text."
        document.save()

        result = reextract_document_with_vision(
            document.id, vision_provider=_ScriptedVisionProvider([ProviderError("down")])
        )

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertEqual(result.text_content, "Alter OCR-Text.")


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class VisionMarkdownIdempotencyTests(TestCase):
    """Kosten- und Wiederholungsvertrag (#1148): dieselbe unveraenderte
    Datei loest keinen zweiten Modellaufruf aus, ein erzwungener Re-Run
    schon.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_second_run_over_an_unchanged_file_calls_no_model(self):
        document = _png_document()
        provider = _ScriptedVisionProvider([_LAB_TABLE])

        first = extract_vision_markdown(document.id, vision_provider=provider)
        second = extract_vision_markdown(document.id, vision_provider=provider)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(provider.calls), 1)

    def test_forced_rerun_calls_the_model_again(self):
        document = _png_document()
        provider = _ScriptedVisionProvider([_LAB_TABLE])

        extract_vision_markdown(document.id, vision_provider=provider)
        extract_vision_markdown(document.id, force=True, vision_provider=provider)

        self.assertEqual(len(provider.calls), 2)

    def test_a_failed_run_is_retried_rather_than_skipped(self):
        from apps.ai.providers.base import ProviderError

        document = _png_document()
        provider = _ScriptedVisionProvider([ProviderError("down"), _LAB_TABLE])

        extract_vision_markdown(document.id, vision_provider=provider)
        result = extract_vision_markdown(document.id, vision_provider=provider)

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )

    def test_a_regular_reextraction_makes_the_document_eligible_again(self):
        """Ein regulaerer Reprocess ersetzt `text_content` durch den
        Kaskadentext -- der Fingerabdruck gehoert zu dem Text, der gerade
        da steht, und muss deshalb mit zurueckgesetzt werden.
        """
        document = _png_document()
        extract_vision_markdown(
            document.id, vision_provider=_ScriptedVisionProvider([_LAB_TABLE])
        )

        with patch(
            "apps.documents.extraction._ocr_image",
            return_value=_OcrOutput(text=_GERMAN_PARAGRAPH, confidence=95.0),
        ):
            extract_document(document.id)

        document.refresh_from_db()
        self.assertEqual(document.vision_reextraction_fingerprint, "")
        self.assertFalse(vision_markdown.is_up_to_date(document))

    @requires_pdf_rasterizer("Fingerabdruck bei abgeschnittenem Lauf")
    @override_settings(FINDUS_VISION_REEXTRACT_MAX_PAGES=1)
    def test_a_truncated_run_stays_eligible_for_the_remaining_pages(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="lang.pdf", data=pdf_bytes, mime_type="application/pdf")

        result = extract_vision_markdown(
            document.id, vision_provider=_ScriptedVisionProvider([_LAB_TABLE])
        )

        self.assertTrue(result.vision_reextraction_truncated)
        self.assertEqual(result.vision_reextraction_fingerprint, "")
        self.assertFalse(vision_markdown.is_up_to_date(result))
