from io import StringIO
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase

from apps.ai.providers.fake import FakeGenerationProvider

from .models import Document

_VALID_REPLY = (
    '{"title": "Rechnung", "summary": "Kurze Zusammenfassung.", '
    '"key_facts": {"document_type": "Rechnung"}, "tag_suggestions": [], '
    '"vorgang_suggestions": []}'
)


class AnalyzeDocumentsCommandTests(TestCase):
    def setUp(self):
        self.provider = FakeGenerationProvider(model="fake-generate", version="1", reply=_VALID_REPLY)
        patcher = patch(
            "apps.documents.analysis.get_generation_provider",
            return_value=self.provider,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_requires_a_selection_mode(self):
        with self.assertRaises(CommandError):
            call_command("analyze_documents")

    def test_id_option_analyzes_only_that_document(self):
        target = Document.objects.create(title="A", text_content="hello world")
        other = Document.objects.create(title="B", text_content="second doc")

        call_command("analyze_documents", document_ids=[target.id])

        target.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(target.summary, "Kurze Zusammenfassung.")
        self.assertEqual(other.summary, "")

    def test_all_option_analyzes_every_document_with_text(self):
        with_text = Document.objects.create(title="A", text_content="hello world")
        without_text = Document.objects.create(title="B", text_content="")

        out = StringIO()
        call_command("analyze_documents", select_all=True, stdout=out)

        with_text.refresh_from_db()
        self.assertEqual(with_text.summary, "Kurze Zusammenfassung.")
        self.assertIn("1 erfolgreich, 0 fehlgeschlagen", out.getvalue())
        without_text.refresh_from_db()
        self.assertEqual(without_text.summary, "")

    def test_failed_option_selects_analysis_error_or_empty_summary(self):
        with_error = Document.objects.create(
            title="A", text_content="hello world", metadata={"analysis_error": "boom"}
        )
        empty_summary = Document.objects.create(
            title="B", text_content="second doc", summary=""
        )
        already_ok = Document.objects.create(
            title="C", text_content="third doc", summary="already analyzed"
        )

        call_command("analyze_documents", failed=True)

        with_error.refresh_from_db()
        empty_summary.refresh_from_db()
        already_ok.refresh_from_db()
        self.assertNotIn("analysis_error", with_error.metadata)
        self.assertEqual(with_error.summary, "Kurze Zusammenfassung.")
        self.assertEqual(empty_summary.summary, "Kurze Zusammenfassung.")
        self.assertEqual(already_ok.summary, "already analyzed")

    def test_since_option_narrows_the_selection(self):
        document = Document.objects.create(title="A", text_content="hello world")

        call_command("analyze_documents", select_all=True, since="2999-01-01")

        document.refresh_from_db()
        self.assertEqual(document.summary, "")

    def test_invalid_since_raises(self):
        with self.assertRaises(CommandError):
            call_command("analyze_documents", select_all=True, since="not-a-date")

    def test_queue_option_enqueues_instead_of_processing_inline(self):
        document = Document.objects.create(title="A", text_content="hello world")

        with patch("django_q.tasks.async_task") as mock_async_task:
            call_command("analyze_documents", select_all=True, queue=True)

        mock_async_task.assert_called_once()
        document.refresh_from_db()
        self.assertEqual(document.summary, "")

    def test_one_failure_does_not_abort_the_remaining_documents(self):
        ok_doc = Document.objects.create(title="ok", text_content="fine")
        bad_doc = Document.objects.create(title="bad", text_content="boom")

        real_generate = self.provider.generate

        def flaky_generate(messages, *, stream=False):
            text = " ".join(message.content for message in messages)
            if "boom" in text:
                raise RuntimeError("boom")
            return real_generate(messages, stream=stream)

        self.provider.generate = flaky_generate

        out = StringIO()
        call_command("analyze_documents", select_all=True, stdout=out)

        self.assertIn("1 erfolgreich, 1 fehlgeschlagen", out.getvalue())
        ok_doc.refresh_from_db()
        bad_doc.refresh_from_db()
        self.assertEqual(ok_doc.summary, "Kurze Zusammenfassung.")
        self.assertIn("boom", bad_doc.metadata["analysis_error"])
        self.assertEqual(ok_doc.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(bad_doc.processing_status, Document.ProcessingStatus.FAILED)

    def test_successful_analysis_leaves_processing_status_ready(self):
        document = Document.objects.create(
            title="A",
            text_content="hello world",
            processing_status=Document.ProcessingStatus.ANALYZING,
        )

        call_command("analyze_documents", document_ids=[document.id])

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)

    def test_rerun_is_idempotent_and_returns_to_ready(self):
        document = Document.objects.create(title="A", text_content="hello world")

        call_command("analyze_documents", document_ids=[document.id])
        call_command("analyze_documents", select_all=True)

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)

    def test_maintenance_run_leaves_a_reviewed_document_reviewed(self):
        """CLAUDE.md, "Wann der Status zurückfällt" (#1153): ein Wartungslauf

        über den Bestand darf jede vorherige Prüfung nicht stillschweigend
        entwerten.
        """
        document = Document.objects.create(
            title="A",
            text_content="hello world",
            processing_status=Document.ProcessingStatus.REVIEWED,
        )

        call_command("analyze_documents", document_ids=[document.id])

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.REVIEWED)

    def test_maintenance_run_moves_a_failed_reviewed_document_to_failed(self):
        """`failed` bleibt der einzige Ausweg aus einer erneut fehlschlagenden

        Analyse, unabhängig davon, ob das Dokument vorher geprüft war.
        """
        document = Document.objects.create(
            title="bad",
            text_content="boom",
            processing_status=Document.ProcessingStatus.REVIEWED,
        )

        def raising_generate(messages, *, stream=False):
            raise RuntimeError("boom")

        self.provider.generate = raising_generate

        call_command("analyze_documents", document_ids=[document.id])

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)

    def test_no_matching_documents_reports_and_does_not_error(self):
        out = StringIO()
        call_command("analyze_documents", select_all=True, stdout=out)

        self.assertIn("Keine Dokumente", out.getvalue())
