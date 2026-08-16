import datetime
import io
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department
from apps.ai.providers.base import EmbeddingResult

from .models import (
    Chunk,
    Correspondent,
    Document,
    DocumentComment,
    DocumentLink,
    SuggestionStatus,
    Tag,
    TagSuggestion,
    Task,
    Vorgang,
    VorgangSuggestion,
    follow_up_week_end,
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

    def test_list_row_shows_action_status_toggle_when_open(self):
        self.own_doc.action_status = Document.ActionStatus.OPEN
        self.own_doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-action-status-toggle text-bg-warning")

    def test_list_row_shows_action_status_toggle_with_neutral_style_when_none(self):
        # #1137: der Umschalter ist ein Bedienelement, kein reines Abzeichen
        # -- er bleibt deshalb auch fuer "keine" sichtbar, statt wie das
        # frühere reine Abzeichen ganz zu verschwinden.
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-action-status-toggle border text-body-secondary")

    def test_filter_bar_includes_action_status_dropdown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'id="filter-action-status"')
        self.assertContains(response, 'name="action_status"')

    def test_filter_bar_includes_follow_up_dropdown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'id="filter-follow-up"')
        self.assertContains(response, 'name="follow_up"')

    def test_filter_by_follow_up_due_excludes_document_without_due_comment(self):
        """Covers requirement #2 (#1125): "fällig" narrows to documents with

        at least one comment whose `follow_up_date` has arrived, same
        "filters narrow, never widen" principle as every other filter here.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"follow_up": "due"})

        self.assertNotContains(response, "Rechnung Acme")

    def test_filter_by_follow_up_due_includes_document_with_overdue_comment(self):
        DocumentComment.objects.create(
            document=self.own_doc,
            body="Kündigungsfrist prüfen",
            follow_up_date=timezone.localdate() - datetime.timedelta(days=1),
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"follow_up": "due"})

        self.assertContains(response, "Rechnung Acme")

    def test_filter_by_follow_up_due_excludes_future_follow_up(self):
        DocumentComment.objects.create(
            document=self.own_doc,
            body="Verlängerung prüfen",
            follow_up_date=timezone.localdate() + datetime.timedelta(days=30),
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"), {"follow_up": "due"})

        self.assertNotContains(response, "Rechnung Acme")

    def test_multiple_due_comments_do_not_duplicate_the_document_row(self):
        """Two due comments on the same document must still surface it

        exactly once -- the `Exists`/`distinct()` annotation guards against
        the join fan-out a plain `filter(comments__...)` would cause. Counts
        table rows (`<tr>`), same technique as
        `test_child_document_does_not_get_its_own_top_level_row`: one header
        row plus one data row, not two data rows for the same document.
        """
        DocumentComment.objects.create(
            document=self.own_doc,
            body="Erste Wiedervorlage",
            follow_up_date=timezone.localdate(),
        )
        DocumentComment.objects.create(
            document=self.own_doc,
            body="Zweite Wiedervorlage",
            follow_up_date=timezone.localdate(),
        )

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"follow_up": "due", "view": ""}, HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.content.count(b"<tr>"), 2)

    def test_filter_by_sphere_excludes_non_matching(self):
        self.own_doc.sphere = Document.Sphere.PRIVAT
        self.own_doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"sphere": Document.Sphere.GESCHAEFTLICH}
        )

        self.assertNotContains(response, "Rechnung Acme")

    def test_filter_by_sphere_includes_matching(self):
        self.own_doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.own_doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"), {"sphere": Document.Sphere.GESCHAEFTLICH}
        )

        self.assertContains(response, "Rechnung Acme")

    def test_filter_bar_includes_sphere_dropdown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'id="filter-sphere"')
        self.assertContains(response, 'name="sphere"')

    def test_filter_bar_includes_tax_relevance_dropdown(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'id="filter-tax-relevance"')
        self.assertContains(response, 'name="tax_relevance"')
        # Der Sammelwert "privat absetzbar (Ja/Vielleicht)" steht zur Auswahl.
        self.assertContains(response, 'value="absetzbar"')

    def test_filter_by_tax_relevance_exact_value(self):
        self.own_doc.tax_relevance = Document.TaxRelevance.JA
        self.own_doc.save(update_fields=["tax_relevance"])

        self.client.force_login(self.user_a)
        matching = self.client.get(
            reverse("documents:home"), {"tax_relevance": Document.TaxRelevance.JA}
        )
        self.assertContains(matching, "Rechnung Acme")

        non_matching = self.client.get(
            reverse("documents:home"), {"tax_relevance": Document.TaxRelevance.NEIN}
        )
        self.assertNotContains(non_matching, "Rechnung Acme")

    def test_filter_by_deductible_bundles_ja_and_vielleicht(self):
        self.own_doc.tax_relevance = Document.TaxRelevance.VIELLEICHT
        self.own_doc.save(update_fields=["tax_relevance"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"),
            {"tax_relevance": Document.TAX_RELEVANCE_DEDUCTIBLE_FILTER},
        )

        # "vielleicht" zählt zum Sammelfilter "absetzbar" dazu.
        self.assertContains(response, "Rechnung Acme")

    def test_filter_by_deductible_excludes_nein(self):
        self.own_doc.tax_relevance = Document.TaxRelevance.NEIN
        self.own_doc.save(update_fields=["tax_relevance"])

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:home"),
            {"tax_relevance": Document.TAX_RELEVANCE_DEDUCTIBLE_FILTER},
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

    def test_grid_view_is_default(self):
        """#1124: Kachel/Grid is the default now -- no `view` param at all
        (fresh visit, no stored/explicit choice) renders the tile grid, not
        the timeline (#1092) or the table.
        """
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-doc-grid")
        self.assertNotContains(response, "findus-timeline\"")
        self.assertNotContains(response, "<table")

    def test_timeline_view_stays_reachable_via_explicit_param(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"), {"view": "timeline"})

        self.assertContains(response, "findus-timeline\"")
        self.assertNotContains(response, "findus-doc-grid")

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

    def test_view_toggle_offers_the_wiedervorlagen_mode(self):
        """Home ist die einzige Seite, die die Wiedervorlagen-Ansicht (#1129)
        rendern kann -- nur hier gehört ihr Umschalter hin. Die Hub-Seiten
        blenden ihn aus (Gegenstück-Tests in den Hub-Testdateien).
        """
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'data-view-value="wiedervorlagen"')

    def test_view_choice_is_persisted_under_its_own_storage_key(self):
        """Die Ansicht ist eine Benutzereinstellung, kein Filter: sie liegt

        unter einem eigenen localStorage-Schlüssel im gemeinsamen Partial --
        nicht im Filter-State von Home, den die Hub-Seiten gar nicht haben.
        Der Test hält fest, dass die Persistenz überhaupt ausgeliefert wird;
        das Wiederherstellen selbst passiert im Browser.
        """
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus:documents:view")

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


_GRID_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-grid-media-")
_GRID_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(MEDIA_ROOT=_GRID_MEDIA_ROOT, STORAGES=_GRID_LOCAL_STORAGES)
class DocumentGridViewTests(TestCase):
    """Kachelansicht der Dokumentliste (#1124): Default-Grid, Kachel-Inhalt
    (Thumbnail bzw. Platzhalter), die drei Aktionen und der Tag-Filter-Link --
    alles auf demselben visibility-gescopten `page_obj` wie Liste/Timeline.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_GRID_MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.acme = Correspondent.objects.create(name="Acme GmbH")
        self.tag = Tag.objects.create(name="Dringend", dimension="Priorität")

        self.doc = Document.objects.create(
            title="Rechnung Acme",
            correspondent=self.acme,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
            metadata={"mime_type": "application/pdf", "original_filename": "rechnung.pdf"},
        )
        self.doc.departments.add(self.dept)
        self.doc.tags.add(self.tag)
        self.doc.original_file.save("rechnung.pdf", io.BytesIO(b"%PDF-1.4 stub"), save=True)

        # Ein Dokument einer fremden Abteilung -- darf in der Kachelansicht
        # ebensowenig auftauchen wie in Liste/Timeline.
        self.hidden_doc = Document.objects.create(
            title="Vertrag Other GmbH",
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.hidden_doc.departments.add(Department.objects.create(name="Fremd"))

    def test_default_view_renders_grid_with_card_content(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-doc-grid")
        self.assertContains(response, "findus-doc-card")
        self.assertContains(response, "Rechnung Acme")
        self.assertContains(response, "Acme GmbH")

    def test_grid_scopes_documents_by_visibility(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "Vertrag Other GmbH")

    def test_card_shows_thumbnail_url_when_thumbnail_present(self):
        self.doc.thumbnail.save("thumb.webp", io.BytesIO(b"RIFFstubWEBP"), save=True)

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:thumbnail", args=[self.doc.id]))
        self.assertNotContains(response, "findus-doc-card-thumb-placeholder")

    def test_card_shows_placeholder_when_no_thumbnail(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "findus-doc-card-thumb-placeholder")
        # Dateiendung im Platzhalter, keine kaputte <img>-Box.
        self.assertContains(response, "PDF")
        self.assertNotContains(response, reverse("documents:thumbnail", args=[self.doc.id]))

    def test_card_exposes_all_three_actions(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:detail", args=[self.doc.id]))
        self.assertContains(response, reverse("documents:original_preview", args=[self.doc.id]))
        self.assertContains(response, reverse("documents:original_download", args=[self.doc.id]))

    def test_card_hides_preview_action_for_non_previewable_format(self):
        self.doc.metadata = {"mime_type": "application/zip", "original_filename": "archiv.zip"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        # Detail/Download bleiben, "Original ansehen" fällt weg (kein toter Button).
        self.assertContains(response, reverse("documents:original_download", args=[self.doc.id]))
        self.assertNotContains(response, reverse("documents:original_preview", args=[self.doc.id]))

    def test_card_tag_badge_links_to_tag_filter(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, 'data-tag-filter="%d"' % self.tag.id)
        self.assertContains(response, "?tag=%d" % self.tag.id)

    def test_delete_action_lives_in_card_overflow_menu(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, reverse("documents:delete", args=[self.doc.id]))
        self.assertContains(response, "dropdown-item text-danger")

    def test_card_shows_due_comment_indicator(self):
        DocumentComment.objects.create(
            document=self.doc,
            body="Kündigungsfrist prüfen",
            follow_up_date=timezone.localdate(),
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(response, "Wiedervorlage fällig")

    def test_card_hides_due_comment_indicator_without_due_comment(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "Wiedervorlage fällig")

    def test_card_hides_due_comment_indicator_for_future_follow_up(self):
        DocumentComment.objects.create(
            document=self.doc,
            body="Verlängerung prüfen",
            follow_up_date=timezone.localdate() + datetime.timedelta(days=30),
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "Wiedervorlage fällig")


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

    def test_detail_shows_five_tabs_with_details_as_default(self):
        """Neue Struktur (#1126): Zusammenfassung ungetabbt, darunter die
        fünf Tabs in fester Reihenfolge; "Details" ist per `active` der
        Default nach jedem Reload.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        for label in [
            "Details",
            "Kommentare",
            "Verknüpfungen",
            "Ähnliche Dokumente",
            "Unterdokumente",
        ]:
            self.assertContains(response, label)
        # "Details" ist der einzige aktive Tab-Button.
        self.assertContains(response, 'id="tab-details"')
        self.assertContains(response, 'class="nav-link active"')

    def test_similar_tab_is_not_rendered_inline(self):
        """Der Ähnlichkeits-Tab lädt erst beim Öffnen per HTMX nach (#1126):
        die Detailseite bringt nur den Platzhalter und den `hx-get`, keine
        Trefferliste.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, reverse("documents:related", args=[self.doc.id]))
        self.assertContains(response, "Wird geladen …")

    def test_tab_titles_show_counts_as_badges(self):
        DocumentComment.objects.create(
            document=self.doc, author=self.user_a, body="Notiz"
        )
        other = Document.objects.create(
            title="Gehört dazu", visibility=Document.Visibility.DEPARTMENT
        )
        other.departments.add(self.dept_a)
        link_documents(self.doc, other, created_by=self.user_a)
        child = Document.objects.create(
            title="Anhang", visibility=Document.Visibility.DEPARTMENT, parent=self.doc
        )
        child.departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        # Alle drei zählbaren Tabs stehen bei 1, keiner ist gedämpft.
        self.assertContains(response, '<span class="badge rounded-pill text-bg-secondary">1</span>', count=3)

    def test_empty_tab_badges_are_muted(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        # Kommentare, Verknüpfungen und Unterdokumente sind leer -> gedämpft.
        self.assertContains(response, "findus-tab-empty", count=3)

    def test_detail_tab_lists_do_not_cause_n_plus_one(self):
        """Die direkt mitgerenderten Tab-Listen (Verknüpfungen, Unter-
        dokumente, Aufgaben, Kommentare) müssen über
        `select_related`/`prefetch_related` laufen -- ein größerer Bestand
        darf die Query-Zahl nicht erhöhen (#1126, Anforderung 7).
        """
        def _populate(count):
            for i in range(count):
                linked = Document.objects.create(
                    title=f"Verknüpft {self.doc.id}-{i}",
                    visibility=Document.Visibility.DEPARTMENT,
                )
                linked.departments.add(self.dept_a)
                link_documents(self.doc, linked, created_by=self.user_a)

                child = Document.objects.create(
                    title=f"Kind {i}",
                    visibility=Document.Visibility.DEPARTMENT,
                    parent=self.doc,
                )
                child.departments.add(self.dept_a)

                DocumentComment.objects.create(
                    document=self.doc, author=self.user_a, body=f"Kommentar {i}"
                )
                task = Task.objects.create(title=f"Aufgabe {i}", kind=Task.Kind.PAY)
                task.departments.add(self.dept_a)
                task.documents.add(self.doc)

        self.client.force_login(self.user_a)
        url = reverse("documents:detail", args=[self.doc.id])

        _populate(1)
        with CaptureQueriesContext(connection) as few:
            self.client.get(url)

        _populate(4)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url)

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))

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

    def test_sphere_badge_is_shown(self):
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Geschäftlich")

    def test_ki_sphere_suggestion_badge_is_shown(self):
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.metadata = {"sphere_source": "ki"}
        self.doc.save(update_fields=["sphere", "metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "KI-Vorschlag")

    def test_tax_relevance_badge_and_reason_and_disclaimer_are_shown(self):
        self.doc.sphere = Document.Sphere.PRIVAT
        self.doc.tax_relevance = Document.TaxRelevance.JA
        self.doc.tax_relevance_reason = "Handwerkerrechnung mit Lohnanteil -> §35a"
        self.doc.save(
            update_fields=["sphere", "tax_relevance", "tax_relevance_reason"]
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Handwerkerrechnung mit Lohnanteil")
        self.assertContains(response, "keine Steuerberatung")

    def test_tax_relevance_hidden_as_nicht_zutreffend_for_business(self):
        # Geschäftliche Belege zeigen "Nicht zutreffend", nie fälschlich "Ja"
        # -- selbst wenn (Alt-)Wert am Feld noch "ja" stünde.
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.tax_relevance = Document.TaxRelevance.JA
        self.doc.save(update_fields=["sphere", "tax_relevance"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Nicht zutreffend")
        self.assertNotContains(response, "keine Steuerberatung")

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

        it -- plus die Key-Facts -- ist die primäre Ansicht.

        Die Key-Facts stehen seit dem Detail-Umbau nicht mehr in einem
        eigenen Panel über der Zusammenfassung, sondern im Zuordnungs-Block:
        Kontakt/Datum/Sprache als bearbeitbare Felder (das Datum deshalb
        ISO-formatiert im `<input type="date">`, nicht mehr als `d.m.Y`),
        Typ und Betrag/Frist als nicht editierbare Zeilen mit
        "KI-extrahiert"-Badge. Der frühere Ausklapper "Volltext anzeigen"
        ist mit dem Umbau entfallen -- das Original liegt daneben in der
        Vorschau, der extrahierte Rohtext wird nur noch gezeigt, solange es
        keine Zusammenfassung gibt (siehe
        `test_fallback_to_raw_text_when_no_summary_yet`).
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
        self.assertContains(response, 'value="2026-01-15"')
        self.assertContains(response, "123 EUR")
        self.assertContains(response, "2026-02-01")
        self.assertContains(response, "KI-generiert")

    def test_fallback_to_raw_text_when_no_summary_yet(self):
        """No KI-Analyse (#1020) has run yet -- the raw extraction result
        is shown directly, clearly marked as such, instead of an empty
        summary section.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "Noch keine KI-Zusammenfassung vorhanden")
        self.assertContains(response, "Betrag: 123 EUR")

    def test_detail_no_longer_lists_linked_tasks(self):
        """Aufgaben sind auf dem Rückzug (CLAUDE.md): die Detailseite zeigt

        keinen "Verknüpfte Aufgaben"-Block mehr, und der Aufruf lädt die
        Aufgaben deshalb auch nicht mehr. Der Test hält beides fest -- der
        Block, damit er nicht beim nächsten Umbau zurückkommt, und der
        fehlende Kontext, damit die Query nicht still wieder mitläuft.
        """
        task = Task.objects.create(title="Rechnung bezahlen", kind=Task.Kind.PAY)
        task.departments.add(self.dept_a)
        task.documents.add(self.doc)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Rechnung bezahlen")
        self.assertNotIn("tasks", response.context)

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


class DocumentDetailLayoutCssTests(SimpleTestCase):
    """Regression für #1127: die Tabs-Leiste im Detail-Grid muss direkt
    unter der Zusammenfassung stehen bleiben, unabhängig von der Höhe der
    rechten Vorschau oder des aktiven Tab-Inhalts.
    """

    def _read_css(self):
        css_path = Path(settings.BASE_DIR) / "static" / "css" / "findus.css"
        return css_path.read_text(encoding="utf-8")

    def test_desktop_grid_has_trailing_filler_row_so_tabs_stay_content_sized(self):
        css = self._read_css()

        media_block = re.search(
            r"@media \(min-width: 992px\)\s*\{\s*\.findus-detail-grid\s*\{(?P<body>.*?)\n    \}",
            css,
            re.DOTALL,
        )
        self.assertIsNotNone(
            media_block, "Desktop-Regel für .findus-detail-grid nicht gefunden."
        )
        body = media_block.group("body")

        # Ohne die dritte, leere Zeile verteilt der Grid-Algorithmus die
        # Extrahöhe der hohen Vorschau auf die `tabs`-Zeile und schiebt sie
        # damit nach unten (siehe #1127). `auto auto 1fr` hält Zusammenfassung
        # und Tabs auf ihrer eigenen Content-Höhe.
        self.assertIn("grid-template-rows: auto auto 1fr;", body)
        self.assertIn('"summary preview"', body)
        self.assertIn('"tabs    preview"', body)
        self.assertIn('".       preview"', body)

    def test_tabs_area_is_pinned_to_the_top_of_its_row(self):
        css = self._read_css()

        rule = re.search(r"\.findus-detail-tabs-area\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(rule, ".findus-detail-tabs-area Regel nicht gefunden.")
        self.assertIn("align-self: start;", rule.group("body"))


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


class DocumentActionStatusToggleViewTests(TestCase):
    """Covers the #1137 one-click toggle used by the overview views (Kachel,

    Liste, Timeline, Wiedervorlagen) -- distinct from `document_action_status`
    above, which stays reserved for the detail view's full dropdown.
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

    def test_toggle_sets_erledigt_from_none(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:action_status_toggle", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.DONE)
        self.assertContains(response, 'aria-pressed="true"')

    def test_toggle_sets_erledigt_from_offen(self):
        self.doc.action_status = Document.ActionStatus.OPEN
        self.doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:action_status_toggle", args=[self.doc.id]))

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.DONE)

    def test_toggle_falls_back_to_offen_from_erledigt(self):
        self.doc.action_status = Document.ActionStatus.DONE
        self.doc.save(update_fields=["action_status"])

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:action_status_toggle", args=[self.doc.id])
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.OPEN)
        self.assertContains(response, 'aria-pressed="false"')

    def test_toggle_ignores_posted_target_state(self):
        # Der neue Zustand kommt ausschliesslich aus der Datenbank, nicht aus
        # dem Request -- zwei schnelle Klicks duerfen den Zustand nicht
        # unkontrolliert kippen lassen.
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:action_status_toggle", args=[self.doc.id]),
            {"action_status": Document.ActionStatus.OPEN},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.action_status, Document.ActionStatus.DONE)

    def test_toggle_touches_no_other_field(self):
        self.doc.title = "Rechnung Acme"
        self.doc.save(update_fields=["title"])
        original_title = self.doc.title
        original_updated_at = self.doc.updated_at

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:action_status_toggle", args=[self.doc.id]))

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.title, original_title)
        self.assertGreater(self.doc.updated_at, original_updated_at)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:action_status_toggle", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 405)

    def test_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:action_status_toggle", args=[self.doc.id])
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

    def test_inline_preview_embeds_iframe_for_previewable_document(self):
        """Die Vorschau ist seit #1126 fest eingebettet (kein Slide-Over):
        ein PDF landet direkt als `<iframe>` auf der Detailseite, der auf den
        auth-gestützten Stream zeigt.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        preview_url = reverse("documents:original_preview", args=[self.doc.id])
        self.assertContains(response, "<iframe")
        self.assertContains(response, preview_url)

    def test_non_previewable_document_shows_placeholder_with_download(self):
        """Ein nicht inline-fähiges Format (#1126) verschwindet nicht, sondern
        zeigt einen Platzhalter mit Dateiname und Download -- und bettet
        keinen Preview-Stream ein.
        """
        self.doc.metadata = {"mime_type": "application/zip", "original_filename": "anhang.zip"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "keine Inline-Vorschau")
        self.assertContains(response, "anhang.zip")
        self.assertNotContains(response, reverse("documents:original_preview", args=[self.doc.id]))
        # Download must still be offered, unaffected by the missing preview.
        self.assertContains(response, "Original herunterladen")
        self.assertContains(response, reverse("documents:original_download", args=[self.doc.id]))

    def test_no_download_shown_without_original_file(self):
        self.doc.original_file.delete(save=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertNotContains(response, "Original herunterladen")
        self.assertContains(response, "Kein Original vorhanden.")

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
        """This is the route the fest eingebettete `<iframe>` (#1126) actually
        points at, so it -- not `original_download` -- must relax the global
        XFrameOptionsMiddleware DENY to SAMEORIGIN, or the browser refuses to
        display the PDF in the iframe (bug report for #1042).
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

    def test_image_preview_embeds_img_on_detail_page(self):
        self.doc.metadata = {"mime_type": "image/jpeg", "original_filename": "scan.jpg"}
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(response, "<img")
        self.assertContains(
            response, reverse("documents:original_preview", args=[self.doc.id])
        )


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


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentChildDetachViewTests(TestCase):
    """Covers #1111: ein Unterdokument (typisch: der wichtige Anhang einer

    belanglosen Mail) wird vom Leitdokument gelöst und damit zum
    eigenständigen Leitdokument -- ohne dass Original, Extraktion oder Index
    angetastet werden. Gegenstück zum Löschen (#1080).
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
            self.doc, title="rechnung.pdf", child_role=Document.ChildRole.MAIL_ATTACHMENT
        )
        self.child.original_file.save(
            "rechnung.pdf", io.BytesIO(b"%PDF-1.4 fake"), save=True
        )
        self.chunk = Chunk.objects.create(
            document=self.child,
            position=0,
            content="rechnung",
            embedding=_one_hot(0),
            embedding_model="stub",
            embedding_model_version="1",
        )

    def _detach(self, parent_id=None, child_id=None, **extra):
        return self.client.post(
            reverse(
                "documents:child_detach",
                args=[parent_id or self.doc.id, child_id or self.child.id],
            ),
            **extra,
        )

    def test_detach_clears_parent_and_child_role(self):
        self.client.force_login(self.user_a)
        self._detach()

        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent_id)
        self.assertEqual(self.child.child_role, "")

    def test_detached_document_shows_up_as_leitdokument(self):
        self.client.force_login(self.user_a)
        self._detach()

        self.assertIn(self.child, Document.objects.roots())
        self.assertIn(self.child, Document.objects.visible_to(self.user_a))

    def test_detach_keeps_document_original_and_index(self):
        file_path = self.child.original_file.path

        self.client.force_login(self.user_a)
        self._detach()

        self.child.refresh_from_db()
        self.assertTrue(Document.objects.filter(pk=self.child.id).exists())
        self.assertTrue(default_storage.exists(file_path))
        self.assertTrue(Chunk.objects.filter(pk=self.chunk.id).exists())
        self.assertTrue(self.child.original_file)

    def test_detach_keeps_parent_and_its_other_children(self):
        sibling = Document.objects.create_child(self.doc, title="logo.png")

        self.client.force_login(self.user_a)
        self._detach()

        self.doc.refresh_from_db()
        sibling.refresh_from_db()
        self.assertIsNone(self.doc.parent_id)
        self.assertEqual(sibling.parent_id, self.doc.id)

    def test_detach_keeps_own_scope(self):
        self.client.force_login(self.user_a)
        self._detach()

        self.child.refresh_from_db()
        self.assertEqual(self.child.visibility, Document.Visibility.DEPARTMENT)
        self.assertEqual(list(self.child.departments.all()), [self.dept_a])
        self.assertNotIn(self.child, Document.objects.visible_to(self.user_b))

    def test_detach_backfills_empty_scope_from_parent(self):
        """Ohne Abteilung wäre das getrennte Dokument für niemanden mehr

        sichtbar -- der Scope des bisherigen Leitdokuments springt ein.
        Erreichbar ist dieser Fall nur als Superuser: für alle anderen ist
        ein abteilungsloses Abteilungs-Dokument schon vor dem Trennen
        unsichtbar (`visible_to`).
        """
        self.child.departments.clear()
        admin = User.objects.create_superuser(username="root", password="x")

        self.client.force_login(admin)
        self._detach()

        self.child.refresh_from_db()
        self.assertEqual(list(self.child.departments.all()), [self.dept_a])
        self.assertIn(self.child, Document.objects.visible_to(self.user_a))

    def test_detach_backfills_owner_from_parent(self):
        self.doc.owner = self.user_a
        self.doc.save()
        self.child.owner = None
        self.child.save()

        self.client.force_login(self.user_a)
        self._detach()

        self.child.refresh_from_db()
        self.assertEqual(self.child.owner_id, self.user_a.id)

    def test_detach_leaves_soft_link_to_former_parent(self):
        self.client.force_login(self.user_a)
        self._detach()

        self.assertTrue(DocumentLink.objects.for_document(self.child).exists())
        link = DocumentLink.objects.for_document(self.child).first()
        self.assertEqual(link.other_document_id(self.child), self.doc.id)
        self.assertIn("vormals", link.note)

    def test_detach_from_parent_view_renders_children_partial(self):
        self.client.force_login(self.user_a)
        response = self._detach(HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "rechnung.pdf")
        self.assertContains(response, "Keine Unterdokumente.")

    def test_detach_asks_htmx_to_reload_the_links_and_related_blocks(self):
        self.client.force_login(self.user_a)
        response = self._detach(HTTP_HX_REQUEST="true")

        # Der Soft-Link (#1088) muss im Verknüpfungs-Tab und das getrennte
        # Kind im Ähnlichkeits-Tab erscheinen -- beide Tabs hängen an eigenen
        # Targets (#1126).
        self.assertIn("findus:links-refresh", response["HX-Trigger"])
        self.assertIn("findus:related-refresh", response["HX-Trigger"])

    def test_detach_from_child_view_redirects_to_child_detail(self):
        self.client.force_login(self.user_a)
        response = self._detach()

        self.assertRedirects(
            response, reverse("documents:detail", args=[self.child.id])
        )

    def test_detached_detail_page_has_no_breadcrumb(self):
        self.client.force_login(self.user_a)
        self._detach()
        response = self.client.get(reverse("documents:detail", args=[self.child.id]))

        self.assertNotContains(response, "findus-document-breadcrumb")

    def test_detach_requires_get_not_allowed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:child_detach", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 405)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_detach_is_scoped_by_parent_visibility(self):
        self.client.force_login(self.user_b)
        response = self._detach()

        self.assertEqual(response.status_code, 404)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_detach_is_scoped_by_own_visibility_even_if_parent_visible(self):
        self.child.visibility = Document.Visibility.PRIVATE
        self.child.owner = self.user_b
        self.child.departments.clear()
        self.child.save()

        self.client.force_login(self.user_a)
        response = self._detach()

        self.assertEqual(response.status_code, 404)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_child_id_must_belong_to_given_parent(self):
        other_doc = Document.objects.create(
            title="Anderes Leitdokument", visibility=Document.Visibility.DEPARTMENT
        )
        other_doc.departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self._detach(parent_id=other_doc.id)

        self.assertEqual(response.status_code, 404)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_detach_requires_login(self):
        response = self._detach()

        self.assertEqual(response.status_code, 302)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_detach_without_csrf_token_is_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user_a)
        response = csrf_client.post(
            reverse("documents:child_detach", args=[self.doc.id, self.child.id])
        )

        self.assertEqual(response.status_code, 403)
        self.child.refresh_from_db()
        self.assertEqual(self.child.parent_id, self.doc.id)

    def test_parent_detail_shows_detach_button(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        self.assertContains(
            response, reverse("documents:child_detach", args=[self.doc.id, self.child.id])
        )
        self.assertContains(response, "Trennen")

    def test_child_detail_shows_detach_button(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.child.id]))

        self.assertContains(
            response, reverse("documents:child_detach", args=[self.doc.id, self.child.id])
        )
        self.assertContains(response, "Von Leitdokument lösen")


class DocumentMetaEditTests(TestCase):
    """Covers the directly editable Zuordnungs-Block: every field renders as
    its own control and saves itself via HTMX.

    A save carries a `field` marker and must touch only that field -- the
    checks below therefore always assert what stayed untouched, too.
    """

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

    def test_block_shows_current_selection(self):
        self.doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, f'value="{self.vorgang.id}"')
        self.assertContains(response, self.vorgang.name)

    def test_block_shows_tag_chip_with_dimension_color_class(self):
        # Tag-Chips tragen dieselbe Dimensions-Farbklasse wie Tag-Badges
        # anderswo in der App (#1124) -- Vorgang-Chips dagegen nie.
        tagged = Tag.objects.create(name="Rechnung", dimension="Dokumenttyp")
        self.doc.tags.add(tagged)
        self.doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))
        content = response.content.decode()

        self.assertIn("findus-tag-dim-", content)
        vorgang_chip_start = content.index(f'data-id="{self.vorgang.id}"')
        vorgang_chip_markup = content[max(0, vorgang_chip_start - 200) : vorgang_chip_start]
        self.assertNotIn("findus-tag-dim-", vorgang_chip_markup)

    def test_block_shows_correspondent_options_and_current_selection(self):
        correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.correspondent = correspondent
        self.doc.save(update_fields=["correspondent"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, f'value="{correspondent.id}" selected')

    def test_block_shows_current_direction(self):
        self.doc.direction = Document.Direction.AUSGANG
        self.doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, f'value="{Document.Direction.AUSGANG}" selected')

    def test_block_has_no_separate_edit_toggle(self):
        """Die Felder sind direkt bearbeitbar -- ohne den früheren
        "Zuordnung bearbeiten"-Umweg.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertNotContains(response, "Zuordnung bearbeiten")

    def test_block_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_post_updates_vorgaenge_and_tags(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {
                "field": ["vorgaenge", "tags"],
                "vorgaenge": [self.vorgang.id],
                "tags": [self.tag.id],
            },
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
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "correspondent", "correspondent": correspondent.id},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent, correspondent)
        self.assertContains(response, "Acme GmbH")

    def test_post_updates_direction(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "direction", "direction": Document.Direction.EINGANG},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)
        self.assertContains(response, f'value="{Document.Direction.EINGANG}" selected')

    def test_post_of_one_field_keeps_the_other_assignments(self):
        """Ein Feld speichern heißt: nur dieses Feld. Vor dem Inline-Umbau
        kam immer das ganze Formular, ein Einzel-POST hätte den Rest
        geleert.
        """
        correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.correspondent = correspondent
        self.doc.save(update_fields=["correspondent"])
        self.doc.vorgaenge.add(self.vorgang)
        self.doc.tags.add(self.tag)

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "direction", "direction": Document.Direction.EINGANG},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)
        self.assertEqual(self.doc.correspondent, correspondent)
        self.assertEqual(list(self.doc.vorgaenge.all()), [self.vorgang])
        self.assertEqual(list(self.doc.tags.all()), [self.tag])

    def test_post_with_invalid_direction_keeps_previous_value(self):
        self.doc.direction = Document.Direction.AUSGANG
        self.doc.save(update_fields=["direction"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "direction", "direction": "bogus"},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.AUSGANG)

    def test_block_shows_current_sphere(self):
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, f'value="{Document.Sphere.GESCHAEFTLICH}" selected')

    def test_post_updates_sphere(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "sphere", "sphere": Document.Sphere.GESCHAEFTLICH},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.sphere, Document.Sphere.GESCHAEFTLICH)
        self.assertContains(response, "Geschäftlich")

    def test_post_with_invalid_sphere_keeps_previous_value(self):
        self.doc.sphere = Document.Sphere.PRIVAT
        self.doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "sphere", "sphere": "bogus"},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.sphere, Document.Sphere.PRIVAT)

    def test_post_clears_ki_sphere_provenance_badge(self):
        """Ein manuelles Speichern bestätigt die Sphäre -- das "KI-Vorschlag"-
        Kennzeichen (#1112) muss danach weg sein, damit das Badge verschwindet.
        """
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.metadata = {"sphere_source": "ki"}
        self.doc.save(update_fields=["sphere", "metadata"])

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "sphere", "sphere": Document.Sphere.PRIVAT},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.sphere, Document.Sphere.PRIVAT)
        self.assertNotIn("sphere_source", self.doc.metadata)
        self.assertNotContains(response, "KI-Vorschlag")

    def test_block_shows_tax_relevance_select(self):
        self.doc.tax_relevance = Document.TaxRelevance.VIELLEICHT
        self.doc.save(update_fields=["tax_relevance"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, 'name="tax_relevance"')
        self.assertContains(
            response, f'value="{Document.TaxRelevance.VIELLEICHT}" selected'
        )

    def test_block_hides_tax_relevance_select_for_business_document(self):
        """Bei einem geschäftlichen Beleg ist die private ESt-Absetzbarkeit
        kein Feld, sondern eine Feststellung (#1113).
        """
        self.doc.sphere = Document.Sphere.GESCHAEFTLICH
        self.doc.save(update_fields=["sphere"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertNotContains(response, 'name="tax_relevance"')
        self.assertContains(response, "Nicht zutreffend")

    def test_post_updates_tax_relevance(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "tax_relevance", "tax_relevance": Document.TaxRelevance.JA},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tax_relevance, Document.TaxRelevance.JA)

    def test_post_with_invalid_tax_relevance_keeps_previous_value(self):
        self.doc.tax_relevance = Document.TaxRelevance.NEIN
        self.doc.save(update_fields=["tax_relevance"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "tax_relevance", "tax_relevance": "bogus"},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tax_relevance, Document.TaxRelevance.NEIN)

    def test_post_changing_tax_relevance_clears_ki_reason_and_badge(self):
        """Ändert der Nutzer die Einschätzung, verliert die KI-Begründung ihre
        Grundlage -- sie wird verworfen, ebenso das "KI-Vorschlag"-Kennzeichen.
        """
        self.doc.sphere = Document.Sphere.PRIVAT
        self.doc.tax_relevance = Document.TaxRelevance.JA
        self.doc.tax_relevance_reason = "Handwerker -> §35a"
        self.doc.metadata = {"tax_relevance_source": "ki"}
        self.doc.save(
            update_fields=[
                "sphere",
                "tax_relevance",
                "tax_relevance_reason",
                "metadata",
            ]
        )

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {
                "field": ["sphere", "tax_relevance"],
                "sphere": Document.Sphere.PRIVAT,
                "tax_relevance": Document.TaxRelevance.NEIN,
            },
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tax_relevance, Document.TaxRelevance.NEIN)
        self.assertEqual(self.doc.tax_relevance_reason, "")
        self.assertNotIn("tax_relevance_source", self.doc.metadata)
        self.assertNotContains(response, "KI-Vorschlag")

    def test_block_shows_current_document_date(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, 'value="2026-01-15"')

    def test_post_updates_document_date(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "document_date", "document_date": "2026-01-15"},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_date, date(2026, 1, 15))
        self.assertContains(response, 'value="2026-01-15"')

    def test_post_can_clear_document_date(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "document_date", "document_date": ""},
        )

        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.document_date)

    def test_post_marks_the_document_date_as_manually_set(self):
        """#1141: der Vermerk ist der Schutz vor dem naechsten Wartungslauf --

        ohne ihn wuerde die erneute KI-Analyse die Korrektur ueberschreiben.
        """
        self.doc.metadata = {
            "document_date_source": "erstellt",
            "document_date_upload_conflict": True,
        }
        self.doc.save(update_fields=["metadata"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "document_date", "document_date": "2026-01-15"},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.metadata["document_date_source"], "manuell")
        self.assertNotIn("document_date_upload_conflict", self.doc.metadata)

    def test_clearing_the_document_date_releases_it_for_the_next_analysis(self):
        """Ein geleertes Feld ist keine zu schuetzende Korrektur, sondern die

        Freigabe, das Datum neu bestimmen zu lassen (#1141).
        """
        self.doc.document_date = date(2026, 1, 15)
        self.doc.metadata = {"document_date_source": "manuell"}
        self.doc.save(update_fields=["document_date", "metadata"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "document_date", "document_date": ""},
        )

        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.document_date)
        self.assertNotIn("document_date_source", self.doc.metadata)

    def test_block_shows_the_document_date_source(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.metadata = {"document_date_source": "zeitraum_ende"}
        self.doc.key_facts = {"period_start": "2025-12-01", "period_end": "2026-01-15"}
        self.doc.save(update_fields=["document_date", "metadata", "key_facts"])

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:meta", args=[self.doc.id]))

        self.assertContains(response, "Ende des Abrechnungszeitraums")
        self.assertContains(response, "01.12.2025 – 15.01.2026")

    def test_post_with_invalid_document_date_keeps_previous_value(self):
        self.doc.document_date = date(2026, 1, 15)
        self.doc.save(update_fields=["document_date"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "document_date", "document_date": "not-a-date"},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_date, date(2026, 1, 15))

    def test_post_with_non_numeric_correspondent_clears_instead_of_500(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "correspondent", "correspondent": "not-a-number"},
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.correspondent)

    def test_post_can_clear_correspondent(self):
        self.doc.correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.doc.save(update_fields=["correspondent"])

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "correspondent", "correspondent": ""},
        )

        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.correspondent)

    def test_post_can_clear_assignments(self):
        """Eine leer geräumte Mehrfachauswahl schickt gar nichts mit -- erst
        die `field`-Markierung macht daraus ein "alles abwählen".
        """
        self.doc.vorgaenge.add(self.vorgang)

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]), {"field": "vorgaenge"}
        )

        self.doc.refresh_from_db()
        self.assertEqual(list(self.doc.vorgaenge.all()), [])

    def test_post_without_field_marker_writes_the_submitted_names(self):
        """Ohne HTMX (ein einfacher Formular-POST) zählt, welche Feldnamen
        mitkommen -- die Markierung ist nur der genauere Weg.
        """
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"direction": Document.Direction.EINGANG},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)

    def test_post_refreshes_the_hub_links_out_of_band(self):
        """Kontakt und Vorgänge stehen auch im Schnellzugriff über der Seite
        (#1098) -- der zieht bei jeder Inline-Änderung mit, statt bis zum
        nächsten Seitenaufbau die alte Zuordnung zu zeigen.
        """
        correspondent = Correspondent.objects.create(name="Acme GmbH")

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "correspondent", "correspondent": correspondent.id},
        )

        self.assertContains(response, 'id="document-hub-links"')
        self.assertContains(response, 'hx-swap-oob="true"')
        self.assertContains(
            response,
            reverse("documents:correspondent_detail", args=[correspondent.id]),
        )

    def test_hub_links_stay_in_the_page_as_swap_target_when_unassigned(self):
        """Ohne Zuordnung bleibt der Schnellzugriff als (verstecktes)
        Swap-Ziel stehen -- sonst hätte die erste Zuweisung nichts zu
        tauschen.
        """
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.doc.id]))

        opening_tag = re.search(
            r"<nav[^>]*id=\"document-hub-links\"[^>]*>", response.content.decode()
        )
        self.assertIsNotNone(opening_tag)
        self.assertIn("hidden", opening_tag.group(0))
        self.assertNotContains(response, "findus-detail-hub-link-group")

    def test_post_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:meta", args=[self.doc.id]),
            {"field": "vorgaenge", "vorgaenge": [self.vorgang.id]},
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

    def test_quick_create_blank_name_with_dimension_shows_visible_error(self):
        # Ein separat mitgeschicktes `tag_dimension` (ältere Zwei-Felder-
        # Schnellanlage, #1136 durch die kombinierte "Dimension:Name"-Eingabe
        # ersetzt) darf ein leeres `tag_name` nicht verdecken -- der Fehler
        # muss trotzdem sichtbar werden.
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "", "tag_dimension": "Priorität"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Tag.objects.count(), 0)
        self.assertContains(response, "Bitte einen Namen eingeben.")

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

    def test_quick_create_tag_with_combined_dimension_syntax(self):
        # Die Token-Eingabe (#1136) schickt Dimension+Name kombiniert als
        # "Dimension:Name" statt über ein separates Feld.
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "Sachgebiet:Miete"},
        )

        self.assertEqual(response.status_code, 200)
        tag = Tag.objects.get(name="Miete", dimension="Sachgebiet")
        self.assertIn(tag, self.doc.tags.all())

    def test_quick_create_tag_without_colon_has_no_dimension(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "Dringend"},
        )

        tag = Tag.objects.get(name="Dringend")
        self.assertEqual(tag.dimension, "")
        self.assertIn(tag, self.doc.tags.all())

    def test_quick_create_chip_response_returns_only_the_new_chip(self):
        # Die Token-Eingabe (#1136) hängt bei Neuanlage nur den Chip an,
        # statt den ganzen Zuordnungs-Block neu zu rendern -- sonst ginge
        # ein noch nicht bestätigter Suchtext im jeweils anderen Feld
        # verloren.
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"vorgang_name": "Umzug 2026", "chip": "1"},
        )

        self.assertEqual(response.status_code, 200)
        vorgang = Vorgang.objects.get(name="Umzug 2026")
        self.assertIn(vorgang, self.doc.vorgaenge.all())
        self.assertContains(response, f'value="{vorgang.id}"')
        self.assertContains(response, "Umzug 2026")
        self.assertNotContains(response, "findus-detail-meta-list")

    def test_quick_create_chip_response_for_vorgang_refreshes_hub_links(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "vorgang"]),
            {"vorgang_name": "Umzug 2026", "chip": "1"},
        )

        self.assertContains(response, 'id="document-hub-links"')
        self.assertContains(response, 'hx-swap-oob="true"')

    def test_quick_create_chip_response_for_tag_has_no_hub_links(self):
        # Tags stehen nicht im Schnellzugriff (#1098) -- anders als Vorgänge.
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:meta_quick_create", args=[self.doc.id, "tag"]),
            {"tag_name": "Dringend", "chip": "1"},
        )

        self.assertNotContains(response, 'id="document-hub-links"')


