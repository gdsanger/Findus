import datetime
import json
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.accounts.models import Department
from apps.ai.providers.fake import FakeGenerationProvider

from .letter_generation import (
    apply_placeholder_values,
    build_messages,
    generate_letter_draft,
)
from .models import (
    Correspondent,
    Document,
    LetterDraft,
    LetterTemplate,
    LetterTemplatePlaceholder,
    Vorgang,
    default_letter_layout,
)

User = get_user_model()

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-letter-generation-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

_BODY = (
    "Sehr geehrte Damen und Herren,\n\n"
    "gegen den Bescheid vom 01.08.2026 lege ich Widerspruch ein.\n\n"
    "Bitte bestätigen Sie den Eingang."
)


def _reply(*, subject="Widerspruch gegen Bescheid 4711", body=_BODY, **overrides):
    payload = {"betreff": subject, "text": body}
    payload.update(overrides)
    return json.dumps(payload)


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class LetterGenerationTests(TestCase):
    """Die KI-Formulierung eines Briefs (#1095): ein generate()-Call, aus
    Anleitung + Bindungen + Kontext des beantworteten Dokuments.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)

        self.self_contact = Correspondent.objects.create(
            name="Anna Beispiel", address="Hauptstr. 1\n10115 Berlin", is_self=True
        )
        self.recipient = Correspondent.objects.create(
            name="Amt für Alles", address="Amtsweg 2\n10117 Berlin"
        )
        self.vorgang = Vorgang.objects.create(name="Bescheid 4711")

        self.source_document = Document.objects.create(
            title="Bescheid vom 01.08.2026",
            correspondent=self.recipient,
            summary="Das Amt fordert 250 EUR nach, Frist bis 31.08.2026.",
            key_facts={"amount": "250", "currency": "EUR", "due_date": "2026-08-31"},
            document_date=datetime.date(2026, 8, 1),
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.source_document.departments.add(self.dept)
        self.source_document.vorgaenge.add(self.vorgang)

        self.template = LetterTemplate.objects.create(
            name="Widerspruch",
            description="Für Widersprüche gegen Bescheide.",
            instructions="## Ton\nSachlich-bestimmt.\n\n## Aufbau\n1. Bezug\n2. Widerspruch",
            signature="Anna Beispiel",
        )
        LetterTemplatePlaceholder.objects.create(
            template=self.template,
            key="aktenzeichen",
            label="Aktenzeichen",
            source="manual",
            required=True,
            order=1,
        )
        LetterTemplatePlaceholder.objects.create(
            template=self.template,
            key="empfaenger_adresse",
            label="Empfängeradresse",
            source="kontakt.address",
            order=2,
        )

    def _draft(self, **overrides):
        fields = {
            "template": self.template,
            "template_name": self.template.name,
            "source_document": self.source_document,
            "vorgang": self.vorgang,
            "recipient": self.recipient,
            "sender": self.self_contact,
            "status": LetterDraft.Status.RUNNING,
            "letter_date": datetime.date(2026, 8, 9),
            "sender_block": "Anna Beispiel\nHauptstr. 1\n10115 Berlin",
            "recipient_block": "Amt für Alles\nAmtsweg 2\n10117 Berlin",
            "signature": self.template.signature,
            "layout": default_letter_layout(),
            "manual_values": {"aktenzeichen": "AZ-2026-4711"},
            "owner": self.user,
        }
        fields.update(overrides)
        draft = LetterDraft.objects.create(**fields)
        draft.departments.add(self.dept)
        return draft

    def test_successful_run_fills_text_and_renders_both_formats(self):
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(draft.status, LetterDraft.Status.READY)
        self.assertEqual(draft.subject, "Widerspruch gegen Bescheid 4711")
        self.assertIn("lege ich Widerspruch ein", draft.body_text)
        self.assertEqual(draft.error, "")
        self.assertEqual(draft.ai_model, "fake-generate")
        self.assertTrue(draft.has_files)

    def test_nothing_is_filed_by_generating(self):
        """Review-first: der Lauf erzeugt einen Entwurf, kein Dokument."""
        draft = self._draft()
        before = Document.objects.count()

        generate_letter_draft(
            draft.pk, self.user.pk, generation_provider=FakeGenerationProvider(reply=_reply())
        )

        draft.refresh_from_db()
        self.assertEqual(Document.objects.count(), before)
        self.assertIsNone(draft.document_id)

    def test_prompt_carries_instructions_bindings_and_document_context(self):
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        prompt = provider.calls[0][-1].content
        self.assertIn("Sachlich-bestimmt", prompt)
        self.assertIn("AZ-2026-4711", prompt)
        self.assertIn("Amtsweg 2", prompt)
        self.assertIn("Das Amt fordert 250 EUR nach", prompt)
        self.assertIn("due_date=2026-08-31", prompt)
        self.assertIn("Bescheid 4711", prompt)

    def test_prompt_marks_unknown_values_instead_of_omitting_them(self):
        """Ein weggelassener Platzhalter lädt das Modell zum Erfinden ein --
        „(nicht bekannt)" sagt ihm, dass der Wert wirklich fehlt.
        """
        draft = self._draft(manual_values={})
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        prompt = provider.calls[0][-1].content
        self.assertIn("{{aktenzeichen}} (Aktenzeichen) [PFLICHT]: (nicht bekannt)", prompt)

    def test_bindings_are_resolved_and_stored_on_the_draft(self):
        draft = self._draft()

        generate_letter_draft(
            draft.pk, self.user.pk, generation_provider=FakeGenerationProvider(reply=_reply())
        )

        draft.refresh_from_db()
        self.assertEqual(
            draft.placeholder_values,
            {"aktenzeichen": "AZ-2026-4711", "empfaenger_adresse": "Amtsweg 2\n10117 Berlin"},
        )

    def test_source_document_outside_the_users_scope_is_left_out(self):
        """`visible_to`-gescoped (#1052): ein Dokument, das der Auslösende
        nicht öffnen darf, geht nicht in den Prompt -- der Brief liefe
        sonst als Umweg um das Sichtbarkeitsmodell herum.
        """
        other_dept = Department.objects.create(name="Dept B")
        self.source_document.departments.set([other_dept])
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        prompt = provider.calls[0][-1].content
        self.assertNotIn("Das Amt fordert 250 EUR nach", prompt)
        self.assertNotIn("Beantwortetes Dokument", prompt)

    def test_text_excerpt_stands_in_for_a_missing_summary(self):
        self.source_document.summary = ""
        self.source_document.text_content = "Sie schulden uns 250 EUR bis zum 31.08.2026."
        self.source_document.save(update_fields=["summary", "text_content"])
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        self.assertIn("Sie schulden uns 250 EUR", provider.calls[0][-1].content)

    def test_closing_echoed_by_the_model_is_stripped(self):
        """Grußformel und Signatur setzt das Layout -- schreibt das Modell
        sie trotzdem, stünden sie doppelt im Brief.
        """
        draft = self._draft()
        provider = FakeGenerationProvider(
            reply=_reply(body=f"{_BODY}\n\nMit freundlichen Grüßen\n\nAnna Beispiel")
        )

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertNotIn("Mit freundlichen Grüßen", draft.body_text)
        self.assertTrue(draft.body_text.endswith("Bitte bestätigen Sie den Eingang."))

    def test_placeholder_syntax_left_in_the_text_is_substituted(self):
        draft = self._draft()
        provider = FakeGenerationProvider(
            reply=_reply(body="Sehr geehrte Damen und Herren,\n\nAktenzeichen {{aktenzeichen}}.")
        )

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertIn("Aktenzeichen AZ-2026-4711.", draft.body_text)

    def test_provider_failure_is_visible_on_the_draft(self):
        """Kein stiller Fehlschlag: der Nutzer hat auf einen Knopf gedrückt
        und bekommt eine Meldung -- und darf danach selbst weiterschreiben.
        """
        draft = self._draft()
        provider = FakeGenerationProvider(reply="kein JSON, nur Prosa")

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertEqual(draft.status, LetterDraft.Status.FAILED)
        self.assertTrue(draft.error)
        self.assertTrue(draft.is_editable)

    def test_reply_without_body_is_a_failure_not_an_empty_letter(self):
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply(body="   "))

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertEqual(draft.status, LetterDraft.Status.FAILED)
        self.assertIn("keinen Brieftext", draft.error)

    def test_truncated_reply_is_retried_with_a_bigger_budget(self):
        """#1096: eine am Output-Limit abgeschnittene Antwort wird nicht
        geflickt, sondern mit doppeltem Budget wiederholt.
        """
        draft = self._draft()
        provider = FakeGenerationProvider(
            replies=['{"betreff": "Widerspruch", "text": "Sehr geehrte', _reply()],
            truncated=[True, False],
        )

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(draft.status, LetterDraft.Status.READY)
        self.assertEqual(
            provider.max_tokens_calls[1], provider.max_tokens_calls[0] * 2
        )

    def test_template_without_instructions_still_produces_a_prompt(self):
        self.template.instructions = ""
        self.template.save(update_fields=["instructions"])
        draft = self._draft()
        provider = FakeGenerationProvider(reply=_reply())

        generate_letter_draft(draft.pk, self.user.pk, generation_provider=provider)

        draft.refresh_from_db()
        self.assertEqual(draft.status, LetterDraft.Status.READY)
        self.assertIn("keine Anleitung hinterlegt", provider.calls[0][-1].content)

    def test_system_prompt_forbids_repeating_the_layout(self):
        draft = self._draft()

        system = build_messages(draft, source_document=self.source_document)[0].content

        self.assertIn("Empfaengeradresse", system)
        self.assertIn("Grussformel", system)
        self.assertIn("ENTWURF", system)


class PlaceholderSubstitutionTests(TestCase):
    def test_known_keys_are_replaced_and_unknown_ones_stay_visible(self):
        text = "AZ {{aktenzeichen}}, Betrag {{betrag}}, Rest {{unbekannt}}."

        result = apply_placeholder_values(text, {"aktenzeichen": "AZ-1", "betrag": ""})

        self.assertEqual(result, "AZ AZ-1, Betrag {{betrag}}, Rest {{unbekannt}}.")
