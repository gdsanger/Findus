"""Seitenansicht und Bearbeitungs-Endpunkte der Scan-Korrektur (#1155).

Prüft die Zusagen, die man einem Nutzer gegeben hat, bevor er auf
"Jetzt ausführen" klickt: Sichtbarkeit (fremdes Dokument = 404, nicht
403), Auslieferung der Vorschaubilder nur über den auth-gestützten
Stream, "nichts passiert vor der Bestätigung" -- und die Randfälle, die
sonst als 500er-Seite enden.
"""

import hashlib
import io
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department

from .models import (
    Document,
    DocumentComment,
    DocumentPagePreview,
    DocumentPdfEditRun,
    link_documents,
)
from .test_extraction import _make_pdf

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-pdf-edit-views-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def tearDownModule():
    shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT, STORAGES=_LOCAL_STORAGES)
class PdfEditViewTestCase(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)
        self.client.force_login(self.user)

        self.data = _make_pdf(["Seite eins", "Seite zwei", "Seite drei"])
        self.document = self._pdf_document("Sammelscan", self.data)

    def _pdf_document(self, title, data, **kwargs):
        document = Document.objects.create(
            title=title,
            owner=self.user,
            processing_status=kwargs.pop(
                "processing_status", Document.ProcessingStatus.READY
            ),
            metadata={"mime_type": "application/pdf", "original_filename": "scan.pdf"},
            sha256=hashlib.sha256(data).hexdigest(),
            **kwargs,
        )
        document.original_file.save("scan.pdf", io.BytesIO(data), save=True)
        document.departments.add(self.department)
        return document