class DocumentMetaSearchTests(TestCase):
    """Covers die Trefferliste der Token-Eingabe (#1136) -- rein lesend."""

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

    def test_search_vorgang_filters_by_icontains(self):
        match = Vorgang.objects.create(name="Steuererklärung 2026")
        Vorgang.objects.create(name="Umzug")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "steuer"}
        )

        self.assertContains(response, match.name)
        self.assertNotContains(response, "Umzug")

    def test_search_excludes_already_assigned_vorgang(self):
        assigned = Vorgang.objects.create(name="Steuererklärung 2026")
        self.doc.vorgaenge.add(assigned)

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "steuer"}
        )

        self.assertNotContains(response, f'data-token-id="{assigned.id}"')

    def test_search_limits_vorgang_results(self):
        for i in range(15):
            Vorgang.objects.create(name=f"Vorgang {i:02d}")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "Vorgang"}
        )

        self.assertEqual(response.content.decode().count("data-token-id="), 10)

    def test_search_vorgang_prefix_match_sorts_first(self):
        Vorgang.objects.create(name="Alte Steuererklärung")
        prefix_match = Vorgang.objects.create(name="Steuererklärung 2026")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "Steuer"}
        )

        content = response.content.decode()
        self.assertLess(
            content.index(prefix_match.name), content.index("Alte Steuererklärung")
        )

    def test_search_vorgang_shows_create_entry_without_exact_match(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]),
            {"q": "Neuer Vorgang"},
        )

        self.assertContains(response, "neu anlegen")
        self.assertContains(response, "Neuer Vorgang")

    def test_search_vorgang_hides_create_entry_with_exact_match(self):
        Vorgang.objects.create(name="Umzug")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "Umzug"}
        )

        self.assertNotContains(response, "neu anlegen")

    def test_search_tag_matches_name_or_dimension(self):
        by_name = Tag.objects.create(name="Rechnung", dimension="Dokumenttyp")
        by_dimension = Tag.objects.create(name="Miete", dimension="Rechnungswesen")
        Tag.objects.create(name="Dringend")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "tag"]), {"q": "rechn"}
        )

        self.assertContains(response, str(by_name))
        self.assertContains(response, str(by_dimension))
        self.assertNotContains(response, "Dringend")

    def test_search_tag_create_entry_uses_dimension_colon_name(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "tag"]),
            {"q": "Sachgebiet:Miete"},
        )

        self.assertContains(response, "Sachgebiet:Miete")
        self.assertContains(response, "neu anlegen")

    def test_search_tag_hides_create_entry_when_dimension_and_name_match(self):
        Tag.objects.create(name="Miete", dimension="Sachgebiet")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "tag"]),
            {"q": "Sachgebiet:Miete"},
        )

        self.assertNotContains(response, "neu anlegen")

    def test_search_blank_query_returns_no_results(self):
        Vorgang.objects.create(name="Umzug")

        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": ""}
        )

        self.assertNotContains(response, "data-token-result")

    def test_search_unknown_kind_returns_404(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "correspondent"]), {"q": "x"}
        )

        self.assertEqual(response.status_code, 404)

    def test_search_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(
            reverse("documents:meta_search", args=[self.doc.id, "vorgang"]), {"q": "x"}
        )

        self.assertEqual(response.status_code, 404)


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

    def _links(self, document=None):
        return self.client.get(
            reverse("documents:links", args=[(document or self.doc).id])
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
        # Ungerichtet: derselbe Verweis steht auch im Verknüpfungs-Tab des
        # anderen Dokuments, ohne dass eine zweite Zeile angelegt wurde.
        self.assertContains(self._links(other), "Rechnung Acme")

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
        aus dem Verknüpfungs-Tab, statt dessen Titel zu leaken.
        """
        foreign = self._document("Fremdes Dokument", dept=self.dept_b)
        link_documents(self.doc, foreign, created_by=self.user_b)

        self.client.force_login(self.user_a)
        response = self._links()

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


class DocumentCommentFollowUpQuerySetTests(TestCase):
    """Covers the time-window queryset methods added for the Wiedervorlagen-

    Ansicht (#1129) -- `overdue`/`due_today`/`due_this_week`/`due_later`,
    kept separate from the existing `due()` (überfällig *und* heute, #1125)
    which continues to drive the card indicator and the reminder job.
    """

    def setUp(self):
        self.document = Document.objects.create(
            title="Rechnung Acme", visibility=Document.Visibility.PRIVATE
        )

    def _comment(self, follow_up_date):
        return DocumentComment.objects.create(
            document=self.document, body="Wiedervorlage", follow_up_date=follow_up_date
        )

    def test_overdue_excludes_today_and_future(self):
        monday = datetime.date(2026, 8, 17)
        overdue = self._comment(monday - datetime.timedelta(days=1))
        self._comment(monday)
        self._comment(monday + datetime.timedelta(days=1))

        result = list(DocumentComment.objects.overdue(on=monday))

        self.assertEqual(result, [overdue])

    def test_due_today_matches_only_today(self):
        monday = datetime.date(2026, 8, 17)
        self._comment(monday - datetime.timedelta(days=1))
        today = self._comment(monday)
        self._comment(monday + datetime.timedelta(days=1))

        result = list(DocumentComment.objects.due_today(on=monday))

        self.assertEqual(result, [today])

    def test_due_this_week_spans_tomorrow_through_sunday(self):
        monday = datetime.date(2026, 8, 17)
        self._comment(monday)  # heute -- gehoert nicht zu "diese Woche"
        tomorrow = self._comment(monday + datetime.timedelta(days=1))
        sunday = self._comment(monday + datetime.timedelta(days=6))
        self._comment(monday + datetime.timedelta(days=7))  # naechster Montag -- "spaeter"

        result = list(DocumentComment.objects.due_this_week(on=monday))

        self.assertCountEqual(result, [tomorrow, sunday])

    def test_due_later_starts_after_sunday(self):
        monday = datetime.date(2026, 8, 17)
        self._comment(monday + datetime.timedelta(days=6))  # Sonntag -- noch "diese Woche"
        later = self._comment(monday + datetime.timedelta(days=7))

        result = list(DocumentComment.objects.due_later(on=monday))

        self.assertEqual(result, [later])

    def test_due_this_week_is_empty_on_a_sunday(self):
        """Am Sonntag ist "heute" bereits der letzte Tag der Woche -- die
        Gruppe "Diese Woche" faellt dann leer aus statt mit `follow_up_date__gte`
        > `follow_up_date__lte` eine kaputte Query zu bauen.
        """
        sunday = datetime.date(2026, 8, 16)
        self._comment(sunday + datetime.timedelta(days=1))

        result = list(DocumentComment.objects.due_this_week(on=sunday))

        self.assertEqual(result, [])

    def test_follow_up_week_end_is_the_upcoming_sunday(self):
        monday = datetime.date(2026, 8, 17)
        self.assertEqual(follow_up_week_end(monday), datetime.date(2026, 8, 23))

        sunday = datetime.date(2026, 8, 16)
        self.assertEqual(follow_up_week_end(sunday), sunday)


class DocumentFollowUpViewTests(TestCase):
    """Covers `view=wiedervorlagen` (#1129): the dokumentübergreifende

    Terminliste that lists individual `DocumentComment` follow-up entries
    instead of documents, grouped by Fälligkeit.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.acme = Correspondent.objects.create(name="Acme GmbH")

        self.doc_a = Document.objects.create(
            title="Rechnung Acme",
            correspondent=self.acme,
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.doc_a.departments.add(self.dept_a)

        self.doc_b = Document.objects.create(
            title="Vertrag Fremd", visibility=Document.Visibility.DEPARTMENT
        )
        self.doc_b.departments.add(self.dept_b)

    def _get(self, **params):
        self.client.force_login(self.user_a)
        params.setdefault("view", "wiedervorlagen")
        return self.client.get(reverse("documents:home"), params)

    def test_view_switcher_offers_wiedervorlagen(self):
        response = self._get()

        self.assertContains(response, 'data-view-value="wiedervorlagen"')
        self.assertContains(response, "Wiedervorlagen")

    def test_follow_up_dropdown_is_hidden_in_this_view(self):
        response = self._get()

        self.assertContains(response, 'id="filter-follow-up-field" hidden')

    def test_empty_state_without_filters(self):
        response = self._get()

        self.assertContains(response, "Keine offenen Wiedervorlagen.")

    def test_empty_state_with_active_filter(self):
        response = self._get(correspondent=self.acme.pk)

        self.assertContains(response, "Keine Wiedervorlagen.")

    def test_entry_shows_date_comment_title_correspondent_and_links(self):
        today = timezone.localdate()
        DocumentComment.objects.create(
            document=self.doc_a, body="Skonto bis 20.08. prüfen", follow_up_date=today
        )

        response = self._get()

        self.assertContains(response, "Skonto bis 20.08. prüfen")
        self.assertContains(response, "Rechnung Acme")
        self.assertContains(response, "Acme GmbH")
        self.assertContains(response, reverse("documents:detail", args=[self.doc_a.pk]))
        self.assertContains(
            response, reverse("documents:correspondent_detail", args=[self.acme.pk])
        )

    def test_entry_includes_action_status_toggle_for_its_document(self):
        # #1137: die Erledigung ist auch in der Wiedervorlagen-Ansicht mit
        # einem Klick umschaltbar -- ohne Umweg ins Dokument-Detail.
        today = timezone.localdate()
        DocumentComment.objects.create(
            document=self.doc_a, body="Skonto bis 20.08. prüfen", follow_up_date=today
        )

        response = self._get()

        self.assertContains(
            response, reverse("documents:action_status_toggle", args=[self.doc_a.pk])
        )
        self.assertContains(response, "findus-action-status-toggle")

    def test_document_with_two_follow_ups_appears_twice(self):
        today = timezone.localdate()
        DocumentComment.objects.create(
            document=self.doc_a, body="Erster Termin", follow_up_date=today
        )
        DocumentComment.objects.create(
            document=self.doc_a, body="Zweiter Termin", follow_up_date=today
        )

        response = self._get()

        self.assertContains(response, "Erster Termin")
        self.assertContains(response, "Zweiter Termin")
        # Zwei Eintraege desselben Dokuments -> zwei Titel-Links (die
        # Vorschaubild-`aria-label`s tragen den Titel zusaetzlich, ein
        # roher Substring-Count auf "Rechnung Acme" waere daher zu hoch).
        self.assertContains(response, 'class="findus-followup-item-title"', count=2)

    def test_grouping_headers_appear_in_ascending_order(self):
        today = timezone.localdate()
        week_end = follow_up_week_end(today)
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Ueberfaellig",
            follow_up_date=today - datetime.timedelta(days=1),
        )
        DocumentComment.objects.create(document=self.doc_a, body="Heute", follow_up_date=today)
        if week_end > today:
            DocumentComment.objects.create(
                document=self.doc_a, body="Diese Woche", follow_up_date=week_end
            )
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Spaeter",
            follow_up_date=week_end + datetime.timedelta(days=1),
        )

        response = self._get()
        content = response.content.decode()

        # Über die Gruppen-Wrapper-Klassen suchen, nicht die reinen Label-
        # Texte -- der Nav-Badge-Titel enthaelt "Überfällige" als Substring
        # und rendert vor dem Content, was `.index("Überfällig")` sonst
        # immer auf die Nav statt die Wiedervorlagen-Gruppe treffen liesse.
        overdue_pos = content.index("findus-followup-group--overdue")
        today_pos = content.index("findus-followup-group--today")
        later_pos = content.index("findus-followup-group--later")
        self.assertLess(overdue_pos, today_pos)
        self.assertLess(today_pos, later_pos)
        if week_end > today:
            this_week_pos = content.index("findus-followup-group--this_week")
            self.assertLess(today_pos, this_week_pos)
            self.assertLess(this_week_pos, later_pos)

    def test_erledigt_document_is_excluded_even_when_overdue(self):
        self.doc_a.action_status = Document.ActionStatus.DONE
        self.doc_a.save(update_fields=["action_status"])
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Laengst faellig",
            follow_up_date=timezone.localdate() - datetime.timedelta(days=30),
        )

        response = self._get()

        self.assertNotContains(response, "Laengst faellig")

    def test_offen_and_keine_action_status_documents_are_both_included(self):
        self.doc_a.action_status = Document.ActionStatus.OPEN
        self.doc_a.save(update_fields=["action_status"])
        today = timezone.localdate()
        DocumentComment.objects.create(document=self.doc_a, body="Offen faellig", follow_up_date=today)

        other = Document.objects.create(
            title="Ohne Handlungsbedarf",
            visibility=Document.Visibility.DEPARTMENT,
            action_status=Document.ActionStatus.NONE,
        )
        other.departments.add(self.dept_a)
        DocumentComment.objects.create(document=other, body="Keine faellig", follow_up_date=today)

        response = self._get()

        self.assertContains(response, "Offen faellig")
        self.assertContains(response, "Keine faellig")

    def test_visibility_scoping_hides_foreign_department_entries(self):
        DocumentComment.objects.create(
            document=self.doc_b, body="Fremder Termin", follow_up_date=timezone.localdate()
        )

        response = self._get()

        self.assertNotContains(response, "Fremder Termin")

    def test_correspondent_filter_narrows_entries(self):
        other = Correspondent.objects.create(name="Andere GmbH")
        other_doc = Document.objects.create(
            title="Anderes Dokument", correspondent=other, visibility=Document.Visibility.DEPARTMENT
        )
        other_doc.departments.add(self.dept_a)
        today = timezone.localdate()
        DocumentComment.objects.create(document=self.doc_a, body="Acme Termin", follow_up_date=today)
        DocumentComment.objects.create(document=other_doc, body="Anderer Termin", follow_up_date=today)

        response = self._get(correspondent=self.acme.pk)

        self.assertContains(response, "Acme Termin")
        self.assertNotContains(response, "Anderer Termin")

    def test_reminder_indicator_reflects_remind_and_reminded_at(self):
        today = timezone.localdate()
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Erinnerung geplant Kommentar",
            follow_up_date=today,
            remind=True,
        )
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Bereits erinnert Kommentar",
            follow_up_date=today,
            remind=True,
            reminded_at=timezone.now(),
        )
        DocumentComment.objects.create(
            document=self.doc_a,
            body="Ohne Erinnerung Kommentar",
            follow_up_date=today,
            remind=False,
        )

        response = self._get()

        self.assertContains(response, "Erinnerung geplant")
        self.assertContains(response, "Erinnert")

    def test_semantic_search_overrides_wiedervorlagen_view(self):
        DocumentComment.objects.create(
            document=self.doc_a, body="Suchbegriff-Termin", follow_up_date=timezone.localdate()
        )

        with patch(
            "apps.documents.retrieval.get_embedding_provider",
            return_value=_StubEmbeddingProvider(_one_hot(0)),
        ):
            response = self._get(q="Rechnung")

        self.assertTemplateUsed(response, "documents/partials/_search_results.html")
        self.assertTemplateNotUsed(response, "documents/partials/_document_followups.html")

    def test_pagination_matches_other_views(self):
        today = timezone.localdate()
        for i in range(21):
            DocumentComment.objects.create(
                document=self.doc_a,
                body=f"Termin {i}",
                follow_up_date=today + datetime.timedelta(days=i),
            )

        response = self._get()

        self.assertContains(response, "Seite 1 von 2")

    def test_no_n_plus_one_query_for_growing_entry_count(self):
        counter = iter(range(1000))

        def _populate(count):
            for _ in range(count):
                n = next(counter)
                correspondent = Correspondent.objects.create(name=f"Kontakt {n}")
                document = Document.objects.create(
                    title=f"Dokument {n}",
                    correspondent=correspondent,
                    visibility=Document.Visibility.DEPARTMENT,
                )
                document.departments.add(self.dept_a)
                DocumentComment.objects.create(
                    document=document, body=f"Termin {n}", follow_up_date=timezone.localdate()
                )

        url = reverse("documents:home")
        self.client.force_login(self.user_a)

        _populate(1)
        with CaptureQueriesContext(connection) as few:
            self.client.get(url, {"view": "wiedervorlagen"})

        _populate(4)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url, {"view": "wiedervorlagen"})

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


