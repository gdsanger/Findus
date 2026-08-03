import importlib

from django.apps import apps
from django.test import TestCase

from .models import Document, Tag, TagSuggestion

_migration = importlib.import_module(
    "apps.documents.migrations.0012_repair_dimension_prefixed_tag_names"
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
