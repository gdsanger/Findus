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
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.accounts.models import Department
from apps.documents.models import Document
from apps.ingest.connectors.folder import WatchFolder, scan_folder
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

    def test_enqueues_processing_task_via_django_q(self):
        with patch("django_q.tasks.async_task") as mock_async_task:
            mock_async_task.return_value = "task-123"
            result = ingest_file(
                BytesIO(b"queued content"), filename="c.pdf", source=Document.Source.UPLOAD
            )

        mock_async_task.assert_called_once()
        from apps.documents.tasks import process_document_task

        args = mock_async_task.call_args.args
        self.assertEqual(args[0], process_document_task)
        self.assertEqual(args[1], result.document.id)


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
