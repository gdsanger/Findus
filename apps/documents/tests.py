from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Department

from .models import Document

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
