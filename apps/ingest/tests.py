"""Unit tests for apps.ingest.

`ingest_file` tests run against a temp filesystem storage (never the real
S3/MinIO backend) and patch `django_q.tasks.async_task` (no real broker/
worker involved). The folder connector tests drive `scan_folder` against
real temp directories to cover the processed/failed move + fault-tolerance
behaviour end to end.
"""

from __future__ import annotations

import shutil
import tempfile
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django_q.models import Schedule

from apps.accounts.models import Department
from apps.documents.models import Correspondent, Document
from apps.ingest.connectors.folder import WatchFolder, scan_folder
from apps.ingest.connectors.mail_graph import GraphMailbox, scan_mailbox as scan_graph_mailbox
from apps.ingest.connectors.mail_imap import ImapMailbox, scan_mailbox as scan_imap_mailbox
from apps.ingest.mail_body import (
    build_index_text,
    clean_body,
    has_substance,
    render_body_pdf,
)
from apps.ingest.schedules import MAIL_INGEST_SCHEDULE_NAME, sync_mail_ingest_schedule
from apps.ingest.service import MailAttachment, ingest_file, ingest_mail

# Ein Body mit genug Woertern, um den Substanz-Check (Default-Schwelle 8)
# zu bestehen -- gemeinsame Fixture fuer die Connector- und Service-Tests.
SUBSTANTIAL_BODY = (
    "anbei die Rechnung, der Betrag weicht wegen der Nachbuchung ab, "
    "bitte bis Freitag pruefen."
)

TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-ingest-media-")

_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class IngestFileServiceTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    @patch("apps.ingest.service._enqueue_processing")
    def test_creates_document_with_sha256_storage_and_metadata(self, mock_enqueue):
        mock_enqueue.return_value = "task-1"
        department = Department.objects.create(name="IT")

        result = ingest_file(
            BytesIO(b"hello world"),
            filename="invoice.pdf",
            source=Document.Source.FOLDER,
            department=department,
            origin_metadata={"ingest_folder": "/data/ingest/it"},
        )

        self.assertTrue(result.created)
        self.assertFalse(result.duplicate)
        document = result.document
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PENDING)
        self.assertEqual(document.source, Document.Source.FOLDER)
        self.assertEqual(len(document.sha256), 64)
        self.assertTrue(document.original_file.name)
        self.assertIn(department, document.departments.all())
        self.assertEqual(document.metadata["original_filename"], "invoice.pdf")
        self.assertEqual(document.metadata["mime_type"], "application/pdf")
        self.assertEqual(document.metadata["size"], len(b"hello world"))
        self.assertEqual(document.metadata["ingest_folder"], "/data/ingest/it")
        mock_enqueue.assert_called_once_with(document.id)

        with document.original_file.open("rb") as stored:
            self.assertEqual(stored.read(), b"hello world")

    @patch("apps.ingest.service._enqueue_processing")
    def test_duplicate_sha256_is_skipped_by_default(self, mock_enqueue):
        ingest_file(BytesIO(b"same bytes"), filename="a.pdf", source=Document.Source.UPLOAD)
        self.assertEqual(Document.objects.count(), 1)

        result = ingest_file(BytesIO(b"same bytes"), filename="b.pdf", source=Document.Source.FOLDER)

        self.assertFalse(result.created)
        self.assertTrue(result.duplicate)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(mock_enqueue.call_count, 1)

    @patch("apps.ingest.service._enqueue_processing")
    def test_duplicate_with_link_records_occurrence_without_reimport(self, mock_enqueue):
        first = ingest_file(
            BytesIO(b"same bytes"), filename="a.pdf", source=Document.Source.UPLOAD
        ).document

        result = ingest_file(
            BytesIO(b"same bytes"),
            filename="b.pdf",
            source=Document.Source.FOLDER,
            origin_metadata={"ingest_folder": "/data/ingest/it"},
            on_duplicate="link",
        )

        self.assertFalse(result.created)
        self.assertEqual(result.document.id, first.id)
        self.assertEqual(Document.objects.count(), 1)
        first.refresh_from_db()
        self.assertEqual(len(first.metadata["duplicate_occurrences"]), 1)
        occurrence = first.metadata["duplicate_occurrences"][0]
        self.assertEqual(occurrence["source"], Document.Source.FOLDER)
        self.assertEqual(occurrence["filename"], "b.pdf")

    @patch("apps.ingest.service._enqueue_processing")
    def test_correspondent_is_attached_to_newly_created_document(self, mock_enqueue):
        mock_enqueue.return_value = "task-1"
        correspondent = Correspondent.objects.create(name="Anna", email="anna@example.com")

        result = ingest_file(
            BytesIO(b"mail attachment"),
            filename="invoice.pdf",
            source=Document.Source.MAIL,
            correspondent=correspondent,
        )

        self.assertEqual(result.document.correspondent_id, correspondent.id)

    @patch("apps.ingest.service._enqueue_processing")
    def test_correspondent_is_not_attached_to_duplicate_document(self, mock_enqueue):
        mock_enqueue.return_value = "task-1"
        ingest_file(BytesIO(b"same bytes"), filename="a.pdf", source=Document.Source.UPLOAD)
        correspondent = Correspondent.objects.create(name="Anna", email="anna@example.com")

        result = ingest_file(
            BytesIO(b"same bytes"),
            filename="b.pdf",
            source=Document.Source.MAIL,
            correspondent=correspondent,
        )

        self.assertTrue(result.duplicate)
        self.assertIsNone(result.document.correspondent)

    def test_enqueues_processing_task_via_django_q(self):
        with patch("django_q.tasks.async_task") as mock_async_task:
            mock_async_task.return_value = "task-123"
            result = ingest_file(
                BytesIO(b"queued content"), filename="c.pdf", source=Document.Source.UPLOAD
            )

        mock_async_task.assert_called_once()
        from apps.documents.tasks import extract_document_task

        args = mock_async_task.call_args.args
        self.assertEqual(args[0], extract_document_task)
        self.assertEqual(args[1], result.document.id)


