from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department
from apps.ai.providers.base import EmbeddingResult

from .models import Chunk, Correspondent, Document, Tag, Vorgang

User = get_user_model()

DIMENSIONS = settings.FINDUS_EMBEDDING_DIMENSIONS


def _one_hot(index: int) -> list[float]:
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


class _StubEmbeddingProvider:
    """Fixed query vector -- lets a test control which chunk ranks first
    without depending on a real embedding provider or matching its
    dimensionality to `settings.FINDUS_EMBEDDING_DIMENSIONS`.
    """

    def __init__(self, vector: list[float]):
        self.vector = vector

    def embed(self, texts):
        texts = list(texts)
        return EmbeddingResult(vectors=[self.vector] * len(texts), model="stub", version="1")


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


class DocumentSearchViewTests(TestCase):
    """Covers the semantic search bar (#1015): a `q` param on the document
    list switches to visibility-filtered, ranked hits from
    `DocumentRetrievalService.search()`, combinable with the same
    Absender/Vorgang/Tag/Status filters, rendered as an HTMX partial with a
    "no hits" state and a click-through link to the detail page.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.acme = Correspondent.objects.create(name="Acme GmbH")
        self.other = Correspondent.objects.create(name="Other GmbH")

        self.own_doc = Document.objects.create(
            title="Rechnung Acme",
            correspondent=self.acme,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.own_doc.departments.add(self.dept_a)
        Chunk.objects.create(
            document=self.own_doc,
            position=0,
            content="Rechnung über Beratungsleistungen, Betrag 500 EUR",
            embedding=_one_hot(0),
            embedding_model="stub",
            embedding_model_version="1",
        )

        self.hidden_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            correspondent=self.other,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.hidden_doc.departments.add(self.dept_b)
        Chunk.objects.create(
            document=self.hidden_doc,
            position=0,
            content="Vertrag",
            embedding=_one_hot(0),
            embedding_model="stub",
            embedding_model_version="1",
        )

    def _search(self, params, **extra):
        with patch(
            "apps.documents.retrieval.get_embedding_provider",
            return_value=_StubEmbeddingProvider(_one_hot(0)),
        ):
            return self.client.get(reverse("documents:home"), params, **extra)

    def test_search_returns_visibility_filtered_ranked_hit(self):
        self.client.force_login(self.user_a)
        response = self._search({"q": "Rechnung"})

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_search_hit_links_to_detail_page(self):
        self.client.force_login(self.user_a)
        response = self._search({"q": "Rechnung"})

        self.assertContains(response, reverse("documents:detail", args=[self.own_doc.id]))

    def test_search_shows_snippet_and_relevance(self):
        self.client.force_login(self.user_a)
        response = self._search({"q": "Rechnung"})

        self.assertContains(response, "Beratungsleistungen")
        self.assertContains(response, "%")

    def test_search_combines_with_correspondent_filter(self):
        self.client.force_login(self.user_a)
        response = self._search({"q": "Rechnung", "correspondent": self.other.id})

        self.assertNotContains(response, "Rechnung Acme")

    def test_search_without_visible_hits_shows_empty_state(self):
        other_user = User.objects.create_user(username="carol", password="x")
        other_user.departments.add(Department.objects.create(name="Dept C"))
        self.client.force_login(other_user)

        response = self._search({"q": "Rechnung"})

        self.assertContains(response, "Keine Treffer")

    def test_htmx_search_returns_bare_partial(self):
        self.client.force_login(self.user_a)
        response = self._search({"q": "Rechnung"}, HTTP_HX_REQUEST="true")

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_blank_query_falls_back_to_structured_browsing(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"q": ""})

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "%")


class DocumentDetailViewTests(TestCase):
    """Covers the document detail page (#1015) that a search hit or list
    row links to: same visibility scoping as list/search, 404 instead of a
    leak for documents outside it.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            text_content="Betrag: 123 EUR",
        )
        self.doc.departments.add(self.dept_a)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_visible_document_renders(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Rechnung Acme")
        self.assertContains(response, "Betrag: 123 EUR")

    def test_document_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)
