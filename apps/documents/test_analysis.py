import datetime
import json

from django.test import TestCase, override_settings

from apps.ai.providers import GenerationResult
from apps.ai.providers.fake import FakeGenerationProvider

from .analysis import analyze_document
from .models import (
    Correspondent,
    Document,
    DocumentReference,
    SuggestionStatus,
    Tag,
    Vorgang,
)
from .references import set_reference

_REPLY_WITH_RECIPIENT = json.dumps(
    {
        "title": "Rechnung Nr. 42 von Acme GmbH",
        "summary": "Acme GmbH stellt eine Rechnung ueber 129,90 EUR, faellig am 2026-09-01.",
        "key_facts": {
            "sender_name": "Acme GmbH",
            "sender_email": "buchhaltung@acme.example",
            "sender_vat_id": "DE999999999",
            "recipient_name": "Christian Angermeier",
            "recipient_vat_id": "DE123456789",
            "document_date": "2026-08-01",
            "document_type": "Rechnung",
            "amount": "129.90",
            "currency": "EUR",
            "due_date": "2026-09-01",
        },
        "tag_suggestions": [],
        "vorgang_suggestions": [],
    }
)

_VALID_REPLY = json.dumps(
    {
        "title": "Rechnung Nr. 42 von Acme GmbH",
        "summary": "Acme GmbH stellt eine Rechnung ueber 129,90 EUR, faellig am 2026-09-01.",
        "key_facts": {
            "sender_name": "Acme GmbH",
            "sender_email": "buchhaltung@acme.example",
            "document_date": "2026-08-01",
            "document_type": "Rechnung",
            "amount": "129.90",
            "currency": "EUR",
            "due_date": "2026-09-01",
        },
        "tag_suggestions": [
            {"name": "Rechnung", "dimension": "Thema", "confidence": 0.9},
            {"name": "Dringend", "dimension": "", "confidence": 0.4},
        ],
        "vorgang_suggestions": [{"name": "Buchhaltung 2026", "confidence": 0.7}],
    }
)


# Missing comma between two key_facts entries -- the exact shape of the
# LLM slip that #1028 fixes ("Expecting ',' delimiter: line 18 column 6").
_SLIGHTLY_BROKEN_REPLY = _VALID_REPLY.replace(
    '"sender_name": "Acme GmbH", "sender_email"',
    '"sender_name": "Acme GmbH" "sender_email"',
)
assert _SLIGHTLY_BROKEN_REPLY != _VALID_REPLY


class _SequencedGenerationProvider:
    """Returns one reply per call, holding on the last one once exhausted --
    for asserting the retry path (#1028) without a real provider.
    """

    name = "fake"

    def __init__(self, replies, *, model="fake-generate", version="1"):
        self._replies = list(replies)
        self.model = model
        self.version = version
        self.calls = []

    def generate(self, messages, *, stream=False):
        messages = list(messages)
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return GenerationResult(text=self._replies[index], model=self.model, version=self.version)


