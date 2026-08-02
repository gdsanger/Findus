from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Department

from .models import Correspondent, Document
from .services import find_or_create_correspondent_by_email

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
