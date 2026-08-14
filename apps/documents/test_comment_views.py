import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Department

from .models import Document, DocumentComment

User = get_user_model()


class DocumentCommentCreateViewTests(TestCase):
    """Covers requirement #1 (#1125): a comment can be created straight from

    the document detail page, without a page reload, validated through
    `DocumentCommentForm` rather than raw POST access (same #1045 pattern
    as `DocumentTaskCreateViewTests`).
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.document = Document.objects.create(title="Rechnung Acme")
        self.document.departments.add(self.dept_a)

    def test_creates_comment_authored_by_current_user(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Rückruf beim Vermieter erledigt.", "follow_up_date": "", "remind": ""},
        )

        self.assertEqual(response.status_code, 200)
        comment = DocumentComment.objects.get(document=self.document)
        self.assertEqual(comment.body, "Rückruf beim Vermieter erledigt.")
        self.assertEqual(comment.author, self.user_a)
        self.assertContains(response, "Rückruf beim Vermieter erledigt.")

    def test_comment_without_follow_up_date_is_a_plain_note(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Nur eine Notiz.", "follow_up_date": "", "remind": ""},
        )

        comment = DocumentComment.objects.get(document=self.document)
        self.assertIsNone(comment.follow_up_date)

    def test_comment_with_follow_up_date_and_remind(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {
                "body": "Kündigungsfrist beachten.",
                "follow_up_date": "2026-09-01",
                "remind": "on",
            },
        )

        comment = DocumentComment.objects.get(document=self.document)
        self.assertEqual(comment.follow_up_date, datetime.date(2026, 9, 1))
        self.assertTrue(comment.remind)

    def test_blank_body_creates_nothing(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "", "follow_up_date": "", "remind": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentComment.objects.count(), 0)
        self.assertContains(response, "Kommentar")

    def test_unparsable_follow_up_date_shows_inline_error_instead_of_crashing(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Notiz", "follow_up_date": "not-a-date", "remind": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentComment.objects.count(), 0)
        self.assertContains(response, "quick-create-comment")

    def test_remind_without_follow_up_date_shows_inline_error(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Notiz", "follow_up_date": "", "remind": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentComment.objects.count(), 0)
        self.assertContains(response, "Erinnern setzt ein Wiedervorlage-Datum voraus.")

    def test_multiple_comments_on_same_document_coexist(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Erste Notiz", "follow_up_date": "", "remind": ""},
        )
        self.client.post(
            reverse("documents:document_comment_create", args=[self.document.pk]),
            {"body": "Zweite Notiz", "follow_up_date": "2026-10-01", "remind": ""},
        )

        self.assertEqual(self.document.comments.count(), 2)

    def test_comment_on_invisible_document_is_404(self):
        other_dept = Department.objects.create(name="Fremd")
        foreign_document = Document.objects.create(title="Fremdes Dokument")
        foreign_document.departments.add(other_dept)

        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_create", args=[foreign_document.pk]),
            {"body": "Sollte nicht ankommen", "follow_up_date": "", "remind": ""},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(DocumentComment.objects.count(), 0)


class DocumentCommentEditDeleteViewTests(TestCase):
    """Covers requirement #1 (#1125): editing/deleting is limited to the

    comment's own author, `created_at` survives an edit, and the row's
    edit/cancel/update endpoints each swap only that one entry.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_a)

        self.document = Document.objects.create(title="Rechnung Acme")
        self.document.departments.add(self.dept_a)

        self.comment = DocumentComment.objects.create(
            document=self.document, body="Ursprünglicher Text", author=self.user_a
        )
        self.original_created_at = self.comment.created_at

    def test_author_can_load_edit_form(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse(
                "documents:document_comment_edit_form", args=[self.document.pk, self.comment.pk]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ursprünglicher Text")

    def test_non_author_cannot_load_edit_form(self):
        self.client.force_login(self.user_b)
        response = self.client.get(
            reverse(
                "documents:document_comment_edit_form", args=[self.document.pk, self.comment.pk]
            )
        )

        self.assertEqual(response.status_code, 403)

    def test_author_can_update_body_and_follow_up_date(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_update", args=[self.document.pk, self.comment.pk]),
            {"body": "Korrigierter Text", "follow_up_date": "2026-09-15", "remind": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "Korrigierter Text")
        self.assertEqual(self.comment.follow_up_date, datetime.date(2026, 9, 15))
        self.assertTrue(self.comment.remind)
        self.assertContains(response, "Korrigierter Text")

    def test_update_does_not_change_created_at(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:document_comment_update", args=[self.document.pk, self.comment.pk]),
            {"body": "Korrigierter Text", "follow_up_date": "", "remind": ""},
        )

        self.comment.refresh_from_db()
        self.assertEqual(self.comment.created_at, self.original_created_at)

    def test_update_with_unparsable_date_shows_inline_error(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_update", args=[self.document.pk, self.comment.pk]),
            {"body": "Korrigierter Text", "follow_up_date": "not-a-date", "remind": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "Ursprünglicher Text")

    def test_non_author_cannot_update(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:document_comment_update", args=[self.document.pk, self.comment.pk]),
            {"body": "Fremder Eingriff", "follow_up_date": "", "remind": ""},
        )

        self.assertEqual(response.status_code, 403)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.body, "Ursprünglicher Text")

    def test_author_can_delete_own_comment(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:document_comment_delete", args=[self.document.pk, self.comment.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(DocumentComment.objects.filter(pk=self.comment.pk).exists())

    def test_non_author_cannot_delete(self):
        self.client.force_login(self.user_b)
        response = self.client.post(
            reverse("documents:document_comment_delete", args=[self.document.pk, self.comment.pk])
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(DocumentComment.objects.filter(pk=self.comment.pk).exists())

    def test_cancel_edit_renders_read_only_row(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse(
                "documents:document_comment_cancel_edit", args=[self.document.pk, self.comment.pk]
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ursprünglicher Text")
        self.assertContains(response, "Bearbeiten")

    def test_comment_actions_on_invisible_document_are_404(self):
        other_dept = Department.objects.create(name="Fremd")
        foreign_document = Document.objects.create(title="Fremdes Dokument")
        foreign_document.departments.add(other_dept)
        foreign_comment = DocumentComment.objects.create(
            document=foreign_document, body="Nicht sichtbar", author=self.user_a
        )

        self.client.force_login(self.user_a)
        for response in (
            self.client.get(
                reverse(
                    "documents:document_comment_edit_form",
                    args=[foreign_document.pk, foreign_comment.pk],
                )
            ),
            self.client.post(
                reverse(
                    "documents:document_comment_update",
                    args=[foreign_document.pk, foreign_comment.pk],
                ),
                {"body": "x", "follow_up_date": "", "remind": ""},
            ),
            self.client.post(
                reverse(
                    "documents:document_comment_delete",
                    args=[foreign_document.pk, foreign_comment.pk],
                )
            ),
        ):
            self.assertEqual(response.status_code, 404)


class DocumentCommentVisibilityAndOrderingTests(TestCase):
    """Covers requirement #4 (#1125): comments inherit the document's

    visibility (no own ACL) and the chronicle sorts newest-first.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.document = Document.objects.create(title="Rechnung Acme")
        self.document.departments.add(self.dept_a)

    def test_document_detail_shows_comments_to_department_member(self):
        DocumentComment.objects.create(document=self.document, body="Für Abteilung A sichtbar")

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        self.assertContains(response, "Für Abteilung A sichtbar")

    def test_document_detail_is_404_for_other_department(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        self.assertEqual(response.status_code, 404)

    def test_comments_render_newest_first(self):
        older = DocumentComment.objects.create(document=self.document, body="Älterer Kommentar")
        DocumentComment.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=1)
        )
        DocumentComment.objects.create(document=self.document, body="Neuerer Kommentar")

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        body = response.content.decode()
        self.assertLess(body.index("Neuerer Kommentar"), body.index("Älterer Kommentar"))

    def test_detail_highlights_overdue_follow_up(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Kündigungsfrist verpasst",
            follow_up_date=timezone.localdate() - datetime.timedelta(days=1),
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        self.assertContains(response, "Überfällig")

    def test_detail_highlights_follow_up_due_today(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Termin heute prüfen",
            follow_up_date=timezone.localdate(),
        )

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:detail", args=[self.document.pk]))

        self.assertContains(response, "Heute fällig")
