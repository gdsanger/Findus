from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

from .models import Correspondent, Document, Tag, Vorgang

User = get_user_model()


class DocumentListViewTests(TestCase):
    """Covers the document list (#1014): visibility scoping, the combinable
    Absender/Vorgang/Tag/Status filters, and the HTMX partial-swap contract
    (full page on a normal GET, bare partial on an HX-Request).
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.acme = Correspondent.objects.create(name="Acme GmbH")
        self.other = Correspondent.objects.create(name="Other GmbH")
        self.vorgang = Vorgang.objects.create(name="Steuererklärung 2026")
        self.tag = Tag.objects.create(name="Dringend")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            correspondent=self.acme,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.own_doc.departments.add(self.dept_a)
        self.own_doc.vorgaenge.add(self.vorgang)
        self.own_doc.tags.add(self.tag)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            correspondent=self.other,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.FAILED,
        )
        self.other_dept_doc.departments.add(self.dept_b)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_visible_documents_are_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Rechnung Acme")

    def test_documents_outside_visibility_are_not_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_full_page_response_includes_base_template_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-sidebar")

    def test_htmx_request_returns_bare_partial_without_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), HTTP_HX_REQUEST="true")

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_filter_by_correspondent(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"correspondent": self.other.id}
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_filter_by_vorgang(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"vorgang": self.vorgang.id})

        self.assertContains(response, "Rechnung Acme")

    def test_filter_by_tag(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"tag": self.tag.id})

        self.assertContains(response, "Rechnung Acme")

    def test_filter_by_status_excludes_non_matching(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"status": Document.ProcessingStatus.FAILED}
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_combined_filters_narrow_results(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"),
            {"correspondent": self.acme.id, "status": Document.ProcessingStatus.FAILED},
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_empty_state_when_no_documents_visible(self):
        self.client.force_login(self.user_b)
        Document.objects.exclude(pk=self.other_dept_doc.pk).delete()
        self.other_dept_doc.delete()

        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Keine Dokumente")

    def test_pagination_second_page(self):
        for index in range(25):
            Document.objects.create(
                title=f"Bulk {index}",
                visibility=Document.Visibility.DEPARTMENT,
            ).departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"page": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["page_obj"].object_list), 6)
