import asyncio
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Department
from apps.documents.models import Document, DocumentComment
from apps.mcp.tools import _document_comments_payload, document_comments

User = get_user_model()


@override_settings(MCP_USER_USERNAME="mcp-bot")
class DocumentCommentsToolTests(TestCase):
    """Covers requirement #5 (#1125): the MCP endpoint can read a

    document's Kommentar-Chronik, scoped through the same `visible_to`
    every other Findus surface uses (`apps.mcp.auth.get_mcp_user`).
    """

    def setUp(self):
        self.department = Department.objects.create(name="Dept A")
        self.mcp_user = User.objects.create_user(username="mcp-bot", password="x")
        self.mcp_user.departments.add(self.department)

        self.author = User.objects.create_user(username="alice", password="x")

        self.document = Document.objects.create(title="Mietvertrag")
        self.document.departments.add(self.department)

    def test_returns_document_title_and_comments(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Kündigungsfrist prüfen",
            follow_up_date=datetime.date(2026, 9, 1),
            remind=True,
            author=self.author,
        )

        payload = _document_comments_payload(self.document.pk)

        self.assertEqual(payload["document_title"], "Mietvertrag")
        self.assertEqual(len(payload["comments"]), 1)
        comment = payload["comments"][0]
        self.assertEqual(comment["body"], "Kündigungsfrist prüfen")
        self.assertEqual(comment["author"], "alice")
        self.assertEqual(comment["follow_up_date"], "2026-09-01")
        self.assertTrue(comment["remind"])
        self.assertIsNone(comment["reminded_at"])

    def test_comments_are_newest_first(self):
        older = DocumentComment.objects.create(document=self.document, body="Älter")
        DocumentComment.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=1)
        )
        DocumentComment.objects.create(document=self.document, body="Neuer")

        payload = _document_comments_payload(self.document.pk)

        self.assertEqual([c["body"] for c in payload["comments"]], ["Neuer", "Älter"])

    def test_document_without_comments_returns_empty_list(self):
        payload = _document_comments_payload(self.document.pk)

        self.assertEqual(payload["comments"], [])

    def test_invisible_document_returns_not_found_error(self):
        other_department = Department.objects.create(name="Fremd")
        foreign_document = Document.objects.create(title="Fremdes Dokument")
        foreign_document.departments.add(other_department)
        DocumentComment.objects.create(document=foreign_document, body="Nicht sichtbar")

        payload = _document_comments_payload(foreign_document.pk)

        self.assertEqual(payload, {"error": "not_found"})

    def test_nonexistent_document_returns_not_found_error(self):
        payload = _document_comments_payload(999999)

        self.assertEqual(payload, {"error": "not_found"})


@override_settings(MCP_USER_USERNAME="mcp-bot")
class DocumentCommentsAsyncToolTests(TransactionTestCase):
    """`sync_to_async(thread_sensitive=True)` runs the payload lookup on a

    separate DB connection -- `TransactionTestCase` (real commits, no
    per-test atomic wrapper) instead of `TestCase` so that connection
    actually sees the fixture data, unlike a plain `TestCase`'s
    uncommitted savepoint.
    """

    def test_async_tool_wraps_the_sync_payload(self):
        department = Department.objects.create(name="Dept A")
        User.objects.create_user(username="mcp-bot", password="x").departments.add(department)
        document = Document.objects.create(title="Mietvertrag")
        document.departments.add(department)
        DocumentComment.objects.create(document=document, body="Über MCP lesbar")

        payload = asyncio.run(document_comments(document.pk))

        self.assertEqual(payload["document_title"], "Mietvertrag")
        self.assertEqual(payload["comments"][0]["body"], "Über MCP lesbar")
