import io
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
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

    def test_list_row_links_to_detail(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:detail", args=[self.own_doc.id]))

    def test_list_row_has_delete_action(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:delete", args=[self.own_doc.id]))


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
        self.doc.save(update_fields=["markdown", "summary", "key_facts"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Rechnung von Acme über 123 EUR, fällig am 01.02.2026.")
        self.assertContains(response, "KI-extrahiert")
        self.assertContains(response, "Rechnung")
        self.assertContains(response, "2026-01-15")
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

    def test_document_links_are_shown_in_both_directions(self):
        other = Document.objects.create(
            title="Mahnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        other.departments.add(self.dept_a)
        DocumentLink.objects.create(
            from_document=self.doc, to_document=other, link_type=DocumentLink.LinkType.RELATED
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Mahnung Acme")
        self.assertContains(response, reverse("documents:detail", args=[other.id]))

    def test_linked_document_outside_visibility_is_not_leaked(self):
        hidden = Document.objects.create(
            title="Geheimvertrag", visibility=Document.Visibility.DEPARTMENT
        )
        hidden.departments.add(self.dept_b)
        DocumentLink.objects.create(
            from_document=self.doc, to_document=hidden, link_type=DocumentLink.LinkType.RELATED
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Geheimvertrag")


_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-detail-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentDetailOriginalDownloadTests(TestCase):
    """Covers requirement #5 (#1016) and its auth-gated streaming (#1024):
    the original file is opened/downloaded through
    `documents:original_download`, never through the storage backend's own
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
            title="Rechnung Acme", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc.departments.add(self.dept_a)
        self.doc.original_file.save("rechnung.pdf", io.BytesIO(b"%PDF-1.4 test"), save=True)

    def test_download_link_is_shown_for_document_with_original_file(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        download_url = reverse("documents:original_download", args=[self.doc.id])
        self.assertContains(response, "Original öffnen/herunterladen")
        self.assertContains(response, download_url)
        self.assertNotContains(response, self.doc.original_file.url)

    def test_no_download_link_without_original_file(self):
        self.doc.original_file.delete(save=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Original öffnen/herunterladen")

    def test_original_download_streams_file_content(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:original_download", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(response["Content-Type"], "application/pdf")

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

    def test_list_delete_button_shown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:delete", args=[self.doc.id]))

    def test_detail_delete_button_shown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, reverse("documents:delete", args=[self.doc.id]))


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
            {"name": "Neue Firma GmbH"},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent.name, "Neue Firma GmbH")
        self.assertContains(response, "Neue Firma GmbH")

    def test_quick_create_vorgang_creates_and_assigns(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"name": "Umzug 2026"},
        )

        self.assertEqual(response.status_code, 200)
        vorgang = Vorgang.objects.get(name="Umzug 2026")
        self.assertIn(vorgang, self.doc.vorgaenge.all())

    def test_quick_create_tag_creates_and_assigns_with_dimension(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"name": "Dringend", "dimension": "Priorität"},
        )

        self.assertEqual(response.status_code, 200)
        tag = Tag.objects.get(name="Dringend", dimension="Priorität")
        self.assertIn(tag, self.doc.tags.all())

    def test_quick_create_reuses_existing_by_name(self):
        existing = Correspondent.objects.create(name="Acme GmbH")

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "correspondent"]),
            {"name": "Acme GmbH"},
        )

        self.assertEqual(Correspondent.objects.count(), 1)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent, existing)

    def test_quick_create_blank_name_is_a_no_op(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]), {"name": ""}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Vorgang.objects.count(), 0)

    def test_quick_create_truncates_overlong_name_instead_of_500(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"name": "x" * 300},
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
            {"name": "Umzug 2026"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Vorgang.objects.count(), 0)


class StammdatenCrudTests(TestCase):
    """Covers CRUD in the user UI for the shared Absender/Vorgang/Tag
    vocabulary (#1021) -- previously only editable through the Django admin.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:stammdaten_list", args=["correspondents"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_home_redirects_to_a_kind(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:stammdaten_home"))

        self.assertEqual(response.status_code, 302)

    def test_unknown_kind_returns_404(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:stammdaten_list", args=["bogus"]))

        self.assertEqual(response.status_code, 404)

    def test_correspondent_list_shows_existing_entries(self):
        Correspondent.objects.create(name="Acme GmbH", email="info@acme.example")

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:stammdaten_list", args=["correspondents"]))

        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, "info@acme.example")

    def test_create_correspondent(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_list", args=["correspondents"]),
            {"name": "Acme GmbH", "email": "info@acme.example"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Correspondent.objects.get().name, "Acme GmbH")

    def test_create_vorgang(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_list", args=["vorgaenge"]), {"name": "Umzug 2026"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vorgang.objects.get().name, "Umzug 2026")

    def test_create_tag_with_dimension(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_list", args=["tags"]),
            {"name": "Dringend", "dimension": "Priorität"},
        )

        self.assertEqual(response.status_code, 302)
        tag = Tag.objects.get()
        self.assertEqual(tag.name, "Dringend")
        self.assertEqual(tag.dimension, "Priorität")

    def test_create_with_duplicate_name_shows_error_and_does_not_create(self):
        Correspondent.objects.create(name="Acme GmbH")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_list", args=["correspondents"]), {"name": "Acme GmbH"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Correspondent.objects.count(), 1)

    def test_edit_form_shows_current_values(self):
        vorgang = Vorgang.objects.create(name="Umzug 2026")

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents:stammdaten_edit", args=["vorgaenge", vorgang.id])
        )

        self.assertContains(response, "Umzug 2026")

    def test_edit_updates_entry(self):
        vorgang = Vorgang.objects.create(name="Umzug 2026")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_edit", args=["vorgaenge", vorgang.id]),
            {"name": "Umzug 2027"},
        )

        self.assertEqual(response.status_code, 302)
        vorgang.refresh_from_db()
        self.assertEqual(vorgang.name, "Umzug 2027")

    def test_delete_removes_entry(self):
        tag = Tag.objects.create(name="Dringend")

        self.client.force_login(self.user)
        response = self.client.post(reverse("documents:stammdaten_delete", args=["tags", tag.id]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Tag.objects.filter(pk=tag.id).exists())

    def test_delete_correspondent_in_use_nulls_document_reference_instead_of_blocking(self):
        correspondent = Correspondent.objects.create(name="Acme GmbH")
        doc = Document.objects.create(title="Rechnung", correspondent=correspondent)

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("documents:stammdaten_delete", args=["correspondents", correspondent.id])
        )

        self.assertEqual(response.status_code, 302)
        doc.refresh_from_db()
        self.assertIsNone(doc.correspondent)

    def test_delete_requires_post(self):
        tag = Tag.objects.create(name="Dringend")

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:stammdaten_delete", args=["tags", tag.id]))

        self.assertEqual(response.status_code, 405)


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
