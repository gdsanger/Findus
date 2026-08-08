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
from apps.ingest.schedules import MAIL_INGEST_SCHEDULE_NAME, sync_mail_ingest_schedule
from apps.ingest.service import ingest_file

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


_FAKE_PDF = b"%PDF-1.4 fake body pdf"
_SUBSTANTIAL_BODY = "Anbei die Rechnung, bitte pruefen und bis Freitag zahlen."


def _build_email_bytes(
    *, subject, sender, body_text=None, body_html=None, message_id="<abc-123@example.com>",
    attachments=None,
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "ingest@findus.example"
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
@patch("apps.ingest.service.render_pdf_from_html", return_value=_FAKE_PDF)
@patch("apps.ingest.service._enqueue_analysis", return_value="task-analyze")
@patch("apps.ingest.service._enqueue_processing", return_value="task-1")
class ImapMailConnectorTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def _mailbox(self, **overrides):
        kwargs = dict(host="imap.example.com", username="u", password="p", department="IT")
        kwargs.update(overrides)
        return ImapMailbox(**kwargs)

    def _run(self, raw):
        fake_connection = FakeImapConnection({b"1": raw})
        with patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL", return_value=fake_connection):
            scan_imap_mailbox(self._mailbox())
        return fake_connection

    def test_mail_with_attachment_creates_lead_and_child(self, *_mocks):
        raw = _build_email_bytes(
            subject="Rechnung",
            sender="Anna Beispiel <anna@example.com>",
            body_text=_SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"%PDF-1.4 content", "application", "pdf")],
        )
        fake_connection = self._run(raw)

        self.assertEqual(Document.objects.count(), 2)
        lead = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertEqual(lead.correspondent.email, "anna@example.com")
        self.assertEqual(lead.departments.get().name, "IT")
        self.assertIn("Rechnung", lead.text_content)
        self.assertIn("bis Freitag", lead.text_content)
        self.assertTrue(lead.original_file.name)
        self.assertEqual(lead.mime_type, "application/pdf")

        child = lead.children.get()
        self.assertEqual(child.child_role, Document.ChildRole.MAIL_ATTACHMENT)
        self.assertEqual(child.kind, Document.Kind.DOCUMENT)
        self.assertEqual(child.source, Document.Source.MAIL)
        self.assertEqual(child.correspondent.email, "anna@example.com")
        self.assertIn(b"1", fake_connection.seen)
        self.assertTrue(fake_connection.logged_out)

    def test_substanceless_mail_is_shell_but_keeps_attachment(self, *_mocks):
        raw = _build_email_bytes(
            subject="RE: Rechnung",
            sender="anna@example.com",
            body_text="Passt, danke",
            attachments=[("invoice.pdf", b"%PDF-1.4 content", "application", "pdf")],
        )
        self._run(raw)

        lead = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertFalse(lead.original_file)
        self.assertTrue(lead.is_body_shell)
        self.assertEqual(lead.text_content, "")
        self.assertTrue(lead.metadata["mail_body_substanceless"])
        self.assertEqual(lead.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(lead.children.count(), 1)

    def test_body_only_mail_creates_lead_with_pdf(self, *_mocks):
        raw = _build_email_bytes(
            subject="Hallo", sender="anna@example.com", body_text=_SUBSTANTIAL_BODY
        )
        self._run(raw)

        self.assertEqual(Document.objects.count(), 1)
        lead = Document.objects.get()
        self.assertEqual(lead.kind, Document.Kind.MAIL_BODY)
        self.assertTrue(lead.original_file.name)
        self.assertTrue(lead.metadata["mail_body_generated"])

    def test_second_poll_does_not_reimport_already_seen_message(self, *_mocks):
        raw = _build_email_bytes(
            subject="Rechnung", sender="anna@example.com", body_text=_SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        with patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL", return_value=fake_connection):
            scan_imap_mailbox(self._mailbox())
            scan_imap_mailbox(self._mailbox())

        self.assertEqual(Document.objects.count(), 2)

    def test_ingest_body_false_forces_shell(self, *_mocks):
        raw = _build_email_bytes(
            subject="Rechnung", sender="anna@example.com", body_text=_SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        with patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL", return_value=fake_connection):
            scan_imap_mailbox(self._mailbox(ingest_body=False))

        lead = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertTrue(lead.is_body_shell)
        self.assertEqual(lead.children.count(), 1)

    def test_failed_message_stays_unseen_for_retry(self, *_mocks):
        raw = _build_email_bytes(
            subject="Rechnung", sender="anna@example.com", body_text=_SUBSTANTIAL_BODY,
            attachments=[("invoice.pdf", b"content", "application", "pdf")],
        )
        fake_connection = FakeImapConnection({b"1": raw})
        with patch("apps.ingest.connectors.mail_imap.imaplib.IMAP4_SSL", return_value=fake_connection), \
                patch("apps.ingest.connectors.mail_imap.ingest_mail", side_effect=RuntimeError("boom")):
            scan_imap_mailbox(self._mailbox())

        self.assertEqual(Document.objects.count(), 0)
        self.assertNotIn(b"1", fake_connection.seen)


def _mock_json_response(json_body):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = json_body
    return response


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
@patch("apps.ingest.service.render_pdf_from_html", return_value=_FAKE_PDF)
@patch("apps.ingest.service._enqueue_analysis", return_value="task-analyze")
@patch("apps.ingest.service._enqueue_processing", return_value="task-1")
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

    def _message(self, **overrides):
        message = {
            "id": "msg-1",
            "subject": "Rechnung",
            "from": {"emailAddress": {"address": "anna@example.com", "name": "Anna Beispiel"}},
            "receivedDateTime": "2026-08-01T10:00:00Z",
            "hasAttachments": False,
            "body": {"contentType": "text", "content": _SUBSTANTIAL_BODY},
        }
        message.update(overrides)
        return message

    def test_mail_with_attachment_creates_lead_and_child(self, *_mocks):
        message = self._message(hasAttachments=True)
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "invoice.pdf",
            "contentType": "application/pdf",
            "contentBytes": "aGVsbG8gd29ybGQ=",
        }
        with patch("apps.mail.backends.graph.requests.post", return_value=_mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600})), \
                patch("apps.ingest.connectors.mail_graph.requests.get", side_effect=[
                    _mock_json_response({"value": [message]}),
                    _mock_json_response({"value": [attachment]}),
                ]), \
                patch("apps.ingest.connectors.mail_graph.requests.patch",
                      return_value=_mock_json_response({})) as mock_patch:
            scan_graph_mailbox(self._mailbox())

        self.assertEqual(Document.objects.count(), 2)
        lead = Document.objects.get(kind=Document.Kind.MAIL_BODY)
        self.assertTrue(lead.original_file.name)
        self.assertEqual(lead.correspondent.email, "anna@example.com")
        child = lead.children.get()
        self.assertEqual(child.child_role, Document.ChildRole.MAIL_ATTACHMENT)
        mock_patch.assert_called_once()
        self.assertIn("/messages/msg-1", mock_patch.call_args.args[0])

    def test_substanceless_mail_is_shell(self, *_mocks):
        message = self._message(body={"contentType": "text", "content": "Passt, danke"})
        with patch("apps.mail.backends.graph.requests.post", return_value=_mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600})), \
                patch("apps.ingest.connectors.mail_graph.requests.get",
                      return_value=_mock_json_response({"value": [message]})), \
                patch("apps.ingest.connectors.mail_graph.requests.patch",
                      return_value=_mock_json_response({})):
            scan_graph_mailbox(self._mailbox())

        lead = Document.objects.get()
        self.assertTrue(lead.is_body_shell)

    def test_failed_message_is_not_marked_read(self, *_mocks):
        message = self._message()
        with patch("apps.mail.backends.graph.requests.post", return_value=_mock_json_response(
            {"access_token": "tok-1", "expires_in": 3600})), \
                patch("apps.ingest.connectors.mail_graph.requests.get",
                      return_value=_mock_json_response({"value": [message]})), \
                patch("apps.ingest.connectors.mail_graph.requests.patch",
                      return_value=_mock_json_response({})) as mock_patch, \
                patch("apps.ingest.connectors.mail_graph.ingest_mail",
                      side_effect=RuntimeError("boom")):
            scan_graph_mailbox(self._mailbox())

        mock_patch.assert_not_called()
        self.assertEqual(Document.objects.count(), 0)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=TEST_MEDIA_ROOT)