class MailIngestScheduleTests(TestCase):
    """`sync_mail_ingest_schedule` runs via `post_migrate` (apps.py), so the
    test DB setup already registered it once before any test method runs."""

    def test_schedule_already_registered_via_post_migrate(self):
        schedule = Schedule.objects.get(name=MAIL_INGEST_SCHEDULE_NAME)
        self.assertEqual(schedule.func, "django.core.management.call_command")
        self.assertEqual(schedule.args, repr(("watch_mail_ingest", "--once")))
        self.assertEqual(schedule.schedule_type, Schedule.MINUTES)
        self.assertEqual(schedule.repeats, -1)

    def test_sync_is_idempotent(self):
        sync_mail_ingest_schedule()
        sync_mail_ingest_schedule()

        self.assertEqual(
            Schedule.objects.filter(name=MAIL_INGEST_SCHEDULE_NAME).count(), 1
        )

    @override_settings(FINDUS_MAIL_POLL_MINUTES=15)
    def test_sync_picks_up_configured_interval(self):
        sync_mail_ingest_schedule()

        schedule = Schedule.objects.get(name=MAIL_INGEST_SCHEDULE_NAME)
        self.assertEqual(schedule.minutes, 15)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class FolderConnectorScanTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="findus-ingest-folder-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.folder = WatchFolder(path=self.tmp_dir, department="IT")

    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_imported_file_is_moved_to_processed(self, mock_enqueue):
        (self.tmp_dir / "scan.pdf").write_bytes(b"scan content")

        scan_folder(self.folder)

        self.assertFalse((self.tmp_dir / "scan.pdf").exists())
        self.assertTrue((self.folder.processed_dir / "scan.pdf").exists())
        self.assertEqual(Document.objects.count(), 1)
        document = Document.objects.get()
        self.assertEqual(document.departments.get().name, "IT")

    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_second_scan_of_same_content_is_deduped_not_reimported(self, mock_enqueue):
        (self.tmp_dir / "first.pdf").write_bytes(b"dup content")
        scan_folder(self.folder)
        (self.tmp_dir / "second.pdf").write_bytes(b"dup content")

        scan_folder(self.folder)

        self.assertEqual(Document.objects.count(), 1)
        self.assertTrue((self.folder.processed_dir / "second.pdf").exists())

    @patch(
        "apps.ingest.connectors.folder.ingest_file",
        side_effect=RuntimeError("boom"),
    )
    def test_failed_file_is_moved_to_failed_and_scan_continues(self, mock_ingest_file):
        (self.tmp_dir / "broken.pdf").write_bytes(b"whatever")
        (self.tmp_dir / "other.pdf").write_bytes(b"also whatever")

        scan_folder(self.folder)

        self.assertTrue((self.folder.failed_dir / "broken.pdf").exists())
        self.assertTrue((self.folder.failed_dir / "other.pdf").exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_disallowed_extension_is_left_in_place(self):
        (self.tmp_dir / "notes.exe").write_bytes(b"binary")

        with override_settings(FINDUS_INGEST_ALLOWED_EXTENSIONS=["pdf"]):
            scan_folder(self.folder)

        self.assertTrue((self.tmp_dir / "notes.exe").exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_missing_folder_is_skipped_without_raising(self):
        missing = WatchFolder(path=self.tmp_dir / "does-not-exist")
        scan_folder(missing)


def _build_email_bytes(
    *, subject, sender, body_text=None, body_html=None, attachments=None,
    message_id="<abc-123@example.com>",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "ingest@findus.example"
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(body_text or "")
    if body_html is not None:
        message.add_alternative(body_html, subtype="html")
    for filename, content, maintype, subtype in attachments or []:
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


class FakeImapConnection:
    """Minimal stand-in for `imaplib.IMAP4`/`IMAP4_SSL` driving `scan_mailbox`
    against in-memory messages instead of a real server."""

    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.seen: set[bytes] = set()
        self.logged_out = False

    def login(self, username, password):
        return "OK", [b"Logged in"]

    def select(self, folder):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criteria):
        assert criteria == "UNSEEN"
        unseen = sorted(mid for mid in self.messages if mid not in self.seen)
        return "OK", [b" ".join(unseen)]

    def fetch(self, message_id, parts):
        raw = self.messages[message_id]
        return "OK", [(f"{message_id.decode()} (BODY[] {{{len(raw)}}}".encode(), raw), b")"]

    def store(self, message_id, flag_op, flags):
        self.seen.add(message_id)
        return "OK", [b"1 (FLAGS (\\Seen))"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"Logging out"]


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class ImapMailConnectorTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def _mailbox(self, **overrides):
        kwargs = dict(host="imap.example.com", username="u", password="p", department="IT")
        kwargs.update(overrides)
        return ImapMailbox(**kwargs)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_mail_with_attachment_creates_leitdokument_plus_child(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        raw = _build_email_bytes(
            subject="Rechnung",
            sender="Anna Beispiel <anna@example.com>",
            body_text=SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"%PDF-1.4 content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 2)
        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertIsNone(leit.parent_id)
        self.assertEqual(leit.source, Document.Source.MAIL)
        self.assertEqual(leit.correspondent.email, "anna@example.com")
        self.assertEqual(leit.departments.get().name, "IT")

        child = Document.objects.get(child_role=Document.ChildRole.MAIL_ATTACHMENT)
        self.assertEqual(child.parent_id, leit.id)
        self.assertEqual(child.original_filename, "invoice.pdf")
        self.assertEqual(child.correspondent.email, "anna@example.com")
        self.assertEqual(child.departments.get().name, "IT")

        self.assertIn(b"1", fake_connection.seen)
        self.assertTrue(fake_connection.logged_out)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_second_poll_does_not_reimport_already_seen_message(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        raw = _build_email_bytes(
            subject="Rechnung",
            sender="anna@example.com",
            body_text=SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))
        scan_imap_mailbox(self._mailbox(ingest_body=True))

        # 1 Leitdokument + 1 Anhang, kein zweiter Import beim erneuten Poll.
        self.assertEqual(Document.objects.count(), 2)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_substantial_body_fills_leitdokument_with_pdf_and_index_text(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        raw = _build_email_bytes(
            subject="Hallo",
            sender="anna@example.com",
            body_text=SUBSTANTIAL_BODY,
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 1)
        leit = Document.objects.get()
        self.assertEqual(leit.kind, Document.Kind.MAIL_BODY)
        self.assertEqual(leit.metadata["mail_subject"], "Hallo")
        self.assertEqual(leit.metadata["mail_from"], "anna@example.com")
        self.assertEqual(leit.metadata["mime_type"], "application/pdf")
        self.assertEqual(leit.metadata["body_source"], "generated_from_mail")
        self.assertTrue(leit.original_file.name.endswith(".pdf"))
        self.assertIn("Nachbuchung", leit.text_content)
        self.assertIn("Betreff: Hallo", leit.text_content)
        with leit.original_file.open("rb") as stored:
            self.assertEqual(stored.read(4), b"%PDF")
        mock_body_enqueue.assert_called_once_with(leit.id)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_substanceless_body_creates_thin_shell_but_keeps_attachment(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        raw = _build_email_bytes(
            subject="Kurz",
            sender="anna@example.com",
            body_text="Passt, danke!",
            attachments=[("invoice.pdf", b"%PDF-1.4 content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 2)
        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertFalse(bool(leit.original_file))
        self.assertEqual(leit.text_content, "")
        self.assertEqual(leit.metadata["body_source"], "thin_shell")
        self.assertEqual(leit.processing_status, Document.ProcessingStatus.READY)
        mock_body_enqueue.assert_not_called()

        child = Document.objects.get(child_role=Document.ChildRole.MAIL_ATTACHMENT)
        self.assertEqual(child.parent_id, leit.id)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_html_body_is_cleaned_before_indexing(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        html = (
            "<html><body>"
            "<div style='display:none'>versteckter Preheader Tracking Text</div>"
            "<p>anbei die Rechnung, der Betrag weicht wegen der Nachbuchung ab, "
            "bitte pruefen.</p>"
            "<img src='http://track/x' width='1' height='1'>"
            "<blockquote>Am 01.01.2026 schrieb Anna: alter Verlauf hier drin</blockquote>"
            "</body></html>"
        )
        raw = _build_email_bytes(subject="Hallo", sender="anna@example.com", body_html=html)
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))

        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertIn("Nachbuchung", leit.text_content)
        self.assertNotIn("versteckter Preheader", leit.text_content)
        self.assertNotIn("alter Verlauf", leit.text_content)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_two_attachments_hang_under_one_leitdokument(
        self, mock_enqueue, mock_imap_ssl, mock_body_enqueue
    ):
        raw = _build_email_bytes(
            subject="Rechnung",
            sender="anna@example.com",
            body_text=SUBSTANTIAL_BODY,
            attachments=[
                ("invoice.pdf", b"content-1", "application", "pdf"),
                ("contract.pdf", b"content-2", "application", "pdf"),
            ],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        scan_imap_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 3)
        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        children = Document.objects.filter(parent=leit)
        self.assertEqual(children.count(), 2)
        for child in children:
            self.assertEqual(child.child_role, Document.ChildRole.MAIL_ATTACHMENT)

    @patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL")
    def test_failed_message_stays_unseen_for_retry(self, mock_imap_ssl):
        raw = _build_email_bytes(
            subject="Rechnung",
            sender="anna@example.com",
            body_text=SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        mock_imap_ssl.return_value = fake_connection

        with patch(
            "apps.ingest.connectors.mail_imap.ingest_mail", side_effect=RuntimeError("boom")
        ):
            scan_imap_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 0)
        self.assertNotIn(b"1", fake_connection.seen)


def _mock_json_response(json_body):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = json_body
    return response


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class GraphMailConnectorTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def _mailbox(self, **overrides):
        kwargs = dict(
            tenant_id="tenant-1",
            client_id="client-1",
            client_secret="secret",
            mailbox="postfach@example.com",
            department="IT",
        )
        kwargs.update(overrides)
        return GraphMailbox(**kwargs)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_graph.requests.patch")
    @patch("apps.ingest.connectors.mail_graph.requests.get")
    @patch("apps.mail.backends.graph.requests.post")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_mail_with_attachment_creates_leitdokument_plus_child(
        self, mock_enqueue, mock_token_post, mock_get, mock_patch, mock_body_enqueue
    ):
        mock_token_post.return_value = _mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600}
        )
        message = {
            "id": "msg-1",
            "subject": "Rechnung",
            "from": {"emailAddress": {"address": "anna@example.com", "name": "Anna Beispiel"}},
            "toRecipients": [{"emailAddress": {"address": "ingest@example.com"}}],
            "receivedDateTime": "2026-08-01T10:00:00Z",
            "hasAttachments": True,
            "body": {"contentType": "text", "content": SUBSTANTIAL_BODY},
        }
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "invoice.pdf",
            "contentType": "application/pdf",
            "contentBytes": "aGVsbG8gd29ybGQ=",
        }
        mock_get.side_effect = [
            _mock_json_response({"value": [message]}),
            _mock_json_response({"value": [attachment]}),
        ]
        mock_patch.return_value = _mock_json_response({})

        scan_graph_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 2)
        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertIsNone(leit.parent_id)
        self.assertEqual(leit.correspondent.email, "anna@example.com")
        self.assertEqual(leit.metadata["mail_to"], "ingest@example.com")

        child = Document.objects.get(child_role=Document.ChildRole.MAIL_ATTACHMENT)
        self.assertEqual(child.parent_id, leit.id)
        self.assertEqual(child.original_filename, "invoice.pdf")
        self.assertEqual(child.departments.get().name, "IT")

        mock_patch.assert_called_once()
        self.assertIn("/messages/msg-1", mock_patch.call_args.args[0])
        self.assertEqual(mock_patch.call_args.kwargs["json"], {"isRead": True})

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_graph.requests.patch")
    @patch("apps.ingest.connectors.mail_graph.requests.get")
    @patch("apps.mail.backends.graph.requests.post")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_html_body_only_mail_becomes_filled_leitdokument(
        self, mock_enqueue, mock_token_post, mock_get, mock_patch, mock_body_enqueue
    ):
        mock_token_post.return_value = _mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600}
        )
        html = (
            "<html><body><p>anbei die Rechnung, der Betrag weicht wegen der "
            "Nachbuchung ab, bitte pruefen.</p>"
            "<blockquote>Am 01.01.2026 schrieb Anna: alter Verlauf</blockquote>"
            "</body></html>"
        )
        message = {
            "id": "msg-1",
            "subject": "Hallo",
            "from": {"emailAddress": {"address": "anna@example.com", "name": "Anna"}},
            "receivedDateTime": "2026-08-01T10:00:00Z",
            "hasAttachments": False,
            "body": {"contentType": "html", "content": html},
        }
        mock_get.return_value = _mock_json_response({"value": [message]})
        mock_patch.return_value = _mock_json_response({})

        scan_graph_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 1)
        leit = Document.objects.get()
        self.assertEqual(leit.kind, Document.Kind.MAIL_BODY)
        self.assertEqual(leit.metadata["mime_type"], "application/pdf")
        self.assertIn("Nachbuchung", leit.text_content)
        self.assertNotIn("alter Verlauf", leit.text_content)
        mock_patch.assert_called_once()

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.connectors.mail_graph.requests.patch")
    @patch("apps.ingest.connectors.mail_graph.requests.get")
    @patch("apps.mail.backends.graph.requests.post")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_two_attachments_hang_under_one_leitdokument(
        self, mock_enqueue, mock_token_post, mock_get, mock_patch, mock_body_enqueue
    ):
        mock_token_post.return_value = _mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600}
        )
        message = {
            "id": "msg-1",
            "subject": "Rechnung",
            "from": {"emailAddress": {"address": "anna@example.com", "name": "Anna Beispiel"}},
            "receivedDateTime": "2026-08-01T10:00:00Z",
            "hasAttachments": True,
            "body": {"contentType": "text", "content": SUBSTANTIAL_BODY},
        }
        attachment_1 = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "invoice.pdf",
            "contentType": "application/pdf",
            "contentBytes": "aGVsbG8gd29ybGQ=",
        }
        attachment_2 = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "contract.pdf",
            "contentType": "application/pdf",
            "contentBytes": "Y29udHJhY3Q=",
        }
        mock_get.side_effect = [
            _mock_json_response({"value": [message]}),
            _mock_json_response({"value": [attachment_1, attachment_2]}),
        ]
        mock_patch.return_value = _mock_json_response({})

        scan_graph_mailbox(self._mailbox(ingest_body=True))

        self.assertEqual(Document.objects.count(), 3)
        leit = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertEqual(Document.objects.filter(parent=leit).count(), 2)

    @patch("apps.ingest.connectors.mail_graph.requests.patch")
    @patch("apps.ingest.connectors.mail_graph.requests.get")
    @patch("apps.mail.backends.graph.requests.post")
    def test_failed_message_is_not_marked_read(self, mock_token_post, mock_get, mock_patch):
        mock_token_post.return_value = _mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600}
        )
        message = {
            "id": "msg-1",
            "subject": "Rechnung",
            "from": {"emailAddress": {"address": "anna@example.com", "name": "Anna"}},
            "receivedDateTime": "2026-08-01T10:00:00Z",
            "hasAttachments": False,
            "body": {"contentType": "text", "content": SUBSTANTIAL_BODY},
        }
        mock_get.return_value = _mock_json_response({"value": [message]})

        with patch(
            "apps.ingest.connectors.mail_graph.ingest_mail", side_effect=RuntimeError("boom")
        ):
            scan_graph_mailbox(self._mailbox(ingest_body=True))

        mock_patch.assert_not_called()
        self.assertEqual(Document.objects.count(), 0)