class PageViewAccessTests(PdfEditViewTestCase):
    def test_page_view_renders_one_entry_per_page(self):
        response = self.client.get(reverse("documents:pages", args=[self.document.pk]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('name="delete_pages"', content)
        for page in (1, 2, 3):
            self.assertIn(f'value="{page}"', content)
        # Schnittmarken sitzen zwischen den Seiten: bei drei Seiten zwei.
        self.assertIn("split_before_2", content)
        self.assertIn("split_before_3", content)
        self.assertNotIn("split_before_1", content)

    def test_a_single_page_document_offers_no_split(self):
        document = self._pdf_document("Einseiter", _make_pdf(["nur eine"]))

        response = self.client.get(reverse("documents:pages", args=[document.pk]))

        self.assertNotContains(response, "split_before_")

    def test_a_non_pdf_document_has_no_page_view(self):
        document = Document.objects.create(
            title="Bild",
            owner=self.user,
            processing_status=Document.ProcessingStatus.READY,
            metadata={"mime_type": "image/png", "original_filename": "foto.png"},
        )
        document.original_file.save("foto.png", io.BytesIO(b"nicht wirklich png"), save=True)
        document.departments.add(self.department)

        response = self.client.get(reverse("documents:pages", args=[document.pk]))

        self.assertEqual(response.status_code, 404)

    def test_a_foreign_document_is_404_not_403(self):
        """Der Endpunkt darf nie verraten, dass eine PK überhaupt
        existiert."""
        other = User.objects.create_user(username="bob", password="x")
        other.departments.add(Department.objects.create(name="Fremd"))
        self.client.force_login(other)

        for url in (
            reverse("documents:pages", args=[self.document.pk]),
            reverse("documents:page_previews", args=[self.document.pk]),
            reverse("documents:page_image", args=[self.document.pk, 1]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for url in (
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            reverse("documents:pdf_edit_apply", args=[self.document.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_a_document_still_processing_is_blocked(self):
        document = self._pdf_document(
            "In Arbeit",
            _make_pdf(["eins", "zwei"]),
            processing_status=Document.ProcessingStatus.EXTRACTING,
        )

        response = self.client.get(reverse("documents:pages", args=[document.pk]))

        self.assertContains(response, "noch verarbeitet")
        self.assertNotContains(response, "delete_pages")

    def test_a_broken_file_gets_a_message_not_a_500(self):
        document = self._pdf_document("Kaputt", b"das ist kein PDF")

        response = self.client.get(reverse("documents:pages", args=[document.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "beschädigt")

    def test_the_detail_page_offers_the_entry_only_for_pdf(self):
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))
        self.assertContains(response, reverse("documents:pages", args=[self.document.pk]))

        other = Document.objects.create(
            title="Textdatei",
            owner=self.user,
            processing_status=Document.ProcessingStatus.READY,
            metadata={"mime_type": "text/plain", "original_filename": "notiz.txt"},
        )
        other.original_file.save("notiz.txt", io.BytesIO(b"hallo"), save=True)
        other.departments.add(self.department)

        response = self.client.get(reverse("documents:detail", args=[other.pk]))
        self.assertNotContains(response, reverse("documents:pages", args=[other.pk]))


class PagePreviewStreamTests(PdfEditViewTestCase):
    def test_previews_are_served_through_the_auth_gated_stream(self):
        self.client.get(reverse("documents:pages", args=[self.document.pk]))

        response = self.client.get(
            reverse("documents:page_image", args=[self.document.pk, 1])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])

    def test_the_page_view_never_links_a_storage_url(self):
        """Die Storage-URL ist ein öffentlicher S3-/MinIO-Link und umgeht
        die Sichtbarkeitsprüfung vollständig."""
        self.client.get(reverse("documents:pages", args=[self.document.pk]))
        preview = DocumentPagePreview.objects.filter(document=self.document).first()
        self.assertIsNotNone(preview)

        response = self.client.get(reverse("documents:pages", args=[self.document.pk]))

        content = response.content.decode()
        self.assertNotIn(preview.image.url, content)
        self.assertIn(
            reverse("documents:page_image", args=[self.document.pk, 1]), content
        )

    def test_an_empty_looking_page_is_marked_but_not_preselected(self):
        """Die Kennzeichnung ist ein Hinweis: eine Seite mit nur einem
        Stempel sieht für die Texterkennung leer aus und ist es nicht."""
        self.document.markdown = (
            "# Scan\n\n## Seite 1\n\nEin ordentlich langer Absatz mit Inhalt.\n\n"
            "## Seite 2\n\n_Kein Text erkannt._\n\n## Seite 3\n\nUnd hier noch mehr Text.\n"
        )
        self.document.save(update_fields=["markdown"])

        response = self.client.get(reverse("documents:pages", args=[self.document.pk]))

        content = response.content.decode()
        self.assertIn("Kein Text erkannt", content)
        marker = 'id="delete-page-2"'
        checkbox = content[content.index(marker) : content.index(marker) + 200]
        self.assertNotIn("checked", checkbox)

    def test_a_missing_preview_is_a_404(self):
        response = self.client.get(
            reverse("documents:page_image", args=[self.document.pk, 99])
        )

        self.assertEqual(response.status_code, 404)


class ConfirmStepTests(PdfEditViewTestCase):
    def test_confirmation_summarises_the_plan_in_words(self):
        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["2"], "rotate_1": "90", "split_before_3": "1"},
        )

        self.assertContains(response, "Seite 2 wird entfernt")
        self.assertContains(response, "Seite 1 wird um 90° gedreht")
        self.assertContains(response, "in 2 Dokumente aufgeteilt")
        self.assertContains(response, "das Original wird gelöscht")

    def test_confirmation_changes_nothing(self):
        before = Document.objects.get(pk=self.document.pk)

        self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["2"]},
        )

        after = Document.objects.get(pk=self.document.pk)
        self.assertEqual(after.sha256, before.sha256)
        self.assertEqual(after.original_file.name, before.original_file.name)
        self.assertFalse(DocumentPdfEditRun.objects.exists())

    def test_confirmation_names_what_is_lost_with_the_original(self):
        DocumentComment.objects.create(document=self.document, body="Wiedervorlage")
        DocumentComment.objects.create(document=self.document, body="Noch etwas")
        neighbour = Document.objects.create(title="Nachbar", owner=self.user)
        link_documents(self.document, neighbour, created_by=self.user)

        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"split_before_2": "1"},
        )

        self.assertContains(response, "2 Kommentare")
        self.assertContains(response, "1 Verknüpfung")

    def test_the_confirmation_names_children_that_would_be_deleted(self):
        """Unterdokumente hängen per CASCADE am Original und gingen sonst
        wortlos mit."""
        child = Document.objects.create(
            title="Anhang", owner=self.user, parent=self.document
        )
        child.departments.add(self.department)

        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"split_before_2": "1"},
        )

        self.assertContains(response, "1 Unterdokument")

    def test_without_a_split_nothing_is_said_about_losses(self):
        DocumentComment.objects.create(document=self.document, body="Wiedervorlage")

        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["2"]},
        )

        self.assertNotContains(response, "geht verloren")

    def test_deleting_every_page_is_refused(self):
        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["1", "2", "3"]},
        )

        self.assertContains(response, "Ein Dokument ohne")
        self.assertNotContains(response, "Jetzt ausführen")

    def test_an_empty_selection_is_refused(self):
        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]), {}
        )

        self.assertContains(response, "nichts ausgewählt")

    def test_a_page_number_outside_the_document_is_an_inline_error(self):
        """Ein unsinniger Wert soll als Meldung landen, nicht als 500."""
        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["99"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Jetzt ausführen")

    def test_the_confirmation_replays_the_same_field_names(self):
        response = self.client.post(
            reverse("documents:pdf_edit_confirm", args=[self.document.pk]),
            {"delete_pages": ["2"], "rotate_1": "90"},
        )

        content = response.content.decode()
        self.assertIn('name="delete_pages" value="2"', content)
        self.assertIn('name="rotate_1" value="90"', content)


class ApplyTests(PdfEditViewTestCase):
    def test_apply_starts_exactly_one_background_job(self):
        with patch("django_q.tasks.async_task") as async_task:
            response = self.client.post(
                reverse("documents:pdf_edit_apply", args=[self.document.pk]),
                {"delete_pages": ["2"]},
            )

        self.assertContains(response, "Die Bearbeitung läuft")
        self.assertEqual(DocumentPdfEditRun.objects.count(), 1)
        run = DocumentPdfEditRun.objects.get()
        self.assertEqual(run.plan["deletions"], [2])
        self.assertEqual(run.status, DocumentPdfEditRun.Status.RUNNING)
        self.assertEqual(async_task.call_count, 1)
        self.assertEqual(
            async_task.call_args.kwargs["timeout"],
            settings.FINDUS_PDF_EDIT_TASK_TIMEOUT_SECONDS,
        )

    def test_a_second_trigger_does_not_start_a_second_run(self):
        with patch("django_q.tasks.async_task") as async_task:
            self.client.post(
                reverse("documents:pdf_edit_apply", args=[self.document.pk]),
                {"split_before_2": "1"},
            )
            self.client.post(
                reverse("documents:pdf_edit_apply", args=[self.document.pk]),
                {"split_before_2": "1"},
            )

        self.assertEqual(DocumentPdfEditRun.objects.count(), 1)
        self.assertEqual(async_task.call_count, 1)

    def test_apply_refuses_a_document_still_processing(self):
        document = self._pdf_document(
            "In Arbeit",
            _make_pdf(["eins", "zwei"]),
            processing_status=Document.ProcessingStatus.EXTRACTING,
        )

        with patch("django_q.tasks.async_task") as async_task:
            response = self.client.post(
                reverse("documents:pdf_edit_apply", args=[document.pk]),
                {"delete_pages": ["2"]},
            )

        self.assertContains(response, "noch verarbeitet")
        self.assertFalse(DocumentPdfEditRun.objects.exists())
        async_task.assert_not_called()


class RunStatusTests(PdfEditViewTestCase):
    def test_the_result_of_a_split_survives_the_deleted_original(self):
        run = DocumentPdfEditRun.objects.create(
            document=None,
            document_title="Sammelscan",
            created_by=self.user,
            mode=DocumentPdfEditRun.Mode.SPLIT,
            status=DocumentPdfEditRun.Status.READY,
            result={
                "parts": [
                    {
                        "index": 1,
                        "document_id": self.document.pk,
                        "title": self.document.title,
                        "pages": [1, 2],
                        "duplicate": False,
                    },
                    {
                        "index": 2,
                        "document_id": 999999,
                        "title": "Teil 2",
                        "pages": [3],
                        "duplicate": True,
                    },
                ]
            },
        )

        response = self.client.get(reverse("documents:pdf_edit_status", args=[run.pk]))

        self.assertContains(response, "Aufgeteilt in 2 Dokumente")
        self.assertContains(response, "bereits vorhandenen Dokument")
        self.assertContains(response, reverse("documents:detail", args=[self.document.pk]))

    def test_a_foreign_run_is_404(self):
        run = DocumentPdfEditRun.objects.create(
            document=self.document, created_by=self.user, plan={}
        )
        other = User.objects.create_user(username="bob", password="x")
        self.client.force_login(other)

        response = self.client.get(reverse("documents:pdf_edit_status", args=[run.pk]))

        self.assertEqual(response.status_code, 404)

    def test_a_stalled_run_ends_in_a_terminal_failure(self):
        """Sonst bleibt die Oberfläche auf einem endlosen Ladeindikator
        stehen."""
        run = DocumentPdfEditRun.objects.create(
            document=self.document, created_by=self.user, plan={}
        )

        with override_settings(FINDUS_PDF_EDIT_POLL_TIMEOUT_SECONDS=0):
            response = self.client.get(reverse("documents:pdf_edit_status", args=[run.pk]))

        run.refresh_from_db()
        self.assertEqual(run.status, DocumentPdfEditRun.Status.FAILED)
        self.assertContains(response, "fehlgeschlagen")
