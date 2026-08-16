"""`manage.py extract_vision_markdown` (#1148) -- Backfill und Re-Run der
strukturerhaltenden KI-Vision-Extraktion ueber den Bestand.

Der Befehl ist der Ort, an dem sich der Kostenvertrag des Features
ueberhaupt bemerkbar macht: ein zweiter Lauf ueber denselben Bestand darf
nichts kosten, ein erzwungener schon.
"""

import io
import shutil
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.ai.providers.fake import FakeVisionProvider

from . import vision_markdown
from .models import Document

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-vision-command-media-")

_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

_TABLE = "| Position | Betrag |\n| --- | --- |\n| Miete | 500,00 |"


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ExtractVisionMarkdownCommandTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.provider = FakeVisionProvider(reply=_TABLE)
        patcher = patch(
            "apps.documents.extraction.get_vision_provider", return_value=self.provider
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        # Der Nachlauf (Analyse -> Embedding) wird eingereiht, nicht hier
        # ausgefuehrt -- ohne Broker im Test ist das ein Mock.
        queue_patcher = patch("django_q.tasks.async_task")
        self.addCleanup(queue_patcher.stop)
        self.async_task = queue_patcher.start()

    def _png(self, title="Scan"):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (60, 30), color="white").save(buffer, format="PNG")
        document = Document.objects.create(title=title, metadata={"mime_type": "image/png"})
        document.original_file.save("scan.png", io.BytesIO(buffer.getvalue()), save=True)
        return document

    def _call(self, **options):
        out, err = StringIO(), StringIO()
        call_command("extract_vision_markdown", stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def test_transcribes_a_scan_and_queues_the_reindex(self):
        document = self._png()

        out, _err = self._call(document_ids=[document.id])

        document.refresh_from_db()
        self.assertIn("| Miete | 500,00 |", document.text_content)
        self.assertIn("1 transkribiert", out)
        self.assertEqual(len(self.provider.calls), 1)
        # Ohne Nachlauf durchsucht Findus weiterhin den alten Text.
        self.async_task.assert_called_once()

    def test_second_run_over_an_unchanged_file_calls_no_model(self):
        document = self._png()
        self._call(document_ids=[document.id])

        out, _err = self._call(document_ids=[document.id])

        self.assertEqual(len(self.provider.calls), 1)
        self.assertIn("Keine Dokumente", out)

    def test_force_transcribes_again(self):
        document = self._png()
        self._call(document_ids=[document.id])

        out, _err = self._call(document_ids=[document.id], force=True)

        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn("1 transkribiert", out)

    def test_formats_without_page_images_are_skipped_not_failed(self):
        document = Document.objects.create(
            title="Notiz", metadata={"mime_type": "text/plain"}
        )
        document.original_file.save("notiz.txt", io.BytesIO(b"nur Text"), save=True)

        out, err = self._call(document_ids=[document.id])

        self.assertIn("Keine Dokumente", out)
        self.assertEqual(err, "")
        self.assertEqual(self.provider.calls, [])

    def test_limit_caps_the_number_of_documents(self):
        for index in range(3):
            self._png(title=f"Scan {index}")

        self._call(limit=2)

        self.assertEqual(len(self.provider.calls), 2)

    def test_queue_enqueues_one_task_per_document_without_calling_the_model(self):
        from apps.documents.tasks import extract_vision_markdown_task

        document = self._png()

        out, _err = self._call(document_ids=[document.id], queue=True)

        self.assertEqual(self.provider.calls, [])
        self.assertIn("eingereiht", out)
        args, kwargs = self.async_task.call_args
        self.assertEqual(args, (extract_vision_markdown_task, document.id))
        self.assertIn("timeout", kwargs)

    def test_a_failed_run_is_reported_and_stays_retryable(self):
        document = Document.objects.create(title="Kaputt", metadata={"mime_type": "image/png"})
        document.original_file.save("kaputt.png", io.BytesIO(b"kein gueltiges PNG"), save=True)

        out, err = self._call(document_ids=[document.id])

        self.assertIn("1 fehlgeschlagen", out)
        self.assertIn(f"Document {document.id}", err)
        document.refresh_from_db()
        self.assertEqual(
            document.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertFalse(vision_markdown.is_up_to_date(document))