@patch("apps.ingest.service.render_pdf_from_html", return_value=_FAKE_PDF)
@patch("apps.ingest.service._enqueue_analysis", return_value="task-analyze")
@patch("apps.ingest.service._enqueue_processing", return_value="task-1")
class IngestMailServiceTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def test_lead_deduped_by_message_id(self, *_mocks):
        from apps.ingest.service import MailAttachment, ingest_mail

        kwargs = dict(
            message_id="<dup@example.com>",
            subject="Rechnung",
            sender_email="anna@example.com",
            body=_SUBSTANTIAL_BODY,
            body_content_type="text/plain",
            attachments=[MailAttachment("a.pdf", b"content", "application/pdf")],
        )
        first = ingest_mail(**kwargs)
        second = ingest_mail(**kwargs)

        self.assertTrue(first.lead_created)
        self.assertFalse(second.lead_created)
        self.assertEqual(first.lead.id, second.lead.id)
        self.assertEqual(Document.objects.filter(kind=Document.Kind.MAIL_BODY).count(), 1)

    def test_quoted_history_and_signature_not_in_index(self, *_mocks):
        from apps.ingest.service import ingest_mail

        body = (
            "Neuer Text mit genug Woertern fuer die Substanzpruefung hier.\n"
            "-- \nChristian\n"
            "Am 01.02.2026 um 10:00 schrieb Max:\n"
            "> alte geheime zitat zeile die nicht in den index darf\n"
        )
        result = ingest_mail(
            message_id="<quote@example.com>",
            subject="Antwort",
            sender_email="anna@example.com",
            body=body,
            body_content_type="text/plain",
        )
        index_text = result.lead.text_content
        self.assertIn("Neuer Text", index_text)
        self.assertNotIn("geheime zitat", index_text)
        self.assertNotIn("Christian", index_text)


