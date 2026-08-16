import datetime
import hashlib
import io
import shutil
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.ai.providers.base import VisionResult
from apps.ai.providers.fake import FakeVisionProvider
from config.test_requirements import requires_ocr, requires_pdf_rasterizer

from .extraction import (
    _OcrOutput,
    _ocr_image,
    _strip_code_fence,
    _VISION_MARKDOWN_PROMPT,
    build_markdown,
    extract_document,
    reextract_document_with_vision,
    vision_markdown_fingerprint,
)
from .models import Document

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-extraction-media-")

_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def tearDownModule():
    """Raeumt `TEST_MEDIA_ROOT` genau einmal auf, nachdem *alle* Klassen
    dieser Datei fertig sind -- ein `tearDownClass` je Klasse (die alte
    Bauart hier) raeumt den geteilten Ordner schon nach der ersten fertigen
    Klasse weg, waehrend beim parallelen Testlauf (Django forkt Worker, die
    alle denselben schon ausgewerteten Pfad erben) eine andere Klasse in
    einem anderen Worker noch mitten in einem Test steckt, der genau diesen
    Ordner braucht -- daraus folgt ein `FileNotFoundError`, keine
    inhaltliche Verletzung. `tearDownModule` ist ein von `unittest`
    vorgesehener Hook, der erst nach der letzten Klasse des Moduls laeuft.
    """
    shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)


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
    document = Document.objects.create(
        title="Testdokument",
        metadata={"mime_type": mime_type},
        sha256=hashlib.sha256(data).hexdigest(),
    )
    document.original_file.save(filename, io.BytesIO(data), save=True)
    return document


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractDocumentTextLayerTests(TestCase):
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


class VisionMarkdownPromptTests(SimpleTestCase):
    """Der Ausgabe-Kontrakt fuer #1149 steht im Prompt selbst -- diese
    Tests stellen sicher, dass jede in CLAUDE.md ("KI-Vision-
    Neuextraktion") zugesagte Strukturregel auch tatsaechlich drinsteht,
    nicht nur im Docstring daneben.
    """

    def test_prompt_demands_pure_transcription(self):
        self.assertIn("Reine Abschrift", _VISION_MARKDOWN_PROMPT)
        self.assertIn("nichts zusammenfassen", _VISION_MARKDOWN_PROMPT)

    def test_prompt_demands_markdown_tables_with_one_row_per_line(self):
        self.assertIn("Markdown-Tabelle", _VISION_MARKDOWN_PROMPT)
        self.assertIn("Referenzbereich und Einheit", _VISION_MARKDOWN_PROMPT)

    def test_prompt_demands_a_separate_section_for_handwriting(self):
        self.assertIn("Handschriftliche Vermerke", _VISION_MARKDOWN_PROMPT)

    def test_prompt_demands_marking_uncertain_readings(self):
        self.assertIn("unsicher", _VISION_MARKDOWN_PROMPT)

    def test_prompt_demands_flagging_blank_or_unreadable_pages(self):
        self.assertIn("leer oder nicht lesbar", _VISION_MARKDOWN_PROMPT)


