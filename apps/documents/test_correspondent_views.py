from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

from .models import Correspondent, Document, Task

User = get_user_model()


class CorrespondentListViewTests(TestCase):
    """Covers the Kontakte index (#1041): all Correspondents are listed

    (shared vocabulary, no `visibility` of its own -- see `Correspondent`),
    while the document count/Eingang-Ausgang-split/last activity per row
    respect `Document.visible_to`.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.acme = Correspondent.objects.create(name="Acme GmbH")
        self.other = Correspondent.objects.create(name="Nebenkosten AG")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=self.acme,
            direction=Document.Direction.EINGANG,
        )
        self.own_doc.departments.add(self.dept_a)

        self.own_outgoing_doc = Document.objects.create(
            title="Antwort an Acme",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=self.acme,
            direction=Document.Direction.AUSGANG,
        )
        self.own_outgoing_doc.departments.add(self.dept_a)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=self.acme,
            direction=Document.Direction.EINGANG,
        )
        self.other_dept_doc.departments.add(self.dept_b)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:correspondent_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_lists_all_correspondents_regardless_of_document_visibility(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_list"))

        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, "Nebenkosten AG")

    def test_document_count_and_direction_split_only_reflect_visible_documents(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_list"))

        row = response.context["correspondents"].get(pk=self.acme.pk)
        self.assertEqual(row.document_count, 2)
        self.assertEqual(row.eingang_count, 1)
        self.assertEqual(row.ausgang_count, 1)

    def test_search_narrows_by_name(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_list"), {"q": "Acme"})

        self.assertContains(response, "Acme GmbH")
        self.assertNotContains(response, "Nebenkosten AG")

    def test_row_links_to_hub(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_list"))

        self.assertContains(response, reverse("documents:correspondent_detail", args=[self.acme.pk]))

    def test_is_self_flag_is_shown(self):
        Correspondent.objects.create(name="Eigene Firma GmbH", is_self=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_list"))

        self.assertContains(response, "Das bin ich")


class CorrespondentDetailViewTests(TestCase):
    """Covers the Kontakt hub (#1041): the context header's Kontaktdaten +

    Kennzahlen (Dokumente/Eingang-Ausgang/offene Aufgaben), and the
    reused/correspondent-scoped document list (`views.filtered_documents` +
    `_document_list.html`, further filterable by Tag/Status/Richtung/Vorgang).
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.correspondent = Correspondent.objects.create(
            name="Acme GmbH", email="info@acme.example", vat_id="DE123456789", iban="DE02120300000000202051"
        )
        self.other_correspondent = Correspondent.objects.create(name="Sonstiges")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
            correspondent=self.correspondent,
            direction=Document.Direction.EINGANG,
        )
        self.own_doc.departments.add(self.dept_a)

        self.unrelated_doc = Document.objects.create(
            title="Anderer Kontakt",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=self.other_correspondent,
        )
        self.unrelated_doc.departments.add(self.dept_a)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=self.correspondent,
        )
        self.other_dept_doc.departments.add(self.dept_b)

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
        response = self.client.get(reverse("documents:correspondent_detail", args=[self.correspondent.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_unknown_correspondent_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_header_shows_kontaktdaten_and_kennzahlen(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_detail", args=[self.correspondent.pk]))

        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, "info@acme.example")
        self.assertContains(response, "DE123456789")
        self.assertContains(response, "DE02120300000000202051")
        self.assertEqual(response.context["document_count"], 1)
        self.assertEqual(response.context["eingang_count"], 1)
        self.assertEqual(response.context["ausgang_count"], 0)
        self.assertEqual(response.context["open_tasks_count"], 1)

    def test_document_list_is_scoped_to_this_correspondent(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_detail", args=[self.correspondent.pk]))

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Anderer Kontakt")

    def test_document_list_respects_visibility(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_detail", args=[self.correspondent.pk]))

        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_document_list_further_filterable_by_status(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:correspondent_detail", args=[self.correspondent.pk]),
            {"status": Document.ProcessingStatus.FAILED},
        )

        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_htmx_request_returns_bare_document_list_partial(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:correspondent_detail", args=[self.correspondent.pk]), HTTP_HX_REQUEST="true"
        )

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_edit_action_links_to_stammdaten_edit(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:correspondent_detail", args=[self.correspondent.pk]))

        self.assertContains(
            response, reverse("documents:stammdaten_edit", args=["correspondents", self.correspondent.pk])
        )
