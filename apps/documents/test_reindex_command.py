from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase

from apps.ai.providers.fake import FakeEmbeddingProvider

from .models import Chunk, Document

DIMENSIONS = settings.FINDUS_EMBEDDING_DIMENSIONS


class ReindexDocumentsCommandTests(TestCase):
    def setUp(self):
        self.provider = FakeEmbeddingProvider(dimensions=DIMENSIONS)
        patcher = patch(
            "apps.documents.processing.get_embedding_provider",
            return_value=self.provider,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_reindexes_all_documents_with_text_by_default(self):
        with_text = Document.objects.create(title="A", text_content="hello world")
        Document.objects.create(title="B", text_content="")

        out = StringIO()
        call_command("reindex_documents", stdout=out)

        self.assertEqual(Chunk.objects.filter(document=with_text).count(), 1)
        self.assertIn("1 erfolgreich, 0 fehlgeschlagen", out.getvalue())

    def test_document_ids_option_restricts_the_target_set(self):
        doc_a = Document.objects.create(title="A", text_content="hello world")
        doc_b = Document.objects.create(title="B", text_content="second doc")

        call_command("reindex_documents", document_ids=[doc_a.id])

        self.assertEqual(Chunk.objects.filter(document=doc_a).count(), 1)
        self.assertEqual(Chunk.objects.filter(document=doc_b).count(), 0)

    def test_queue_option_enqueues_instead_of_processing_inline(self):
        doc = Document.objects.create(title="A", text_content="hello world")

        with patch("django_q.tasks.async_task") as mock_async_task:
            call_command("reindex_documents", queue=True)

        mock_async_task.assert_called_once()
        self.assertEqual(Chunk.objects.filter(document=doc).count(), 0)

    def test_one_failure_does_not_abort_the_remaining_documents(self):
        ok_doc = Document.objects.create(title="ok", text_content="fine")
        bad_doc = Document.objects.create(title="bad", text_content="boom")

        real_embed = self.provider.embed

        def flaky_embed(texts):
            texts = list(texts)
            if any("boom" in text for text in texts):
                raise RuntimeError("boom")
            return real_embed(texts)

        self.provider.embed = flaky_embed

        out = StringIO()
        call_command("reindex_documents", stdout=out)

        self.assertIn("1 erfolgreich, 1 fehlgeschlagen", out.getvalue())
        self.assertEqual(Chunk.objects.filter(document=ok_doc).count(), 1)
        bad_doc.refresh_from_db()
        self.assertEqual(bad_doc.processing_status, Document.ProcessingStatus.FAILED)

    def test_no_matching_documents_reports_and_does_not_error(self):
        out = StringIO()
        call_command("reindex_documents", stdout=out)

        self.assertIn("Keine Dokumente", out.getvalue())
