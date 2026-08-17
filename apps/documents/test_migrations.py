import datetime
import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Document, Tag, TagSuggestion

User = get_user_model()

_migration = importlib.import_module(
    "apps.documents.migrations.0012_repair_dimension_prefixed_tag_names"
)
_stuck_analyzing_migration = importlib.import_module(
    "apps.documents.migrations.0013_repair_stuck_analyzing_documents"
)
_document_date_source_migration = importlib.import_module(
    "apps.documents.migrations.0035_document_date_source_backfill"
)
_reviewed_backfill_migration = importlib.import_module(
    "apps.documents.migrations.0040_document_reviewed_backfill"
)


class RepairDimensionPrefixedTagNamesTests(TestCase):
    """Covers the one-off data migration for #1034 -- exercised directly

    against the real models (same shape as the historical ones the
    migration itself uses) rather than a full migration-state test, since
    the fields involved haven't changed since the tables were created.
    """

    def _run(self):
        _migration.repair_dimension_prefixed_names(apps, None)

    def test_splits_name_and_keeps_existing_dimension(self):
        tag = Tag.objects.create(name="Dokumenttyp:Eingangsrechnung", dimension="Dokumenttyp")

        self._run()

        tag.refresh_from_db()
        self.assertEqual(tag.name, "Eingangsrechnung")
        self.assertEqual(tag.dimension, "Dokumenttyp")

    def test_fills_empty_dimension_from_prefix(self):
        tag = Tag.objects.create(name="Dokumenttyp:Eingangsrechnung", dimension="")

        self._run()

        tag.refresh_from_db()
        self.assertEqual(tag.name, "Eingangsrechnung")
        self.assertEqual(tag.dimension, "Dokumenttyp")

    def test_merges_into_existing_correct_tag_and_keeps_document_link(self):
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")
        broken = Tag.objects.create(name="Dokumenttyp:Eingangsrechnung", dimension="Dokumenttyp")
        correct = Tag.objects.create(name="Eingangsrechnung", dimension="Dokumenttyp")
        document.tags.add(broken)

        self._run()

        self.assertFalse(Tag.objects.filter(pk=broken.pk).exists())
        self.assertIn(correct, document.tags.all())
        self.assertEqual(Tag.objects.filter(dimension="Dokumenttyp", name="Eingangsrechnung").count(), 1)

    def test_leaves_names_without_colon_untouched(self):
        tag = Tag.objects.create(name="Dringend", dimension="")

        self._run()

        tag.refresh_from_db()
        self.assertEqual(tag.name, "Dringend")
        self.assertEqual(tag.dimension, "")

    def test_repairs_tag_suggestion_too(self):
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")
        suggestion = TagSuggestion.objects.create(
            document=document, name="Dokumenttyp:Eingangsrechnung", dimension="Dokumenttyp"
        )

        self._run()

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.name, "Eingangsrechnung")
        self.assertEqual(suggestion.dimension, "Dokumenttyp")


