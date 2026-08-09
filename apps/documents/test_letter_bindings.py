import datetime

from django.core.exceptions import ValidationError
from django.test import TestCase

from .analysis import _KEY_FACT_FIELDS
from .letter_bindings import (
    LetterContext,
    SourceField,
    SourceNamespace,
    UnknownSourceError,
    build_context,
    missing_required_keys,
    resolve_placeholders,
    resolve_source,
    source_choices,
    source_label,
    split_source,
    validate_source,
)
from .models import Correspondent, Document, LetterTemplate, LetterTemplatePlaceholder, Vorgang


class SourceRegistryTests(TestCase):
    """Die Quellen-Registry (#1094) -- was die UI anbieten darf und was ein
    Platzhalter überhaupt binden kann.
    """

    def test_split_source_only_splits_at_the_first_dot(self):
        self.assertEqual(split_source("document.keyfacts.amount"), ("document", "keyfacts.amount"))
        self.assertEqual(split_source("heute"), ("heute", ""))

    def test_all_internal_source_groups_are_offered(self):
        groups = dict(source_choices())
        self.assertEqual(
            set(groups),
            {
                "Absender (eigene Identität)",
                "Empfänger (Kontakt)",
                "Dokument",
                "Vorgang",
                "Datum",
                "Manuelle Eingabe",
            },
        )

    def test_key_fact_sources_cover_the_analysis_fields(self):
        """Bindungs-Schicht und KI-Analyse (#1020) dürfen nicht auseinander
        laufen: jedes Key-Fact ist genau einmal als Quelle anwählbar.
        """
        offered = {
            value.removeprefix("document.keyfacts.")
            for _, options in source_choices()
            for value, _label in options
            if value.startswith("document.keyfacts.")
        }
        self.assertEqual(offered, set(_KEY_FACT_FIELDS))

    def test_validate_source_accepts_registered_sources(self):
        validate_source("self.name")
        validate_source("document.keyfacts.amount")
        validate_source("manual")

    def test_validate_source_rejects_unknown_namespace_and_field(self):
        with self.assertRaises(ValidationError):
            validate_source("crm.kunde")
        with self.assertRaises(ValidationError):
            validate_source("kontakt.geheimnis")

    def test_unknown_source_raises_on_resolve(self):
        with self.assertRaises(UnknownSourceError):
            resolve_source("crm.kunde", LetterContext())

    def test_a_new_namespace_needs_no_model_or_form_change(self):
        """Der Erweiterungspunkt für spätere externe Quellen: eine
        registrierte Namespace mit freiem Restpfad ist sofort gültig und
        auflösbar -- ohne Migration, ohne Formular-Anpassung.
        """
        from apps.documents import letter_bindings

        namespace = SourceNamespace(
            prefix="testapi",
            label="Test-API",
            fields=(SourceField("ping", "Ping", lambda context, key="": "pong"),),
            resolve=lambda path, context, key="": f"frei:{path}",
        )
        letter_bindings.register_source_namespace(namespace)
        try:
            validate_source("testapi.beliebiger.pfad")
            self.assertEqual(resolve_source("testapi.ping", LetterContext()), "pong")
            self.assertEqual(
                resolve_source("testapi.beliebiger.pfad", LetterContext()),
                "frei:beliebiger.pfad",
            )
        finally:
            letter_bindings._NAMESPACES.pop("testapi")