class NavDueFollowUpBadgeTests(TestCase):
    """Covers the nav badge next to "Dokumente" (#1129): überfällige + heute

    fällige Wiedervorlagen offener Dokumente, fed by
    `apps.documents.context_processors.due_follow_up_count` -- present on
    every page, not just Home.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

    def _document(self, *, action_status=Document.ActionStatus.NONE):
        document = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            action_status=action_status,
        )
        document.departments.add(self.dept_a)
        return document

    def test_badge_counts_overdue_and_today_only(self):
        today = timezone.localdate()
        document = self._document()
        DocumentComment.objects.create(
            document=document, body="Ueberfaellig", follow_up_date=today - datetime.timedelta(days=1)
        )
        DocumentComment.objects.create(document=document, body="Heute", follow_up_date=today)
        DocumentComment.objects.create(
            document=document, body="Diese Woche", follow_up_date=today + datetime.timedelta(days=1)
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertContains(
            response,
            '<span class="badge text-bg-warning findus-nav-badge" '
            'title="Überfällige und heute fällige Wiedervorlagen">2</span>',
        )

    def test_badge_excludes_erledigt_documents(self):
        today = timezone.localdate()
        document = self._document(action_status=Document.ActionStatus.DONE)
        DocumentComment.objects.create(
            document=document, body="Ueberfaellig", follow_up_date=today - datetime.timedelta(days=1)
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "findus-nav-badge")

    def test_badge_is_absent_when_zero(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))

        self.assertNotContains(response, "findus-nav-badge")
