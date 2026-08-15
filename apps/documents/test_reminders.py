"""Covers the #1125 acceptance criteria for the reminder job: exactly one

mail per due+remind comment, bundled per recipient, and no resend once
`reminded_at` is set.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.mail.backends.fake import FakeMailBackend

from .models import Document, DocumentComment
from .reminders import send_due_comment_reminders

User = get_user_model()


class SendDueCommentRemindersTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="alice", password="x", email="alice@example.com"
        )
        self.document = Document.objects.create(title="Mietvertrag")
        self.today = timezone.localdate()
        self.fake_backend = FakeMailBackend()
        self._patcher = patch(
            "apps.mail.service.get_mail_backend", return_value=self.fake_backend
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_due_and_remind_comment_sends_a_mail(self):
        comment = DocumentComment.objects.create(
            document=self.document,
            body="Kündigungsfrist beachten",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 1)
        self.assertEqual(self.fake_backend.sent[0].to, ["alice@example.com"])
        comment.refresh_from_db()
        self.assertIsNotNone(comment.reminded_at)

    def test_mail_body_includes_document_title_comment_and_link(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Kündigungsfrist beachten",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()

        sent = self.fake_backend.sent[0]
        self.assertIn("Mietvertrag", sent.text_body)
        self.assertIn("Kündigungsfrist beachten", sent.text_body)
        self.assertIn(reverse("documents:detail", args=[self.document.pk]), sent.text_body)

    def test_second_run_does_not_resend(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Kündigungsfrist beachten",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()
        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 1)

    def test_multiple_due_comments_for_same_author_are_bundled(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Erste Wiedervorlage",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )
        other_document = Document.objects.create(title="Versicherung")
        DocumentComment.objects.create(
            document=other_document,
            body="Zweite Wiedervorlage",
            follow_up_date=self.today - datetime.timedelta(days=1),
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 1)
        sent = self.fake_backend.sent[0]
        self.assertIn("Erste Wiedervorlage", sent.text_body)
        self.assertIn("Zweite Wiedervorlage", sent.text_body)

    def test_overdue_and_due_today_are_distinguished_in_mail_body(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Heute fällig",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )
        other_document = Document.objects.create(title="Versicherung")
        DocumentComment.objects.create(
            document=other_document,
            body="Bereits überfällig",
            follow_up_date=self.today - datetime.timedelta(days=3),
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()

        sent = self.fake_backend.sent[0]
        self.assertIn("fällig heute", sent.text_body)
        self.assertIn("ÜBERFÄLLIG", sent.text_body)
        self.assertIsNotNone(sent.html_body)
        self.assertIn("Fällig heute", sent.html_body)
        self.assertIn("Überfällig seit", sent.html_body)

    def test_comment_without_remind_is_not_sent(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Nur eine Notiz mit Datum",
            follow_up_date=self.today,
            remind=False,
            author=self.author,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 0)

    def test_future_follow_up_date_is_not_sent(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Verlängerung prüfen",
            follow_up_date=self.today + datetime.timedelta(days=30),
            remind=True,
            author=self.author,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 0)

    def test_comment_without_author_is_skipped(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Ohne Autor",
            follow_up_date=self.today,
            remind=True,
            author=None,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 0)

    def test_author_without_email_is_skipped(self):
        author_without_email = User.objects.create_user(username="bob", password="x", email="")
        DocumentComment.objects.create(
            document=self.document,
            body="Autor ohne Mailadresse",
            follow_up_date=self.today,
            remind=True,
            author=author_without_email,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 0)

    def test_different_authors_get_separate_mails(self):
        other_author = User.objects.create_user(
            username="bob", password="x", email="bob@example.com"
        )
        DocumentComment.objects.create(
            document=self.document,
            body="Für Alice",
            follow_up_date=self.today,
            remind=True,
            author=self.author,
        )
        DocumentComment.objects.create(
            document=self.document,
            body="Für Bob",
            follow_up_date=self.today,
            remind=True,
            author=other_author,
        )

        send_due_comment_reminders()

        self.assertEqual(len(self.fake_backend.sent), 2)
        recipients = {tuple(sent.to) for sent in self.fake_backend.sent}
        self.assertEqual(recipients, {("alice@example.com",), ("bob@example.com",)})


class DocumentCommentReminderScheduleTests(TestCase):
    """`sync_document_comment_reminder_schedule` runs via `post_migrate`

    (apps.py), so the test DB setup already registered it once before any
    test method runs -- same pattern as
    `apps.ingest.tests.MailIngestScheduleTests`.
    """

    def test_schedule_already_registered_via_post_migrate(self):
        from django_q.models import Schedule

        from .schedules import DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME

        schedule = Schedule.objects.get(name=DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME)
        self.assertEqual(schedule.func, "apps.documents.reminders.send_due_comment_reminders")
        self.assertEqual(schedule.schedule_type, Schedule.DAILY)
        self.assertEqual(schedule.repeats, -1)

    def test_sync_is_idempotent(self):
        from django_q.models import Schedule

        from .schedules import (
            DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME,
            sync_document_comment_reminder_schedule,
        )

        sync_document_comment_reminder_schedule()
        sync_document_comment_reminder_schedule()

        self.assertEqual(
            Schedule.objects.filter(name=DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME).count(), 1
        )