class StripCodeFenceTests(SimpleTestCase):
    def test_removes_a_fence_wrapping_the_whole_response(self):
        self.assertEqual(_strip_code_fence("```markdown\n# Titel\n\nText\n```"), "# Titel\n\nText")

    def test_removes_a_bare_fence_without_a_language_tag(self):
        self.assertEqual(_strip_code_fence("```\nText\n```"), "Text")

    def test_leaves_text_without_a_wrapping_fence_untouched(self):
        self.assertEqual(_strip_code_fence("# Titel\n\nText"), "# Titel\n\nText")

    def test_leaves_a_fence_covering_only_part_of_the_response_untouched(self):
        text = "Vorspann\n\n```\ncode\n```\n\nNachspann"
        self.assertEqual(_strip_code_fence(text), text)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ReextractDocumentWithVisionTests(TestCase):
    """Manuelle KI-Vision-Neuextraktion (#1143), Markdown-Ausgabe (#1149):
    erzwingt Vision fuer jede Seite, unabhaengig davon, ob Text-Layer/OCR
    ausgereicht haetten -- siehe CLAUDE.md "KI-Funktionen".
    """

    def test_image_is_replaced_by_the_vision_transcript_as_markdown(self):
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

        self.assertIn("Sauberes Vision-Transkript.", result.text_content)
        self.assertEqual(result.markdown, result.text_content)
        self.assertEqual(result.content_format, Document.ContentFormat.MARKDOWN)
        self.assertEqual(result.extraction_method, Document.ExtractionMethod.VISION)
        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )
        self.assertIsNotNone(result.vision_reextraction_completed_at)
        self.assertEqual(result.vision_reextraction_pages_processed, 1)
        self.assertEqual(result.vision_reextraction_pages_total, 1)
        self.assertEqual(result.vision_reextraction_pages_failed, 0)
        self.assertFalse(result.vision_reextraction_truncated)
        self.assertEqual(result.vision_reextraction_model, "fake-vision")
        self.assertEqual(result.vision_reextraction_model_version, "1")
        self.assertEqual(
            result.vision_reextraction_fingerprint, vision_markdown_fingerprint(result)
        )
        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(len(vision_provider.calls), 1)
        _, prompt = vision_provider.calls[0]
        self.assertEqual(prompt, _VISION_MARKDOWN_PROMPT)

    def test_table_rows_keep_their_columns_together(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="kontoauszug.png", data=buffer.getvalue(), mime_type="image/png"
        )
        table = (
            "| Bezeichnung | Wert | Referenz | Einheit |\n"
            "| --- | --- | --- | --- |\n"
            "| Kalium | 4.5 | 3.5-5.1 | mmol/l |\n"
        )
        vision_provider = FakeVisionProvider(reply=table)

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertIn("| Kalium | 4.5 | 3.5-5.1 | mmol/l |", result.text_content)

    def test_handwritten_notes_land_in_their_own_section(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="brief.png", data=buffer.getvalue(), mime_type="image/png"
        )
        reply = (
            "Sehr geehrte Damen und Herren, ...\n\n"
            "## Handschriftliche Vermerke\n\n"
            "'geprüft, ok - HM'"
        )
        vision_provider = FakeVisionProvider(reply=reply)

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertIn("## Handschriftliche Vermerke", result.text_content)

    @requires_pdf_rasterizer("mehrseitige KI-Vision-Neuextraktion")
    def test_multi_page_pdf_joins_pages_with_visible_markdown_page_markers(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="mixed.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Transkript.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(result.vision_reextraction_pages_processed, 2)
        self.assertEqual(result.vision_reextraction_pages_total, 2)
        self.assertIn("## Seite 1", result.text_content)
        self.assertIn("## Seite 2", result.text_content)
        self.assertEqual(len(vision_provider.calls), 2)
        # Text-Layer wird bewusst NICHT beruecksichtigt -- jede Seite geht
        # trotz vorhandenem Text-Layer durch Vision.
        image, prompt = vision_provider.calls[0]
        self.assertEqual(image.mime_type, "image/png")
        self.assertEqual(prompt, _VISION_MARKDOWN_PROMPT)

    @requires_pdf_rasterizer("Seitenobergrenze der KI-Vision-Neuextraktion")
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
        # Ein abgeschnittener Lauf gilt nicht als "aktuell" -- ein
        # spaeterer Klick (ohne `force`) soll die restlichen Seiten
        # nachholen koennen, statt gegen den Fingerabdruck zu scheitern.
        self.assertEqual(result.vision_reextraction_fingerprint, "")

    @requires_pdf_rasterizer("teilweiser Seitenfehler bei der KI-Vision-Neuextraktion")
    def test_one_failing_page_does_not_devalue_the_others(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH, _GERMAN_PARAGRAPH])
        document = _make_document(filename="drei.pdf", data=pdf_bytes, mime_type="application/pdf")

        class _FlakyVisionProvider:
            def __init__(self):
                self.calls = 0

            def describe_image(self, image, prompt):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("Seite 2 boom")
                return VisionResult(text=f"Inhalt Seite {self.calls}", model="m", version="v")

        result = reextract_document_with_vision(
            document.id, vision_provider=_FlakyVisionProvider()
        )

        self.assertEqual(
            result.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )
        self.assertEqual(result.vision_reextraction_pages_processed, 3)
        self.assertEqual(result.vision_reextraction_pages_failed, 1)
        self.assertIn("Inhalt Seite 1", result.text_content)
        self.assertIn("Inhalt Seite 3", result.text_content)
        self.assertIn("## Seite 2", result.text_content)
        # Kein vollstaendiger Erfolg -> kein Idempotenz-Fingerabdruck, ein
        # spaeterer Klick ohne `force` darf die fehlgeschlagene Seite 2
        # erneut versuchen.
        self.assertEqual(result.vision_reextraction_fingerprint, "")

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


