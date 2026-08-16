import datetime

from django.core.exceptions import ValidationError
from django.test import TestCase

from .analysis import _KEY_FACT_FIELDS
from .letter_bindings import (
    MANUAL_ORIGIN_LABEL,
    LetterContext,
    SourceField,
    SourceNamespace,
    UnknownSourceError,
    build_context,
    missing_required_keys,
    resolve_bindings,
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
            phone="0871 123456",
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
        self.assertEqual(resolve_source("self.phone", context), "0871 123456")
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


class ResolveBindingsTests(TestCase):
    """Die eine Auflösung (#1138): je Platzhalter Wert **plus Herkunft plus
    Grund bei Fehlen** -- die Quelle, aus der sich Anzeige und Prüfung
    gemeinsam speisen.
    """

    def setUp(self):
        self.self_identity = Correspondent.objects.create(
            name="Perculasoft e.K.", address="Musterweg 1\n12345 Musterstadt", is_self=True
        )
        self.kontakt = Correspondent.objects.create(name="Finanzamt Musterstadt")
        self.vorgang = Vorgang.objects.create(name="Steuer 2026")
        self.document = Document.objects.create(
            title="Bescheid 2026",
            correspondent=self.kontakt,
            document_date=datetime.date(2026, 7, 1),
            key_facts={"amount": "1.234,00"},
        )
        self.document.vorgaenge.add(self.vorgang)

        self.template = LetterTemplate.objects.create(name="Widerspruch")
        self._placeholder("betreff", "document.title", 1)
        self._placeholder("empfaenger_adresse", "kontakt.address", 2, required=True)
        self._placeholder("frist", "document.keyfacts.due_date", 3)
        self._placeholder("aktenzeichen", "manual", 4)

    def _placeholder(self, key, source, order, **extra):
        return LetterTemplatePlaceholder.objects.create(
            template=self.template, key=key, source=source, order=order, **extra
        )

    def _bindings(self, **context_kwargs):
        context = build_context(document=self.document, **context_kwargs)
        return {binding.key: binding for binding in resolve_bindings(self.template, context)}

    def test_resolved_binding_carries_value_and_origin(self):
        binding = self._bindings()["betreff"]

        self.assertEqual(binding.value, "Bescheid 2026")
        self.assertEqual(binding.origin, "Dokument · Titel (Betreff)")
        self.assertFalse(binding.is_missing)
        self.assertEqual(binding.missing_reason, "")

    def test_a_gap_in_the_contact_names_the_contact_as_the_reason(self):
        binding = self._bindings()["empfaenger_adresse"]

        self.assertTrue(binding.is_missing)
        self.assertEqual(binding.missing_reason, "am Kontakt nicht hinterlegt")
        # Genau diese Lücke gehört dauerhaft an den Kontakt -- deshalb bietet
        # das Formular hier „am Kontakt speichern" an.
        self.assertEqual(binding.contact_field, "address")

    def test_a_gap_in_the_document_names_the_document_as_the_reason(self):
        binding = self._bindings()["frist"]

        self.assertEqual(binding.missing_reason, "im Dokument nicht gefunden")
        # Eine Key-Fact-Lücke gehört ins Dokument, nicht an den Kontakt.
        self.assertEqual(binding.contact_field, "")

    def test_a_missing_context_object_reads_differently_than_an_empty_field(self):
        """„Es gibt gar keinen Kontakt" und „am Kontakt nicht hinterlegt"
        führen zu verschiedenen Handlungen und dürfen nicht gleich klingen.
        """
        self.document.correspondent = None
        self.document.save(update_fields=["correspondent"])

        binding = self._bindings()["empfaenger_adresse"]

        self.assertEqual(
            binding.missing_reason,
            "kein Empfänger gewählt (das Bezugsdokument hat keinen Kontakt)",
        )

    def test_a_manually_entered_value_wins_over_its_binding(self):
        """Sonst stünde der nachgetragene Wert zwar im Formular, käme aber
        nie im Brief an -- die Bindung überschriebe ihn beim nächsten
        Auflösen wieder.
        """
        bindings = self._bindings(manual_values={"empfaenger_adresse": "Amtsgasse 2"})

        self.assertEqual(bindings["empfaenger_adresse"].value, "Amtsgasse 2")
        self.assertEqual(bindings["empfaenger_adresse"].origin, MANUAL_ORIGIN_LABEL)
        self.assertFalse(bindings["empfaenger_adresse"].is_missing)

    def test_an_unknown_source_stays_empty_with_a_reason(self):
        LetterTemplatePlaceholder.objects.filter(key="betreff").update(source="crm.kunde")

        binding = self._bindings()["betreff"]

        self.assertTrue(binding.is_missing)
        self.assertEqual(binding.missing_reason, "Quelle ist nicht (mehr) bekannt")

    def test_resolve_placeholders_is_the_narrow_view_on_the_same_answer(self):
        context = build_context(document=self.document)

        values = resolve_placeholders(self.template, context)
        bindings = resolve_bindings(self.template, context)

        self.assertEqual(values, {binding.key: binding.value for binding in bindings})

    def test_no_value_is_taken_from_another_document_of_the_vorgang(self):
        """Ein Betrag aus einem fremden Dokument in einem Schreiben mit
        Fristsetzung wäre ein gefährlicher Komfort (#1138).
        """
        sibling = Document.objects.create(
            title="Mahnung 2026",
            correspondent=self.kontakt,
            key_facts={"due_date": "2026-09-30"},
        )
        sibling.vorgaenge.add(self.vorgang)

        self.assertTrue(self._bindings()["frist"].is_missing)