class MailBodyCleaningTests(TestCase):
    """Unit tests fuer `apps.ingest.mail_body` -- HTML-Entruempelung,
    Substanz-Check, Index-Text und PDF-Rendering (#1070)."""

    def test_html_stripping_removes_scripts_styles_tracking_and_hidden(self):
        html = (
            "<html><head><style>.x{color:red}</style></head><body>"
            "<script>evil()</script>"
            "<div style='display:none'>versteckter Preheader</div>"
            "<p>Sichtbarer Absatz mit Inhalt.</p>"
            "<img src='http://t/x' width='1' height='1'>"
            "</body></html>"
        )
        cleaned = clean_body(html, "text/html")
        self.assertIn("Sichtbarer Absatz", cleaned)
        self.assertNotIn("evil()", cleaned)
        self.assertNotIn("color:red", cleaned)
        self.assertNotIn("versteckter Preheader", cleaned)

    def test_quoted_history_and_signature_are_dropped(self):
        text = (
            "Meine eigentliche Antwort steht hier.\n"
            "-- \n"
            "Max Mustermann\n"
            "Gesendet von meinem iPhone\n"
        )
        cleaned = clean_body(text, "text/plain")
        self.assertIn("Meine eigentliche Antwort", cleaned)
        self.assertNotIn("Max Mustermann", cleaned)
        self.assertNotIn("Gesendet von meinem", cleaned)

    def test_outlook_header_block_starts_the_quote(self):
        text = (
            "Kurze Rueckmeldung von mir.\n\n"
            "Von: Anna <anna@example.com>\n"
            "Gesendet: Montag, 1. Januar 2026\n"
            "An: ich@example.com\n"
            "Betreff: Re: Test\n\n"
            "der ganze alte Verlauf hier\n"
        )
        cleaned = clean_body(text, "text/plain")
        self.assertIn("Kurze Rueckmeldung", cleaned)
        self.assertNotIn("der ganze alte Verlauf", cleaned)

    def test_has_substance_respects_threshold(self):
        self.assertFalse(has_substance("Passt, danke!"))
        self.assertTrue(has_substance(SUBSTANTIAL_BODY))
        self.assertTrue(has_substance("nur drei Woerter hier", min_words=3))
        self.assertFalse(has_substance("nur drei Woerter hier", min_words=10))

    def test_index_text_carries_metadata_header(self):
        meta = {
            "mail_subject": "Rechnung",
            "mail_from": "anna@example.com",
            "mail_date": "Fri, 01 Jan 2026",
        }
        index = build_index_text(meta, "Body-Inhalt.")
        self.assertIn("Betreff: Rechnung", index)
        self.assertIn("Von: anna@example.com", index)
        self.assertIn("Body-Inhalt.", index)

    def test_render_body_pdf_returns_pdf_bytes(self):
        meta = {"mail_subject": "Rechnung März — 5€", "mail_from": "anna@example.com"}
        pdf = render_body_pdf(meta, "Betrag weicht ab — bitte prüfen … 😀 日本語")
        self.assertEqual(pdf[:4], b"%PDF")
        self.assertGreater(len(pdf), 100)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
