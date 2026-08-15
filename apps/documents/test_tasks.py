from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from .models import Document, Vorgang, VorgangRecommendationRun
from .tasks import (
    analyze_document_task,
    extract_document_task,
    generate_vorgang_recommendations_hook,
    process_document_task,
)


class ExtractDocumentTaskTests(TestCase):
    """`extract_document_task` is the queued entry point for the
    extraction cascade (#1009); on success it must hand the document off
    to `analyze_document_task` (#1020, KI-Analyse) so the pipeline keeps
    moving without a connector or view having to know about it.
    """

    def test_enqueues_analyze_document_task_on_success(self):
        document = Document.objects.create(title="Doc", text_content="already extracted")

        with patch("apps.documents.tasks.extract_document") as mock_extract, patch(
            "django_q.tasks.async_task"
        ) as mock_async_task:
            mock_async_task.return_value = "task-1"
            extract_document_task(document.id)

        mock_extract.assert_called_once_with(document.id)
        mock_async_task.assert_called_once_with(analyze_document_task, document.id)

    def test_does_not_enqueue_analysis_when_extraction_fails(self):
        with patch(
            "apps.documents.tasks.extract_document", side_effect=RuntimeError("boom")
        ), patch("django_q.tasks.async_task") as mock_async_task:
            with self.assertRaisesMessage(RuntimeError, "boom"):
                extract_document_task(1)

        mock_async_task.assert_not_called()


class AnalyzeDocumentTaskTests(TestCase):
    """`analyze_document_task` is the queued entry point for the KI-Analyse

    (#1020), the pipeline stage between extraction and embedding.
    """

    def test_enqueues_process_document_task_on_success(self):
        document = Document.objects.create(title="Doc", text_content="already extracted")

        with patch("apps.documents.tasks.analyze_document") as mock_analyze, patch(
            "django_q.tasks.async_task"
        ) as mock_async_task:
            mock_async_task.return_value = "task-1"
            analyze_document_task(document.id)

        mock_analyze.assert_called_once_with(document.id)
        mock_async_task.assert_called_once_with(process_document_task, document.id)


class GenerateVorgangRecommendationsHookTests(TestCase):
    """`generate_vorgang_recommendations_hook` (#1134) ist das Netz fuer
    einen Django-Q-Task, der ohne Ergebnis endet, ohne dass
    `generate_vorgang_recommendations()` selbst noch Gelegenheit hatte,
    den Lauf auf "failed" zu setzen -- siehe der Docstring der Funktion
    fuer den Regelfall, den das *nicht* abdecken muss (der laeuft ueber
    das `except TimeoutException` in `recommendations.py`).
    """

    def setUp(self):
        self.vorgang = Vorgang.objects.create(name="Forderung Acme")
        self.run = VorgangRecommendationRun.objects.create(
            vorgang=self.vorgang, status=VorgangRecommendationRun.Status.RUNNING
        )

    def test_successful_task_leaves_the_run_untouched(self):
        task = SimpleNamespace(success=True, args=(self.vorgang.pk, 1))

        generate_vorgang_recommendations_hook(task)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, VorgangRecommendationRun.Status.RUNNING)

    def test_failed_task_marks_a_still_running_run_as_failed(self):
        task = SimpleNamespace(success=False, args=(self.vorgang.pk, 1))

        generate_vorgang_recommendations_hook(task)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, VorgangRecommendationRun.Status.FAILED)
        self.assertTrue(self.run.error)

    def test_failed_task_does_not_overwrite_an_already_finished_run(self):
        """Der Hook feuert *immer* nach dem Task, auch nach einem Erfolg,
        bei dem `task.success` aus anderen Gruenden False sein koennte
        (Django-Q-eigene Nachbearbeitung) -- ein bereits gesetztes
        Ergebnis (`ready`) darf er nie ueberschreiben.
        """
        self.run.status = VorgangRecommendationRun.Status.READY
        self.run.error = ""
        self.run.save(update_fields=["status", "error"])
        task = SimpleNamespace(success=False, args=(self.vorgang.pk, 1))

        generate_vorgang_recommendations_hook(task)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, VorgangRecommendationRun.Status.READY)

    def test_no_args_is_a_no_op(self):
        task = SimpleNamespace(success=False, args=())

        generate_vorgang_recommendations_hook(task)  # must not raise
