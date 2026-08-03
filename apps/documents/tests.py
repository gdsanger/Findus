import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Department

from .models import ChecklistItem, Correspondent, Document, Task, TaskTemplate, TaskTemplateItem
from .services import (
    create_task_from_template,
    find_correspondent,
    find_or_create_correspondent,
    find_or_create_correspondent_by_email,
    match_correspondent_by_ids,
)

User = get_user_model()


class DocumentVisibleToTests(TestCase):
    """Covers the two-level visibility model (see Architektur.md,

    "Sichtbarkeitsmodell"): department members see all department
    documents, private documents are visible only to their owner.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.dept_doc = Document.objects.create(
            title="Dept Doc", visibility=Document.Visibility.DEPARTMENT
        )
        self.dept_doc.departments.add(self.dept_a)

        self.private_doc = Document.objects.create(
            title="Private Doc",
            visibility=Document.Visibility.PRIVATE,
            owner=self.user_a,
        )

    def test_department_member_sees_department_doc(self):
        self.assertIn(self.dept_doc, Document.objects.visible_to(self.user_a))

    def test_other_department_does_not_see_department_doc(self):
        self.assertNotIn(self.dept_doc, Document.objects.visible_to(self.user_b))

    def test_owner_sees_private_doc(self):
        self.assertIn(self.private_doc, Document.objects.visible_to(self.user_a))

    def test_non_owner_does_not_see_private_doc(self):
        self.assertNotIn(self.private_doc, Document.objects.visible_to(self.user_b))


class TaskVisibleToTests(TestCase):
    """Covers `Task.visible_to`, which mirrors `Document.visible_to` (#1012)

    -- a task should not leak visibility beyond what its documents already
    grant.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.dept_task = Task.objects.create(
            title="Nebenkostenabrechnung prüfen", visibility=Task.Visibility.DEPARTMENT
        )
        self.dept_task.departments.add(self.dept_a)

        self.private_task = Task.objects.create(
            title="Finanzamt beantworten",
            visibility=Task.Visibility.PRIVATE,
            owner=self.user_a,
        )

    def test_department_member_sees_department_task(self):
        self.assertIn(self.dept_task, Task.objects.visible_to(self.user_a))

    def test_other_department_does_not_see_department_task(self):
        self.assertNotIn(self.dept_task, Task.objects.visible_to(self.user_b))

    def test_owner_sees_private_task(self):
        self.assertIn(self.private_task, Task.objects.visible_to(self.user_a))

    def test_non_owner_does_not_see_private_task(self):
        self.assertNotIn(self.private_task, Task.objects.visible_to(self.user_b))


class TaskTests(TestCase):
    def test_marking_done_sets_done_at(self):
        task = Task.objects.create(title="Rechnung zahlen")
        self.assertIsNone(task.done_at)

        task.status = Task.Status.DONE
        task.save()

        self.assertIsNotNone(task.done_at)

    def test_reopening_clears_done_at(self):
        task = Task.objects.create(title="Rechnung zahlen", status=Task.Status.DONE)
        self.assertIsNotNone(task.done_at)

        task.status = Task.Status.OPEN
        task.save()

        self.assertIsNone(task.done_at)

    def test_task_can_link_multiple_documents_and_vice_versa(self):
        invoice = Document.objects.create(title="Rechnung")
        reminder = Document.objects.create(title="Mahnung")
        task = Task.objects.create(title="Rechnung zahlen")

        task.documents.add(invoice, reminder)

        self.assertEqual(set(task.documents.all()), {invoice, reminder})
        self.assertIn(task, invoice.tasks.all())
        self.assertIn(task, reminder.tasks.all())

    def test_checklist_items_are_ordered(self):
        task = Task.objects.create(title="Nebenkostenabrechnung prüfen")
        ChecklistItem.objects.create(task=task, text="Belege sammeln", order=1)
        ChecklistItem.objects.create(task=task, text="Betrag prüfen", order=0)

        self.assertEqual(
            list(task.checklist_items.values_list("text", flat=True)),
            ["Betrag prüfen", "Belege sammeln"],
        )


class TaskTemplateVisibleToTests(TestCase):
    """Covers `TaskTemplate.visible_to`, which mirrors `Task.visible_to`

    (#1037) -- a template shouldn't be discoverable by anyone who couldn't
    also see the task it would create.
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

        self.private_template = TaskTemplate.objects.create(
            name="Persönliche Steuererklärung",
            visibility=TaskTemplate.Visibility.PRIVATE,
            owner=self.user_a,
        )

    def test_department_member_sees_department_template(self):
        self.assertIn(self.dept_template, TaskTemplate.objects.visible_to(self.user_a))

    def test_other_department_does_not_see_department_template(self):
        self.assertNotIn(self.dept_template, TaskTemplate.objects.visible_to(self.user_b))

    def test_owner_sees_private_template(self):
        self.assertIn(self.private_template, TaskTemplate.objects.visible_to(self.user_a))

    def test_non_owner_does_not_see_private_template(self):
        self.assertNotIn(self.private_template, TaskTemplate.objects.visible_to(self.user_b))


class TaskTemplateItemTests(TestCase):
    def test_items_are_ordered(self):
        template = TaskTemplate.objects.create(name="Umsatzsteuer-Voranmeldung")
        TaskTemplateItem.objects.create(template=template, text="Belege sammeln", order=1)
        TaskTemplateItem.objects.create(template=template, text="Formular ausfüllen", order=0)

        self.assertEqual(
            list(template.items.values_list("text", flat=True)),
            ["Formular ausfüllen", "Belege sammeln"],
        )


class CreateTaskFromTemplateTests(TestCase):
    """Covers `create_task_from_template` (#1037): the sole way a

    `TaskTemplate` turns into a real `Task` -- deliberately manual, no
    auto-scheduling.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.template = TaskTemplate.objects.create(
            name="Umsatzsteuer-Voranmeldung",
            default_kind=Task.Kind.SUBMIT,
            default_description="Monatliche UStVA einreichen",
            default_due_offset_days=10,
        )
        TaskTemplateItem.objects.create(template=self.template, text="Belege sammeln", order=0)
        TaskTemplateItem.objects.create(template=self.template, text="Formular ausfüllen", order=1)

    def test_copies_defaults_onto_the_task(self):
        task = create_task_from_template(self.template, self.user)

        self.assertEqual(task.title, self.template.name)
        self.assertEqual(task.kind, Task.Kind.SUBMIT)
        self.assertEqual(task.description, "Monatliche UStVA einreichen")
        self.assertEqual(task.owner, self.user)

    def test_due_date_is_today_plus_offset(self):
        task = create_task_from_template(self.template, self.user)

        self.assertEqual(
            task.due_date, timezone.localdate() + datetime.timedelta(days=10)
        )

    def test_no_offset_means_no_due_date(self):
        self.template.default_due_offset_days = None
        self.template.save()

        task = create_task_from_template(self.template, self.user)

        self.assertIsNone(task.due_date)

    def test_copies_checklist_items_in_order(self):
        task = create_task_from_template(self.template, self.user)

        self.assertEqual(
            list(task.checklist_items.values_list("text", flat=True)),
            ["Belege sammeln", "Formular ausfüllen"],
        )

    def test_title_override_wins_over_template_defaults(self):
        task = create_task_from_template(
            self.template, self.user, overrides={"title": "UStVA Juli"}
        )

        self.assertEqual(task.title, "UStVA Juli")

    def test_default_title_wins_over_name_when_set(self):
        self.template.default_title = "UStVA fällig"
        self.template.save()

        task = create_task_from_template(self.template, self.user)

        self.assertEqual(task.title, "UStVA fällig")

    def test_changing_the_template_afterwards_does_not_touch_existing_tasks(self):
        task = create_task_from_template(self.template, self.user)

        self.template.default_description = "Geändert"
        self.template.items.create(text="Zusätzlicher Schritt", order=2)
        self.template.save()

        task.refresh_from_db()
        self.assertEqual(task.description, "Monatliche UStVA einreichen")
        self.assertEqual(task.checklist_items.count(), 2)

    def test_visibility_follows_the_creating_user_departments(self):
        task = create_task_from_template(self.template, self.user)

        self.assertEqual(task.visibility, Task.Visibility.DEPARTMENT)
        self.assertIn(self.dept, task.departments.all())

    def test_visibility_is_private_for_a_user_without_departments(self):
        lone_user = User.objects.create_user(username="carla", password="x")

        task = create_task_from_template(self.template, lone_user)

        self.assertEqual(task.visibility, Task.Visibility.PRIVATE)


class FindOrCreateCorrespondentByEmailTests(TestCase):
    def test_creates_new_correspondent_with_display_name(self):
        correspondent = find_or_create_correspondent_by_email(
            "Anna@Example.com", "Anna Beispiel"
        )

        self.assertEqual(correspondent.email, "anna@example.com")
        self.assertEqual(correspondent.name, "Anna Beispiel")

    def test_falls_back_to_email_when_no_display_name(self):
        correspondent = find_or_create_correspondent_by_email("anna@example.com", "")
        self.assertEqual(correspondent.name, "anna@example.com")

    def test_matches_existing_correspondent_case_insensitively(self):
        existing = Correspondent.objects.create(name="Anna Beispiel", email="anna@example.com")

        correspondent = find_or_create_correspondent_by_email("ANNA@EXAMPLE.COM", "Ignored Name")

        self.assertEqual(correspondent.id, existing.id)
        self.assertEqual(Correspondent.objects.count(), 1)

    def test_name_collision_with_unrelated_correspondent_is_disambiguated(self):
        Correspondent.objects.create(name="Buchhaltung", email="buchhaltung@firma-a.de")

        correspondent = find_or_create_correspondent_by_email(
            "buchhaltung@firma-b.de", "Buchhaltung"
        )

        self.assertNotEqual(correspondent.name, "Buchhaltung")
        self.assertEqual(correspondent.email, "buchhaltung@firma-b.de")
        self.assertEqual(Correspondent.objects.count(), 2)

    def test_blank_email_returns_none(self):
        self.assertIsNone(find_or_create_correspondent_by_email("", "Someone"))


class MatchCorrespondentByIdsTests(TestCase):
    """USt-IdNr/IBAN/Steuernummer-Matching (#1030) -- the identity keys

    that survive OCR name variance and are what prevents the KI-Analyse
    from ever duplicating an `is_self` correspondent.
    """

    def test_matches_by_vat_id_case_insensitively(self):
        existing = Correspondent.objects.create(name="Perculasoft e.K.", vat_id="DE123456789")

        match = match_correspondent_by_ids(vat_id="de123456789")

        self.assertEqual(match, existing)

    def test_vat_id_takes_precedence_over_iban(self):
        by_vat = Correspondent.objects.create(name="A", vat_id="DE111111111", iban="DE0")
        Correspondent.objects.create(name="B", iban="DE9999")

        match = match_correspondent_by_ids(vat_id="DE111111111", iban="DE9999")

        self.assertEqual(match, by_vat)

    def test_matches_by_iban_ignoring_spaces_and_case(self):
        existing = Correspondent.objects.create(name="Christian Angermeier", iban="DE02120300000000202051")

        match = match_correspondent_by_ids(iban="de02 1203 0000 0000 2020 51")

        self.assertEqual(match, existing)

    def test_no_ids_given_returns_none(self):
        Correspondent.objects.create(name="Irrelevant", vat_id="DE1")

        self.assertIsNone(match_correspondent_by_ids())


class FindCorrespondentTests(TestCase):
    """Read-only lookup (#1030) used for the KI-Analyse's recipient side --

    must never create a `Correspondent` even on a full miss.
    """

    def test_returns_existing_match_by_name(self):
        existing = Correspondent.objects.create(name="Christian Angermeier", is_self=True)

        match = find_correspondent(name="Christian Angermeier")

        self.assertEqual(match, existing)

    def test_unmatched_recipient_returns_none_and_creates_nothing(self):
        match = find_correspondent(name="Unbekannte Person")

        self.assertIsNone(match)
        self.assertEqual(Correspondent.objects.count(), 0)


class FindOrCreateCorrespondentIdsTests(TestCase):
    def test_prefers_vat_id_match_over_creating_a_new_correspondent(self):
        existing = Correspondent.objects.create(
            name="Perculasoft eK", is_self=True, vat_id="DE123456789"
        )

        # Slightly different spelling than the stored name -- exactly the
        # OCR-variance case the dedup rule guards against.
        match = find_or_create_correspondent(name="Perculasoft e.K.", vat_id="DE123456789")

        self.assertEqual(match, existing)
        self.assertEqual(Correspondent.objects.count(), 1)
