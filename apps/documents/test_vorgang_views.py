import datetime
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department

from .models import (
    Document,
    Task,
    Vorgang,
    VorgangRecommendation,
    VorgangRecommendationRun,
)

User = get_user_model()


class VorgangListViewTests(TestCase):
    """Covers the Vorgänge index (#1040): all Vorgänge are listed (shared
    vocabulary, no `visibility` of its own -- see `Vorgang`), while the
    document count/last activity per row respect `visible_to`.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.vorgang_a = Vorgang.objects.create(name="Steuererklärung 2026")
        self.vorgang_b = Vorgang.objects.create(name="Nebenkostenabrechnung")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.own_doc.departments.add(self.dept_a)
        self.own_doc.vorgaenge.add(self.vorgang_a)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.other_dept_doc.departments.add(self.dept_b)
        self.other_dept_doc.vorgaenge.add(self.vorgang_a)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:vorgang_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_lists_all_vorgaenge_regardless_of_document_visibility(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        self.assertContains(response, "Steuererklärung 2026")
        self.assertContains(response, "Nebenkostenabrechnung")

    def test_document_count_only_reflects_visible_documents(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        open_vorgaenge = response.context["open_vorgaenge"]
        matched = next(v for v in open_vorgaenge if v.pk == self.vorgang_a.pk)
        self.assertEqual(matched.document_count, 1)

    def test_search_narrows_by_name(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"), {"q": "Steuer"})

        self.assertContains(response, "Steuererklärung 2026")
        self.assertNotContains(response, "Nebenkostenabrechnung")

    def test_search_narrows_by_description(self):
        self.vorgang_b.description = "Betrifft die Nebenkosten fuer 2026"
        self.vorgang_b.save()

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"), {"q": "Nebenkosten fuer"})

        self.assertContains(response, "Nebenkostenabrechnung")
        self.assertNotContains(response, "Steuererklärung 2026")

    def test_row_links_to_hub(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        self.assertContains(response, reverse("documents:vorgang_detail", args=[self.vorgang_a.pk]))

    def test_open_and_closed_vorgaenge_are_grouped_separately(self):
        closed = Vorgang.objects.create(name="Altfall abgeschlossen", status=Vorgang.Status.CLOSED)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        open_names = [v.name for v in response.context["open_vorgaenge"]]
        closed_names = [v.name for v in response.context["closed_vorgaenge"]]
        self.assertIn(self.vorgang_a.name, open_names)
        self.assertIn(self.vorgang_b.name, open_names)
        self.assertNotIn(closed.name, open_names)
        self.assertEqual(closed_names, [closed.name])

    def test_abgeschlossen_badge_is_shown_for_closed_vorgang(self):
        Vorgang.objects.create(name="Altfall abgeschlossen", status=Vorgang.Status.CLOSED)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        self.assertContains(response, "text-bg-success")
        self.assertContains(response, "Abgeschlossen")

    def test_status_filter_open_excludes_closed_vorgaenge(self):
        Vorgang.objects.create(name="Altfall abgeschlossen", status=Vorgang.Status.CLOSED)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"), {"status": "open"})

        self.assertContains(response, self.vorgang_a.name)
        self.assertNotContains(response, "Altfall abgeschlossen")

    def test_status_filter_closed_only_shows_closed_vorgaenge(self):
        closed = Vorgang.objects.create(name="Altfall abgeschlossen", status=Vorgang.Status.CLOSED)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"), {"status": "closed"})

        self.assertEqual(response.context["open_vorgaenge"], [])
        self.assertEqual([v.pk for v in response.context["closed_vorgaenge"]], [closed.pk])


class VorgangDetailViewTests(TestCase):
    """Covers the Vorgang hub (#1040): the editable Vorgangsdaten plus its
    Kennzahlen, and the reused/vorgang-scoped document list
    (`views.filtered_documents` + `_document_list.html`, further
    filterable by Tag/Status/Richtung).

    Der Hub listet bewusst keine verknüpften Aufgaben mehr -- Aufgaben sind
    auf dem Rückzug (CLAUDE.md); was aus einer Handlungsempfehlung
    übernommen wurde, verlinkt das Empfehlungs-Panel selbst.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.vorgang = Vorgang.objects.create(name="Steuererklärung 2026")
        self.other_vorgang = Vorgang.objects.create(name="Sonstiges")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.own_doc.departments.add(self.dept_a)
        self.own_doc.vorgaenge.add(self.vorgang)

        self.unrelated_doc = Document.objects.create(
            title="Anderer Vorgang",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.unrelated_doc.departments.add(self.dept_a)
        self.unrelated_doc.vorgaenge.add(self.other_vorgang)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.other_dept_doc.departments.add(self.dept_b)
        self.other_dept_doc.vorgaenge.add(self.vorgang)

        self.task = Task.objects.create(
            title="Belege einreichen",
            status=Task.Status.OPEN,
            visibility=Task.Visibility.DEPARTMENT,
        )
        self.task.departments.add(self.dept_a)
        self.task.documents.add(self.own_doc)

        self.done_task = Task.objects.create(
            title="Bereits erledigt",
            status=Task.Status.DONE,
            visibility=Task.Visibility.DEPARTMENT,
        )
        self.done_task.departments.add(self.dept_a)
        self.done_task.documents.add(self.own_doc)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_unknown_vorgang_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_header_shows_name_and_kennzahlen(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Steuererklärung 2026")
        self.assertEqual(response.context["document_count"], 1)

    def test_header_shows_high_contrast_abgeschlossen_badge_for_closed_vorgang(self):
        self.vorgang.status = Vorgang.Status.CLOSED
        self.vorgang.save()

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "text-bg-success")
        self.assertContains(response, "Abgeschlossen")

    def test_document_list_is_scoped_to_this_vorgang(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Anderer Vorgang")

    def test_document_list_respects_visibility(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_document_list_further_filterable_by_status(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk]),
            {"status": Document.ProcessingStatus.FAILED},
        )

        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_htmx_request_returns_bare_document_list_partial(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk]), HTTP_HX_REQUEST="true"
        )

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_hub_no_longer_lists_linked_tasks(self):
        """Aufgaben sind auf dem Rückzug (CLAUDE.md): der Hub zeigt keinen

        "Verknüpfte Aufgaben"-Block mehr. Der Test hält das fest, damit die
        Tabelle nicht beim nächsten Umbau versehentlich zurückkommt.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertNotContains(response, "Belege einreichen")
        self.assertNotIn("tasks", response.context)

    def test_document_list_defaults_to_grid_view(self):
        """Kachel ist auch auf dem Vorgang-Hub der Default (#1124) -- dieselbe

        Ansicht wie auf Home, damit sie beim Wechsel Home <-> Hub nicht
        springt. Die Zeitleiste bleibt über den Umschalter erreichbar.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "findus-doc-grid")
        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-timeline\"")

    def test_view_toggle_hides_the_wiedervorlagen_mode(self):
        """Die Wiedervorlagen-Ansicht (#1129) listet DocumentComment und gibt
        es nur auf Home -- der Umschalter wird hier ausgeblendet statt einen
        Knopf anzubieten, der still auf die Tabelle zurückfällt.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, 'data-view-value="grid"')
        self.assertNotContains(response, 'data-view-value="wiedervorlagen"')

    def test_document_list_offers_timeline_view(self):
        """The Vorgang hub reuses the shared document-list block (#1087) --

        the Timeline view is available here too, still scoped to this
        Vorgang, without a second document-list implementation.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk]), {"view": "timeline"}
        )

        self.assertContains(response, "findus-timeline\"")
        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Anderer Vorgang")

    def test_hub_offers_inline_edit_form_and_delete_action(self):
        """The Vorgang-Hub (#1050) edits/deletes directly -- the shared
        Stammdaten page (#1021) it used to detour through is gone entirely
        (#1051).
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, reverse("documents:vorgang_detail", args=[self.vorgang.pk]))
        self.assertContains(response, reverse("documents:vorgang_delete", args=[self.vorgang.pk]))


class VorgangCreateViewTests(TestCase):
    """Covers "Neu anlegen" on the Vorgänge-Index (#1050)."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:vorgang_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_create_vorgang_redirects_to_its_hub(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:vorgang_create"), {"name": "Umzug 2026", "status": Vorgang.Status.OPEN}
        )

        vorgang = Vorgang.objects.get(name="Umzug 2026")
        self.assertRedirects(response, reverse("documents:vorgang_detail", args=[vorgang.pk]))

    def test_create_vorgang_with_description_status_and_department(self):
        dept = Department.objects.create(name="Finanzen")

        self.client.force_login(self.user)
        self.client.post(
            reverse("documents:vorgang_create"),
            {
                "name": "Umzug 2026",
                "description": "Umzug ins neue Büro",
                "status": Vorgang.Status.IN_PROGRESS,
                "department": dept.pk,
            },
        )

        vorgang = Vorgang.objects.get(name="Umzug 2026")
        self.assertEqual(vorgang.description, "Umzug ins neue Büro")
        self.assertEqual(vorgang.status, Vorgang.Status.IN_PROGRESS)
        self.assertEqual(vorgang.department, dept)

    def test_create_with_duplicate_name_shows_error_and_does_not_create(self):
        Vorgang.objects.create(name="Umzug 2026")

        self.client.force_login(self.user)
        response = self.client.post(reverse("documents:vorgang_create"), {"name": "Umzug 2026"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Vorgang.objects.count(), 1)


class VorgangEditViewTests(TestCase):
    """Covers editing Vorgangsdaten directly on the hub (#1050), replacing
    the old Stammdaten edit page (#1021).
    """

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.vorgang = Vorgang.objects.create(name="Umzug 2026")

    def test_edit_updates_name_description_status_and_department(self):
        dept = Department.objects.create(name="Finanzen")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk]),
            {
                "name": "Umzug 2027",
                "description": "Verschoben",
                "status": Vorgang.Status.CLOSED,
                "department": dept.pk,
            },
        )

        self.assertRedirects(response, reverse("documents:vorgang_detail", args=[self.vorgang.pk]))
        self.vorgang.refresh_from_db()
        self.assertEqual(self.vorgang.name, "Umzug 2027")
        self.assertEqual(self.vorgang.description, "Verschoben")
        self.assertEqual(self.vorgang.status, Vorgang.Status.CLOSED)
        self.assertEqual(self.vorgang.department, dept)

    def test_invalid_edit_reshows_form_with_errors(self):
        other = Vorgang.objects.create(name="Sonstiges")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk]),
            {"name": other.name, "status": Vorgang.Status.OPEN},
        )

        self.assertEqual(response.status_code, 200)
        self.vorgang.refresh_from_db()
        self.assertEqual(self.vorgang.name, "Umzug 2026")