class AnalyzeDocumentTests(TestCase):
    def _provider(self, reply=_VALID_REPLY):
        return FakeGenerationProvider(model="fake-generate", version="1", reply=reply)

    def test_sets_title_summary_and_key_facts(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.title, "Rechnung Nr. 42 von Acme GmbH")
        self.assertIn("129,90 EUR", result.summary)
        self.assertEqual(result.key_facts["document_type"], "Rechnung")
        self.assertEqual(result.key_facts["amount"], "129.90")
        self.assertEqual(result.key_facts["ai_model"], "fake-generate")
        self.assertEqual(result.key_facts["ai_model_version"], "1")
        self.assertEqual(result.processing_status, Document.ProcessingStatus.ANALYZING)

    def test_nul_bytes_in_model_reply_are_stripped_before_save(self):
        """#1061: a NUL byte anywhere in the parsed reply (title, summary,

        key_facts, tag/vorgang suggestions) must not reach `save()` --
        Postgres `text` columns reject it outright (`psycopg.DataError`).
        """
        reply = json.dumps(
            {
                "title": "Rech\x00nung Nr. 42",
                "summary": "Betrag 129,90\x00 EUR faellig am 2026-09-01.",
                "key_facts": {"sender_name": "Acme\x00 GmbH", "document_type": "Rech\x00nung"},
                "tag_suggestions": [{"name": "Dring\x00end", "confidence": 0.9}],
                "vorgang_suggestions": [{"name": "Buchhaltung\x00 2026", "confidence": 0.7}],
            }
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider(reply))

        self.assertEqual(result.title, "Rechnung Nr. 42")
        self.assertNotIn("\x00", result.summary)
        self.assertEqual(result.key_facts["sender_name"], "Acme GmbH")
        self.assertEqual(document.tag_suggestions.get().name, "Dringend")
        self.assertEqual(document.vorgang_suggestions.get().name, "Buchhaltung 2026")

    def test_creates_correspondent_from_sender_email(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertIsNotNone(result.correspondent)
        self.assertEqual(result.correspondent.email, "buchhaltung@acme.example")
        self.assertEqual(result.correspondent.name, "Acme GmbH")

    def test_matches_existing_correspondent_by_email(self):
        existing = Correspondent.objects.create(
            name="Acme GmbH (Buchhaltung)", email="buchhaltung@acme.example"
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.correspondent, existing)
        self.assertEqual(Correspondent.objects.count(), 1)

    def test_does_not_overwrite_existing_correspondent(self):
        existing = Correspondent.objects.create(name="Manuell zugeordnet")
        document = Document.objects.create(
            title="rechnung.pdf", text_content="Rechnung Inhalt", correspondent=existing
        )

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.correspondent, existing)

    def test_sets_document_date_from_key_facts(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.document_date, datetime.date(2026, 8, 1))

    def test_does_not_overwrite_existing_document_date(self):
        """#1085: a manually corrected (or previously KI-set) `document_date`

        must survive a re-run -- otherwise "Analyse erneut ausfuehren"
        would silently discard a user's correction.
        """
        document = Document.objects.create(
            title="rechnung.pdf",
            text_content="Rechnung Inhalt",
            document_date=datetime.date(2025, 12, 24),
        )

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.document_date, datetime.date(2025, 12, 24))

    def test_malformed_document_date_is_ignored(self):
        reply = json.dumps(
            {
                "title": "Doc",
                "summary": "",
                "key_facts": {"document_date": "irgendwann letzten Monat"},
                "tag_suggestions": [],
                "vorgang_suggestions": [],
            }
        )
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider(reply))

        self.assertIsNone(result.document_date)

    def test_creates_tag_and_vorgang_suggestions(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        analyze_document(document.id, generation_provider=self._provider())

        tag_names = set(document.tag_suggestions.values_list("name", flat=True))
        self.assertEqual(tag_names, {"Rechnung", "Dringend"})
        self.assertTrue(
            document.tag_suggestions.filter(status=SuggestionStatus.PENDING).exists()
        )
        vorgang_suggestion = document.vorgang_suggestions.get()
        self.assertEqual(vorgang_suggestion.name, "Buchhaltung 2026")
        self.assertEqual(vorgang_suggestion.confidence, 0.7)

    def test_dimension_prefixed_tag_name_is_split_when_dimension_also_set(self):
        """#1034: the KI sometimes fills both `name` ("Dimension:Wert")

        and `dimension`, which would otherwise double the dimension in
        every display built from the two fields.
        """
        reply = json.dumps(
            {
                "title": "Doc",
                "summary": "",
                "key_facts": {},
                "tag_suggestions": [
                    {
                        "name": "Dokumenttyp:Eingangsrechnung",
                        "dimension": "Dokumenttyp",
                        "confidence": 0.9,
                    }
                ],
                "vorgang_suggestions": [],
            }
        )
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=self._provider(reply))

        suggestion = document.tag_suggestions.get()
        self.assertEqual(suggestion.name, "Eingangsrechnung")
        self.assertEqual(suggestion.dimension, "Dokumenttyp")

    def test_dimension_prefixed_tag_name_fills_empty_dimension(self):
        """Same bug, but `dimension` came back empty -- the prefix is the

        only place the dimension is known, so it must still end up split
        into the `dimension` field instead of staying in `name`.
        """
        reply = json.dumps(
            {
                "title": "Doc",
                "summary": "",
                "key_facts": {},
                "tag_suggestions": [
                    {"name": "Dokumenttyp:Eingangsrechnung", "confidence": 0.9}
                ],
                "vorgang_suggestions": [],
            }
        )
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=self._provider(reply))

        suggestion = document.tag_suggestions.get()
        self.assertEqual(suggestion.name, "Eingangsrechnung")
        self.assertEqual(suggestion.dimension, "Dokumenttyp")

    def test_clamps_out_of_range_confidence(self):
        reply = json.dumps(
            {
                "title": "Doc",
                "summary": "",
                "key_facts": {},
                "tag_suggestions": [{"name": "Rechnung", "confidence": 5}],
                "vorgang_suggestions": [{"name": "Vorgang X", "confidence": -1}],
            }
        )
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=self._provider(reply))

        self.assertEqual(document.tag_suggestions.get().confidence, 1.0)
        self.assertEqual(document.vorgang_suggestions.get().confidence, 0.0)

    def test_reanalysis_replaces_only_pending_suggestions(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")
        analyze_document(document.id, generation_provider=self._provider())

        accepted = document.tag_suggestions.get(name="Rechnung")
        accepted.status = SuggestionStatus.ACCEPTED
        accepted.save(update_fields=["status"])
        rejected = document.tag_suggestions.get(name="Dringend")
        rejected.status = SuggestionStatus.REJECTED
        rejected.save(update_fields=["status"])

        analyze_document(document.id, generation_provider=self._provider())

        # Re-analysis must not resurrect a decision already made -- only a
        # genuinely new suggestion name would create a fresh row.
        self.assertEqual(document.tag_suggestions.count(), 2)
        self.assertFalse(
            document.tag_suggestions.filter(status=SuggestionStatus.PENDING).exists()
        )

    def test_invalid_json_response_does_not_raise_and_records_error(self):
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(
            document.id, generation_provider=self._provider("not json at all")
        )

        self.assertEqual(result.processing_status, Document.ProcessingStatus.ANALYZING)
        self.assertIn("analysis_error", result.metadata)
        self.assertEqual(result.title, "doc.pdf")
        self.assertEqual(result.summary, "")

    def test_provider_failure_does_not_raise_and_records_error(self):
        class _RaisingProvider:
            def generate(self, messages, *, stream=False):
                raise RuntimeError("provider down")

        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(document.id, generation_provider=_RaisingProvider())

        self.assertIn("provider down", result.metadata["analysis_error"])
        self.assertEqual(Tag.objects.count(), 0)

    def test_response_wrapped_in_prose_is_still_parsed(self):
        wrapped = f"Hier ist die Analyse:\n```json\n{_VALID_REPLY}\n```\nEnde."
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider(wrapped))

        self.assertEqual(result.title, "Rechnung Nr. 42 von Acme GmbH")

    def test_slightly_malformed_json_is_repaired_without_a_retry_call(self):
        """The exact bug from #1028: a missing comma used to blow up
        `json.loads` and leave `summary`/`key_facts` empty with an
        `analysis_error`. The repair pass must fix it inline, so this
        still costs exactly one `generate()` call.
        """
        provider = self._provider(_SLIGHTLY_BROKEN_REPLY)
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(document.id, generation_provider=provider)

        self.assertIn("129,90 EUR", result.summary)
        self.assertEqual(result.key_facts["document_type"], "Rechnung")
        self.assertNotIn("analysis_error", result.metadata)
        self.assertEqual(len(provider.calls), 1)

    def test_json_unrecoverable_by_repair_is_fixed_by_retry(self):
        """When the repair pass can't turn the first reply into an
        object at all, one retry with a stricter instruction gives the
        model a second chance before `analysis_error` is recorded.
        """
        provider = _SequencedGenerationProvider(["Sicher, hier ist die Analyse.", _VALID_REPLY])
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        result = analyze_document(document.id, generation_provider=provider)

        self.assertIn("129,90 EUR", result.summary)
        self.assertNotIn("analysis_error", result.metadata)
        self.assertEqual(len(provider.calls), 2)

    @override_settings(FINDUS_ANALYSIS_MAX_CHARS=10)
    def test_prompt_truncates_text_content_to_max_chars(self):
        provider = self._provider()
        document = Document.objects.create(
            title="doc.pdf", text_content="x" * 1000
        )

        analyze_document(document.id, generation_provider=provider)

        user_message = provider.calls[0][1]
        self.assertIn("x" * 10, user_message.content)
        self.assertNotIn("x" * 11, user_message.content)

    def test_prompt_lists_existing_tags_and_vorgaenge_for_reuse(self):
        Tag.objects.create(name="Steuer", dimension="Thema")
        Vorgang.objects.create(name="Steuererklaerung 2026")
        provider = self._provider()
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=provider)

        user_message = provider.calls[0][1]
        self.assertIn("Thema:Steuer", user_message.content)
        self.assertIn("Steuererklaerung 2026", user_message.content)

    def test_prompt_lists_existing_contacts_with_is_self_marker(self):
        """#1048: bestehende Kontakte muessen inkl. `is_self`-Markierung in

        den Prompt-Kontext, damit das Modell Aussteller/Empfaenger dagegen
        abgleichen kann.
        """
        Correspondent.objects.create(
            name="Software Entwicklung Angermeier", is_self=True, vat_id="DE123456789"
        )
        Correspondent.objects.create(name="Karl Ebner Vermietungs GmbH & Co. KG")
        provider = self._provider()
        document = Document.objects.create(title="doc.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=provider)

        user_message = provider.calls[0][1]
        self.assertIn(
            "Software Entwicklung Angermeier [ICH SELBST] (DE123456789)",
            user_message.content,
        )
        self.assertIn("Karl Ebner Vermietungs GmbH & Co. KG", user_message.content)
        self.assertNotIn(
            "Karl Ebner Vermietungs GmbH & Co. KG [ICH SELBST]", user_message.content
        )


class DocumentDirectionTests(TestCase):
    """Eingang/Ausgang/Intern-Ableitung aus `Correspondent.is_self` (#1030)."""

    def _provider(self, reply=_REPLY_WITH_RECIPIENT):
        return FakeGenerationProvider(model="fake-generate", version="1", reply=reply)

    def test_recipient_is_self_sets_eingang(self):
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE123456789"
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.EINGANG)

    def test_sender_is_self_sets_ausgang(self):
        Correspondent.objects.create(name="Acme GmbH", is_self=True, vat_id="DE999999999")
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.AUSGANG)

    def test_both_self_sets_intern(self):
        Correspondent.objects.create(name="Acme GmbH", is_self=True, vat_id="DE999999999")
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE123456789"
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.INTERN)

    def test_recipient_match_does_not_create_a_new_correspondent(self):
        """Dedup rule: the recipient side is a read-only lookup -- a miss

        must never invent a `Correspondent`, only the sender side may.
        """
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        analyze_document(document.id, generation_provider=self._provider())

        # Only the sender ("Acme GmbH") was auto-created -- the recipient
        # ("Christian Angermeier") found no is_self match and was not
        # turned into a spurious foreign correspondent.
        self.assertEqual(Correspondent.objects.count(), 1)
        self.assertEqual(Correspondent.objects.get().name, "Acme GmbH")

    def test_does_not_overwrite_a_manually_set_direction(self):
        """Direction stays user-overridable: once decided, a re-analysis

        must not clobber it back to a KI-derived value.
        """
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE123456789"
        )
        document = Document.objects.create(
            title="rechnung.pdf",
            text_content="Rechnung Inhalt",
            direction=Document.Direction.AUSGANG,
        )

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.AUSGANG)

    def test_no_self_match_leaves_direction_unbekannt(self):
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.UNBEKANNT)

    def test_ausgang_sets_recipient_not_self_as_correspondent(self):
        """RE0041-Fall (#1048): Aussteller ist `is_self` -> Kontakt darf

        NICHT die eigene Identitaet sein, sondern muss die Gegenstelle
        (Empfaenger) sein.
        """
        Correspondent.objects.create(name="Acme GmbH", is_self=True, vat_id="DE999999999")
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.AUSGANG)
        self.assertIsNotNone(result.correspondent)
        self.assertFalse(result.correspondent.is_self)
        self.assertEqual(result.correspondent.name, "Christian Angermeier")

    def test_model_direction_used_when_self_identity_wording_differs(self):
        """RE0041-Fall (#1048): die eigene Identitaet ist im Dokument leicht

        anders geschrieben ("Software Entwicklung Angermeier / Christian
        Angermeier") als der gespeicherte `is_self`-Datensatz ("Christian
        Angermeier") -- kein Treffer per USt-IdNr/IBAN/Name moeglich. Das
        Modell kennt aus dem Prompt-Kontext trotzdem beide Parteien und
        gibt "direction" explizit aus; dieser Wert muss dann greifen statt
        bei "unbekannt" zu bleiben, und der Kontakt muss die Gegenstelle
        sein (nie die eigene Identitaet).
        """
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE111111111"
        )
        reply = json.dumps(
            {
                "title": "Rechnung RE0041",
                "summary": (
                    "Software Entwicklung Angermeier stellt der Karl Ebner "
                    "Vermietung eine Rechnung."
                ),
                "key_facts": {
                    "sender_name": "Software Entwicklung Angermeier",
                    "recipient_name": "Karl Ebner Vermietungs GmbH & Co. KG",
                    "document_type": "Rechnung",
                },
                "direction": "ausgang",
                "tag_suggestions": [
                    {"name": "Ausgangsrechnung", "dimension": "Dokumenttyp", "confidence": 0.9}
                ],
                "vorgang_suggestions": [],
            }
        )
        document = Document.objects.create(title="RE0041.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider(reply))

        self.assertEqual(result.direction, Document.Direction.AUSGANG)
        self.assertIsNotNone(result.correspondent)
        self.assertEqual(result.correspondent.name, "Karl Ebner Vermietungs GmbH & Co. KG")
        self.assertFalse(result.correspondent.is_self)

    def test_anthropic_case_db_match_overrides_model_direction(self):
        """Anthropic-Fall (#1048): eine US-Firma ohne USt-IdNr (Reverse

        Charge) verwirrt das Modell offenbar so sehr, dass es selbst
        "intern" ausgibt -- der Empfaenger (ich) matcht aber per USt-IdNr
        zweifelsfrei auf die eigene `is_self`-Identitaet, waehrend der
        Absender gegen keinen bestehenden Kontakt matcht. Der DB-Abgleich
        muss dann Vorrang vor der (falschen) Modell-Angabe haben, und der
        Kontakt muss die Gegenstelle (Anthropic, PBC) sein, nie ich selbst.
        """
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE123456789"
        )
        reply = json.dumps(
            {
                "title": "Anthropic, PBC - Zahlungsbeleg ueber 90,00 EUR",
                "summary": "Anthropic, PBC stellt einen Zahlungsbeleg ueber 90,00 EUR aus.",
                "key_facts": {
                    "sender_name": "Anthropic, PBC",
                    "recipient_name": "Christian Angermeier",
                    "recipient_vat_id": "DE123456789",
                    "document_type": "Zahlungsbeleg",
                    "amount": "90.00",
                    "currency": "EUR",
                },
                "direction": "intern",
                "tag_suggestions": [],
                "vorgang_suggestions": [],
            }
        )
        document = Document.objects.create(title="anthropic.pdf", text_content="Zahlungsbeleg Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider(reply))

        self.assertEqual(result.direction, Document.Direction.EINGANG)
        self.assertIsNotNone(result.correspondent)
        self.assertEqual(result.correspondent.name, "Anthropic, PBC")
        self.assertFalse(result.correspondent.is_self)

    def test_both_self_leaves_correspondent_unset(self):
        """Sicherheitsnetz (#1048): bei "intern" (beide Seiten `is_self`)

        gibt es keine Gegenstelle -- niemals eine `is_self`-Identitaet als
        Kontakt setzen, lieber `correspondent` leer lassen.
        """
        Correspondent.objects.create(name="Acme GmbH", is_self=True, vat_id="DE999999999")
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, vat_id="DE123456789"
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.direction, Document.Direction.INTERN)
        self.assertIsNone(result.correspondent)


