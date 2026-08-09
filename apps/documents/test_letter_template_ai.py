import json

from django.test import TestCase, override_settings

from apps.ai.providers.fake import FakeGenerationProvider

from .letter_template_ai import (
    LetterTemplateDraftError,
    draft_letter_template,
    normalize_placeholder_key,
)

_INSTRUCTIONS = "## Ton\nSachlich-bestimmt.\n\n## Aufbau\n1. Bezug\n\n## Vorgaben\nFrist nennen."


def _reply(*, placeholders=None, instructions=_INSTRUCTIONS, **overrides):
    payload = {
        "anleitung": instructions,
        "platzhalter": placeholders
        if placeholders is not None
        else [
            {
                "key": "aktenzeichen",
                "label": "Aktenzeichen",
                "quelle": "manual",
                "pflicht": True,
            },
            {
                "key": "empfaenger_adresse",
                "label": "Empfängeradresse",
                "quelle": "kontakt.address",
                "pflicht": False,
            },
        ],
        "name": "Widerspruch Inkasso",
        "kategorie": "Widerspruch",
        "beschreibung": "Für den Widerspruch gegen eine Inkasso-Forderung.",
    }
    payload.update(overrides)
    return json.dumps(payload)


class LetterTemplateDraftTests(TestCase):
    """Der „skill-creator" für Brief-Vorlagen (#1097): ein generate()-Call,
    aus dem Anleitung + Platzhalter-Vorschläge werden -- gespeichert wird
    hier nichts.
    """

    def test_draft_returns_instructions_placeholders_and_meta(self):
        provider = FakeGenerationProvider(reply=_reply())

        draft = draft_letter_template(
            intent="Widerspruch gegen eine Inkasso-Forderung, mit Fristsetzung",
            generation_provider=provider,
        )

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(draft.instructions, _INSTRUCTIONS)
        self.assertEqual(draft.name, "Widerspruch Inkasso")
        self.assertEqual(draft.category, "Widerspruch")
        self.assertEqual(
            draft.description, "Für den Widerspruch gegen eine Inkasso-Forderung."
        )
        self.assertEqual(
            [(p.key, p.source, p.required) for p in draft.placeholders],
            [
                ("aktenzeichen", "manual", True),
                ("empfaenger_adresse", "kontakt.address", False),
            ],
        )
        self.assertEqual(draft.unknown_sources, ())

    def test_prompt_lists_the_registered_sources_and_the_intent(self):
        """Der Quellen-Katalog kommt aus der Registry, nicht aus einer
        zweiten Liste -- sonst schlägt die KI Quellen vor, die es nicht
        (mehr) gibt.
        """
        provider = FakeGenerationProvider(reply=_reply())

        draft_letter_template(
            intent="Kündigung eines Vertrags",
            category="Kündigung",
            tone="höflich, aber bestimmt",
            generation_provider=provider,
        )

        prompt = "\n".join(message.content for message in provider.calls[0])
        self.assertIn("document.keyfacts.amount", prompt)
        self.assertIn("kontakt.address", prompt)
        self.assertIn("self.name", prompt)
        self.assertIn("Kündigung eines Vertrags", prompt)
        self.assertIn("höflich, aber bestimmt", prompt)
        # Die Platzhalter-Schreibweise darf die .format()-Vorlage überleben.
        self.assertIn("{{schluessel}}", prompt)

    @override_settings(FINDUS_LETTER_TEMPLATE_DRAFT_MAX_OUTPUT_TOKENS=1234)
    def test_call_uses_its_own_output_budget(self):
        provider = FakeGenerationProvider(reply=_reply())

        draft_letter_template(intent="Mahnung", generation_provider=provider)

        self.assertEqual(provider.max_tokens_calls, [1234])

    def test_unknown_source_falls_back_to_manual_and_is_reported(self):
        provider = FakeGenerationProvider(
            reply=_reply(
                placeholders=[
                    {"key": "aktenzeichen", "quelle": "document.aktenzeichen"},
                ]
            )
        )

        draft = draft_letter_template(intent="Widerspruch", generation_provider=provider)

        self.assertEqual(draft.placeholders[0].source, "manual")
        self.assertEqual(draft.unknown_sources, ("document.aktenzeichen",))

    def test_keys_are_normalized_and_duplicates_dropped(self):
        provider = FakeGenerationProvider(
            reply=_reply(
                placeholders=[
                    {"key": "{{Empfänger-Adresse}}", "quelle": "kontakt.address"},
                    {"key": "empfaenger_adresse", "quelle": "manual"},
                    {"key": "123", "quelle": "manual"},
                    {"key": "", "quelle": "manual"},
                ]
            )
        )

        draft = draft_letter_template(intent="Widerspruch", generation_provider=provider)

        self.assertEqual([p.key for p in draft.placeholders], ["empfaenger_adresse"])

    @override_settings(FINDUS_LETTER_TEMPLATE_DRAFT_MAX_PLACEHOLDERS=2)
    def test_placeholder_list_is_capped(self):
        provider = FakeGenerationProvider(
            reply=_reply(
                placeholders=[
                    {"key": f"feld_{index}", "quelle": "manual"} for index in range(5)
                ]
            )
        )

        draft = draft_letter_template(intent="Widerspruch", generation_provider=provider)

        self.assertEqual([p.key for p in draft.placeholders], ["feld_0", "feld_1"])

    def test_missing_instructions_is_an_error_not_an_empty_draft(self):
        """Formal vollständig, inhaltlich leer: der Nutzer hat einen Knopf
        gedrückt und muss erfahren, dass nichts entstanden ist.
        """
        provider = FakeGenerationProvider(reply=_reply(instructions="   "))

        with self.assertRaises(LetterTemplateDraftError):
            draft_letter_template(intent="Widerspruch", generation_provider=provider)

    def test_unparseable_reply_raises_draft_error(self):
        provider = FakeGenerationProvider(reply="Kein JSON, nur Prosa.")

        with self.assertRaises(LetterTemplateDraftError):
            draft_letter_template(intent="Widerspruch", generation_provider=provider)

        # Ein Retry (#1028), dann ein sichtbarer Fehler -- kein leerer Entwurf.
        self.assertEqual(len(provider.calls), 2)

    def test_truncated_reply_is_not_patched_into_a_half_draft(self):
        """Muster #1096: eine am Output-Limit abgeschnittene Antwort ist ein
        Fehler mit Retry, keine Vorlage ohne Platzhalter.
        """
        provider = FakeGenerationProvider(
            reply='{"anleitung": "## Ton\\nSachlich', truncated=True
        )

        with self.assertRaises(LetterTemplateDraftError):
            draft_letter_template(intent="Widerspruch", generation_provider=provider)

    def test_provider_failure_becomes_a_draft_error(self):
        class BrokenProvider:
            def generate(self, messages, **kwargs):
                raise RuntimeError("Provider nicht erreichbar")

        with self.assertRaises(LetterTemplateDraftError):
            draft_letter_template(intent="Widerspruch", generation_provider=BrokenProvider())


class NormalizePlaceholderKeyTests(TestCase):
    def test_normalizes_model_freetext_into_a_valid_key(self):
        cases = {
            "Aktenzeichen": "aktenzeichen",
            "{{Empfänger-Adresse}}": "empfaenger_adresse",
            "Betrag (EUR)": "betrag_eur",
            "  frist  ": "frist",
            "ÜBERWEISUNG": "ueberweisung",
            "1. Anlage": "anlage",
            "123": "",
            "": "",
            None: "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_placeholder_key(value), expected)
