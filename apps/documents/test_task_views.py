import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department

from .models import ChecklistItem, Document, Task

User = get_user_model()


class TaskListViewTests(TestCase):
    """Covers the task list (#1023): nav entry lands here, visibility

    scoping matches `Task.visible_to`, and the Status/Frist filters narrow
    without ever widening it (same principle as `_filtered_documents`).
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        today = timezone.localdate()

        self.open_task = Task.objects.create(title="Rechnung zahlen", due_date=today - datetime.timedelta(days=2))
        self.open_task.departments.add(self.dept_a)

        self.done_task = Task.objects.create(title="Mahnung geprüft", status=Task.Status.DONE)
        self.done_task.departments.add(self.dept_a)

        self.no_due_task = Task.objects.create(title="Ablage sortieren")
        self.no_due_task.departments.add(self.dept_a)

        self.other_dept_task = Task.objects.create(title="Fremde Abteilung")
        self.other_dept_task.departments.add(self.dept_b)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:task_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_visible_tasks_are_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:task_list"))

        self.assertContains(response, "Rechnung zahlen")
        self.assertContains(response, "Mahnung geprüft")
        self.assertNotContains(response, "Fremde Abteilung")

    def test_status_filter_narrows_to_open(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:task_list"), {"status": "open"})

        self.assertContains(response, "Rechnung zahlen")
        self.assertNotContains(response, "Mahnung geprüft")

    def test_due_filter_overdue(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:task_list"), {"due": "overdue"})

        self.assertContains(response, "Rechnung zahlen")
        self.assertNotContains(response, "Ablage sortieren")

    def test_due_filter_none(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:task_list"), {"due": "none"})

        self.assertContains(response, "Ablage sortieren")
        self.assertNotContains(response, "Rechnung zahlen")


class TaskCreateViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.document = Document.objects.create(title="Rechnung Acme")
        self.document.departments.add(self.dept_a)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:task_create"))
        self.assertEqual(response.status_code, 302)

    def test_creates_task_scoped_to_owner_departments(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_create"),
            {
                "title": "Rechnung zahlen",
                "kind": Task.Kind.PAY,
                "status": Task.Status.OPEN,
                "due_date": "",
                "description": "",
                "documents": [str(self.document.pk)],
            },
        )

        task = Task.objects.get(title="Rechnung zahlen")
        self.assertRedirects(response, reverse("documents:task_detail", args=[task.pk]))
        self.assertEqual(task.owner, self.user_a)
        self.assertIn(self.dept_a, task.departments.all())
        self.assertEqual(task.visibility, Task.Visibility.DEPARTMENT)
        self.assertIn(self.document, task.documents.all())

    def test_department_less_user_creates_private_task(self):
        user = User.objects.create_user(username="carol", password="x")
        self.client.force_login(user)
        self.client.post(
            reverse("documents:task_create"),
            {"title": "Privat prüfen", "kind": Task.Kind.OTHER, "status": Task.Status.OPEN, "due_date": ""},
        )

        task = Task.objects.get(title="Privat prüfen")
        self.assertEqual(task.visibility, Task.Visibility.PRIVATE)
        self.assertEqual(task.owner, user)


class TaskDetailViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.task = Task.objects.create(title="Rechnung zahlen", kind=Task.Kind.PAY)
        self.task.departments.add(self.dept_a)

    def test_task_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:task_detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 404)

    def test_edit_updates_fields_and_documents(self):
        document = Document.objects.create(title="Rechnung Acme")
        document.departments.add(self.dept_a)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_detail", args=[self.task.pk]),
            {
                "title": "Rechnung zahlen (angepasst)",
                "kind": Task.Kind.PAY,
                "status": Task.Status.OPEN,
                "due_date": "2026-09-01",
                "description": "Frist beachten",
                "documents": [str(document.pk)],
            },
        )

        self.assertRedirects(response, reverse("documents:task_detail", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Rechnung zahlen (angepasst)")
        self.assertEqual(self.task.due_date.isoformat(), "2026-09-01")
        self.assertIn(document, self.task.documents.all())

    def test_setting_status_done_sets_done_at(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:task_detail", args=[self.task.pk]),
            {
                "title": self.task.title,
                "kind": self.task.kind,
                "status": Task.Status.DONE,
                "due_date": "",
                "description": "",
            },
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertIsNotNone(self.task.done_at)

    def test_toggle_status_marks_open_task_done_and_sets_done_at(self):
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("documents:task_toggle_status", args=[self.task.pk]))

        self.assertRedirects(response, reverse("documents:task_detail", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertIsNotNone(self.task.done_at)

    def test_toggle_status_reopens_done_task_and_clears_done_at(self):
        self.task.status = Task.Status.DONE
        self.task.save()

        self.client.force_login(self.user_a)
        self.client.post(reverse("documents:task_toggle_status", args=[self.task.pk]))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.OPEN)
        self.assertIsNone(self.task.done_at)

    def test_toggle_status_on_invisible_task_is_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(reverse("documents:task_toggle_status", args=[self.task.pk]))
        self.assertEqual(response.status_code, 404)


class ChecklistItemViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.task = Task.objects.create(title="Nebenkosten prüfen")
        self.task.departments.add(self.dept_a)

    def test_add_checklist_item(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:checklist_item_add", args=[self.task.pk]),
            {"text": "Belege sammeln"},
        )

        self.assertContains(response, "Belege sammeln")
        self.assertEqual(self.task.checklist_items.count(), 1)

    def test_toggle_checklist_item(self):
        item = ChecklistItem.objects.create(task=self.task, text="Belege sammeln")
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:checklist_item_toggle", args=[self.task.pk, item.pk])
        )

        item.refresh_from_db()
        self.assertTrue(item.is_done)
        self.assertContains(response, "Belege sammeln")

    def test_delete_checklist_item(self):
        item = ChecklistItem.objects.create(task=self.task, text="Belege sammeln")
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:checklist_item_delete", args=[self.task.pk, item.pk])
        )

        self.assertEqual(self.task.checklist_items.count(), 0)

    def test_update_checklist_item_renames_text(self):
        item = ChecklistItem.objects.create(task=self.task, text="Belege sammeln")
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:checklist_item_update", args=[self.task.pk, item.pk]),
            {"text": "Belege einscannen"},
        )

        item.refresh_from_db()
        self.assertEqual(item.text, "Belege einscannen")
        self.assertContains(response, "Belege einscannen")

    def test_update_checklist_item_ignores_blank_text(self):
        item = ChecklistItem.objects.create(task=self.task, text="Belege sammeln")
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:checklist_item_update", args=[self.task.pk, item.pk]),
            {"text": "  "},
        )

        item.refresh_from_db()
        self.assertEqual(item.text, "Belege sammeln")

    def test_move_checklist_item_up_swaps_order_with_previous(self):
        first = ChecklistItem.objects.create(task=self.task, text="Erstens", order=1)
        second = ChecklistItem.objects.create(task=self.task, text="Zweitens", order=2)

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:checklist_item_move", args=[self.task.pk, second.pk, "up"])
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.order, 1)
        self.assertEqual(first.order, 2)

    def test_move_checklist_item_up_at_top_is_a_no_op(self):
        first = ChecklistItem.objects.create(task=self.task, text="Erstens", order=1)
        ChecklistItem.objects.create(task=self.task, text="Zweitens", order=2)

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:checklist_item_move", args=[self.task.pk, first.pk, "up"])
        )

        first.refresh_from_db()
        self.assertEqual(first.order, 1)

    def test_move_checklist_item_invalid_direction_is_404(self):
        item = ChecklistItem.objects.create(task=self.task, text="Belege sammeln")
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:checklist_item_move", args=[self.task.pk, item.pk, "sideways"])
        )
        self.assertEqual(response.status_code, 404)

    def test_checklist_action_on_invisible_task_is_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:checklist_item_add", args=[self.task.pk]),
            {"text": "Sollte nicht klappen"},
        )
        self.assertEqual(response.status_code, 404)


class DocumentTaskCreateViewTests(TestCase):
    """Covers requirement #5 (#1023): a task can be created directly from

    the document detail page, already linked to that document.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.document = Document.objects.create(title="Rechnung Acme")
        self.document.departments.add(self.dept_a)

    def test_creates_task_linked_to_document(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_task_create", args=[self.document.pk]),
            {"title": "Rechnung zahlen", "kind": Task.Kind.PAY, "due_date": ""},
        )

        self.assertEqual(response.status_code, 200)
        task = Task.objects.get(title="Rechnung zahlen")
        self.assertIn(self.document, task.documents.all())
        self.assertEqual(task.owner, self.user_a)
        self.assertContains(response, "Rechnung zahlen")

    def test_blank_title_creates_nothing(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:document_task_create", args=[self.document.pk]),
            {"title": "", "kind": Task.Kind.PAY, "due_date": ""},
        )

        self.assertEqual(Task.objects.count(), 0)