class DocumentSphereTests(TestCase):
    """Geschäftlich/privat-Ableitung aus der eigenen Seite (#1112) --
    spiegelbildlich zu `DocumentDirectionTests`.
    """

    def _provider(self, reply=_REPLY_WITH_RECIPIENT):
        return FakeGenerationProvider(model="fake-generate", version="1", reply=reply)

    def _provider_with_sphere(self, sphere):
        reply = json.dumps(
            {
                "title": "Schreiben",
                "summary": "Ein Schreiben.",
                "key_facts": {"sender_name": "Acme GmbH"},
                "sphere": sphere,
                "tag_suggestions": [],
                "vorgang_suggestions": [],
            }
        )
        return FakeGenerationProvider(model="fake-generate", version="1", reply=reply)

    def test_own_business_self_identity_sets_geschaeftlich(self):
        # Empfänger-Seite ist "meine Firma" (is_own_business) -> geschäftlich.
        Correspondent.objects.create(
            name="Christian Angermeier",
            is_self=True,
            is_own_business=True,
            vat_id="DE123456789",
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.sphere, Document.Sphere.GESCHAEFTLICH)
        # Der KI-Vorschlag ist als solcher markiert -- daran hängt das Badge.
        self.assertEqual(result.metadata.get("sphere_source"), "ki")

    def test_private_self_identity_sets_privat(self):
        # Eigene Seite matcht per Name (keine USt-IdNr, kein Gewerbe) -> privat.
        Correspondent.objects.create(
            name="Christian Angermeier", is_self=True, is_own_business=False
        )
        document = Document.objects.create(title="rechnung.pdf", text_content="Rechnung Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.sphere, Document.Sphere.PRIVAT)

    def test_no_self_match_falls_back_to_model_sphere(self):
        document = Document.objects.create(title="brief.pdf", text_content="Brief Inhalt")

        result = analyze_document(
            document.id,
            generation_provider=self._provider_with_sphere("geschaeftlich"),
        )

        self.assertEqual(result.sphere, Document.Sphere.GESCHAEFTLICH)
        self.assertEqual(result.metadata.get("sphere_source"), "ki")

    def test_no_self_match_and_no_model_sphere_stays_unbekannt(self):
        document = Document.objects.create(title="brief.pdf", text_content="Brief Inhalt")

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.sphere, Document.Sphere.UNBEKANNT)
        self.assertNotIn("sphere_source", result.metadata)

    def test_does_not_overwrite_a_manually_set_sphere(self):
        """Sphäre bleibt vom Nutzer überschreibbar: eine bereits gesetzte
        Wahl darf eine erneute Analyse nicht auf einen KI-Wert zurückwerfen.
        """
        Correspondent.objects.create(
            name="Christian Angermeier",
            is_self=True,
            is_own_business=True,
            vat_id="DE123456789",
        )
        document = Document.objects.create(
            title="rechnung.pdf",
            text_content="Rechnung Inhalt",
            sphere=Document.Sphere.PRIVAT,
        )

        result = analyze_document(document.id, generation_provider=self._provider())

        self.assertEqual(result.sphere, Document.Sphere.PRIVAT)
        self.assertNotIn("sphere_source", result.metadata)