class VorgangDeleteViewTests(TestCase):
    """Covers deleting a Vorgang from its hub (#1050): only the assignment
    on affected Documents is dropped, never the Documents themselves.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.vorgang = Vorgang.objects.create(name="Umzug 2026")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(reverse("documents:vorgang_delete", args=[self.vorgang.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_delete_requires_post(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:vorgang_delete", args=[self.vorgang.pk]))

        self.assertEqual(response.status_code, 405)

    def test_delete_removes_vorgang_and_redirects_to_list(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("documents:vorgang_delete", args=[self.vorgang.pk]))

        self.assertRedirects(response, reverse("documents:vorgang_list"))
        self.assertFalse(Vorgang.objects.filter(pk=self.vorgang.pk).exists())

    def test_delete_in_use_removes_only_the_assignment_not_the_document(self):
        doc = Document.objects.create(title="Rechnung")
        doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user)
        self.client.post(reverse("documents:vorgang_delete", args=[self.vorgang.pk]))

        doc.refresh_from_db()
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())
        self.assertEqual(doc.vorgaenge.count(), 0)


_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-vorgang-upload-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class VorgangDocumentUploadViewTests(TestCase):
    """Covers the Vorgang-Hub multi-upload with Auto-Zuweisung (#1049):

    same ingest contract as the global upload (#1019), just with the
    newly created `Document` added to this `Vorgang`'s `vorgaenge` M2M
    right away -- the KI-Analyse (#1048) only ever *suggests* Vorgaenge
    (`VorgangSuggestion`), it never assigns one on its own, so this manual
    assignment can never be overwritten by it.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.vorgang = Vorgang.objects.create(name="Steuererklärung 2026")

    def _upload(self, files, user=None):
        self.client.force_login(user or self.user_a)
        with patch("apps.ingest.service._enqueue_processing", return_value="task-1"):
            return self.client.post(
                reverse("documents:vorgang_upload", args=[self.vorgang.pk]), {"files": files}
            )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(
            reverse("documents:vorgang_upload", args=[self.vorgang.pk]),
            {"files": [SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf")]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_unknown_vorgang_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:vorgang_upload", args=[999999]),
            {"files": [SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf")]},
        )

        self.assertEqual(response.status_code, 404)

    def test_uploaded_document_is_assigned_to_this_vorgang(self):
        response = self._upload(
            [SimpleUploadedFile("rechnung.pdf", b"%PDF-1.4 content", content_type="application/pdf")]
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hochgeladen")
        document = Document.objects.get()
        self.assertIn(self.vorgang, document.vorgaenge.all())
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PENDING)

    def test_multiple_files_are_all_assigned_to_this_vorgang(self):
        response = self._upload(
            [
                SimpleUploadedFile("a.pdf", b"content a", content_type="application/pdf"),
                SimpleUploadedFile("b.pdf", b"content b", content_type="application/pdf"),
            ]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Document.objects.count(), 2)
        for document in Document.objects.all():
            self.assertIn(self.vorgang, document.vorgaenge.all())

    def test_upload_response_triggers_list_refresh(self):
        response = self._upload(
            [SimpleUploadedFile("rechnung.pdf", b"%PDF-1.4 content", content_type="application/pdf")]
        )

        self.assertEqual(response["HX-Trigger"], "findus:documents-changed")

    def test_duplicate_document_keeps_its_existing_vorgaenge(self):
        other_vorgang = Vorgang.objects.create(name="Sonstiges")
        existing = Document.objects.create(
            title="Bereits vorhanden",
            visibility=Document.Visibility.DEPARTMENT,
            sha256="a" * 64,
        )
        existing.departments.add(self.dept_a)
        existing.vorgaenge.add(other_vorgang)

        with patch("apps.ingest.service._hash_and_size", return_value=("a" * 64, 3)):
            response = self._upload(
                [SimpleUploadedFile("copy.pdf", b"abc", content_type="application/pdf")]
            )

        self.assertContains(response, "Duplikat")
        existing.refresh_from_db()
        self.assertEqual(list(existing.vorgaenge.all()), [other_vorgang])


class VorgangRecommendationViewTests(TestCase):
    """Covers the "Handlungsempfehlungen"-Panel am Vorgang-Hub (#1093):
    Generieren auf Knopfdruck (async), Provenienz/Disclaimer in der
    Anzeige, und die 1-Klick-Übernahme in eine Aufgabe (#1012).
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.vorgang = Vorgang.objects.create(name="Forderung Acme")

        self.document = Document.objects.create(
            title="Mahnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.document.departments.add(self.dept_a)
        self.document.vorgaenge.add(self.vorgang)

        self.hidden_document = Document.objects.create(
            title="Fremdakte", visibility=Document.Visibility.DEPARTMENT
        )
        self.hidden_document.departments.add(self.dept_b)
        self.hidden_document.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)

    def _ready_run(self, **kwargs):
        run = VorgangRecommendationRun.objects.create(
            vorgang=self.vorgang,
            status=VorgangRecommendationRun.Status.READY,
            situation="Die Forderung ist offen.",
            generated_at=timezone.now(),
            based_on={
                # So, wie ein von `user_a` ausgeloester Lauf es hinterlassen
                # wuerde: nur dessen sichtbare Dokumente.
                "document_ids": [self.document.pk],
                "considered_document_ids": [self.document.pk],
                "document_count": 1,
                "used_count": 1,
                "truncated": False,
                "latest_document_date": None,
            },
            **kwargs,
        )
        recommendation = VorgangRecommendation.objects.create(
            run=run,
            title="Mahnung fristgerecht beantworten",
            rationale="Die Frist läuft am 15.09.2026 ab.",
            due_date=datetime.date(2026, 9, 15),
            priority=VorgangRecommendation.Priority.HIGH,
        )
        recommendation.documents.set([self.document, self.hidden_document])
        return run, recommendation

    def test_hub_shows_generate_button_and_disclaimer_without_a_run(self):
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Handlungsempfehlungen")
        self.assertContains(response, "Empfehlungen generieren")
        self.assertContains(response, "Noch keine Handlungsempfehlungen")
        self.assertContains(response, "keine Rechts-/Steuerberatung")

    def test_generate_marks_the_run_running_and_queues_the_worker(self):
        from apps.documents.tasks import generate_vorgang_recommendations_task

        with patch("django_q.tasks.async_task") as mock_async_task:
            mock_async_task.return_value = "task-1"
            response = self.client.post(
                reverse("documents:vorgang_recommendations_generate", args=[self.vorgang.pk])
            )

        self.assertEqual(response.status_code, 200)
        run = VorgangRecommendationRun.objects.get(vorgang=self.vorgang)
        self.assertEqual(run.status, VorgangRecommendationRun.Status.RUNNING)
        mock_async_task.assert_called_once_with(
            generate_vorgang_recommendations_task, self.vorgang.pk, self.user_a.pk
        )
        self.assertContains(response, "Empfehlungen werden erstellt")

    def test_generate_get_is_not_allowed(self):
        response = self.client.get(
            reverse("documents:vorgang_recommendations_generate", args=[self.vorgang.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_regenerating_keeps_the_previous_result_visible(self):
        self._ready_run()

        with patch("django_q.tasks.async_task"):
            response = self.client.post(
                reverse("documents:vorgang_recommendations_generate", args=[self.vorgang.pk])
            )

        self.assertContains(response, "Mahnung fristgerecht beantworten")
        self.assertContains(response, "Empfehlungen werden erstellt")

    def test_panel_shows_situation_recommendation_and_disclaimer(self):
        self._ready_run()

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertContains(response, "Die Forderung ist offen.")
        self.assertContains(response, "Mahnung fristgerecht beantworten")
        self.assertContains(response, "Die Frist läuft am 15.09.2026 ab.")
        self.assertContains(response, "keine Rechts-/Steuerberatung")
        self.assertContains(response, "15.09.2026")

    def test_panel_only_links_sources_the_user_may_see(self):
        """Eine Quelle, die nach dem Lauf unsichtbar wurde, verschwindet aus
        der Provenienz-Zeile, statt ihren Titel weiter preiszugeben.
        """
        self._ready_run()

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertContains(response, "Mahnung Acme")
        self.assertNotContains(response, "Fremdakte")

    def test_result_is_withheld_when_the_basis_is_not_visible(self):
        """Lage/Begruendungen sind aus den Zusammenfassungen der Datenbasis
        destilliert -- ein Kollege ohne Zugriff auf diese Dokumente bekommt
        sie deshalb gar nicht erst zu sehen (#1052).
        """
        self._ready_run()
        self.client.force_login(self.user_b)

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertNotContains(response, "Die Forderung ist offen.")
        self.assertNotContains(response, "Mahnung fristgerecht beantworten")
        self.assertContains(response, "nicht sichtbar")

    def test_accept_is_refused_when_the_basis_is_not_visible(self):
        _, recommendation = self._ready_run()
        self.client.force_login(self.user_b)

        response = self.client.post(
            reverse(
                "documents:vorgang_recommendation_accept",
                args=[self.vorgang.pk, recommendation.pk],
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Task.objects.count(), 0)

    def test_panel_reports_a_truncated_basis(self):
        run, _ = self._ready_run()
        run.based_on = {**run.based_on, "truncated": True, "document_count": 99, "used_count": 2}
        run.save(update_fields=["based_on"])

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertContains(response, "gekürzt")
        self.assertContains(response, "von 99 Dokumenten")

    def test_panel_shows_stale_hint_when_a_document_was_added(self):
        self._ready_run()
        added = Document.objects.create(
            title="Neues Schreiben", visibility=Document.Visibility.DEPARTMENT
        )
        added.departments.add(self.dept_a)
        added.vorgaenge.add(self.vorgang)

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertContains(response, "Veraltet")
        self.assertContains(response, "Empfehlungen neu generieren")

    def test_panel_shows_a_failed_run(self):
        VorgangRecommendationRun.objects.create(
            vorgang=self.vorgang,
            status=VorgangRecommendationRun.Status.FAILED,
            error="Provider weg",
        )

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertContains(response, "konnten nicht erstellt werden")
        self.assertContains(response, "Provider weg")

    def test_accept_creates_a_task_linked_to_the_visible_sources(self):
        _, recommendation = self._ready_run()

        response = self.client.post(
            reverse(
                "documents:vorgang_recommendation_accept",
                args=[self.vorgang.pk, recommendation.pk],
            )
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get()
        self.assertEqual(task.title, "Mahnung fristgerecht beantworten")
        self.assertIn("Die Frist läuft am 15.09.2026 ab.", task.description)
        self.assertIn("Forderung Acme", task.description)
        self.assertEqual(task.due_date, datetime.date(2026, 9, 15))
        self.assertEqual(task.owner, self.user_a)
        self.assertEqual(list(task.documents.all()), [self.document])
        self.assertEqual(list(task.departments.all()), [self.dept_a])

        recommendation.refresh_from_db()
        self.assertEqual(recommendation.status, VorgangRecommendation.Status.ACCEPTED)
        self.assertEqual(recommendation.task, task)
        self.assertContains(response, "Übernommen")

    def test_accepted_task_stays_reachable_from_the_panel(self):
        """Der Hub listet keine Aufgaben mehr -- die übernommene Aufgabe

        darf deshalb nicht unauffindbar werden: das Panel verlinkt sie an
        der Empfehlung selbst ("Aufgabe öffnen").
        """
        _, recommendation = self._ready_run()
        self.client.post(
            reverse(
                "documents:vorgang_recommendation_accept",
                args=[self.vorgang.pk, recommendation.pk],
            )
        )

        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Mahnung fristgerecht beantworten")
        recommendation.refresh_from_db()
        self.assertContains(
            response, reverse("documents:task_detail", args=[recommendation.task.pk])
        )

    def test_accept_is_idempotent(self):
        _, recommendation = self._ready_run()
        url = reverse(
            "documents:vorgang_recommendation_accept", args=[self.vorgang.pk, recommendation.pk]
        )

        self.client.post(url)
        self.client.post(url)

        self.assertEqual(Task.objects.count(), 1)

    def test_accept_get_is_not_allowed(self):
        _, recommendation = self._ready_run()

        response = self.client.get(
            reverse(
                "documents:vorgang_recommendation_accept",
                args=[self.vorgang.pk, recommendation.pk],
            )
        )

        self.assertEqual(response.status_code, 405)
        self.assertEqual(Task.objects.count(), 0)

    def test_dismiss_marks_the_recommendation_without_creating_a_task(self):
        _, recommendation = self._ready_run()

        response = self.client.post(
            reverse(
                "documents:vorgang_recommendation_dismiss",
                args=[self.vorgang.pk, recommendation.pk],
            )
        )

        self.assertEqual(response.status_code, 200)
        recommendation.refresh_from_db()
        self.assertEqual(recommendation.status, VorgangRecommendation.Status.DISMISSED)
        self.assertEqual(Task.objects.count(), 0)
        self.assertContains(response, "Verworfen")

    def test_recommendation_of_another_vorgang_is_not_reachable(self):
        _, recommendation = self._ready_run()
        other = Vorgang.objects.create(name="Anderer Vorgang")

        response = self.client.post(
            reverse(
                "documents:vorgang_recommendation_accept", args=[other.pk, recommendation.pk]
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Task.objects.count(), 0)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.get(
            reverse("documents:vorgang_recommendations", args=[self.vorgang.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
