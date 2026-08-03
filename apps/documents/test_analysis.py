import json

from django.test import TestCase, override_settings

from apps.ai.providers.fake import FakeGenerationProvider

from .analysis import analyze_document
from .models import Correspondent, Document, SuggestionStatus, Tag, Vorgang

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
