"""Die Ausführung der Scan-Korrektur (#1155): was mit Dokument, Chunks,
Teilen und dem Original tatsächlich passiert.

Zwei Fälle, zwei Versprechen: Drehen/Löschen behält die Identität des
Dokuments (deswegen wird das hier überhaupt gebaut), Aufteilen legt erst
alle Teile an und löscht dann das Original -- nie umgekehrt.
"""

import hashlib
import io
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

import apps.ingest.service as ingest_service
from apps.accounts.models import Department

from .models import (
    Chunk,
    Correspondent,
    Document,
    DocumentComment,
    DocumentPdfEditRun,
    Tag,
    Vorgang,
    link_documents,
)
from .pdf_edit import apply_pdf_edit_run
from .pdf_editing import PdfEditPlan, iter_edited_parts
from .test_extraction import _make_pdf

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-pdf-edit-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def tearDownModule():
    shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)


def _plan(**kwargs):
    return PdfEditPlan(
        rotations=kwargs.get("rotations") or {},
        deletions=tuple(kwargs.get("deletions") or ()),
        splits=tuple(kwargs.get("splits") or ()),
    )


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT, STORAGES=_LOCAL_STORAGES)
class PdfEditRunTestCase(TestCase):
    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)

        self.data = _make_pdf(["Seite eins", "Seite zwei", "Seite drei"])
        self.document = Document.objects.create(
            title="Sammelscan",
            owner=self.user,
            visibility=Document.Visibility.DEPARTMENT,
            processing_status=Document.ProcessingStatus.REVIEWED,
            action_status=Document.ActionStatus.OPEN,
            sphere=Document.Sphere.GESCHAEFTLICH,
            direction=Document.Direction.EINGANG,
            source=Document.Source.FOLDER,
            metadata={"mime_type": "application/pdf", "original_filename": "scan.pdf"},
            sha256=hashlib.sha256(self.data).hexdigest(),
        )
        self.document.original_file.save("scan.pdf", io.BytesIO(self.data), save=True)
        self.document.departments.add(self.department)

    def _run(self, plan):
        run = DocumentPdfEditRun.objects.create(
            document=self.document,
            document_title=self.document.title,
            created_by=self.user,
            plan=plan.as_dict(),
        )
        # Der Enqueue der Folgeverarbeitung gehört nicht in diesen Test --
        # geprüft wird, was am Archiv passiert, nicht die Queue.
        with patch("django_q.tasks.async_task") as async_task:
            self.async_task = async_task
            return apply_pdf_edit_run(run.id)