class RepairStuckAnalyzingDocumentsTests(TestCase):
    """Covers the one-off data migration for #1035 -- `analyze_documents`

    (#1029) used to leave `processing_status="analyzing"` set forever,
    both on success and on failure.
    """

    def _run(self):
        _stuck_analyzing_migration.repair_stuck_analyzing_documents(apps, None)

    def test_successful_analysis_moves_to_ready(self):
        document = Document.objects.create(
            title="A",
            text_content="hello world",
            summary="Kurze Zusammenfassung.",
            processing_status=Document.ProcessingStatus.ANALYZING,
        )

        self._run()

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)

    def test_failed_analysis_moves_to_failed(self):
        document = Document.objects.create(
            title="A",
            text_content="hello world",
            metadata={"analysis_error": "boom"},
            processing_status=Document.ProcessingStatus.ANALYZING,
        )

        self._run()

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)

    def test_other_statuses_are_left_untouched(self):
        ready = Document.objects.create(
            title="A", text_content="hello world", processing_status=Document.ProcessingStatus.READY
        )
        embedding = Document.objects.create(
            title="B", text_content="hello world", processing_status=Document.ProcessingStatus.EMBEDDING
        )

        self._run()

        ready.refresh_from_db()
        embedding.refresh_from_db()
        self.assertEqual(ready.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(embedding.processing_status, Document.ProcessingStatus.EMBEDDING)


class StampAiDocumentDatesTests(TestCase):
    """Covers the one-off data migration for #1141 -- ohne den

    Herkunftsvermerk waere der komplette Bestand vor einer erneuten
    KI-Analyse gesperrt, ausgerechnet die falsch datierten Kontoauszuege
    also unkorrigierbar. Gestempelt wird nur, was nachweislich die Analyse
    gesetzt hat; alles andere bleibt geschuetzt.
    """

    def _run(self):
        _document_date_source_migration.stamp_ai_document_dates(apps, None)

    def test_stamps_a_date_matching_the_key_facts(self):
        document = Document.objects.create(
            title="Kontoauszug",
            document_date=datetime.date(2026, 8, 15),
            key_facts={"document_date": "2026-08-15"},
        )

        self._run()

        document.refresh_from_db()
        self.assertEqual(document.metadata["document_date_source"], "modell")

    def test_leaves_a_hand_corrected_date_unmarked(self):
        document = Document.objects.create(
            title="Kontoauszug",
            document_date=datetime.date(2023, 11, 30),
            key_facts={"document_date": "2026-08-15"},
        )

        self._run()

        document.refresh_from_db()
        self.assertNotIn("document_date_source", document.metadata)

    def test_leaves_a_date_without_key_facts_unmarked(self):
        """z. B. `document_date` aus dem `Date`-Header eines EML-Imports."""
        document = Document.objects.create(
            title="Mail", document_date=datetime.date(2025, 7, 1)
        )

        self._run()

        document.refresh_from_db()
        self.assertNotIn("document_date_source", document.metadata)

    def test_keeps_an_existing_source_marker(self):
        document = Document.objects.create(
            title="Kontoauszug",
            document_date=datetime.date(2026, 8, 15),
            key_facts={"document_date": "2026-08-15"},
            metadata={"document_date_source": "manuell"},
        )

        self._run()

        document.refresh_from_db()
        self.assertEqual(document.metadata["document_date_source"], "manuell")

    def test_does_not_touch_updated_at(self):
        """Ein Wartungslauf soll den Bestand nicht als "gerade bearbeitet"

        erscheinen lassen -- dieselbe Ruecksicht wie bei den
        `long_summary_*`-Saves (siehe CLAUDE.md).
        """
        document = Document.objects.create(
            title="Kontoauszug",
            document_date=datetime.date(2026, 8, 15),
            key_facts={"document_date": "2026-08-15"},
        )
        before = Document.objects.get(pk=document.pk).updated_at

        self._run()

        self.assertEqual(Document.objects.get(pk=document.pk).updated_at, before)


class MarkReadyAsReviewedTests(TestCase):
    """Covers the one-off data migration for #1153 -- ohne diesen Backfill

    stuenden alle rund 400 im Bestand bereits fertig verarbeiteten
    Dokumente am ersten Tag in der neuen Liste "Zu pruefen".
    """

    def _run(self):
        _reviewed_backfill_migration.mark_ready_as_reviewed(apps, None)

    def _reverse(self):
        _reviewed_backfill_migration.mark_reviewed_as_ready(apps, None)

    def test_lifts_ready_documents_to_reviewed(self):
        document = Document.objects.create(
            title="Bestand", processing_status=Document.ProcessingStatus.READY
        )

        self._run()

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.REVIEWED)

    def test_leaves_other_statuses_untouched(self):
        failed = Document.objects.create(
            title="Fehlgeschlagen", processing_status=Document.ProcessingStatus.FAILED
        )
        pending = Document.objects.create(
            title="Ausstehend", processing_status=Document.ProcessingStatus.PENDING
        )

        self._run()

        failed.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(failed.processing_status, Document.ProcessingStatus.FAILED)
        self.assertEqual(pending.processing_status, Document.ProcessingStatus.PENDING)

    def test_does_not_invent_review_provenance(self):
        """Es hat niemand tatsaechlich geprueft -- ein erfundener Zeitstempel

        waere irrefuehrend.
        """
        document = Document.objects.create(
            title="Bestand", processing_status=Document.ProcessingStatus.READY
        )

        self._run()

        document.refresh_from_db()
        self.assertIsNone(document.processing_reviewed_at)
        self.assertIsNone(document.processing_reviewed_by)

    def test_reverse_sets_ready_and_clears_provenance(self):
        user = User.objects.create_user(username="alice", password="x")
        document = Document.objects.create(
            title="Bestand",
            processing_status=Document.ProcessingStatus.REVIEWED,
            processing_reviewed_by=user,
        )

        self._reverse()

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.READY)
        self.assertIsNone(document.processing_reviewed_at)
        self.assertIsNone(document.processing_reviewed_by)
