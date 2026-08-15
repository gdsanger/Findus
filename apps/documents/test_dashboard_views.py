import datetime

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department

from .models import Correspondent, Document, DocumentComment

User = get_user_model()


class DashboardViewTests(TestCase):
    """Covers the Dashboard cockpit (#1065): document/storage KPIs,
    Erledigung counters and the Wiedervorlagen widget (#1130, replacing the
    former Aufgaben widget) must all respect `visible_to` scoping, same as
    the underlying list views.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.own_ready = Document.objects.create(
            title="Rechnung",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
            action_status=Document.ActionStatus.OPEN,
            metadata={"size": 1_000_000},
        )
        self.own_ready.departments.add(self.dept_a)

        self.own_failed = Document.objects.create(
            title="Mahnung",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.FAILED,
            action_status=Document.ActionStatus.DONE,
            metadata={"size": 500_000},
        )
        self.own_failed.departments.add(self.dept_a)

        self.other_dept_doc = Document.objects.create(
            title="Fremde Abteilung",
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.READY,
            metadata={"size": 9_000_000},
        )
        self.other_dept_doc.departments.add(self.dept_b)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_document_and_storage_kpis_are_visibility_scoped(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertEqual(response.context["doc_stats"]["total"], 2)
        self.assertEqual(response.context["doc_stats"]["ready"], 1)
        self.assertEqual(response.context["doc_stats"]["failed"], 1)
        self.assertEqual(response.context["doc_stats"]["total_size"], 1_500_000)

    def test_action_status_breakdown_is_visibility_scoped(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertEqual(response.context["doc_stats"]["action_open"], 1)
        self.assertEqual(response.context["doc_stats"]["action_done"], 1)
        self.assertEqual(response.context["doc_stats"]["action_none"], 0)

    def test_no_task_block_left_in_dashboard_context(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertNotIn("task_stats", response.context)
        self.assertNotIn("open_tasks", response.context)
        self.assertNotContains(response, "Alle Aufgaben")

    def test_follow_up_widget_excludes_done_and_other_department_documents(self):
        acme = Correspondent.objects.create(name="Acme GmbH")
        self.own_ready.correspondent = acme
        self.own_ready.save(update_fields=["correspondent"])
        today = timezone.localdate()

        DocumentComment.objects.create(
            document=self.own_ready, body="Skonto pruefen", follow_up_date=today - datetime.timedelta(days=3)
        )
        DocumentComment.objects.create(
            document=self.own_failed, body="Erledigtes Dokument", follow_up_date=today - datetime.timedelta(days=1)
        )
        DocumentComment.objects.create(
            document=self.other_dept_doc, body="Fremde Abteilung Termin", follow_up_date=today
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, "Skonto pruefen")
        self.assertContains(response, "Rechnung")
        self.assertContains(response, "Acme GmbH")
        self.assertNotContains(response, "Erledigtes Dokument")
        self.assertNotContains(response, "Fremde Abteilung Termin")

    def test_follow_ups_are_sorted_ascending_with_overdue_first(self):
        today = timezone.localdate()
        DocumentComment.objects.create(document=self.own_ready, body="Spaeter", follow_up_date=today + datetime.timedelta(days=5))
        DocumentComment.objects.create(document=self.own_ready, body="Ueberfaellig", follow_up_date=today - datetime.timedelta(days=2))
        DocumentComment.objects.create(document=self.own_ready, body="Heute", follow_up_date=today)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        follow_ups = response.context["follow_ups"]
        self.assertEqual([entry.body for entry in follow_ups], ["Ueberfaellig", "Heute", "Spaeter"])
        self.assertTrue(follow_ups[0].is_overdue)
        self.assertContains(response, "Überfällig")

    def test_follow_up_widget_limited_to_six_entries(self):
        today = timezone.localdate()
        for i in range(9):
            DocumentComment.objects.create(
                document=self.own_ready, body=f"Termin {i}", follow_up_date=today + datetime.timedelta(days=i)
            )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertEqual(len(response.context["follow_ups"]), 6)

    def test_follow_up_widget_empty_state(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, "Keine offenen Wiedervorlagen.")

    def test_follow_up_widget_links_to_full_wiedervorlagen_view(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, reverse("documents:home") + "?view=wiedervorlagen")

    def test_no_n_plus_one_query_for_growing_follow_up_count(self):
        today = timezone.localdate()

        def _add_follow_up(n):
            correspondent = Correspondent.objects.create(name=f"Kontakt {n}")
            document = Document.objects.create(
                title=f"Dokument {n}",
                correspondent=correspondent,
                visibility=Document.Visibility.DEPARTMENT,
            )
            document.departments.add(self.dept_a)
            DocumentComment.objects.create(document=document, body=f"Termin {n}", follow_up_date=today)

        _add_follow_up(1)
        self.client.force_login(self.user_a)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("documents:dashboard"))

        for n in range(2, 5):
            _add_follow_up(n)
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("documents:dashboard"))

        self.assertEqual(len(many.captured_queries), len(few.captured_queries))

    def test_kpi_links_target_filtered_document_list(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, "?action_status=offen")
        self.assertContains(response, "?status=failed")

    def test_dashboard_reachable_from_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))
        self.assertContains(response, reverse("documents:dashboard"))
