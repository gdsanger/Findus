from unittest.mock import patch

from django.test import TestCase

from .models import Document
from .tasks import extract_document_task, process_document_task


class ExtractDocumentTaskTests(TestCase):
    """`extract_document_task` is the queued entry point for the
    extraction cascade (#1009); on success it must hand the document off
    to `process_document_task` (#1010, chunking/embedding) so the pipeline
    keeps moving without a connector or view having to know about it.
    """

    def test_enqueues_process_document_task_on_success(self):
        document = Document.objects.create(title="Doc", text_content="already extracted")

        with patch("apps.documents.tasks.extract_document") as mock_extract, patch(
            "django_q.tasks.async_task"
        ) as mock_async_task:
            mock_async_task.return_value = "task-1"
            extract_document_task(document.id)

        mock_extract.assert_called_once_with(document.id)
        mock_async_task.assert_called_once_with(process_document_task, document.id)

    def test_does_not_enqueue_embedding_when_extraction_fails(self):
        with patch(
            "apps.documents.tasks.extract_document", side_effect=RuntimeError("boom")
        ), patch("django_q.tasks.async_task") as mock_async_task:
            with self.assertRaisesMessage(RuntimeError, "boom"):
                extract_document_task(1)

        mock_async_task.assert_not_called()