class DocumentReferenceExtractionTests(TestCase):
    """Kennungen aus derselben Analyse-Antwort (#1099) -- eine Liste, kein

    Einzelfeld, und ein Re-Run darf die Handarbeit des Nutzers nicht
    wegräumen.
    """

    def _provider(self, references):
        reply = json.dumps(
            {
                "title": "Mahnung",
                "summary": "Mahnung zur Forderung.",
                "key_facts": {},
                "references": references,
                "tag_suggestions": [],
                "vorgang_suggestions": [],
            }
        )
        return FakeGenerationProvider(model="fake-generate", version="1", reply=reply)

    def test_extracts_multiple_typed_references(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [
                    {"type": "aktenzeichen", "value": "Az. 123/45", "role": "deren"},
                    {"type": "forderungsnummer", "value": "F-9988", "role": ""},
                ]
            ),
        )

        references = list(document.references.order_by("type"))
        self.assertEqual(len(references), 2)
        aktenzeichen = document.references.get(type=DocumentReference.Type.AKTENZEICHEN)
        self.assertEqual(aktenzeichen.value_raw, "Az. 123/45")
        self.assertEqual(aktenzeichen.value_normalized, "123/45")
        self.assertEqual(aktenzeichen.role, DocumentReference.Role.THEIRS)
        self.assertEqual(aktenzeichen.source, DocumentReference.Source.AI)

    def test_unknown_type_and_role_are_not_guessed(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [{"type": "mondzahl", "value": "42", "role": "vielleicht"}]
            ),
        )

        reference = document.references.get()
        self.assertEqual(reference.type, DocumentReference.Type.SONSTIGE)
        self.assertEqual(reference.role, "")

    def test_valueless_and_malformed_entries_are_skipped(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [
                    {"type": "aktenzeichen", "value": "  "},
                    "kein objekt",
                    {"type": "aktenzeichen", "value": "123/45"},
                ]
            ),
        )

        self.assertEqual(document.references.count(), 1)

    def test_duplicate_reference_in_one_reply_is_stored_once(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [
                    {"type": "aktenzeichen", "value": "Az. 123/45"},
                    {"type": "aktenzeichen", "value": "123 / 45"},
                ]
            ),
        )

        self.assertEqual(document.references.count(), 1)

    @override_settings(FINDUS_ANALYSIS_MAX_REFERENCES=2)
    def test_runaway_reply_is_capped(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [{"type": "sonstige", "value": f"NR-{index}"} for index in range(10)]
            ),
        )

        self.assertEqual(document.references.count(), 2)

    def test_reanalysis_replaces_ai_references_but_keeps_manual_ones(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")
        analyze_document(
            document.id,
            generation_provider=self._provider(
                [{"type": "aktenzeichen", "value": "123/45"}]
            ),
        )
        set_reference(
            document,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [{"type": "aktenzeichen", "value": "999/99"}]
            ),
        )

        values = set(document.references.values_list("value_normalized", flat=True))
        self.assertEqual(values, {"999/99", "4711"})

    def test_ai_does_not_duplicate_a_manually_added_reference(self):
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")
        set_reference(
            document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        analyze_document(
            document.id,
            generation_provider=self._provider(
                [{"type": "aktenzeichen", "value": "Az. 123 / 45"}]
            ),
        )

        reference = document.references.get()
        self.assertEqual(reference.source, DocumentReference.Source.MANUAL)

    def test_prompt_names_the_available_reference_types(self):
        provider = self._provider([])
        document = Document.objects.create(title="mahnung.pdf", text_content="Inhalt")

        analyze_document(document.id, generation_provider=provider)

        system_message = provider.calls[0][0]
        self.assertIn('"references"', system_message.content)
        self.assertIn("aktenzeichen", system_message.content)
        self.assertIn("forderungsnummer", system_message.content)