class VisionMarkdownIdempotencyTests(TestCase):
    """#1149: eine unveraenderte Datei loest keinen erneuten Modellaufruf
    aus, ein erzwungener Re-Run schon.

    Eigener, privater `MEDIA_ROOT` statt des modulweit geteilten
    `TEST_MEDIA_ROOT`: dessen `tearDownClass` in anderen Klassen dieser
    Datei raeumt beim parallelen Testlauf (Django forkt Worker, die alle
    denselben schon ausgewerteten Pfad erben) sonst mitten in einem Test
    dieser Klasse den Ordner leer -- ein voellig eigener Ordner kann mit
    keiner anderen Klasse um dieselbe Ressource wettlaufen.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="findus-vision-idempotency-media-")
        cls._media_override = override_settings(
            STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=cls._media_root
        )
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def _make_image_document(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        return _make_document(filename="scan.png", data=buffer.getvalue(), mime_type="image/png")

    def test_second_run_without_changes_makes_no_model_call(self):
        document = self._make_image_document()
        vision_provider = FakeVisionProvider(reply="Transkript.")

        reextract_document_with_vision(document.id, vision_provider=vision_provider)
        second = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(len(vision_provider.calls), 1)
        self.assertEqual(
            second.vision_reextraction_status, Document.VisionReextractionStatus.READY
        )

    def test_forced_run_makes_a_new_model_call(self):
        document = self._make_image_document()
        vision_provider = FakeVisionProvider(reply="Transkript.")

        reextract_document_with_vision(document.id, vision_provider=vision_provider)
        reextract_document_with_vision(document.id, vision_provider=vision_provider, force=True)

        self.assertEqual(len(vision_provider.calls), 2)

    def test_a_changed_file_is_not_treated_as_up_to_date(self):
        document = self._make_image_document()
        vision_provider = FakeVisionProvider(reply="Transkript.")
        reextract_document_with_vision(document.id, vision_provider=vision_provider)

        document.refresh_from_db()
        document.sha256 = "a-different-sha256"
        document.save(update_fields=["sha256"])

        reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(len(vision_provider.calls), 2)

    @requires_pdf_rasterizer("Ruecksetzen des Fingerabdrucks bei regulaerem Reprocess")
    def test_regular_reprocess_makes_the_document_eligible_again(self):
        pdf_bytes = _make_pdf([_GERMAN_PARAGRAPH])
        document = _make_document(filename="a.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Transkript.")
        reextract_document_with_vision(document.id, vision_provider=vision_provider)

        extract_document(document.id)
        reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertEqual(len(vision_provider.calls), 2)


class VisionMarkdownNumberCrosscheckTests(TestCase):
    """#1149: Zahlen aus dem Vision-Markdown werden gegen den PDF-Text-Layer
    geprueft, wo einer vorhanden ist.

    Eigener `MEDIA_ROOT` aus demselben Grund wie
    `VisionMarkdownIdempotencyTests`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="findus-vision-numbercheck-media-")
        cls._media_override = override_settings(
            STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=cls._media_root
        )
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    @requires_pdf_rasterizer("Zahlen-Gegenprobe der KI-Vision-Neuextraktion")
    def test_matching_numbers_report_no_deviation(self):
        pdf_bytes = _make_pdf([f"{_GERMAN_PARAGRAPH} Betrag 1.234,56 EUR, Konto 987654321."])
        document = _make_document(filename="beleg.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Betrag 1.234,56 EUR, Konto 987654321.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertTrue(result.vision_reextraction_number_check["performed"])
        self.assertEqual(result.vision_reextraction_number_check["unmatched"], [])

    @requires_pdf_rasterizer("Zahlen-Gegenprobe der KI-Vision-Neuextraktion")
    def test_a_misread_number_is_reported_as_unmatched(self):
        pdf_bytes = _make_pdf([f"{_GERMAN_PARAGRAPH} Betrag 1.234,56 EUR."])
        document = _make_document(filename="beleg.pdf", data=pdf_bytes, mime_type="application/pdf")
        vision_provider = FakeVisionProvider(reply="Betrag 9.999,00 EUR.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertTrue(result.vision_reextraction_number_check["performed"])
        self.assertIn("9.999,00", result.vision_reextraction_number_check["unmatched"])

    def test_no_text_layer_skips_the_check_entirely(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (100, 40), color="white").save(buffer, format="PNG")
        document = _make_document(
            filename="scan.png", data=buffer.getvalue(), mime_type="image/png"
        )
        vision_provider = FakeVisionProvider(reply="Betrag 1.234,56 EUR.")

        result = reextract_document_with_vision(document.id, vision_provider=vision_provider)

        self.assertFalse(result.vision_reextraction_number_check["performed"])
        self.assertEqual(result.vision_reextraction_number_check["unmatched"], [])
