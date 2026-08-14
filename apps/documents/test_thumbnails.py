import io
import shutil
import tempfile
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.accounts.models import Department

from .models import Document
from .test_extraction import _make_pdf
from .thumbnails import (
    generate_thumbnail,
    generate_thumbnail_for_document,
    render_thumbnail,
)

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-thumbnail-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _png_bytes(size=(800, 600), color="red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_document(*, filename, data, mime_type) -> Document:
    document = Document.objects.create(
        title="Testdokument",
        metadata={"mime_type": mime_type, "original_filename": filename},
    )
    document.original_file.save(filename, io.BytesIO(data), save=True)
    return document


class RenderThumbnailTests(TestCase):
    """The pure rendering layer (#1123): PDF first page and images become a
    small WebP, everything else returns None -- the whitelist that drives
    "which documents get a thumbnail".
    """

    def test_pdf_first_page_renders_to_webp(self):
        rendered = render_thumbnail(_make_pdf(["Seite eins"]), "application/pdf")

        self.assertIsNotNone(rendered)
        image_bytes, ext = rendered
        self.assertEqual(ext, "webp")
        image = Image.open(io.BytesIO(image_bytes))
        self.assertEqual(image.format, "WEBP")
        # Longest edge capped at the configured max (bewusst klein).
        self.assertLessEqual(max(image.size), 400)

    def test_image_is_scaled_down(self):
        rendered = render_thumbnail(_png_bytes(size=(1600, 1200)), "image/png")

        self.assertIsNotNone(rendered)
        image_bytes, ext = rendered
        self.assertEqual(ext, "webp")
        image = Image.open(io.BytesIO(image_bytes))
        self.assertEqual(max(image.size), 400)

    def test_unsupported_type_returns_none(self):
        self.assertIsNone(render_thumbnail(b"PK\x03\x04", "application/zip"))
        self.assertIsNone(
            render_thumbnail(
                b"x",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        )


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class GenerateThumbnailTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def test_pdf_document_gets_a_thumbnail(self):
        document = _make_document(
            filename="rechnung.pdf", data=_make_pdf(["Hallo"]), mime_type="application/pdf"
        )

        created = generate_thumbnail(document)

        self.assertTrue(created)
        document.refresh_from_db()
        self.assertTrue(document.thumbnail)
        self.assertTrue(document.thumbnail.name.endswith(".webp"))

    def test_image_document_gets_a_thumbnail(self):
        document = _make_document(
            filename="scan.png", data=_png_bytes(), mime_type="image/png"
        )

        self.assertTrue(generate_thumbnail(document))
        document.refresh_from_db()
        self.assertTrue(document.thumbnail)

    def test_unsupported_document_gets_no_thumbnail(self):
        document = _make_document(
            filename="anhang.zip", data=b"PK\x03\x04", mime_type="application/zip"
        )

        self.assertFalse(generate_thumbnail(document))
        document.refresh_from_db()
        self.assertFalse(document.thumbnail)

    def test_without_original_file_returns_false(self):
        document = Document.objects.create(
            title="Leer", metadata={"mime_type": "application/pdf"}
        )

        self.assertFalse(generate_thumbnail(document))

    def test_existing_thumbnail_is_kept_without_force(self):
        document = _make_document(
            filename="rechnung.pdf", data=_make_pdf(["Hallo"]), mime_type="application/pdf"
        )
        generate_thumbnail(document)
        first_name = document.thumbnail.name

        # A second run is a no-op (idempotent backfill).
        self.assertFalse(generate_thumbnail(document))
        document.refresh_from_db()
        self.assertEqual(document.thumbnail.name, first_name)

    def test_force_regenerates_and_removes_the_old_file(self):
        document = _make_document(
            filename="rechnung.pdf", data=_make_pdf(["Hallo"]), mime_type="application/pdf"
        )
        generate_thumbnail(document)
        old_name = document.thumbnail.name
        old_storage = document.thumbnail.storage

        self.assertTrue(generate_thumbnail(document, force=True))
        document.refresh_from_db()
        # New file written, previous one cleaned up from storage.
        self.assertNotEqual(document.thumbnail.name, old_name)
        self.assertFalse(old_storage.exists(old_name))

    def test_render_error_is_swallowed_by_the_pipeline_entry_point(self):
        document = _make_document(
            filename="rechnung.pdf", data=_make_pdf(["Hallo"]), mime_type="application/pdf"
        )

        with patch(
            "apps.documents.thumbnails.render_thumbnail",
            side_effect=RuntimeError("kaputt"),
        ):
            # Fault tolerance (#1020 principle): must not raise, must not set
            # a thumbnail -- the document is still perfectly usable.
            generate_thumbnail_for_document(document.id)

        document.refresh_from_db()
        self.assertFalse(document.thumbnail)

    def test_pipeline_entry_point_tolerates_missing_document(self):
        # No exception for a document that was deleted between enqueue and run.
        generate_thumbnail_for_document(999999)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DocumentThumbnailViewTests(TestCase):
    """The auth-gated delivery view (#1123) -- same `visible_to` scoping as
    `document_original_preview` (#1036): a foreign document is a 404, not a
    403 leak, and the file never comes from the public storage URL.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)
        self.user_b = User.objects.create_user(username="bob", password="x")
        self.user_b.departments.add(self.dept_b)

        self.doc = _make_document(
            filename="rechnung.pdf", data=_make_pdf(["Hallo"]), mime_type="application/pdf"
        )
        self.doc.visibility = Document.Visibility.DEPARTMENT
        self.doc.save(update_fields=["visibility"])
        self.doc.departments.add(self.dept_a)
        generate_thumbnail(self.doc)

    def test_serves_thumbnail_with_caching_and_nosniff(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:thumbnail", args=[self.doc.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/webp")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("max-age=", response["Cache-Control"])
        self.assertTrue(b"".join(response.streaming_content))

    def test_404_when_no_thumbnail(self):
        self.doc.thumbnail.delete(save=True)

        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:thumbnail", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_scoped_by_visibility_returns_404_for_foreign_document(self):
        self.client.force_login(self.user_b)
        response = self.client.get(reverse("documents:thumbnail", args=[self.doc.id]))

        self.assertEqual(response.status_code, 404)

    def test_requires_login(self):
        response = self.client.get(reverse("documents:thumbnail", args=[self.doc.id]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class GenerateThumbnailsCommandTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def test_generates_for_the_stock_and_skips_unsupported(self):
        pdf = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )
        zip_doc = _make_document(
            filename="b.zip", data=b"PK\x03\x04", mime_type="application/zip"
        )

        out = StringIO()
        call_command("generate_thumbnails", stdout=out)

        pdf.refresh_from_db()
        zip_doc.refresh_from_db()
        self.assertTrue(pdf.thumbnail)
        self.assertFalse(zip_doc.thumbnail)
        self.assertIn("1 erzeugt", out.getvalue())
        self.assertIn("1 uebersprungen", out.getvalue())

    def test_is_idempotent_and_skips_existing(self):
        doc = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )
        call_command("generate_thumbnails")
        doc.refresh_from_db()
        first_name = doc.thumbnail.name

        out = StringIO()
        call_command("generate_thumbnails", stdout=out)

        doc.refresh_from_db()
        self.assertEqual(doc.thumbnail.name, first_name)
        self.assertIn("0 erzeugt", out.getvalue())

    def test_force_regenerates_existing(self):
        doc = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )
        call_command("generate_thumbnails")
        doc.refresh_from_db()
        old_name = doc.thumbnail.name

        out = StringIO()
        call_command("generate_thumbnails", force=True, stdout=out)

        doc.refresh_from_db()
        self.assertNotEqual(doc.thumbnail.name, old_name)
        self.assertIn("1 erzeugt", out.getvalue())

    def test_document_ids_restricts_target_set(self):
        doc_a = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )
        doc_b = _make_document(
            filename="b.pdf", data=_make_pdf(["B"]), mime_type="application/pdf"
        )

        call_command("generate_thumbnails", document_ids=[doc_a.id])

        doc_a.refresh_from_db()
        doc_b.refresh_from_db()
        self.assertTrue(doc_a.thumbnail)
        self.assertFalse(doc_b.thumbnail)

    def test_queue_option_enqueues_instead_of_rendering_inline(self):
        doc = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )

        with patch("django_q.tasks.async_task") as mock_async_task:
            call_command("generate_thumbnails", queue=True)

        mock_async_task.assert_called_once()
        doc.refresh_from_db()
        self.assertFalse(doc.thumbnail)

    def test_no_matching_documents_reports_and_does_not_error(self):
        out = StringIO()
        call_command("generate_thumbnails", stdout=out)

        self.assertIn("Keine Dokumente", out.getvalue())


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class ExtractPipelineThumbnailTests(TestCase):
    """The ingest step (#1123): `extract_document_task` renders the thumbnail
    after extraction, and a render failure must never break the pipeline.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def test_thumbnail_is_generated_after_extraction(self):
        from .tasks import extract_document_task

        document = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )

        with patch("apps.documents.tasks.extract_document"), patch(
            "django_q.tasks.async_task"
        ):
            extract_document_task(document.id)

        document.refresh_from_db()
        self.assertTrue(document.thumbnail)

    def test_render_failure_still_enqueues_next_stage(self):
        from .tasks import analyze_document_task, extract_document_task

        document = _make_document(
            filename="a.pdf", data=_make_pdf(["A"]), mime_type="application/pdf"
        )

        with patch("apps.documents.tasks.extract_document"), patch(
            "apps.documents.thumbnails.render_thumbnail",
            side_effect=RuntimeError("kaputt"),
        ), patch("django_q.tasks.async_task") as mock_async_task:
            extract_document_task(document.id)

        # Pipeline keeps moving despite the thumbnail failure.
        mock_async_task.assert_called_once_with(analyze_document_task, document.id)
        document.refresh_from_db()
        self.assertFalse(document.thumbnail)