class MailBodyPreparationTests(TestCase):
    def test_html_body_is_decluttered(self):
        from apps.ingest.mail_body import prepare_body

        html = (
            "<html><head><style>x{}</style></head><body>"
            "<div style='display:none'>preheader</div>"
            "<p>Sichtbarer Inhalt der Nachricht steht hier.</p>"
            "<img src='t.gif' width='1' height='1'>"
            "<blockquote>alter verlauf</blockquote>"
            "</body></html>"
        )
        result = prepare_body(html, "text/html")
        self.assertIn("Sichtbarer Inhalt", result.text)
        self.assertNotIn("preheader", result.text)
        self.assertNotIn("alter verlauf", result.text)
        self.assertNotIn("script", result.html.lower())

    def test_substance_word_count(self):
        from apps.ingest.mail_body import prepare_body

        self.assertEqual(prepare_body("Passt, danke", "text/plain").word_count, 2)
        self.assertGreaterEqual(
            prepare_body(_SUBSTANTIAL_BODY, "text/plain").word_count, 5
        )

    def test_pdf_render_missing_binary_raises_pdfrendererror(self):
        from apps.ingest.mail_body import PdfRenderError, render_pdf_from_html

        with patch("apps.ingest.mail_body.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(PdfRenderError):
                render_pdf_from_html("<html></html>")
