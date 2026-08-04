import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department

from .models import Document, Task

User = get_user_model()


class DashboardViewTests(TestCase):
    """Covers the Dashboard cockpit (#1065): document/storage KPIs,
    Erledigung counters and the open-Aufgaben widget must all respect
    `visible_to` scoping, same as the underlying list views.
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

        today = timezone.localdate()
        self.overdue_task = Task.objects.create(title="Überfällige Aufgabe", due_date=today - datetime.timedelta(days=3))
        self.overdue_task.departments.add(self.dept_a)

        self.upcoming_task = Task.objects.create(title="Baldige Aufgabe", due_date=today + datetime.timedelta(days=2))
        self.upcoming_task.departments.add(self.dept_a)

        self.done_task = Task.objects.create(
            title="Erledigte Aufgabe", status=Task.Status.DONE, due_date=today - datetime.timedelta(days=1)
        )
        self.done_task.departments.add(self.dept_a)

        self.other_dept_task = Task.objects.create(title="Fremde Aufgabe", due_date=today)
        self.other_dept_task.departments.add(self.dept_b)

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

    def test_open_tasks_widget_excludes_done_and_other_department(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, "Überfällige Aufgabe")
        self.assertContains(response, "Baldige Aufgabe")
        self.assertNotContains(response, "Erledigte Aufgabe")
        self.assertNotContains(response, "Fremde Aufgabe")
        self.assertEqual(response.context["task_stats"]["open"], 2)
        self.assertEqual(response.context["task_stats"]["overdue"], 1)

    def test_open_tasks_are_sorted_soonest_first(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        open_tasks = response.context["open_tasks"]
        self.assertEqual([task.title for task in open_tasks], ["Überfällige Aufgabe", "Baldige Aufgabe"])

    def test_kpi_links_target_filtered_document_list(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:dashboard"))

        self.assertContains(response, "?action_status=offen")
        self.assertContains(response, "?status=failed")

    def test_dashboard_reachable_from_nav(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:home"))
        self.assertContains(response, reverse("documents:dashboard"))
