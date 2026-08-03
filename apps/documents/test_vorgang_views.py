from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

from .models import Document, Task, Vorgang

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

        self.assertEqual(response.context["vorgaenge"].get(pk=self.vorgang_a.pk).document_count, 1)

    def test_search_narrows_by_name(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"), {"q": "Steuer"})

        self.assertContains(response, "Steuererklärung 2026")
        self.assertNotContains(response, "Nebenkostenabrechnung")

    def test_row_links_to_hub(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_list"))

        self.assertContains(response, reverse("documents:vorgang_detail", args=[self.vorgang_a.pk]))


class VorgangDetailViewTests(TestCase):
    """Covers the Vorgang hub (#1040): the context header's Kennzahlen, the
    reused/vorgang-scoped document list (`views.filtered_documents` +
    `_document_list.html`, further filterable by Tag/Status/Richtung), and
    the tasks linked through Document:Task n:n.
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
        self.assertEqual(response.context["open_tasks_count"], 1)

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

    def test_linked_tasks_are_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, "Belege einreichen")

    def test_new_task_action_links_to_task_create(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(response, reverse("documents:task_create"))

    def test_edit_action_links_to_stammdaten_edit(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:vorgang_detail", args=[self.vorgang.pk]))

        self.assertContains(
            response, reverse("documents:stammdaten_edit", args=["vorgaenge", self.vorgang.pk])
        )
