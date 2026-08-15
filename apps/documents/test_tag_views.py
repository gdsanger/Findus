from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

from .models import Correspondent, Document, Tag

User = get_user_model()


class TagListViewTests(TestCase):
    """Covers the Tags-Index (#1051): all Tags grouped by dimension, with
    the document count per Tag scoped by `Document.visible_to`.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.dringend = Tag.objects.create(name="Dringend", dimension="Priorität")
        self.rechnung = Tag.objects.create(name="Rechnung", dimension="Thema")
        self.ohne_dimension = Tag.objects.create(name="Sonstiges")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.own_doc.departments.add(self.dept_a)
        self.own_doc.tags.add(self.dringend)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH", visibility=Document.Visibility.DEPARTMENT
        )
        self.other_dept_doc.departments.add(self.dept_b)
        self.other_dept_doc.tags.add(self.dringend)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:tag_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_lists_tags_grouped_by_dimension(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_list"))

        groups = {group["dimension"]: [tag.name for tag in group["tags"]] for group in response.context["groups"]}
        self.assertEqual(groups["Priorität"], ["Dringend"])
        self.assertEqual(groups["Thema"], ["Rechnung"])
        self.assertEqual(groups[""], ["Sonstiges"])

    def test_document_count_only_reflects_visible_documents(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_list"))

        dringend_group = next(g for g in response.context["groups"] if g["dimension"] == "Priorität")
        dringend = dringend_group["tags"][0]
        self.assertEqual(dringend.document_count, 1)

    def test_search_narrows_by_name_or_dimension(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_list"), {"q": "Rechnung"})

        self.assertContains(response, "Rechnung")
        self.assertNotContains(response, "Dringend")

    def test_row_links_to_hub(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_list"))

        self.assertContains(response, reverse("documents:tag_detail", args=[self.dringend.pk]))


class TagCreateViewTests(TestCase):
    """Covers "Neu anlegen" on the Tags-Index (#1051)."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:tag_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_create_tag_redirects_to_its_hub(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:tag_create"),
            {"name": "Dringend", "dimension": "Priorität", "color": "#ff0000"},
        )

        tag = Tag.objects.get(name="Dringend")
        self.assertRedirects(response, reverse("documents:tag_detail", args=[tag.pk]))
        self.assertEqual(tag.dimension, "Priorität")
        self.assertEqual(tag.color, "#ff0000")

    def test_create_with_duplicate_name_and_dimension_shows_error_and_does_not_create(self):
        Tag.objects.create(name="Dringend", dimension="Priorität")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:tag_create"), {"name": "Dringend", "dimension": "Priorität"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tag.objects.count(), 1)

    def test_same_name_in_different_dimension_is_allowed(self):
        Tag.objects.create(name="Dringend", dimension="Priorität")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:tag_create"), {"name": "Dringend", "dimension": "Status"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Tag.objects.filter(name="Dringend").count(), 2)


class TagDetailViewTests(TestCase):
    """Covers the Tag hub (#1051): the context header (Name/Dimension/
    Farbe) + the reused, Tag-scoped document list (`views.filtered_documents`
    + `_document_list.html`, further filterable by Kontakt/Status/Richtung/
    Vorgang), plus inline edit/delete.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.tag = Tag.objects.create(name="Dringend", dimension="Priorität", color="#ff0000")
        self.other_tag = Tag.objects.create(name="Rechnung")

        self.correspondent = Correspondent.objects.create(name="Acme GmbH")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
            correspondent=self.correspondent,
        )
        self.own_doc.departments.add(self.dept_a)
        self.own_doc.tags.add(self.tag)

        self.unrelated_doc = Document.objects.create(
            title="Anderes Dokument", visibility=Document.Visibility.DEPARTMENT
        )
        self.unrelated_doc.departments.add(self.dept_a)
        self.unrelated_doc.tags.add(self.other_tag)

        self.other_dept_doc = Document.objects.create(
            title="Vertrag Other GmbH", visibility=Document.Visibility.DEPARTMENT
        )
        self.other_dept_doc.departments.add(self.dept_b)
        self.other_dept_doc.tags.add(self.tag)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_unknown_tag_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_header_shows_name_dimension_and_color(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertContains(response, "Dringend")
        self.assertContains(response, "Priorität")
        self.assertContains(response, "#ff0000")
        self.assertEqual(response.context["document_count"], 1)

    def test_document_list_is_scoped_to_this_tag(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Anderes Dokument")

    def test_document_list_respects_visibility(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_document_list_further_filterable_by_status(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:tag_detail", args=[self.tag.pk]),
            {"status": Document.ProcessingStatus.FAILED},
        )

        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_htmx_request_returns_bare_document_list_partial(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:tag_detail", args=[self.tag.pk]), HTTP_HX_REQUEST="true"
        )

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_document_list_defaults_to_grid_view(self):
        """Kachel ist auch auf dem Tag-Hub der Default (#1124) -- dieselbe

        Ansicht wie auf Home, damit sie beim Wechsel Home <-> Hub nicht
        springt. Die Zeitleiste bleibt über den Umschalter erreichbar.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertContains(response, "findus-doc-grid")
        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-timeline\"")

    def test_view_toggle_hides_the_wiedervorlagen_mode(self):
        """Die Wiedervorlagen-Ansicht (#1129) listet DocumentComment und gibt
        es nur auf Home -- der Umschalter wird hier ausgeblendet statt einen
        Knopf anzubieten, der still auf die Tabelle zurückfällt.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertContains(response, 'data-view-value="grid"')
        self.assertNotContains(response, 'data-view-value="wiedervorlagen"')

    def test_document_list_offers_timeline_view(self):
        """The Tag hub reuses the shared document-list block (#1087) -- the

        Timeline view is available here too, still scoped to this Tag.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:tag_detail", args=[self.tag.pk]), {"view": "timeline"}
        )

        self.assertContains(response, "findus-timeline\"")
        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Anderes Dokument")

    def test_hub_offers_inline_edit_form_and_delete_action(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:tag_detail", args=[self.tag.pk]))

        self.assertContains(response, reverse("documents:tag_detail", args=[self.tag.pk]))
        self.assertContains(response, reverse("documents:tag_delete", args=[self.tag.pk]))

    def test_edit_updates_name_dimension_and_color(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:tag_detail", args=[self.tag.pk]),
            {"name": "Sehr dringend", "dimension": "Priorität", "color": "#00ff00"},
        )

        self.assertRedirects(response, reverse("documents:tag_detail", args=[self.tag.pk]))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "Sehr dringend")
        self.assertEqual(self.tag.color, "#00ff00")


class TagDeleteViewTests(TestCase):
    """Covers deleting a Tag from its hub (#1051): only the assignment on
    affected Documents is dropped, never the Documents themselves.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.tag = Tag.objects.create(name="Dringend")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(reverse("documents:tag_delete", args=[self.tag.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_delete_requires_post(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:tag_delete", args=[self.tag.pk]))

        self.assertEqual(response.status_code, 405)

    def test_delete_removes_tag_and_redirects_to_list(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("documents:tag_delete", args=[self.tag.pk]))

        self.assertRedirects(response, reverse("documents:tag_list"))
        self.assertFalse(Tag.objects.filter(pk=self.tag.pk).exists())

    def test_delete_in_use_removes_assignment_instead_of_deleting_document(self):
        doc = Document.objects.create(title="Rechnung")
        doc.tags.add(self.tag)

        self.client.force_login(self.user)
        self.client.post(reverse("documents:tag_delete", args=[self.tag.pk]))

        doc.refresh_from_db()
        self.assertFalse(doc.tags.exists())