class DeleteAndRotateKeepsIdentityTests(PdfEditRunTestCase):
    """Der Regelfall bei Leerseiten: es entsteht **kein** neues Dokument."""

    def setUp(self):
        super().setUp()
        self.comment = DocumentComment.objects.create(
            document=self.document, body="Bitte prüfen", author=self.user
        )
        self.tag = Tag.objects.create(name="Rechnung")
        self.document.tags.add(self.tag)
        self.vorgang = Vorgang.objects.create(name="Umbau")
        self.document.vorgaenge.add(self.vorgang)
        self.document.correspondent = Correspondent.objects.create(name="Stadtwerke")
        self.document.save(update_fields=["correspondent"])

        self.other = Document.objects.create(title="Nachbar", owner=self.user)
        self.link = link_documents(self.document, self.other, created_by=self.user)[0]

        Chunk.objects.create(
            document=self.document,
            position=0,
            content="Text der geloeschten Seite",
            embedding=[0.0] * settings.FINDUS_EMBEDDING_DIMENSIONS,
            embedding_model="stub",
            embedding_model_version="1",
        )

    def test_document_keeps_pk_relations_and_action_status(self):
        run = self._run(_plan(deletions=(2,)))

        self.assertEqual(run.status, DocumentPdfEditRun.Status.READY)
        self.assertEqual(run.mode, DocumentPdfEditRun.Mode.EDIT)
        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.pk, self.document.pk)
        self.assertEqual(document.comments.count(), 1)
        self.assertEqual(list(document.tags.all()), [self.tag])
        self.assertEqual(list(document.vorgaenge.all()), [self.vorgang])
        self.assertEqual(document.correspondent, self.document.correspondent)
        self.assertEqual(document.action_status, Document.ActionStatus.OPEN)
        self.assertTrue(document.links_as_a.exists() or document.links_as_b.exists())

    def test_file_is_replaced_and_the_checksum_recomputed(self):
        self._run(_plan(deletions=(2,)))

        document = Document.objects.get(pk=self.document.pk)
        document.original_file.open("rb")
        try:
            new_data = document.original_file.read()
        finally:
            document.original_file.close()
        self.assertNotEqual(new_data, self.data)
        self.assertEqual(document.sha256, hashlib.sha256(new_data).hexdigest())
        self.assertEqual(document.metadata["page_count"], 2)

    def test_old_chunks_are_removed(self):
        """Sonst bliebe der Text gelöschter Seiten durchsuchbar -- ein
        Fehler, der niemandem auffällt, weil nichts kaputtgeht."""
        self._run(_plan(deletions=(2,)))

        self.assertEqual(Chunk.objects.filter(document=self.document).count(), 0)

    def test_processing_starts_over_from_pending(self):
        """Der Inhalt hat sich geändert -- es soll noch einmal jemand
        daraufschauen: aus `reviewed` wird die Kette neu durchlaufen."""
        self._run(_plan(deletions=(2,)))

        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.processing_status, Document.ProcessingStatus.PENDING)
        self.assertEqual(document.processing_error, "")
        enqueued = [call.args[0].__name__ for call in self.async_task.call_args_list]
        self.assertIn("extract_document_task", enqueued)

    def test_rotation_reaches_the_stored_file(self):
        self._run(_plan(rotations={1: 90}))

        from pypdf import PdfReader

        document = Document.objects.get(pk=self.document.pk)
        document.original_file.open("rb")
        try:
            reader = PdfReader(io.BytesIO(document.original_file.read()))
        finally:
            document.original_file.close()
        self.assertEqual(reader.pages[0].get("/Rotate", 0), 90)

    def test_a_duplicate_of_another_document_is_refused_without_changes(self):
        """Nicht still zusammenführen: welches der beiden gewinnt, hat
        niemand entschieden."""
        _pages, buffer = next(iter_edited_parts(io.BytesIO(self.data), _plan(deletions=(2,))))
        edited = buffer.read()
        twin = Document.objects.create(
            title="Schon da", owner=self.user, sha256=hashlib.sha256(edited).hexdigest()
        )

        run = self._run(_plan(deletions=(2,)))

        self.assertEqual(run.status, DocumentPdfEditRun.Status.FAILED)
        self.assertIn(twin.title, run.error)
        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.sha256, hashlib.sha256(self.data).hexdigest())
        self.assertEqual(document.processing_status, Document.ProcessingStatus.REVIEWED)
        self.assertEqual(Chunk.objects.filter(document=document).count(), 1)


class SplitTests(PdfEditRunTestCase):
    """Aufteilen: N Dokumente über den Ingest-Dienst, danach das Original
    weg."""

    def test_parts_are_created_and_the_original_is_deleted(self):
        run = self._run(_plan(splits=(3,)))

        self.assertEqual(run.status, DocumentPdfEditRun.Status.READY)
        self.assertEqual(run.mode, DocumentPdfEditRun.Mode.SPLIT)
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())
        parts = run.result["parts"]
        self.assertEqual([entry["pages"] for entry in parts], [[1, 2], [3]])
        self.assertEqual(Document.objects.filter(pk__in=[e["document_id"] for e in parts]).count(), 2)

    def test_parts_inherit_only_what_hangs_on_the_paper(self):
        self.document.correspondent = Correspondent.objects.create(name="Stadtwerke")
        self.document.document_date = self.document.created_at.date()
        self.document.save(update_fields=["correspondent", "document_date"])
        self.document.tags.add(Tag.objects.create(name="Rechnung"))
        self.document.vorgaenge.add(Vorgang.objects.create(name="Umbau"))

        run = self._run(_plan(splits=(3,)))

        part = Document.objects.get(pk=run.result["parts"][0]["document_id"])
        self.assertEqual(part.owner, self.user)
        self.assertEqual(part.visibility, self.document.visibility)
        self.assertEqual(list(part.departments.all()), [self.department])
        self.assertEqual(part.sphere, Document.Sphere.GESCHAEFTLICH)
        self.assertEqual(part.direction, Document.Direction.EINGANG)
        self.assertEqual(part.source, Document.Source.FOLDER)
        self.assertEqual(part.created_at, self.document.created_at)

        self.assertIsNone(part.correspondent)
        self.assertIsNone(part.document_date)
        self.assertEqual(list(part.tags.all()), [])
        self.assertEqual(list(part.vorgaenge.all()), [])

    def test_parts_are_not_linked_to_each_other(self):
        """Sie haben inhaltlich nichts miteinander zu tun -- sie lagen nur
        zufällig im selben Einzug."""
        run = self._run(_plan(splits=(3,)))

        first = Document.objects.get(pk=run.result["parts"][0]["document_id"])
        self.assertFalse(first.links_as_a.exists())
        self.assertFalse(first.links_as_b.exists())
        self.assertIsNone(first.parent_id)

    def test_parts_go_through_the_ingest_service(self):
        """Vertrag (CLAUDE.md, "Pipelines & Services"): Dedup, Ablage,
        Sichtbarkeit und Enqueue sind *ein* Vertrag -- kein eigener
        Ablagepfad daneben."""
        with patch("apps.ingest.service.ingest_file", wraps=ingest_service.ingest_file) as ingest:
            run = self._run(_plan(splits=(3,)))

        self.assertEqual(run.status, DocumentPdfEditRun.Status.READY)
        self.assertEqual(ingest.call_count, 2)

    def test_a_duplicate_part_is_reported_not_swallowed(self):
        parts = list(iter_edited_parts(io.BytesIO(self.data), _plan(splits=(3,))))
        existing_sha = hashlib.sha256(parts[1][1].read()).hexdigest()
        Document.objects.create(title="Schon vorhanden", owner=self.user, sha256=existing_sha)

        run = self._run(_plan(splits=(3,)))

        self.assertTrue(run.result["parts"][1]["duplicate"])
        self.assertFalse(run.result["parts"][0]["duplicate"])

    def test_a_failing_part_leaves_everything_unchanged(self):
        """Es darf kein Zustand entstehen, in dem das Original weg und die
        Teile unvollständig sind."""
        real_ingest = ingest_service.ingest_file
        calls = {"n": 0}

        def failing(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("Storage kaputt")
            return real_ingest(*args, **kwargs)

        with patch("apps.ingest.service.ingest_file", side_effect=failing):
            run = self._run(_plan(splits=(3,)))

        self.assertEqual(run.status, DocumentPdfEditRun.Status.FAILED)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())
        self.assertEqual(Document.objects.count(), 1)