class ResolveSourceTests(TestCase):
    """Auflösung der Findus-internen Quellen gegen einen echten Kontext."""

    def setUp(self):
        self.self_identity = Correspondent.objects.create(
            name="Perculasoft e.K.",
            address="Musterweg 1\n12345 Musterstadt",
            is_self=True,
            vat_id="DE123456789",
            iban="DE02120300000000202051",
        )
        self.kontakt = Correspondent.objects.create(
            name="Finanzamt Musterstadt",
            address="Amtsgasse 2\n12345 Musterstadt",
            email="post@fa-musterstadt.de",
        )
        self.vorgang = Vorgang.objects.create(name="Steuer 2026")
        self.document = Document.objects.create(
            title="Bescheid 2026",
            correspondent=self.kontakt,
            document_date=datetime.date(2026, 7, 1),
            key_facts={"amount": "1.234,00", "due_date": "2026-08-31", "document_type": "Bescheid"},
        )
        self.document.vorgaenge.add(self.vorgang)

    def test_build_context_takes_sender_from_is_self_and_recipient_from_document(self):
        context = build_context(document=self.document)

        self.assertEqual(resolve_source("self.name", context), "Perculasoft e.K.")
        self.assertEqual(resolve_source("self.vat_id", context), "DE123456789")
        self.assertEqual(resolve_source("kontakt.name", context), "Finanzamt Musterstadt")
        self.assertEqual(
            resolve_source("kontakt.address", context), "Amtsgasse 2\n12345 Musterstadt"
        )
        self.assertEqual(resolve_source("vorgang.name", context), "Steuer 2026")
        self.assertEqual(resolve_source("vorgang.status", context), "Offen")

    def test_key_facts_and_dates_resolve(self):
        context = build_context(document=self.document, today=datetime.date(2026, 8, 9))

        self.assertEqual(resolve_source("document.keyfacts.amount", context), "1.234,00")
        self.assertEqual(resolve_source("document.keyfacts.due_date", context), "2026-08-31")
        self.assertEqual(resolve_source("document.title", context), "Bescheid 2026")
        self.assertEqual(resolve_source("document.date", context), "1. Juli 2026")
        self.assertEqual(resolve_source("heute", context), "9. August 2026")

    def test_manual_source_reads_the_value_under_the_placeholder_key(self):
        context = build_context(document=self.document, manual_values={"aktenzeichen": "A/42"})

        self.assertEqual(resolve_source("manual", context, "aktenzeichen"), "A/42")
        self.assertEqual(resolve_source("manual", context, "unbekannt"), "")

    def test_missing_objects_resolve_to_empty_string(self):
        """Ein Kontext ohne Dokument/Vorgang darf nicht knallen -- eine
        Vorlage kann an einem Vorgang ohne Bezugsdokument hängen.
        """
        context = build_context()

        self.assertEqual(resolve_source("kontakt.name", context), "")
        self.assertEqual(resolve_source("document.keyfacts.amount", context), "")
        self.assertEqual(resolve_source("vorgang.name", context), "")

    def test_resolve_placeholders_and_missing_required(self):
        template = LetterTemplate.objects.create(name="Antwort ans Finanzamt")
        LetterTemplatePlaceholder.objects.create(
            template=template, key="absender", source="self.name", order=1
        )
        LetterTemplatePlaceholder.objects.create(
            template=template, key="empfaenger", source="kontakt.address", order=2
        )
        LetterTemplatePlaceholder.objects.create(
            template=template, key="aktenzeichen", source="manual", required=True, order=3
        )

        context = build_context(document=self.document)
        values = resolve_placeholders(template, context)

        self.assertEqual(values["absender"], "Perculasoft e.K.")
        self.assertEqual(values["empfaenger"], "Amtsgasse 2\n12345 Musterstadt")
        self.assertEqual(values["aktenzeichen"], "")
        self.assertEqual(missing_required_keys(template, context), ["aktenzeichen"])

        filled = build_context(document=self.document, manual_values={"aktenzeichen": "A/42"})
        self.assertEqual(missing_required_keys(template, filled), [])

    def test_source_label_is_human_readable(self):
        self.assertEqual(source_label("document.keyfacts.amount"), "Dokument · Key-Fact: Betrag")
        self.assertEqual(source_label("manual"), "Manuelle Eingabe · Bei der Erzeugung ausfüllen")
        self.assertEqual(source_label("crm.kunde"), "crm.kunde")
