import datetime
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department

from .letter_render import render_draft_files
from .models import (
    Correspondent,
    Document,
    DocumentLink,
    LetterDraft,
    LetterTemplate,
    LetterTemplatePlaceholder,
    Vorgang,
    default_letter_layout,
)

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-letter-views-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class LetterDraftFlowTestCase(TestCase):
    """Gemeinsames Setup für den Brief-Ablauf (#1095): Vorlage, Kontakte,
    beantwortetes Dokument, Vorgang.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.other_dept = Department.objects.create(name="Dept B")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.other_user = User.objects.create_user(username="bob", password="x")
        self.other_user.departments.add(self.other_dept)

        self.self_contact = Correspondent.objects.create(
            name="Anna Beispiel", address="Hauptstr. 1\n10115 Berlin", is_self=True
        )
        self.recipient = Correspondent.objects.create(
            name="Amt für Alles", address="Amtsweg 2\n10117 Berlin"
        )
        self.vorgang = Vorgang.objects.create(name="Bescheid 4711")

        self.document = Document.objects.create(
            title="Bescheid vom 01.08.2026",
            correspondent=self.recipient,
            summary="Das Amt fordert 250 EUR nach.",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.document.departments.add(self.dept)
        self.document.vorgaenge.add(self.vorgang)

        self.template = LetterTemplate.objects.create(
            name="Widerspruch",
            instructions="## Ton\nSachlich.",
            signature="Anna Beispiel",
            visibility=LetterTemplate.Visibility.DEPARTMENT,
        )
        self.template.departments.add(self.dept)
        LetterTemplatePlaceholder.objects.create(
            template=self.template,
            key="aktenzeichen",
            label="Aktenzeichen",
            source="manual",
            required=True,
            order=1,
        )
        LetterTemplatePlaceholder.objects.create(
            template=self.template,
            key="empfaenger_adresse",
            label="Empfängeradresse",
            source="kontakt.address",
            order=2,
        )

    def _draft(self, *, user=None, department=None, **overrides):
        fields = {
            "template": self.template,
            "template_name": self.template.name,
            "source_document": self.document,
            "vorgang": self.vorgang,
            "recipient": self.recipient,
            "sender": self.self_contact,
            "status": LetterDraft.Status.READY,
            "subject": "Widerspruch gegen Bescheid 4711",
            "body_text": "Sehr geehrte Damen und Herren,\n\nhiermit widerspreche ich.",
            "letter_date": datetime.date(2026, 8, 9),
            "sender_block": "Anna Beispiel\nHauptstr. 1\n10115 Berlin",
            "recipient_block": "Amt für Alles\nAmtsweg 2\n10117 Berlin",
            "signature": "Anna Beispiel",
            "layout": default_letter_layout(),
            "placeholder_values": {"aktenzeichen": "AZ-1"},
            "owner": user or self.user,
        }
        fields.update(overrides)
        draft = LetterDraft.objects.create(**fields)
        draft.departments.add(department or self.dept)
        return draft


class LetterDraftStartViewTests(LetterDraftFlowTestCase):
    """Einstieg: Vorlage wählen, manuelle Platzhalter ausfüllen, Entwurf
    anstoßen -- vom Dokument-Detail (Hauptweg) oder vom Vorgang.
    """

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:letter_draft_start"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_start_page_offers_only_visible_templates(self):
        hidden = LetterTemplate.objects.create(name="Fremde Vorlage")
        hidden.departments.add(self.other_dept)
        self.client.force_login(self.user)

        response = self.client.get(
            f"{reverse('documents:letter_draft_start')}?document={self.document.pk}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Widerspruch")
        self.assertNotContains(response, "Fremde Vorlage")

    def test_document_outside_the_scope_is_not_a_usable_context(self):
        self.document.departments.set([self.other_dept])
        self.client.force_login(self.user)

        response = self.client.get(
            f"{reverse('documents:letter_draft_start')}?document={self.document.pk}"
        )

        self.assertEqual(response.status_code, 404)

    def test_choosing_a_template_shows_manual_fields_and_resolved_bindings(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("documents:letter_draft_start"),
            {"document": self.document.pk, "template": self.template.pk},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "manual_aktenzeichen")
        # Auto-Bindung: aufgelöst und sichtbar, aber kein Eingabefeld.
        self.assertContains(response, "Amtsweg 2")
        self.assertNotContains(response, "manual_empfaenger_adresse")

    def test_post_creates_a_running_draft_and_queues_the_worker(self):
        self.client.force_login(self.user)

        with patch("django_q.tasks.async_task") as async_task:
            response = self.client.post(
                reverse("documents:letter_draft_start"),
                {
                    "document": self.document.pk,
                    "vorgang": self.vorgang.pk,
                    "template": self.template.pk,
                    "manual_aktenzeichen": "AZ-2026-4711",
                    "notes": "Frist deutlich benennen",
                },
            )

        draft = LetterDraft.objects.get()
        self.assertRedirects(response, reverse("documents:letter_draft_detail", args=[draft.pk]))
        self.assertEqual(draft.status, LetterDraft.Status.RUNNING)
        self.assertEqual(draft.manual_values, {"aktenzeichen": "AZ-2026-4711"})
        self.assertEqual(draft.notes, "Frist deutlich benennen")
        self.assertEqual(draft.source_document, self.document)
        self.assertEqual(draft.vorgang, self.vorgang)
        self.assertEqual(draft.recipient, self.recipient)
        self.assertEqual(draft.sender, self.self_contact)
        self.assertIn("Amtsweg 2", draft.recipient_block)
        self.assertEqual(draft.signature, "Anna Beispiel")
        self.assertEqual(draft.layout["closing"], "Mit freundlichen Grüßen")
        self.assertEqual(list(draft.departments.all()), [self.dept])
        async_task.assert_called_once()

    def test_starting_a_draft_files_nothing(self):
        """Review-first: bis zur Freigabe entsteht kein Dokument."""
        self.client.force_login(self.user)
        before = Document.objects.count()

        with patch("django_q.tasks.async_task"):
            self.client.post(
                reverse("documents:letter_draft_start"),
                {
                    "document": self.document.pk,
                    "template": self.template.pk,
                    "manual_aktenzeichen": "AZ-1",
                },
            )

        self.assertEqual(Document.objects.count(), before)

    def test_missing_required_manual_value_blocks_the_draft(self):
        self.client.force_login(self.user)

        with patch("django_q.tasks.async_task") as async_task:
            response = self.client.post(
                reverse("documents:letter_draft_start"),
                {"document": self.document.pk, "template": self.template.pk},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(LetterDraft.objects.exists())
        async_task.assert_not_called()

    def test_vorgang_is_inherited_from_the_document(self):
        self.client.force_login(self.user)

        with patch("django_q.tasks.async_task"):
            self.client.post(
                reverse("documents:letter_draft_start"),
                {
                    "document": self.document.pk,
                    "template": self.template.pk,
                    "manual_aktenzeichen": "AZ-1",
                },
            )

        self.assertEqual(LetterDraft.objects.get().vorgang, self.vorgang)

    def test_entry_point_is_offered_on_the_document_detail(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        self.assertContains(response, "Antwortschreiben erstellen")
        self.assertContains(
            response, f"{reverse('documents:letter_draft_start')}?document={self.document.pk}"
        )

    def test_entry_point_is_offered_on_the_vorgang_hub(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Schreiben erstellen")


class LetterDraftReviewViewTests(LetterDraftFlowTestCase):
    """Die Werkbank: prüfen, bearbeiten, neu rendern, neu generieren."""

    def test_draft_of_another_department_is_not_reachable(self):
        draft = self._draft(department=self.other_dept)
        self.client.force_login(self.user)

        self.assertEqual(
            self.client.get(reverse("documents:letter_draft_detail", args=[draft.pk])).status_code,
            404,
        )

    def test_review_page_shows_text_files_and_provenance(self):
        draft = self._draft()
        render_draft_files(draft)
        self.client.force_login(self.user)

        response = self.client.get(reverse("documents:letter_draft_detail", args=[draft.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hiermit widerspreche ich.")
        self.assertContains(response, "Word herunterladen")
        self.assertContains(response, "PDF herunterladen")
        self.assertContains(response, "AZ-1")
        self.assertContains(response, "Freigeben")

    def test_panel_polls_while_the_worker_is_running(self):
        draft = self._draft(status=LetterDraft.Status.RUNNING)
        self.client.force_login(self.user)

        response = self.client.get(reverse("documents:letter_draft_panel", args=[draft.pk]))

        self.assertContains(response, "hx-trigger=\"every 3s\"")
        self.assertContains(response, "Der Entwurf wird formuliert")

    def test_editing_the_text_re_renders_both_formats(self):
        draft = self._draft()
        render_draft_files(draft)
        old_pdf_size = draft.pdf_file.size
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("documents:letter_draft_update", args=[draft.pk]),
            {
                "subject": "Widerspruch – überarbeitet",
                "body_text": "Sehr geehrte Damen und Herren,\n\nhiermit widerspreche ich "
                "ausdrücklich und fordere eine Neuberechnung.",
            },
        )

        draft.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(draft.subject, "Widerspruch – überarbeitet")
        self.assertIn("Neuberechnung", draft.body_text)
        self.assertTrue(draft.has_files)
        self.assertNotEqual(draft.pdf_file.size, old_pdf_size)

    def test_editing_clears_a_previous_failure(self):
        draft = self._draft(status=LetterDraft.Status.FAILED, error="Provider weg")
        self.client.force_login(self.user)

        self.client.post(
            reverse("documents:letter_draft_update", args=[draft.pk]),
            {"subject": "Selbst geschrieben", "body_text": "Sehr geehrte Damen und Herren, …"},
        )

        draft.refresh_from_db()
        self.assertEqual(draft.status, LetterDraft.Status.READY)
        self.assertEqual(draft.error, "")

    def test_a_filed_letter_is_no_longer_editable(self):
        document = Document.objects.create(title="Abgelegt")
        draft = self._draft(status=LetterDraft.Status.FINALIZED, document=document)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("documents:letter_draft_update", args=[draft.pk]),
            {"subject": "Doch noch anders", "body_text": "Text"},
        )

        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.subject, "Widerspruch gegen Bescheid 4711")

    def test_regenerate_queues_the_worker_and_keeps_the_current_text(self):
        draft = self._draft()
        self.client.force_login(self.user)

        with patch("django_q.tasks.async_task") as async_task:
            response = self.client.post(
                reverse("documents:letter_draft_regenerate", args=[draft.pk]),
                {"notes": "kürzer bitte"},
            )

        draft.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(draft.status, LetterDraft.Status.RUNNING)
        self.assertEqual(draft.notes, "kürzer bitte")
        self.assertIn("hiermit widerspreche ich.", draft.body_text)
        async_task.assert_called_once()

    def test_downloads_serve_both_formats_auth_gated(self):
        draft = self._draft()
        render_draft_files(draft)
        self.client.force_login(self.user)

        docx = self.client.get(reverse("documents:letter_draft_download", args=[draft.pk, "docx"]))
        pdf = self.client.get(reverse("documents:letter_draft_download", args=[draft.pk, "pdf"]))

        self.assertEqual(docx.status_code, 200)
        self.assertEqual(
            docx["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertEqual(pdf["X-Content-Type-Options"], "nosniff")
        self.assertTrue(b"".join(pdf.streaming_content).startswith(b"%PDF"))

    def test_download_of_a_foreign_draft_is_404(self):
        draft = self._draft(department=self.other_dept)
        render_draft_files(draft)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("documents:letter_draft_download", args=[draft.pk, "pdf"])
        )

        self.assertEqual(response.status_code, 404)

    def test_unknown_download_format_is_404(self):
        draft = self._draft()
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("documents:letter_draft_download", args=[draft.pk, "odt"])
        )

        self.assertEqual(response.status_code, 404)

    def test_discarding_a_draft_removes_it(self):
        draft = self._draft()
        render_draft_files(draft)
        self.client.force_login(self.user)

        response = self.client.post(reverse("documents:letter_draft_delete", args=[draft.pk]))

        self.assertRedirects(response, reverse("documents:detail", args=[self.document.pk]))
        self.assertFalse(LetterDraft.objects.exists())


class LetterDraftFinalizeViewTests(LetterDraftFlowTestCase):
    """Die Freigabe: der einzige Pfad, der ein Dokument anlegt."""

    def _finalize(self, draft):
        """Die Freigabe reiht das Embedding per `transaction.on_commit` ein --
        ohne `captureOnCommitCallbacks` liefe der Callback im Test nie.
        """
        self.client.force_login(self.user)
        with patch("django_q.tasks.async_task") as async_task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("documents:letter_draft_finalize", args=[draft.pk])
                )
        return response, async_task

    def test_filing_creates_the_document_at_the_vorgang(self):
        draft = self._draft()
        render_draft_files(draft)

        response, async_task = self._finalize(draft)

        draft.refresh_from_db()
        document = draft.document
        self.assertRedirects(response, reverse("documents:detail", args=[document.pk]))
        self.assertEqual(draft.status, LetterDraft.Status.FINALIZED)
        self.assertEqual(document.source, Document.Source.GENERATED_LETTER)
        self.assertEqual(document.direction, Document.Direction.AUSGANG)
        self.assertEqual(document.correspondent, self.recipient)
        self.assertEqual(document.document_date, datetime.date(2026, 8, 9))
        self.assertEqual(list(document.vorgaenge.all()), [self.vorgang])
        self.assertEqual(list(document.departments.all()), [self.dept])
        self.assertEqual(document.owner, self.user)
        self.assertEqual(document.title, "Widerspruch gegen Bescheid 4711")
        async_task.assert_called_once()

    def test_the_filed_document_carries_the_pdf_and_the_letter_text(self):
        draft = self._draft()
        render_draft_files(draft)

        self._finalize(draft)

        document = LetterDraft.objects.get().document
        self.assertEqual(document.mime_type, "application/pdf")
        self.assertTrue(document.original_file.name.endswith(".pdf"))
        with document.original_file.open("rb") as handle:
            self.assertTrue(handle.read(4) == b"%PDF")
        self.assertIn("hiermit widerspreche ich.", document.text_content)
        self.assertIn("Amt für Alles", document.text_content)
        self.assertEqual(document.key_facts["document_type"], "Brief")
        self.assertEqual(document.key_facts["recipient_name"], "Amt für Alles")

    def test_the_filed_document_is_linked_to_the_answered_document(self):
        draft = self._draft()
        render_draft_files(draft)

        self._finalize(draft)

        document = LetterDraft.objects.get().document
        link = DocumentLink.objects.for_document(document).get()
        self.assertEqual(link.other_document_id(document), self.document.pk)
        self.assertEqual(link.note, "Antwortschreiben")

    def test_filing_is_idempotent(self):
        draft = self._draft()
        render_draft_files(draft)

        self._finalize(draft)
        self._finalize(draft)

        self.assertEqual(Document.objects.filter(source=Document.Source.GENERATED_LETTER).count(), 1)

    def test_filing_renders_missing_files_instead_of_refusing(self):
        """Wenn das Rendering nach der Generierung schiefging, holt die
        Freigabe es nach -- der Text ist ja da.
        """
        draft = self._draft()

        self._finalize(draft)

        draft.refresh_from_db()
        self.assertTrue(draft.has_files)
        self.assertIsNotNone(draft.document)

    def test_a_running_draft_cannot_be_filed(self):
        draft = self._draft(status=LetterDraft.Status.RUNNING)

        response, _async_task = self._finalize(draft)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Document.objects.filter(source=Document.Source.GENERATED_LETTER).exists())

    def test_the_document_detail_links_the_word_master(self):
        draft = self._draft()
        render_draft_files(draft)
        self._finalize(draft)
        document = LetterDraft.objects.get().document

        response = self.client.get(reverse("documents:detail", args=[document.pk]))

        self.assertContains(response, "Word-Master herunterladen")
        self.assertContains(
            response, reverse("documents:letter_draft_download", args=[draft.pk, "docx"])
        )

    def test_discarding_after_filing_keeps_the_document(self):
        draft = self._draft()
        render_draft_files(draft)
        self._finalize(draft)
        document = LetterDraft.objects.get().document

        self.client.post(reverse("documents:letter_draft_delete", args=[draft.pk]))

        self.assertFalse(LetterDraft.objects.exists())
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
