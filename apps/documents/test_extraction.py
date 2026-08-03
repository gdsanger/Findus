import io
import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.ai.providers.fake import FakeVisionProvider

from .extraction import _OcrOutput, build_markdown, extract_document
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


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
@override_settings(FINDUS_EXTRACTION_MIN_CHARS_PER_PAGE=20, FINDUS_EXTRACTION_MIN_OCR_CONFIDENCE=60)
class ExtractDocumentOcrTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

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
