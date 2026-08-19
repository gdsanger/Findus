"""Seitenvorschauen und Leerseiten-Kennzeichnung (#1155).

Die Bilder sind ein Cache -- geprüft wird deshalb vor allem, dass er
idempotent entsteht, beim Ändern der Datei verschwindet und die
Kennzeichnung "ohne verwertbaren Inhalt" ein Hinweis bleibt, keine
Vorauswahl.
"""

import hashlib
import io
import shutil
import tempfile

from django.test import TestCase, override_settings

from .models import Document, DocumentPagePreview
from .page_previews import (
    discard_page_previews,
    generate_page_previews,
    page_previews_for,
)
from .test_extraction import _make_pdf

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-page-preview-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def tearDownModule():
    shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT, STORAGES=_LOCAL_STORAGES)
class GeneratePagePreviewsTests(TestCase):
    def setUp(self):
        self.data = _make_pdf(["eins", "zwei", "drei"])
        self.document = Document.objects.create(
            title="Scan",
            metadata={"mime_type": "application/pdf", "original_filename": "scan.pdf"},
            sha256=hashlib.sha256(self.data).hexdigest(),
        )
        self.document.original_file.save("scan.pdf", io.BytesIO(self.data), save=True)

    def test_one_image_per_page(self):
        created = generate_page_previews(self.document.id)

        self.assertEqual(created, 3)
        self.assertEqual(
            list(self.document.page_previews.values_list("page_number", flat=True)), [1, 2, 3]
        )

    def test_a_second_run_creates_nothing_new(self):
        """Zwei kurz hintereinander geöffnete Seitenansichten dürfen nicht
        doppelt rastern."""
        generate_page_previews(self.document.id)

        self.assertEqual(generate_page_previews(self.document.id), 0)
        self.assertEqual(self.document.page_previews.count(), 3)

    def test_discard_removes_rows_and_files(self):
        generate_page_previews(self.document.id)
        names = [preview.image.name for preview in self.document.page_previews.all()]
        storage = DocumentPagePreview.image.field.storage

        discard_page_previews(self.document)

        self.assertEqual(self.document.page_previews.count(), 0)
        for name in names:
            self.assertFalse(storage.exists(name))

    def test_a_non_pdf_document_gets_no_previews(self):
        document = Document.objects.create(
            title="Notiz", metadata={"mime_type": "text/plain", "original_filename": "n.txt"}
        )
        document.original_file.save("n.txt", io.BytesIO(b"hallo"), save=True)

        self.assertEqual(generate_page_previews(document.id), 0)

    def test_a_broken_file_does_not_raise(self):
        document = Document.objects.create(
            title="Kaputt", metadata={"mime_type": "application/pdf", "original_filename": "x.pdf"}
        )
        document.original_file.save("x.pdf", io.BytesIO(b"kein PDF"), save=True)

        self.assertEqual(generate_page_previews(document.id), 0)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT, STORAGES=_LOCAL_STORAGES)
class EmptyPageMarkingTests(TestCase):
    """Die Kennzeichnung ist ein Hinweis, keine Vorauswahl -- eine Seite mit
    nur einem Stempel sieht für die Texterkennung leer aus und ist es
    nicht."""

    def _document(self, markdown):
        return Document.objects.create(
            title="Scan",
            markdown=markdown,
            metadata={"mime_type": "application/pdf", "original_filename": "scan.pdf"},
        )

    def test_a_page_without_recognised_text_is_marked(self):
        document = self._document(
            "# Scan\n\n## Seite 1\n\nEin ordentlich langer Absatz mit Inhalt.\n\n"
            "## Seite 2\n\n_Kein Text erkannt._\n"
        )

        previews = page_previews_for(document, page_count=2)

        self.assertFalse(previews[0].looks_empty)
        self.assertTrue(previews[1].looks_empty)

    def test_the_vision_marker_for_an_empty_page_counts_too(self):
        document = self._document(
            "# Scan\n\n## Seite 1\n\nEin ordentlich langer Absatz mit Inhalt.\n\n"
            "## Seite 2\n\n_Seite leer oder nicht lesbar._\n"
        )

        self.assertTrue(page_previews_for(document, page_count=2)[1].looks_empty)

    def test_a_technically_failed_page_is_not_called_empty(self):
        """"Konnte nicht verarbeitet werden" heißt nicht "leer" -- die
        beiden Fälle dürfen in der Ansicht nicht gleich aussehen."""
        document = self._document(
            "# Scan\n\n## Seite 1\n\nInhalt genug fuer diese Seite hier.\n\n"
            "## Seite 2\n\n_Seite konnte technisch nicht verarbeitet werden._\n"
        )

        self.assertFalse(page_previews_for(document, page_count=2)[1].looks_empty)

    def test_a_single_page_document_without_headings_is_read_too(self):
        document = self._document("# Scan\n\n_Kein Text erkannt._\n")

        self.assertTrue(page_previews_for(document, page_count=1)[0].looks_empty)

    def test_without_any_extracted_text_nothing_is_marked(self):
        """Keine Angabe ist nicht dasselbe wie "leer"."""
        document = self._document("")

        previews = page_previews_for(document, page_count=2)

        self.assertFalse(any(preview.looks_empty for preview in previews))