class IngestMailServiceTests(TestCase):
    """`ingest_mail` orchestriert Leitdokument + Unterdokumente (#1070)."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_duplicate_message_id_is_not_reingested(self, mock_enqueue, mock_body_enqueue):
        meta = {"message_id": "<dup-1@example.com>", "mail_subject": "Test"}

        first = ingest_mail(
            body=SUBSTANTIAL_BODY,
            body_content_type="text/plain",
            attachments=[],
            mail_metadata=meta,
        )
        second = ingest_mail(
            body=SUBSTANTIAL_BODY,
            body_content_type="text/plain",
            attachments=[],
            mail_metadata=meta,
        )

        self.assertTrue(first.created)
        self.assertTrue(second.duplicate)
        self.assertFalse(second.created)
        self.assertEqual(second.leitdokument.id, first.leitdokument.id)
        self.assertEqual(Document.objects.filter(kind=Document.Kind.MAIL_BODY).count(), 1)

    @patch("apps.ingest.service._enqueue_body_processing", return_value="task-body")
    @patch("apps.ingest.service._enqueue_processing", return_value="task-1")
    def test_fill_body_false_forces_thin_shell(self, mock_enqueue, mock_body_enqueue):
        result = ingest_mail(
            body=SUBSTANTIAL_BODY,
            body_content_type="text/plain",
            attachments=[
                MailAttachment(BytesIO(b"%PDF-1.4 x"), "invoice.pdf", "application/pdf")
            ],
            mail_metadata={"message_id": "<shell-1@example.com>", "mail_subject": "S"},
            fill_body=False,
        )

        leit = result.leitdokument
        self.assertEqual(leit.metadata["body_source"], "thin_shell")
        self.assertFalse(bool(leit.original_file))
        self.assertEqual(leit.text_content, "")
        mock_body_enqueue.assert_not_called()
        self.assertEqual(Document.objects.filter(parent=leit).count(), 1)
