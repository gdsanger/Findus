from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

from .models import Task, TaskTemplate, TaskTemplateItem

User = get_user_model()


class TaskTemplateListViewTests(TestCase):
    """Covers the Vorlagen-Verwaltung list (#1038): visibility scoping

    matches `TaskTemplate.visible_to`, same principle as `task_list`.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.dept_template = TaskTemplate.objects.create(
            name="Monatsabschluss", visibility=TaskTemplate.Visibility.DEPARTMENT
        )
        self.dept_template.departments.add(self.dept_a)

        self.other_dept_template = TaskTemplate.objects.create(
            name="Fremde Abteilung", visibility=TaskTemplate.Visibility.DEPARTMENT
        )
        self.other_dept_template.departments.add(self.dept_b)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:task_template_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_visible_templates_are_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:task_template_list"))

        self.assertContains(response, "Monatsabschluss")
        self.assertNotContains(response, "Fremde Abteilung")


class TaskTemplateCreateViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

    def test_creates_template_scoped_to_owner_departments(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_template_create"),
            {
                "name": "Umsatzsteuer-Voranmeldung",
                "default_kind": Task.Kind.SUBMIT,
                "default_title": "USt-VA einreichen",
                "default_description": "",
                "default_due_offset_days": 10,
            },
        )

        template = TaskTemplate.objects.get(name="Umsatzsteuer-Voranmeldung")
        self.assertRedirects(response, reverse("documents:task_template_detail", args=[template.pk]))
        self.assertEqual(template.owner, self.user_a)
        self.assertIn(self.dept_a, template.departments.all())
        self.assertEqual(template.visibility, TaskTemplate.Visibility.DEPARTMENT)

    def test_department_less_user_creates_private_template(self):
        user = User.objects.create_user(username="carol", password="x")
        self.client.force_login(user)
        self.client.post(
            reverse("documents:task_template_create"),
            {"name": "Privat", "default_kind": "", "default_title": "", "default_description": ""},
        )

        template = TaskTemplate.objects.get(name="Privat")
        self.assertEqual(template.visibility, TaskTemplate.Visibility.PRIVATE)
        self.assertEqual(template.owner, user)


class TaskTemplateDetailViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.template = TaskTemplate.objects.create(name="Monatsabschluss")
        self.template.departments.add(self.dept_a)

    def test_template_outside_visibility_returns_404(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:task_template_detail", args=[self.template.pk]))
        self.assertEqual(response.status_code, 404)

    def test_edit_updates_fields(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_template_detail", args=[self.template.pk]),
            {
                "name": "Monatsabschluss (angepasst)",
                "default_kind": Task.Kind.REVIEW,
                "default_title": "Abschluss prüfen",
                "default_description": "Belege prüfen",
                "default_due_offset_days": 5,
            },
        )

        self.assertRedirects(
            response, reverse("documents:task_template_detail", args=[self.template.pk])
        )
        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Monatsabschluss (angepasst)")
        self.assertEqual(self.template.default_due_offset_days, 5)

    def test_delete_removes_template(self):
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("documents:task_template_delete", args=[self.template.pk]))

        self.assertRedirects(response, reverse("documents:task_template_list"))
        self.assertFalse(TaskTemplate.objects.filter(pk=self.template.pk).exists())

    def test_delete_on_invisible_template_is_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(reverse("documents:task_template_delete", args=[self.template.pk]))
        self.assertEqual(response.status_code, 404)


class TaskTemplateItemViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.template = TaskTemplate.objects.create(name="Monatsabschluss")
        self.template.departments.add(self.dept_a)

    def test_add_item(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_template_item_add", args=[self.template.pk]),
            {"text": "Belege sammeln"},
        )

        self.assertContains(response, "Belege sammeln")
        self.assertEqual(self.template.items.count(), 1)

    def test_update_item_renames_text(self):
        item = TaskTemplateItem.objects.create(template=self.template, text="Belege sammeln")
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_template_item_update", args=[self.template.pk, item.pk]),
            {"text": "Belege einscannen"},
        )

        item.refresh_from_db()
        self.assertEqual(item.text, "Belege einscannen")
        self.assertContains(response, "Belege einscannen")

    def test_update_item_ignores_blank_text(self):
        item = TaskTemplateItem.objects.create(template=self.template, text="Belege sammeln")
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:task_template_item_update", args=[self.template.pk, item.pk]),
            {"text": "  "},
        )

        item.refresh_from_db()
        self.assertEqual(item.text, "Belege sammeln")

    def test_delete_item(self):
        item = TaskTemplateItem.objects.create(template=self.template, text="Belege sammeln")
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:task_template_item_delete", args=[self.template.pk, item.pk])
        )

        self.assertEqual(self.template.items.count(), 0)

    def test_move_item_up_swaps_order_with_previous(self):
        first = TaskTemplateItem.objects.create(template=self.template, text="Erstens", order=1)
        second = TaskTemplateItem.objects.create(template=self.template, text="Zweitens", order=2)

        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:task_template_item_move", args=[self.template.pk, second.pk, "up"])
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.order, 1)
        self.assertEqual(first.order, 2)

    def test_move_item_invalid_direction_is_404(self):
        item = TaskTemplateItem.objects.create(template=self.template, text="Belege sammeln")
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:task_template_item_move", args=[self.template.pk, item.pk, "sideways"])
        )
        self.assertEqual(response.status_code, 404)

    def test_item_action_on_invisible_template_is_404(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:task_template_item_add", args=[self.template.pk]),
            {"text": "Sollte nicht klappen"},
        )
        self.assertEqual(response.status_code, 404)
