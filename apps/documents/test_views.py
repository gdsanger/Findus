import io
import shutil
import tempfile
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department
from apps.ai.providers.base import EmbeddingResult

from .models import (
    Chunk,
    Correspondent,
    Document,
    DocumentLink,
    SuggestionStatus,
    Tag,
    TagSuggestion,
    Task,
    Vorgang,
    VorgangSuggestion,
    link_documents,
)

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

    def test_child_document_does_not_get_its_own_top_level_row(self):
        """Unterdokumente (#1069) hängen eingeklappt unter ihrem

        Leitdokument -- der Eingang bleibt übersichtlich, statt jeden
        Mail-Anhang als eigene Zeile zu zeigen. Der einzige sichtbare
        Leitdokument-Root ist `own_doc`, also darf die Tabelle nur eine
        Kopf- und eine Datenzeile haben, nicht zwei Datenzeilen.
        """
        Document.objects.create_child(self.own_doc, title="Anhang.pdf")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"view": ""}, HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.content.count(b"<tr>"), 2)

    def test_child_title_is_reachable_collapsed_under_leitdokument(self):
        child = Document.objects.create_child(self.own_doc, title="Anhang.pdf")

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Anhang.pdf")
        self.assertContains(response, reverse("documents:detail", args=[child.id]))

    def test_full_page_response_includes_base_template_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-sidebar")

    def test_htmx_request_returns_bare_partial_without_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), HTTP_HX_REQUEST="true")

        self.assertContains(response, "Rechnung Acme")
        self.assertNotContains(response, "findus-sidebar")

    def test_full_page_response_includes_filter_persistence_script(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus:documents:filters")
        self.assertContains(response, 'id="filter-reset"')

    def test_filter_persistence_script_covers_action_status(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, '"action_status"')

    def test_htmx_request_does_not_duplicate_persistence_script(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), HTTP_HX_REQUEST="true")

        self.assertNotContains(response, "findus:documents:filters")

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

    def test_filter_by_direction_excludes_non_matching(self):
        self.own_doc.direction = Document.Direction.EINGANG
        self.own_doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"direction": Document.Direction.AUSGANG}
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_filter_by_direction_includes_matching(self):
        self.own_doc.direction = Document.Direction.EINGANG
        self.own_doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"direction": Document.Direction.EINGANG}
        )

        self.assertContains(response, "Rechnung Acme")

    def test_list_row_shows_direction_badge(self):
        self.own_doc.direction = Document.Direction.EINGANG
        self.own_doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Eingang")

    def test_filter_by_action_status_excludes_non_matching(self):
        self.own_doc.action_status = Document.ActionStatus.OPEN
        self.own_doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"action_status": Document.ActionStatus.DONE}
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_filter_by_action_status_includes_matching(self):
        self.own_doc.action_status = Document.ActionStatus.OPEN
        self.own_doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"action_status": Document.ActionStatus.OPEN}
        )

        self.assertContains(response, "Rechnung Acme")

    def test_list_row_shows_action_status_badge_when_open(self):
        self.own_doc.action_status = Document.ActionStatus.OPEN
        self.own_doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-action-status-badge text-bg-warning")

    def test_list_row_hides_action_status_badge_when_none(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "findus-action-status-badge")

    def test_filter_bar_includes_action_status_dropdown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'id="filter-action-status"')
        self.assertContains(response, 'name="action_status"')

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

    def test_list_row_links_to_detail(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:detail", args=[self.own_doc.id]))

    def test_list_row_has_delete_action(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:delete", args=[self.own_doc.id]))

    def test_list_row_shows_document_date_not_upload_date(self):
        self.own_doc.document_date = date(2020, 3, 1)
        self.own_doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "01.03.2020")
        self.assertNotContains(response, "(Upload)")

    def test_list_row_marks_upload_date_fallback(self):
        """`own_doc` has no `document_date` -- the Datum column must fall

        back to the Upload-Datum (#1085) and mark it as such, not display
        it as an unqualified/possibly-misleading Dokumentdatum.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "(Upload)")

    def test_list_orders_by_document_date_desc_with_upload_fallback(self):
        """Default-Sortierung (#1085): `document_date` absteigend, mit dem

        Upload-Datum als Fallback fuer Dokumente ohne erkanntes Datum --
        ein Fallback-Dokument muss an seiner chronologisch richtigen
        Stelle einsortiert werden, nicht pauschal ans Ende rutschen.
        """
        self.own_doc.document_date = date(2026, 1, 1)
        self.own_doc.save(update_fields=["document_date"])
        self.other_dept_doc.departments.add(self.dept_a)
        self.other_dept_doc.document_date = date(2026, 6, 1)
        self.other_dept_doc.save(update_fields=["document_date"])
        undated = Document.objects.create(
            title="Ohne erkanntes Datum", visibility=Document.Visibility.DEPARTMENT
        )
        undated.departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        titles = [doc.title for doc in response.context["page_obj"]]
        # other_dept_doc (2026-06-01) > undated (Upload-Datum, "heute") > own_doc (2026-01-01)
        self.assertEqual(
            titles, ["Ohne erkanntes Datum", "Vertrag Other GmbH", "Rechnung Acme"]
        )


class DocumentTimelineViewTests(TestCase):
    """Timeline-Ansicht (#1087): reiner Anzeige-Modus des gemeinsamen

    Dokumentlisten-Bausteins -- gruppiert das bereits gefilterte/sortierte
    `page_obj` nach Monat/Jahr von `display_date`, statt selbst zu filtern
    oder zu sortieren.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.acme = Correspondent.objects.create(name="Acme GmbH")

        self.august_doc = Document.objects.create(
            title="August-Rechnung",
            document_date=date(2026, 8, 1),
            correspondent=self.acme,
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.august_doc.departments.add(self.dept)

        self.july_doc = Document.objects.create(
            title="Juli-Vertrag",
            document_date=date(2026, 7, 15),
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.july_doc.departments.add(self.dept)

        self.undated_doc = Document.objects.create(
            title="Ohne Dokumentdatum",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.undated_doc.departments.add(self.dept)

    def test_timeline_view_is_default(self):
        """#1092: Timeline is the default now that #1087's toggle exists --
        no `view` param at all (fresh visit, no stored/explicit choice).
        """
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-timeline\"")
        self.assertNotContains(response, "<table")

    def test_list_view_stays_reachable_via_explicit_param(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": ""})

        self.assertContains(response, "<table")
        self.assertNotContains(response, "findus-timeline\"")

    def test_view_toggle_is_shown(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-view-toggle")
        self.assertContains(response, 'name="view"')

    def test_timeline_view_groups_by_month(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})

        self.assertContains(response, "findus-timeline\"")
        self.assertContains(response, "August 2026")
        self.assertContains(response, "Juli 2026")

    def test_timeline_view_marks_upload_date_fallback(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})

        self.assertContains(response, "(Upload)")

    def test_timeline_view_links_to_detail(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})

        self.assertContains(response, reverse("documents:detail", args=[self.august_doc.id]))

    def test_timeline_view_still_respects_filters(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents:home"), {"view": "timeline", "correspondent": self.acme.id}
        )

        self.assertContains(response, "August-Rechnung")
        self.assertNotContains(response, "Juli-Vertrag")

    def test_view_param_is_preserved_in_pagination_links(self):
        for index in range(25):
            doc = Document.objects.create(
                title=f"Bulk {index}",
                document_date=date(2026, 1, 1),
                visibility=Document.Visibility.DEPARTMENT,
            )
            doc.departments.add(self.dept)

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})

        self.assertContains(response, "view=timeline")


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

    def test_search_hit_in_child_document_shows_breadcrumb_to_leitdokument(self):
        """A hit inside a Unterdokument (#1069) must surface on its own,

        with a breadcrumb back to its Leitdokument -- unlike structured
        browsing, search never collapses children away.
        """
        child = Document.objects.create_child(self.own_doc, title="Anhang.pdf")
        Chunk.objects.create(
            document=child,
            position=0,
            content="Kontoauszug Januar",
            embedding=_one_hot(5),
            embedding_model="stub",
            embedding_model_version="1",
        )

        self.client.force_login(self.user_a)
        with patch(
            "apps.documents.retrieval.get_embedding_provider",
            return_value=_StubEmbeddingProvider(_one_hot(5)),
        ):
            response = self.client.get(reverse("documents:home"), {"q": "Kontoauszug"})

        self.assertContains(response, "Anhang.pdf")
        self.assertContains(response, reverse("documents:detail", args=[child.id]))
        self.assertContains(response, "Rechnung Acme")
        self.assertContains(response, reverse("documents:detail", args=[self.own_doc.id]))

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

    def test_markdown_cache_is_rendered_as_html(self):
        self.doc.markdown = "# Rechnung Acme\n\nBetrag: **123 EUR**"
        self.doc.save(update_fields=["markdown"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "<h1>Rechnung Acme</h1>", html=False)
        self.assertContains(response, "<strong>123 EUR</strong>", html=False)

    def test_markdown_cache_escapes_embedded_html(self):
        self.doc.markdown = "# Titel\n\n<script>alert(1)</script>"
        self.doc.save(update_fields=["markdown"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_falls_back_to_text_content_when_markdown_cache_is_empty(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Betrag: 123 EUR")

    def test_extraction_method_and_language_are_shown(self):
        self.doc.extraction_method = Document.ExtractionMethod.OCR
        self.doc.metadata = {"language": "de"}
        self.doc.save(update_fields=["extraction_method", "metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "OCR")
        self.assertContains(response, "Deutsch")

    def test_direction_badge_is_shown(self):
        self.doc.direction = Document.Direction.EINGANG
        self.doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Eingang")

    def test_action_status_badge_is_shown_when_open(self):
        self.doc.action_status = Document.ActionStatus.OPEN
        self.doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "findus-action-status-badge text-bg-warning")

    def test_action_status_control_is_shown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, 'name="action_status"')
        self.assertContains(response, reverse("documents:action_status", args=[self.doc.id]))

    def test_summary_and_key_facts_are_shown_as_primary_view(self):
        """Covers #1024: once a KI-Analyse (#1020) has produced a summary,
        it -- plus the Key-Facts panel -- is the primary view, not the raw
        markdown/text.
        """
        self.doc.markdown = "# Rechnung Acme\n\nBetrag: **123 EUR**"
        self.doc.summary = "Rechnung von Acme über 123 EUR, fällig am 01.02.2026."
        self.doc.key_facts = {
            "document_type": "Rechnung",
            "document_date": "2026-01-15",
            "amount": "123",
            "currency": "EUR",
            "due_date": "2026-02-01",
        }
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["markdown", "summary", "key_facts", "document_date"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Rechnung von Acme über 123 EUR, fällig am 01.02.2026.")
        self.assertContains(response, "KI-extrahiert")
        self.assertContains(response, "Rechnung")
        self.assertContains(response, "15.01.2026")
        self.assertContains(response, "123 EUR")
        self.assertContains(response, "2026-02-01")
        self.assertContains(response, "KI-generiert")
        self.assertContains(response, "Volltext anzeigen")

    def test_fallback_to_raw_text_when_no_summary_yet(self):
        """No KI-Analyse (#1020) has run yet -- the raw extraction result
        is shown directly, clearly marked as such, instead of an empty
        summary section.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Noch keine KI-Zusammenfassung vorhanden")
        self.assertContains(response, "Betrag: 123 EUR")

    def test_linked_task_is_shown(self):
        task = Task.objects.create(title="Rechnung bezahlen", kind=Task.Kind.PAY)
        task.departments.add(self.dept_a)
        task.documents.add(self.doc)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Rechnung bezahlen")

    def test_task_outside_visibility_is_not_shown(self):
        other_dept = Department.objects.create(name="Dept C")
        task = Task.objects.create(
            title="Privater Task", visibility=Task.Visibility.PRIVATE
        )
        task.departments.add(other_dept)
        task.documents.add(self.doc)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Privater Task")

    def test_correspondent_link_to_hub_is_shown(self):
        """Covers #1098: from the document, the Kontakt-Hub (#1041) should
        be one click away instead of only showing the name as plain text.
        """
        correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.correspondent = correspondent
        self.doc.save(update_fields=["correspondent"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(
            response,
            reverse("documents:correspondent_detail", args=[correspondent.id]),
        )

    def test_no_correspondent_link_when_unassigned(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "findus-detail-hub-link-group")

    def test_vorgang_links_to_hub_are_shown(self):
        """Covers #1098: each assigned Vorgang (M2M) links to its own
        Vorgang-Hub (#1040).
        """
        vorgang_a = Vorgang.objects.create(name="Mietvertrag 2026")
        vorgang_b = Vorgang.objects.create(name="Nebenkosten 2026")
        self.doc.vorgaenge.add(vorgang_a, vorgang_b)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(
            response, reverse("documents:vorgang_detail", args=[vorgang_a.id])
        )
        self.assertContains(
            response, reverse("documents:vorgang_detail", args=[vorgang_b.id])
        )

    def test_no_vorgang_link_when_unassigned(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "findus-detail-hub-link-group")

    def test_children_are_shown_on_leitdokument_detail_page(self):
        child = Document.objects.create_child(
            self.doc, title="Anhang.pdf", child_role=Document.ChildRole.MAIL_ATTACHMENT
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Anhang.pdf")
        self.assertContains(response, reverse("documents:detail", args=[child.id]))

    def test_child_outside_visibility_is_not_leaked(self):
        Document.objects.create_child(
            self.doc, title="Geheimanhang.pdf", departments=[self.dept_b]
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Geheimanhang.pdf")

    def test_child_detail_page_shows_breadcrumb_to_leitdokument(self):
        child = Document.objects.create_child(self.doc, title="Anhang.pdf")

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[child.id]))

        self.assertContains(response, "Rechnung Acme")
        self.assertContains(response, reverse("documents:detail", args=[self.doc.id]))

    def test_breadcrumb_is_hidden_when_leitdokument_is_not_visible(self):
        private_parent = Document.objects.create(
            title="Geheime Mail", visibility=Document.Visibility.PRIVATE, owner=self.user_b
        )
        # An overridden, broader scope than the parent's (#1069) -- the
        # rare case a child needs its own visibility -- must not leak the
        # otherwise-hidden parent's title into the breadcrumb.
        child = Document.objects.create_child(
            private_parent,
            title="Anhang.pdf",
            visibility=Document.Visibility.DEPARTMENT,
            departments=[self.dept_a],
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[child.id]))

        self.assertNotContains(response, "Geheime Mail")


class DocumentActionStatusViewTests(TestCase):
    """Covers the #1057 badge+toggle endpoint: setting `action_status`

    straight from the list row or detail page's control, via HTMX,
    without going through the general "Zuordnung bearbeiten" edit form.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)

    def test_post_sets_action_status(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:action_status", args=[self.doc.id]),
            {"action_status": Document.ActionStatus.OPEN},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.OPEN)
        self.assertContains(response, "findus-action-status-badge text-bg-warning")

    def test_post_with_invalid_value_keeps_previous_status(self):
        self.doc.action_status = Document.ActionStatus.OPEN
        self.doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:action_status", args=[self.doc.id]), {"action_status": "bogus"}
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.OPEN)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:action_status", args=[self.doc.id]))

        self.assertEqual(response.status_code, 405)

    def test_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:action_status", args=[self.doc.id]),
            {"action_status": Document.ActionStatus.OPEN},
        )

        self.assertEqual(response.status_code, 404)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.NONE)


class DocumentAnalysisActionsViewTests(TestCase):
    """Covers the #1063 detail-page controls: "Analyse erneut ausfuehren"

    (re-runs just the KI-Analyse) and "Neu verarbeiten" (re-runs the whole
    extraction -> analysis -> embedding pipeline), both queued on the
    Django-Q worker rather than run inline, and both gated by the same
    `visible_to` scope as every other document action.
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
            processing_status=Document.ProcessingStatus.FAILED,
            processing_error="Kaputtes PDF",
        )
        self.doc.departments.add(self.dept_a)

    def test_rerun_post_sets_analyzing_and_queues_worker_task(self):
        from apps.documents.analysis import analyze_and_finalize

        self.client.force_login(self.user_a)
        with patch("django_q.tasks.async_task") as mock_async_task:
            mock_async_task.return_value = "task-1"
            response = self.client.post(reverse("documents:analysis_rerun", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.ANALYZING)
        mock_async_task.assert_called_once_with(analyze_and_finalize, self.doc.id)
        self.assertContains(response, "Analyse läuft")

    def test_rerun_get_is_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:analysis_rerun", args=[self.doc.id]))

        self.assertEqual(response.status_code, 405)

    def test_rerun_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        with patch("django_q.tasks.async_task") as mock_async_task:
            response = self.client.post(reverse("documents:analysis_rerun", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)
        mock_async_task.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.FAILED)

    def test_reprocess_post_resets_status_and_queues_pipeline_task(self):
        from apps.documents.tasks import extract_document_task

        self.client.force_login(self.user_a)
        with patch("django_q.tasks.async_task") as mock_async_task:
            mock_async_task.return_value = "task-1"
            response = self.client.post(reverse("documents:reprocess", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.PENDING)
        self.assertEqual(self.doc.processing_error, "")
        mock_async_task.assert_called_once_with(extract_document_task, self.doc.id)

    def test_reprocess_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        with patch("django_q.tasks.async_task") as mock_async_task:
            response = self.client.post(reverse("documents:reprocess", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)
        mock_async_task.assert_not_called()

    def test_status_partial_shows_actions_when_terminal(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:analysis_status", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Analyse erneut ausführen")
        self.assertContains(response, "Neu verarbeiten")
        self.assertEqual(response["HX-Refresh"], "true")

    def test_status_partial_polls_while_pending(self):
        self.doc.processing_status = Document.ProcessingStatus.ANALYZING
        self.doc.save(update_fields=["processing_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:analysis_status", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'hx-trigger="every 3s"')
        self.assertNotIn("HX-Refresh", response)

    def test_status_endpoint_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:analysis_status", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)


_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-detail-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentDetailOriginalDownloadTests(TestCase):
    """Covers requirement #5 (#1016), its auth-gated streaming (#1024), and

    the inline Slide-Over preview + always-available download (#1036) --
    original access always goes through `documents:original_download` /
    `documents:original_preview`, never through the storage backend's own
    (public) URL. Uses local `FileSystemStorage` instead of the real S3/
    MinIO backend (see `test_extraction.py`), since only the storage
    backend choice is under test here, not object storage itself.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

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
            metadata={"mime_type": "application/pdf", "original_filename": "rechnung.pdf"},
        )
        self.doc.departments.add(self.dept_a)
        self.doc.original_file.save("rechnung.pdf", io.BytesIO(b"%PDF-1.4 test"), save=True)

    def test_download_link_is_shown_for_document_with_original_file(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        download_url = reverse("documents:original_download", args=[self.doc.id])
        self.assertContains(response, "Original herunterladen")
        self.assertContains(response, download_url)
        self.assertNotContains(response, self.doc.original_file.url)

    def test_preview_trigger_is_shown_for_previewable_document(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        panel_url = reverse("documents:original_preview_panel", args=[self.doc.id])
        self.assertContains(response, "Original öffnen/herunterladen")
        self.assertContains(response, panel_url)

    def test_preview_hint_shown_instead_of_trigger_for_non_previewable_document(self):
        self.doc.metadata = {"mime_type": "application/zip", "original_filename": "anhang.zip"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Vorschau nicht verfügbar")
        self.assertNotContains(response, "Original öffnen/herunterladen")
        # Download must still be offered, unaffected by the missing preview.
        self.assertContains(response, "Original herunterladen")
        self.assertContains(response, reverse("documents:original_download", args=[self.doc.id]))

    def test_no_actions_shown_without_original_file(self):
        self.doc.original_file.delete(save=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Original öffnen/herunterladen")
        self.assertNotContains(response, "Original herunterladen")

    def test_original_download_streams_file_content_as_attachment(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_download", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("rechnung.pdf", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_original_download_allows_same_origin_framing(self):
        """The inline PDF preview (#1036) embeds this route in the slide-over
        iframe. The global XFrameOptionsMiddleware default of DENY would block
        that, so this endpoint must override to SAMEORIGIN -- while every other
        page keeps DENY.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_download", args=[self.doc.id]))

        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_original_download_is_scoped_by_visibility(self):
        """A user outside the document's department must not reach the
        original through the streaming endpoint either -- the whole point
        of #1024 is that the ACL applies here, not just on the detail page.
        """
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:original_download", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_original_download_404_without_original_file(self):
        self.doc.original_file.delete(save=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_download", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_original_preview_streams_inline_for_pdf(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_preview", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_original_preview_streams_inline_for_image(self):
        self.doc.metadata = {"mime_type": "image/png", "original_filename": "scan.png"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_preview", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("inline", response["Content-Disposition"])

    def test_original_preview_allows_same_origin_framing(self):
        """This is the route the Slide-Over's `<iframe>` actually points at
        (see `_detail_original_preview.html`), so it -- not
        `original_download` -- must relax the global XFrameOptionsMiddleware
        DENY to SAMEORIGIN, or the browser refuses to display the PDF in the
        iframe (bug report for #1042).
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_preview", args=[self.doc.id]))

        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_original_preview_404_for_non_previewable_mime_type(self):
        self.doc.metadata = {"mime_type": "application/zip", "original_filename": "anhang.zip"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_preview", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_original_preview_is_scoped_by_visibility(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:original_preview", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_original_preview_panel_renders_iframe_for_pdf(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:original_preview_panel", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<iframe")
        self.assertContains(
            response, reverse("documents:original_preview", args=[self.doc.id])
        )

    def test_original_preview_panel_renders_img_for_image(self):
        self.doc.metadata = {"mime_type": "image/jpeg", "original_filename": "scan.jpg"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:original_preview_panel", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<img")

    def test_original_preview_panel_404_for_non_previewable_mime_type(self):
        self.doc.metadata = {"mime_type": "application/zip", "original_filename": "anhang.zip"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:original_preview_panel", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 404)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentDeleteViewTests(TestCase):
    """Covers requirement #5 (#1022): a document can be deleted from the

    list and the detail page, gated by the same `visible_to` scope
    (department/owner) as every other document view, and taking its
    `Chunk`s, Task links and object-storage original with it -- no orphans
    left behind.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)
        self.doc.original_file.save("rechnung.pdf", io.BytesIO(b"%PDF-1.4 test"), save=True)
        self.chunk = Chunk.objects.create(
            document=self.doc,
            position=0,
            content="Rechnung Inhalt",
            embedding=_one_hot(0),
            embedding_model="stub",
            embedding_model_version="1",
        )
        self.task = Task.objects.create(title="Rechnung bezahlen", kind=Task.Kind.PAY)
        self.task.documents.add(self.doc)

    def test_delete_removes_document(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertFalse(Document.objects.filter(pk=self.doc.id).exists())

    def test_delete_removes_chunks(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertFalse(Chunk.objects.filter(pk=self.chunk.id).exists())

    def test_delete_removes_original_file_from_storage(self):
        file_path = self.doc.original_file.path

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertFalse(default_storage.exists(file_path))

    def test_delete_removes_task_link_but_keeps_task(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.task.refresh_from_db()
        self.assertTrue(Task.objects.filter(pk=self.task.id).exists())
        self.assertEqual(self.task.documents.count(), 0)

    def test_delete_redirects_to_list(self):
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertRedirects(response, reverse("documents:home"))

    def test_deleting_leitdokument_cascades_to_children(self):
        """Kaskadenverhalten (#1069): ein Unterdokument ist ohne sein

        Leitdokument eine verwaiste Karteikarte ohne erklärten Scope, daher
        `on_delete=CASCADE` -- Löschen des Leitdokuments nimmt seine ganze
        Unterdokument-Kette mit.
        """
        child = Document.objects.create_child(self.doc, title="Anhang.pdf")
        grandchild = Document.objects.create_child(child, title="Anhang-Anhang.pdf")

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertFalse(Document.objects.filter(pk=child.id).exists())
        self.assertFalse(Document.objects.filter(pk=grandchild.id).exists())

    def test_deleting_leitdokument_removes_children_original_files(self):
        child = Document.objects.create_child(self.doc, title="Anhang.pdf")
        child.original_file.save("anhang.pdf", io.BytesIO(b"%PDF-1.4 attachment"), save=True)
        file_path = child.original_file.path

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertFalse(default_storage.exists(file_path))

    def test_delete_requires_get_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:delete", args=[self.doc.id]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Document.objects.filter(pk=self.doc.id).exists())

    def test_delete_is_scoped_by_visibility(self):
        """A user outside the document's department must not be able to

        delete it, even by POSTing the URL directly -- same `visible_to`
        gate that already scopes every read/write document view.
        """
        self.client.force_login(self.user_b)
        response = self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=self.doc.id).exists())

    def test_delete_requires_login(self):
        response = self.client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Document.objects.filter(pk=self.doc.id).exists())

    def test_delete_without_csrf_token_is_rejected(self):
        """Baseline #1052: mutations rely on Django's global CsrfViewMiddleware

        (no view in this app carries `@csrf_exempt`) -- a POST missing the
        CSRF token must be rejected with 403 before the view body runs.
        """
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user_a)
        response = csrf_client.post(reverse("documents:delete", args=[self.doc.id]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Document.objects.filter(pk=self.doc.id).exists())

    def test_list_delete_button_shown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:delete", args=[self.doc.id]))

    def test_detail_delete_button_shown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, reverse("documents:delete", args=[self.doc.id]))


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentChildDeleteViewTests(TestCase):
    """Covers #1080: a single Unterdokument (e.g. a mail attachment like a

    signature logo) can be removed from a Leitdokument's children list
    without discarding the rest of the mail -- same scoping and cleanup
    guarantees as the whole-document delete (#1022), just narrowed to one
    child.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Mail von Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)

        self.child = Document.objects.create_child(
            self.doc, title="logo.png", child_role=Document.ChildRole.MAIL_ATTACHMENT
        )
        self.child.original_file.save("logo.png", io.BytesIO(b"\x89PNG fake"), save=True)
        self.chunk = Chunk.objects.create(
            document=self.child,
            position=0,
            content="logo",
            embedding=_one_hot(0),
            embedding_model="stub",
            embedding_model_version="1",
        )

    def test_delete_removes_child_but_keeps_parent(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:child_delete", args=[self.doc.id, self.child.id]))

        self.assertFalse(Document.objects.filter(pk=self.child.id).exists())
        self.assertTrue(Document.objects.filter(pk=self.doc.id).exists())

    def test_delete_removes_chunks(self):
        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:child_delete", args=[self.doc.id, self.child.id]))

        self.assertFalse(Chunk.objects.filter(pk=self.chunk.id).exists())

    def test_delete_removes_original_file_from_storage(self):
        file_path = self.child.original_file.path

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:child_delete", args=[self.doc.id, self.child.id]))

        self.assertFalse(default_storage.exists(file_path))

    def test_delete_removes_grandchildren_and_their_files(self):
        grandchild = Document.objects.create_child(self.child, title="inline.png")
        grandchild.original_file.save("inline.png", io.BytesIO(b"\x89PNG fake"), save=True)
        file_path = grandchild.original_file.path

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:child_delete", args=[self.doc.id, self.child.id]))

        self.assertFalse(Document.objects.filter(pk=grandchild.id).exists())
        self.assertFalse(default_storage.exists(file_path))

    def test_delete_renders_updated_children_partial(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "logo.png")
        self.assertContains(response, "Keine Unterdokumente.")

    def test_delete_requires_get_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_delete_is_scoped_by_parent_visibility(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_delete_is_scoped_by_own_visibility_even_if_parent_visible(self):
        """A child's scope can be overridden independently of its parent

        (`Document.create_child`) -- the delete must recheck the child's own
        visibility, not just trust that the parent was visible.
        """
        self.child.visibility = Document.Visibility.PRIVATE
        self.child.owner = self.user_b
        self.child.departments.clear()
        self.child.save()

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_child_id_must_belong_to_given_parent(self):
        other_doc = Document.objects.create(
            title="Anderes Leitdokument", visibility=Document.Visibility.DEPARTMENT
        )
        other_doc.departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:child_delete", args=[other_doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_delete_requires_login(self):
        response = self.client.post(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_delete_without_csrf_token_is_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user_a)
        response = csrf_client.post(
            reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())

    def test_detail_shows_child_delete_button(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(
            response, reverse("documents:child_delete", args=[self.doc.id, self.child.id])
        )


class DocumentMetaEditTests(TestCase):
    """Covers the nice-to-have HTMX editing of Vorgang/Tag assignments."""

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.vorgang = Vorgang.objects.create(name="Steuererklärung 2026")
        self.other_vorgang = Vorgang.objects.create(name="Umzug")
        self.tag = Tag.objects.create(name="Dringend")

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)

    def test_edit_form_shows_current_selection(self):
        self.doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta_edit", args=[self.doc.id]))

        self.assertContains(response, "selected")
        self.assertContains(response, self.vorgang.name)

    def test_edit_form_shows_correspondent_options_and_current_selection(self):
        correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.correspondent = correspondent
        self.doc.save(update_fields=["correspondent"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta_edit", args=[self.doc.id]))

        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, f'value="{correspondent.id}" selected')

    def test_edit_form_shows_current_direction(self):
        self.doc.direction = Document.Direction.AUSGANG
        self.doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta_edit", args=[self.doc.id]))

        self.assertContains(response, f'value="{Document.Direction.AUSGANG}" selected')

    def test_edit_form_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:meta_edit", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_post_updates_vorgaenge_and_tags(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"vorgaenge": [self.vorgang.id], "tags": [self.tag.id]},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(list(self.doc.vorgaenge.all()), [self.vorgang])
        self.assertEqual(list(self.doc.tags.all()), [self.tag])
        self.assertContains(response, self.vorgang.name)

    def test_post_updates_correspondent(self):
        correspondent = Correspondent.objects.create(name="Acme GmbH")

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"correspondent": correspondent.id}
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent, correspondent)
        self.assertContains(response, "Acme GmbH")

    def test_post_updates_direction(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"direction": Document.Direction.EINGANG},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)
        self.assertContains(response, "Eingang")

    def test_post_with_invalid_direction_keeps_previous_value(self):
        self.doc.direction = Document.Direction.AUSGANG
        self.doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:meta", args=[self.doc.id]), {"direction": "bogus"})

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.AUSGANG)

    def test_edit_form_shows_current_document_date(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta_edit", args=[self.doc.id]))

        self.assertContains(response, 'value="2026-01-15"')

    def test_post_updates_document_date(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"document_date": "2026-01-15"}
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_date, date(2026, 1, 15))
        self.assertContains(response, "15.01.2026")

    def test_post_can_clear_document_date(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:meta", args=[self.doc.id]), {"document_date": ""})

        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.document_date)

    def test_post_with_invalid_document_date_keeps_previous_value(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"document_date": "not-a-date"}
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_date, date(2026, 1, 15))

    def test_post_with_non_numeric_correspondent_clears_instead_of_500(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"correspondent": "not-a-number"}
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.correspondent)

    def test_post_can_clear_correspondent(self):
        self.doc.correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.save(update_fields=["correspondent"])

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:meta", args=[self.doc.id]), {})

        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.correspondent)

    def test_post_can_clear_assignments(self):
        self.doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:meta", args=[self.doc.id]), {})

        self.doc.refresh_from_db()
        self.assertEqual(list(self.doc.vorgaenge.all()), [])

    def test_post_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"vorgaenge": [self.vorgang.id]}
        )

        self.assertEqual(response.status_code, 404)
        self.doc.refresh_from_db()
        self.assertEqual(list(self.doc.vorgaenge.all()), [])


class DocumentMetaQuickCreateTests(TestCase):
    """Covers inline creation of a new Absender/Vorgang/Tag straight from the
    Zuordnung edit form (#1021) -- no context switch to the Stammdaten pages.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)

    def test_quick_create_correspondent_creates_and_assigns(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "correspondent"]),
            {"correspondent_name": "Neue Firma GmbH"},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent.name, "Neue Firma GmbH")
        self.assertContains(response, "Neue Firma GmbH")

    def test_quick_create_vorgang_creates_and_assigns(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"vorgang_name": "Umzug 2026"},
        )

        self.assertEqual(response.status_code, 200)
        vorgang = Vorgang.objects.get(name="Umzug 2026")
        self.assertIn(vorgang, self.doc.vorgaenge.all())

    def test_quick_create_tag_creates_and_assigns_with_dimension(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "Dringend", "tag_dimension": "Priorität"},
        )

        self.assertEqual(response.status_code, 200)
        tag = Tag.objects.get(name="Dringend", dimension="Priorität")
        self.assertIn(tag, self.doc.tags.all())

    def test_quick_create_uses_typed_name_despite_sibling_blank_blocks(self):
        # Regression for #1064: the "+ Anlegen" button lives inside the meta
        # <form>, so HTMX serialises the whole form -- including the empty
        # Vorgang/Tag quick-create inputs. When all three shared name="name",
        # Django's QueryDict.get() returned the last (empty) value and the
        # typed name was silently dropped. Distinct field names keep them apart.
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "correspondent"]),
            {
                "correspondent": "",
                "correspondent_name": "Neue Firma GmbH",
                "direction": Document.Direction.EINGANG,
                "vorgang_name": "",
                "tag_name": "",
                "tag_dimension": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent.name, "Neue Firma GmbH")
        self.assertNotContains(response, "Bitte einen Namen eingeben.")

    def test_quick_create_blank_name_keeps_typed_dimension_visible(self):
        # The typed value must survive an error render so the user does not
        # lose it (#1064: clear the input only after a successful submit).
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "", "tag_dimension": "Priorität"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertContains(response, "Bitte einen Namen eingeben.")
        self.assertContains(response, 'value="Priorität"')

    def test_quick_create_reuses_existing_by_name(self):
        existing = Correspondent.objects.create(name="Acme GmbH")

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "correspondent"]),
            {"correspondent_name": "Acme GmbH"},
        )

        self.assertEqual(Correspondent.objects.count(), 1)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent, existing)

    def test_quick_create_blank_name_shows_visible_error(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]), {"vorgang_name": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Vorgang.objects.count(), 0)
        self.assertContains(response, "Bitte einen Namen eingeben.")

    def test_quick_create_blank_name_shows_visible_error_for_correspondent(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "correspondent"]), {"correspondent_name": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Correspondent.objects.count(), 0)
        self.assertContains(response, "Bitte einen Namen eingeben.")

    def test_quick_create_blank_name_shows_visible_error_for_tag(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]), {"tag_name": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertContains(response, "Bitte einen Namen eingeben.")

    def test_quick_create_truncates_overlong_name_instead_of_500(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"vorgang_name": "x" * 300},
        )

        self.assertEqual(response.status_code, 200)
        vorgang = Vorgang.objects.get()
        self.assertEqual(len(vorgang.name), 255)

    def test_quick_create_unknown_kind_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "bogus"]), {"name": "x"}
        )

        self.assertEqual(response.status_code, 404)

    def test_quick_create_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"vorgang_name": "Umzug 2026"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Vorgang.objects.count(), 0)


class DocumentSuggestionActionTests(TestCase):
    """Covers accepting/rejecting KI-Analyse tag/Vorgang suggestions (#1020)

    -- the "Vorschläge, die der Nutzer annimmt/verwirft" principle: a
    suggestion only ever becomes a real Tag/Vorgang assignment through this
    explicit action, never automatically.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)

        self.tag_suggestion = TagSuggestion.objects.create(
            document=self.doc, name="Rechnung", dimension="Thema", confidence=0.9
        )
        self.vorgang_suggestion = VorgangSuggestion.objects.create(
            document=self.doc, name="Buchhaltung 2026", confidence=0.7
        )

    def test_pending_suggestions_are_shown_on_detail_page(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Rechnung")
        self.assertContains(response, "Buchhaltung 2026")

    def test_accept_tag_suggestion_creates_and_assigns_tag(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:tag_suggestion_accept", args=[self.doc.id, self.tag_suggestion.id])
        )

        self.assertEqual(response.status_code, 200)
        self.tag_suggestion.refresh_from_db()
        self.assertEqual(self.tag_suggestion.status, SuggestionStatus.ACCEPTED)
        tag = Tag.objects.get(name="Rechnung", dimension="Thema")
        self.assertIn(tag, self.doc.tags.all())

    def test_reject_tag_suggestion_does_not_create_tag(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:tag_suggestion_reject", args=[self.doc.id, self.tag_suggestion.id])
        )

        self.assertEqual(response.status_code, 200)
        self.tag_suggestion.refresh_from_db()
        self.assertEqual(self.tag_suggestion.status, SuggestionStatus.REJECTED)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertEqual(self.doc.tags.count(), 0)

    def test_accept_vorgang_suggestion_creates_and_assigns_vorgang(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse(
                "documents:vorgang_suggestion_accept",
                args=[self.doc.id, self.vorgang_suggestion.id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.vorgang_suggestion.refresh_from_db()
        self.assertEqual(self.vorgang_suggestion.status, SuggestionStatus.ACCEPTED)
        vorgang = Vorgang.objects.get(name="Buchhaltung 2026")
        self.assertIn(vorgang, self.doc.vorgaenge.all())

    def test_reject_vorgang_suggestion_does_not_create_vorgang(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse(
                "documents:vorgang_suggestion_reject",
                args=[self.doc.id, self.vorgang_suggestion.id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.vorgang_suggestion.refresh_from_db()
        self.assertEqual(self.vorgang_suggestion.status, SuggestionStatus.REJECTED)
        self.assertEqual(Vorgang.objects.count(), 0)

    def test_accepted_suggestion_no_longer_shown_as_pending(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:tag_suggestion_accept", args=[self.doc.id, self.tag_suggestion.id])
        )
        self.client.post(
            reverse(
                "documents:vorgang_suggestion_reject",
                args=[self.doc.id, self.vorgang_suggestion.id],
            )
        )

        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertNotContains(response, "Übernehmen")

    def test_suggestion_action_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:tag_suggestion_accept", args=[self.doc.id, self.tag_suggestion.id])
        )

        self.assertEqual(response.status_code, 404)
        self.tag_suggestion.refresh_from_db()
        self.assertEqual(self.tag_suggestion.status, SuggestionStatus.PENDING)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentRelatedViewTests(TestCase):
    """Covers the "Ähnliche Dokumente"-Block (#1088): automatische Treffer
    aus der Embedding-Ähnlichkeit plus manuelle Querverweise
    (`DocumentLink`), beides `visible_to`-gescoped.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = self._document("Rechnung Acme")
        self._add_chunk(self.doc, _one_hot(0))

    def _document(self, title, *, dept=None):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT
        )
        document.departments.add(dept or self.dept_a)
        return document

    def _add_chunk(self, document, vector):
        return Chunk.objects.create(
            document=document,
            position=0,
            content="chunk text",
            embedding=vector,
            embedding_model="stub",
            embedding_model_version="1",
        )

    def _related(self, document=None):
        return self.client.get(
            reverse("documents:related", args=[(document or self.doc).id])
        )

    def test_detail_page_lazy_loads_the_block(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Ähnliche Dokumente")
        self.assertContains(response, reverse("documents:related", args=[self.doc.id]))

    def test_similar_document_is_listed_with_score_and_link(self):
        similar = self._document("Rechnung Acme (Nachtrag)")
        self._add_chunk(similar, _one_hot(0))

        self.client.force_login(self.user_a)
        response = self._related()

        self.assertContains(response, "Rechnung Acme (Nachtrag)")
        self.assertContains(response, reverse("documents:detail", args=[similar.id]))
        self.assertContains(response, "100%")

    def test_document_below_threshold_is_not_listed(self):
        unrelated = self._document("Völlig anderes Thema")
        self._add_chunk(unrelated, _one_hot(1))

        self.client.force_login(self.user_a)
        response = self._related()

        # Nur die Trefferliste ist leer -- in der Auswahlliste für einen
        # manuellen Querverweis darf das Dokument sehr wohl stehen.
        self.assertNotContains(
            response, reverse("documents:detail", args=[unrelated.id])
        )
        self.assertContains(response, "Keine ähnlichen Dokumente")

    def test_document_outside_visibility_is_not_listed(self):
        foreign = self._document("Fremdes Dokument", dept=self.dept_b)
        self._add_chunk(foreign, _one_hot(0))

        self.client.force_login(self.user_a)
        response = self._related()

        self.assertNotContains(response, "Fremdes Dokument")

    def test_unindexed_document_shows_a_hint_instead_of_an_empty_list(self):
        unindexed = self._document("Noch nicht verarbeitet")

        self.client.force_login(self.user_a)
        response = self._related(unindexed)

        self.assertContains(response, "Noch nicht indiziert")

    def test_related_view_outside_visibility_returns_404(self):
        foreign = self._document("Fremdes Dokument", dept=self.dept_b)

        self.client.force_login(self.user_a)

        self.assertEqual(self._related(foreign).status_code, 404)

    def test_manual_link_is_created_and_shown_from_both_sides(self):
        other = self._document("Gehört dazu")

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_create", args=[self.doc.id]),
            {"target": other.id, "note": "gehört zu"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Gehört dazu")
        self.assertEqual(DocumentLink.objects.count(), 1)
        # Ungerichtet: derselbe Verweis steht auch im Detail des anderen
        # Dokuments, ohne dass eine zweite Zeile angelegt wurde.
        self.assertContains(self._related(other), "Rechnung Acme")

    def test_linking_the_same_pair_twice_does_not_create_a_second_link(self):
        other = self._document("Gehört dazu")

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:link_create", args=[self.doc.id]), {"target": other.id}
        )
        self.client.post(
            reverse("documents:link_create", args=[other.id]), {"target": self.doc.id}
        )

        self.assertEqual(DocumentLink.objects.count(), 1)

    def test_linked_document_is_not_repeated_as_a_similarity_hit(self):
        similar = self._document("Rechnung Acme (Nachtrag)")
        self._add_chunk(similar, _one_hot(0))

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_create", args=[self.doc.id]), {"target": similar.id}
        )

        self.assertEqual(response.content.count("Rechnung Acme (Nachtrag)".encode()), 1)

    def test_link_to_document_outside_visibility_is_rejected(self):
        foreign = self._document("Fremdes Dokument", dept=self.dept_b)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_create", args=[self.doc.id]), {"target": foreign.id}
        )

        self.assertEqual(DocumentLink.objects.count(), 0)
        self.assertNotContains(response, "Fremdes Dokument")
        self.assertContains(response, "Bitte ein Dokument zum Verknüpfen auswählen.")

    def test_link_to_itself_is_rejected(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_create", args=[self.doc.id]), {"target": self.doc.id}
        )

        self.assertEqual(DocumentLink.objects.count(), 0)
        self.assertContains(response, "nicht mit sich selbst")

    def test_link_without_target_shows_an_error(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_create", args=[self.doc.id]), {"target": ""}
        )

        self.assertEqual(DocumentLink.objects.count(), 0)
        self.assertContains(response, "Bitte ein Dokument zum Verknüpfen auswählen.")

    def test_link_can_be_removed_again(self):
        other = self._document("Gehört dazu")
        link, _created = link_documents(self.doc, other, created_by=self.user_a)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_delete", args=[self.doc.id, link.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentLink.objects.count(), 0)

    def test_link_of_another_document_cannot_be_removed_through_this_document(self):
        first = self._document("Erstes")
        second = self._document("Zweites")
        link, _created = link_documents(first, second, created_by=self.user_a)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:link_delete", args=[self.doc.id, link.id])
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(DocumentLink.objects.count(), 1)

    def test_link_to_document_outside_visibility_is_not_shown(self):
        """Ein Querverweis auf ein Dokument, das der Nutzer nicht sehen darf
        (z. B. weil sich der Scope später geändert hat), verschwindet still
        aus dem Block, statt dessen Titel zu leaken.
        """
        foreign = self._document("Fremdes Dokument", dept=self.dept_b)
        link_documents(self.doc, foreign, created_by=self.user_b)

        self.client.force_login(self.user_a)
        response = self._related()

        self.assertNotContains(response, "Fremdes Dokument")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self._related()

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class DocumentUploadViewTests(TestCase):
    """Covers the UI upload (#1019): files go through the same ingest
    contract (`apps.ingest.service.ingest_file`) as the folder/mail
    connectors -- dedup, storage, visibility, enqueue -- just fed by an
    HTMX multipart POST instead of a watched folder/mailbox.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

    def _upload(self, files, user=None):
        self.client.force_login(user or self.user_a)
        with patch("apps.ingest.service._enqueue_processing", return_value="task-1"):
            return self.client.post(reverse("documents:upload"), {"files": files})

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.post(
            reverse("documents:upload"),
            {"files": [SimpleUploadedFile("a.pdf", b"%PDF-1.4", content_type="application/pdf")]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_upload_creates_document_pending_and_enqueues_processing(self):
        response = self._upload(
            [SimpleUploadedFile("rechnung.pdf", b"%PDF-1.4 content", content_type="application/pdf")]
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hochgeladen")
        document = Document.objects.get()
        self.assertEqual(document.source, Document.Source.UPLOAD)
        self.assertEqual(document.owner, self.user_a)
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PENDING)

    def test_upload_response_triggers_list_refresh(self):
        response = self._upload(
            [SimpleUploadedFile("rechnung.pdf", b"%PDF-1.4 content", content_type="application/pdf")]
        )

        self.assertEqual(response["HX-Trigger"], "findus:documents-changed")

    def test_multiple_files_are_ingested_independently(self):
        response = self._upload(
            [
                SimpleUploadedFile("a.pdf", b"content a", content_type="application/pdf"),
                SimpleUploadedFile("b.pdf", b"content b", content_type="application/pdf"),
            ]
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Document.objects.count(), 2)

    def test_duplicate_sha256_is_recognized_and_not_reimported(self):
        self._upload([SimpleUploadedFile("a.pdf", b"same bytes", content_type="application/pdf")])

        response = self._upload(
            [SimpleUploadedFile("b.pdf", b"same bytes", content_type="application/pdf")]
        )

        self.assertContains(response, "Duplikat")
        self.assertEqual(Document.objects.count(), 1)

    def test_disallowed_extension_is_rejected_without_creating_document(self):
        with override_settings(FINDUS_INGEST_ALLOWED_EXTENSIONS=["pdf"]):
            response = self._upload(
                [SimpleUploadedFile("malware.exe", b"binary", content_type="application/octet-stream")]
            )

        self.assertContains(response, "wird nicht unterstützt")
        self.assertEqual(Document.objects.count(), 0)

    def test_oversized_file_is_rejected_without_creating_document(self):
        with override_settings(FINDUS_UPLOAD_MAX_SIZE_MB=0.00001):
            response = self._upload(
                [SimpleUploadedFile("big.pdf", b"more than ten bytes", content_type="application/pdf")]
            )

        self.assertContains(response, "zu groß")
        self.assertEqual(Document.objects.count(), 0)

    def test_uploaded_document_is_scoped_to_uploader_department(self):
        self._upload([SimpleUploadedFile("a.pdf", b"content a", content_type="application/pdf")])

        document = Document.objects.get()
        self.assertEqual(document.visibility, Document.Visibility.DEPARTMENT)
        self.assertIn(self.dept_a, document.departments.all())

    def test_uploaded_document_is_private_for_user_without_department(self):
        lone_user = User.objects.create_user(username="carol", password="x")

        self._upload([SimpleUploadedFile("a.pdf", b"content a", content_type="application/pdf")], user=lone_user)

        document = Document.objects.get()
        self.assertEqual(document.visibility, Document.Visibility.PRIVATE)
        self.assertEqual(document.owner, lone_user)

    def test_uploaded_document_is_only_visible_per_visibility_rule(self):
        other_dept = Department.objects.create(name="Dept B")
        other_user = User.objects.create_user(username="bob", password="x")
        other_user.departments.add(other_dept)

        self._upload([SimpleUploadedFile("a.pdf", b"content a", content_type="application/pdf")])

        self.client.force_login(other_user)
        response = self.client.get(reverse("documents:home"))
        self.assertNotContains(response, "a.pdf")


class NavOpenActionStatusBadgeTests(TestCase):
    """Covers the #1057 "Zu erledigen" nav shortcut: a link straight to the

    `action_status=offen` filter preset, with a count badge fed by
    `apps.documents.context_processors.open_action_status_count` -- present
    on every page since it lives in the sidebar, not just the Home view.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

    def _make_document(self, action_status):
        document = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            action_status=action_status,
        )
        document.departments.add(self.dept_a)
        return document

    def test_nav_link_targets_open_action_status_preset(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Zu erledigen")
        self.assertContains(response, "?action_status=offen")

    def test_nav_badge_counts_only_open_documents_visible_to_user(self):
        self._make_document(Document.ActionStatus.OPEN)
        self._make_document(Document.ActionStatus.OPEN)
        self._make_document(Document.ActionStatus.DONE)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, '<span class="badge text-bg-warning findus-nav-badge">2</span>')

    def test_nav_badge_is_absent_when_no_open_documents(self):
        self._make_document(Document.ActionStatus.DONE)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "findus-nav-badge")
