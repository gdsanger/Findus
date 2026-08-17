"""Prüfschritt für neu eingegangene Dokumente (#1153): der neue

Verarbeitungsstatus `reviewed`, die Sicht "Zu prüfen" und ihre
Bedienelemente (Listen-Umschalter, Detail-"Geprüft"-Knopf mit Sprung zum
nächsten Dokument, "Speichern und als geprüft markieren" im
Zuordnungsformular).
"""

import datetime

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import Department

from .models import Correspondent, Document, Tag, TagSuggestion, Vorgang, VorgangSuggestion

User = get_user_model()


class DocumentProcessingStatusToggleViewTests(TestCase):
    """Ein-Klick-Umschalter der Übersichten (#1153) -- analog

    `DocumentActionStatusToggleViewTests` (#1137): der neue Zustand kommt
    ausschließlich aus dem gespeicherten Wert, nie aus dem Request.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.doc.departments.add(self.dept)
        self.client.force_login(self.user)

    def test_toggle_marks_ready_document_reviewed(self):
        response = self.client.post(
            reverse("documents:processing_status_toggle", args=[self.doc.id])
        )

        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.REVIEWED)
        self.assertIsNotNone(self.doc.processing_reviewed_at)
        self.assertEqual(self.doc.processing_reviewed_by, self.user)
        self.assertContains(response, 'aria-pressed="true"')

    def test_toggle_falls_back_to_ready_from_reviewed_and_clears_provenance(self):
        self.doc.processing_status = Document.ProcessingStatus.REVIEWED
        self.doc.processing_reviewed_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.doc.processing_reviewed_by = self.user
        self.doc.save(update_fields=["processing_status", "processing_reviewed_at", "processing_reviewed_by"])

        response = self.client.post(
            reverse("documents:processing_status_toggle", args=[self.doc.id])
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.READY)
        self.assertIsNone(self.doc.processing_reviewed_at)
        self.assertIsNone(self.doc.processing_reviewed_by)
        self.assertContains(response, 'aria-pressed="false"')

    def test_toggle_ignores_posted_target_state(self):
        # Zwei schnelle Klicks duerfen den Zustand nicht unkontrolliert
        # kippen lassen -- der neue Wert kommt ausschliesslich aus der DB.
        self.client.post(
            reverse("documents:processing_status_toggle", args=[self.doc.id]),
            {"processing_status": Document.ProcessingStatus.PENDING},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.REVIEWED)

    def test_toggle_is_a_noop_for_a_document_still_in_progress(self):
        self.doc.processing_status = Document.ProcessingStatus.ANALYZING
        self.doc.save(update_fields=["processing_status"])

        self.client.post(reverse("documents:processing_status_toggle", args=[self.doc.id]))

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.ANALYZING)

    def test_get_is_not_allowed(self):
        response = self.client.get(
            reverse("documents:processing_status_toggle", args=[self.doc.id])
        )
        self.assertEqual(response.status_code, 405)


class DocumentReviewToggleViewTests(TestCase):
    """Der "Geprüft"-Knopf der Detailseite (#1153): setzt den Status und

    springt zum nächsten zu prüfenden Dokument (dieselbe Auswahl-/
    Sortierlogik wie die Liste, älteste zuerst).
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

    def _make(self, title, *, status=Document.ProcessingStatus.READY, days_ago=0):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT, processing_status=status
        )
        document.departments.add(self.dept)
        if days_ago:
            Document.objects.filter(pk=document.pk).update(
                created_at=document.created_at - datetime.timedelta(days=days_ago)
            )
            document.refresh_from_db()
        return document

    def test_marking_reviewed_jumps_to_the_oldest_remaining_document(self):
        older = self._make("Älteres Dokument", days_ago=5)
        current = self._make("Aktuelles Dokument")
        self._make("Neueres Dokument")

        response = self.client.post(reverse("documents:review_toggle", args=[current.id]))

        current.refresh_from_db()
        self.assertEqual(current.processing_status, Document.ProcessingStatus.REVIEWED)
        self.assertRedirects(response, reverse("documents:detail", args=[older.id]))

    def test_marking_reviewed_with_nothing_left_falls_back_to_the_queue(self):
        only = self._make("Einziges Dokument")

        response = self.client.post(reverse("documents:review_toggle", args=[only.id]))

        only.refresh_from_db()
        self.assertEqual(only.processing_status, Document.ProcessingStatus.REVIEWED)
        self.assertRedirects(response, reverse("documents:home") + "?status=ready")

    def test_review_can_be_taken_back(self):
        document = self._make("Dokument", status=Document.ProcessingStatus.REVIEWED)
        document.processing_reviewed_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        document.processing_reviewed_by = self.user
        document.save(update_fields=["processing_reviewed_at", "processing_reviewed_by"])

        response = self.client.post(reverse("documents:review_toggle", args=[document.id]))

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)
        self.assertIsNone(document.processing_reviewed_at)
        self.assertIsNone(document.processing_reviewed_by)
        self.assertRedirects(response, reverse("documents:detail", args=[document.id]))

    def test_a_document_still_in_progress_is_left_untouched(self):
        document = self._make("Wird noch verarbeitet", status=Document.ProcessingStatus.ANALYZING)

        response = self.client.post(reverse("documents:review_toggle", args=[document.id]))

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.ANALYZING)
        self.assertRedirects(response, reverse("documents:detail", args=[document.id]))

    def test_get_is_not_allowed(self):
        document = self._make("Dokument")
        response = self.client.get(reverse("documents:review_toggle", args=[document.id]))
        self.assertEqual(response.status_code, 405)


