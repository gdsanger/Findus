from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from .models import Document, Vorgang, VorgangRecommendationRun
from .tasks import (
    analyze_document_task,
    extract_document_task,
    extract_vision_markdown_hook,
    extract_vision_markdown_task,
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


@override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE="scans")
class AutomaticVisionMarkdownHandoffTests(TestCase):
    """Automatische KI-Vision-Extraktion nach Markdown (#1148): nach der
    Extraktion entscheidet die Konfiguration, ob das Dokument zusaetzlich
    strukturerhaltend transkribiert wird -- und wer die Pipeline weiterreicht.
    """

    def _scan(self, method=Document.ExtractionMethod.OCR):
        return Document.objects.create(
            title="Scan",
            sha256="abc123",
            metadata={"mime_type": "application/pdf"},
            extraction_method=method,
            text_content="OCR-Text",
        )

    @override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE="off")
    def test_scope_off_hands_straight_to_the_analysis(self):
        """Der Default: kein Anhang verlaesst die Instanz automatisch, die
        Pipeline laeuft unveraendert weiter.
        """
        document = self._scan()

        with patch("apps.documents.tasks.extract_document"), patch(
            "apps.documents.tasks.generate_thumbnail_for_document"
        ), patch("django_q.tasks.async_task") as mock_async_task:
            extract_document_task(document.id)

        mock_async_task.assert_called_once_with(analyze_document_task, document.id)

    def test_a_scan_is_handed_to_the_vision_markdown_task_instead(self):
        document = self._scan()

        with patch("apps.documents.tasks.extract_document"), patch(
            "apps.documents.tasks.generate_thumbnail_for_document"
        ), patch("django_q.tasks.async_task") as mock_async_task:
            extract_document_task(document.id)

        mock_async_task.assert_called_once()
        args, kwargs = mock_async_task.call_args
        self.assertEqual(args, (extract_vision_markdown_task, document.id))
        # Eigener, grosszuegiger Task-Timeout plus Hook -- der Lauf macht
        # mehrere Modellaufrufe (CLAUDE.md, "Hintergrundjobs mit LLM-Aufruf").
        self.assertEqual(
            kwargs["timeout"], settings.FINDUS_VISION_REEXTRACT_TASK_TIMEOUT_SECONDS
        )
        self.assertEqual(kwargs["hook"], extract_vision_markdown_hook)

    def test_a_born_digital_pdf_is_left_to_the_normal_pipeline(self):
        document = self._scan(method=Document.ExtractionMethod.TEXT_LAYER)

        with patch("apps.documents.tasks.extract_document"), patch(
            "apps.documents.tasks.generate_thumbnail_for_document"
        ), patch("django_q.tasks.async_task") as mock_async_task:
            extract_document_task(document.id)

        mock_async_task.assert_called_once_with(analyze_document_task, document.id)

    def test_the_vision_task_always_continues_the_pipeline(self):
        """Auch ein uebersprungener oder fehlgeschlagener Zusatzlauf darf das
        Dokument nicht unindiziert liegen lassen -- der Kaskadentext ist ja
        noch da.
        """
        document = self._scan()

        with patch(
            "apps.documents.tasks.extract_vision_markdown", return_value=None
        ) as mock_extract, patch("django_q.tasks.async_task") as mock_async_task:
            extract_vision_markdown_task(document.id)

        mock_extract.assert_called_once_with(document.id)
        mock_async_task.assert_called_once_with(analyze_document_task, document.id)


class ExtractVisionMarkdownHookTests(TestCase):
    """Sicherheitsnetz fuer einen Worker, der stirbt, bevor der Task selbst
    noch etwas aufzeichnen oder weiterreichen konnte (#1148).
    """

    def _running_document(self):
        return Document.objects.create(
            title="Scan",
            vision_reextraction_status=Document.VisionReextractionStatus.RUNNING,
        )

    def test_successful_task_is_left_alone(self):
        document = self._running_document()

        with patch("django_q.tasks.async_task") as mock_async_task:
            extract_vision_markdown_hook(SimpleNamespace(success=True, args=[document.id]))

        mock_async_task.assert_not_called()
        document.refresh_from_db()
        self.assertEqual(
            document.vision_reextraction_status, Document.VisionReextractionStatus.RUNNING
        )

    def test_dead_task_is_marked_failed_and_the_pipeline_continues(self):
        document = self._running_document()

        with patch("django_q.tasks.async_task") as mock_async_task:
            extract_vision_markdown_hook(SimpleNamespace(success=False, args=[document.id]))

        document.refresh_from_db()
        self.assertEqual(
            document.vision_reextraction_status, Document.VisionReextractionStatus.FAILED
        )
        self.assertNotEqual(document.vision_reextraction_error, "")
        mock_async_task.assert_called_once_with(analyze_document_task, document.id)

    def test_no_args_is_a_no_op(self):
        with patch("django_q.tasks.async_task") as mock_async_task:
            extract_vision_markdown_hook(SimpleNamespace(success=False, args=[]))

        mock_async_task.assert_not_called()