class DoubleExecutionTests(PdfEditRunTestCase):
    """Ein zweites Mal ausgelöst darf keine doppelten Teile erzeugen."""

    def test_a_second_attempt_on_the_same_run_does_nothing(self):
        run = DocumentPdfEditRun.objects.create(
            document=self.document,
            document_title=self.document.title,
            created_by=self.user,
            plan=_plan(splits=(3,)).as_dict(),
        )
        with patch("django_q.tasks.async_task"):
            apply_pdf_edit_run(run.id)
            apply_pdf_edit_run(run.id)

        self.assertEqual(Document.objects.count(), 2)


class MissingDocumentTests(PdfEditRunTestCase):
    def test_a_run_without_its_document_fails_readably(self):
        run = DocumentPdfEditRun.objects.create(
            document=None, created_by=self.user, plan=_plan(deletions=(2,)).as_dict()
        )

        result = apply_pdf_edit_run(run.id)

        self.assertEqual(result.status, DocumentPdfEditRun.Status.FAILED)
        self.assertIn("existiert nicht mehr", result.error)


class ManualValuesSurviveTests(PdfEditRunTestCase):
    """Wer Datum oder Kontakt von Hand korrigiert hat, bekommt sie nach der
    Seitenbearbeitung nicht wieder überschrieben.

    Der Schutz selbst steckt in `analysis._apply_analysis` ("einmal
    befüllen, nie ungefragt überschreiben", Herkunftsvermerk
    `metadata["document_date_source"]`). Geprüft wird hier, dass die
    Bearbeitung ihn nicht nebenbei einkassiert -- sie schreibt an derselben
    `metadata` und könnte den Vermerk verlieren.
    """

    def test_a_manually_set_date_survives_the_edit_and_the_new_analysis(self):
        import datetime

        from apps.ai.providers.fake import FakeGenerationProvider

        from .analysis import analyze_document
        from .test_analysis import _VALID_REPLY

        self.document.document_date = datetime.date(2025, 12, 24)
        self.document.metadata = {**self.document.metadata, "document_date_source": "manuell"}
        self.document.save(update_fields=["document_date", "metadata"])

        self._run(_plan(deletions=(2,)))

        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.metadata["document_date_source"], "manuell")

        document.text_content = "Rechnung Inhalt"
        document.save(update_fields=["text_content"])
        analyzed = analyze_document(
            document.id,
            generation_provider=FakeGenerationProvider(
                model="fake-generate", version="1", reply=_VALID_REPLY
            ),
        )

        self.assertEqual(analyzed.document_date, datetime.date(2025, 12, 24))