class DocumentMetaReviewViewTests(TestCase):
    """"Speichern und als geprüft markieren" im Zuordnungsformular (#1153):

    ein Full-Form-Submit, der alle Felder speichert und danach den
    Prüfstatus setzt -- der übliche Fall ist "Vorgang und Tags vergeben,
    dann bestätigen".
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        self.correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.vorgang = Vorgang.objects.create(name="Vorgang A")
        self.tag = Tag.objects.create(name="Wichtig")

        self.doc = Document.objects.create(
            title="Rechnung",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.doc.departments.add(self.dept)

    def test_saves_all_fields_and_marks_reviewed(self):
        response = self.client.post(
            reverse("documents:meta_review", args=[self.doc.id]),
            {
                "correspondent": str(self.correspondent.id),
                "direction": Document.Direction.EINGANG,
                "sphere": Document.Sphere.GESCHAEFTLICH,
                "document_date": "2026-01-15",
                "vorgaenge": [str(self.vorgang.id)],
                "tags": [str(self.tag.id)],
            },
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.correspondent, self.correspondent)
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)
        self.assertEqual(self.doc.sphere, Document.Sphere.GESCHAEFTLICH)
        self.assertEqual(self.doc.document_date, datetime.date(2026, 1, 15))
        self.assertIn(self.vorgang, self.doc.vorgaenge.all())
        self.assertIn(self.tag, self.doc.tags.all())
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.REVIEWED)
        self.assertIsNotNone(self.doc.processing_reviewed_at)
        self.assertEqual(self.doc.processing_reviewed_by, self.user)
        self.assertEqual(response.status_code, 302)

    def test_an_emptied_multiselect_clears_the_assignment(self):
        self.doc.vorgaenge.add(self.vorgang)
        self.doc.tags.add(self.tag)

        self.client.post(
            reverse("documents:meta_review", args=[self.doc.id]),
            {"direction": Document.Direction.EINGANG},
        )

        self.doc.refresh_from_db()
        self.assertEqual(list(self.doc.vorgaenge.all()), [])
        self.assertEqual(list(self.doc.tags.all()), [])

    def test_does_not_mark_reviewed_when_processing_is_not_yet_ready(self):
        self.doc.processing_status = Document.ProcessingStatus.ANALYZING
        self.doc.save(update_fields=["processing_status"])

        self.client.post(
            reverse("documents:meta_review", args=[self.doc.id]),
            {"direction": Document.Direction.EINGANG},
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.direction, Document.Direction.EINGANG)
        self.assertEqual(self.doc.processing_status, Document.ProcessingStatus.ANALYZING)

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("documents:meta_review", args=[self.doc.id]))
        self.assertEqual(response.status_code, 405)


class NeedsReviewQuerySetTests(TestCase):
    """`DocumentQuerySet.needs_review()` (#1153): nur `ready`, älteste

    zuerst -- die gemeinsame Basis für Nav-Badge, Dashboard-Block und den
    Sprung zum nächsten Dokument.
    """

    def test_filters_to_ready_and_sorts_oldest_first(self):
        older = Document.objects.create(
            title="Älter", processing_status=Document.ProcessingStatus.READY
        )
        Document.objects.filter(pk=older.pk).update(
            created_at=older.created_at - datetime.timedelta(days=3)
        )
        newer = Document.objects.create(
            title="Neuer", processing_status=Document.ProcessingStatus.READY
        )
        Document.objects.create(
            title="Geprüft", processing_status=Document.ProcessingStatus.REVIEWED
        )
        Document.objects.create(
            title="Fehlgeschlagen", processing_status=Document.ProcessingStatus.FAILED
        )

        result = list(Document.objects.needs_review())

        self.assertEqual(result, [older, newer])


class FilteredDocumentsReviewQueueSortTests(TestCase):
    """Sicht "Zu prüfen" (#1153): die gefilterte Liste sortiert ausgerechnet

    hier nach Eingang aufsteigend statt wie sonst nach Dokumentdatum
    absteigend -- was am längsten liegt, ist am ehesten vergessen.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        self.older = Document.objects.create(
            title="Älteres Dokument",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.older.departments.add(self.dept)
        Document.objects.filter(pk=self.older.pk).update(
            created_at=self.older.created_at - datetime.timedelta(days=3)
        )

        self.newer = Document.objects.create(
            title="Neueres Dokument",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.newer.departments.add(self.dept)

    def test_ready_filter_sorts_ascending_by_created_at(self):
        response = self.client.get(reverse("documents:home"), {"status": "ready", "view": ""})

        titles = [d.title for d in response.context["page_obj"]]
        self.assertEqual(titles, ["Älteres Dokument", "Neueres Dokument"])

    def test_other_filters_keep_the_usual_descending_sort(self):
        response = self.client.get(reverse("documents:home"), {"view": ""})

        titles = [d.title for d in response.context["page_obj"]]
        self.assertEqual(titles, ["Neueres Dokument", "Älteres Dokument"])


class NeedsReviewNavBadgeTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

    def test_badge_counts_ready_documents_and_hides_at_zero(self):
        response = self.client.get(reverse("documents:home"))
        self.assertNotContains(response, "Fertig verarbeitete, noch nicht bestätigte Dokumente")

        doc = Document.objects.create(
            title="Rechnung",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        doc.departments.add(self.dept)

        response = self.client.get(reverse("documents:home"))
        self.assertEqual(response.context["needs_review_count"], 1)
        self.assertContains(
            response, 'title="Fertig verarbeitete, noch nicht bestätigte Dokumente">1</span>'
        )

    def test_badge_ignores_documents_outside_visibility(self):
        other_dept = Department.objects.create(name="Fremd")
        foreign = Document.objects.create(
            title="Fremd", visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        foreign.departments.add(other_dept)

        response = self.client.get(reverse("documents:home"))
        self.assertEqual(response.context["needs_review_count"], 0)


class OpenSuggestionCountTests(TestCase):
    """Offene KI-Vorschläge je Eintrag in der Sicht "Zu prüfen" (#1153) --

    die eigentlichen Prüfkandidaten sind Dokumente mit noch nicht
    angenommenen/verworfenen Tag-/Vorgangsvorschlägen. Nur angezeigt, wenn
    nach `status=ready` gefiltert wird -- die Zahl sagt sonst nichts über
    den aktuellen Filterkontext.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        self.doc = Document.objects.create(
            title="Rechnung Acme",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
        )
        self.doc.departments.add(self.dept)

    def test_counts_pending_tag_and_vorgang_suggestions_together(self):
        TagSuggestion.objects.create(document=self.doc, name="Wichtig")
        TagSuggestion.objects.create(document=self.doc, name="Dringend")
        VorgangSuggestion.objects.create(document=self.doc, name="Vorgang A")
        # Ein bereits angenommener/verworfener Vorschlag zaehlt nicht mit.
        TagSuggestion.objects.create(
            document=self.doc, name="Erledigt", status="accepted"
        )

        response = self.client.get(reverse("documents:home"), {"status": "ready", "view": ""})

        document = response.context["page_obj"][0]
        self.assertEqual(document.open_suggestion_count, 3)
        self.assertContains(response, "3 offene Vorschläge")

    def test_count_is_not_annotated_outside_the_ready_filter(self):
        TagSuggestion.objects.create(document=self.doc, name="Wichtig")

        response = self.client.get(reverse("documents:home"), {"view": ""})

        self.assertNotContains(response, "offene Vorschläge")

    def test_no_n_plus_one_query_for_growing_suggestion_count(self):
        def _make_document_with_suggestion(n):
            document = Document.objects.create(
                title=f"Dokument {n}",
                visibility=Document.Visibility.DEPARTMENT,
                processing_status=Document.ProcessingStatus.READY,
            )
            document.departments.add(self.dept)
            TagSuggestion.objects.create(document=document, name=f"Tag {n}")
            VorgangSuggestion.objects.create(document=document, name=f"Vorgang {n}")

        _make_document_with_suggestion(1)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("documents:home"), {"status": "ready", "view": ""})

        for n in range(2, 5):
            _make_document_with_suggestion(n)
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("documents:home"), {"status": "ready", "view": ""})

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))
