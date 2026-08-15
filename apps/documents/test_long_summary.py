import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django_q.exceptions import TimeoutException

from apps.accounts.models import Department
from apps.ai.providers.fake import FakeGenerationProvider

from .long_summary import (
    build_document_long_summary_messages,
    build_vorgang_long_summary_messages,
    document_long_summary_is_stale,
    generate_document_long_summary,
    generate_vorgang_long_summary,
    vorgang_long_summary_is_stale,
    vorgang_long_summary_new_document_count,
)
from .models import Correspondent, Document, DocumentComment, Vorgang

User = get_user_model()


def _reply(text: str) -> str:
    return json.dumps({"zusammenfassung": text})


class DocumentLongSummaryPromptTests(TestCase):
    """Covers `build_document_long_summary_messages` (#1135) in Isolation --
    kein Modellaufruf, nur was tatsaechlich in den Prompt geht.
    """

    def setUp(self):
        self.correspondent = Correspondent.objects.create(name="Acme GmbH")
        self.document = Document.objects.create(
            title="Rechnung 42",
            correspondent=self.correspondent,
            text_content="Sehr geehrte Damen und Herren, hiermit stellen wir Ihnen 129,90 EUR in Rechnung.",
            summary="Acme stellt 129,90 EUR in Rechnung.",
            key_facts={"amount": "129.90", "ai_model": "fake", "ai_model_version": "1"},
        )

    def _user_message(self, document=None):
        messages = build_document_long_summary_messages(document or self.document)
        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[1].role, "user")
        return messages[1].content

    def test_includes_full_text_summary_and_key_facts(self):
        content = self._user_message()
        self.assertIn("Rechnung.", content)
        self.assertIn("hiermit stellen wir Ihnen 129,90 EUR", content)
        self.assertIn("Acme stellt 129,90 EUR in Rechnung.", content)
        self.assertIn("amount=129.90", content)
        # KI-Provenienz-Felder sind kein fachlicher Key-Fact.
        self.assertNotIn("ai_model", content)

    def test_truncates_very_long_text_and_marks_it(self):
        long_document = Document.objects.create(
            title="Vertrag", text_content="Absatz. " * 20
        )
        with override_settings(FINDUS_LONG_SUMMARY_MAX_CHARS=50):
            content = self._user_message(long_document)
        self.assertIn("[... Text gekuerzt, das Dokument ist laenger ...]", content)

    def test_no_comments_section_without_comments(self):
        content = self._user_message()
        self.assertNotIn("Notizen des Nutzers", content)

    def test_comments_appear_as_own_section_not_as_document_content(self):
        DocumentComment.objects.create(
            document=self.document, body="Mit Kunde telefoniert, ist erledigt."
        )
        content = self._user_message()
        self.assertIn("Notizen des Nutzers (chronologisch", content)
        self.assertIn("Mit Kunde telefoniert, ist erledigt.", content)

    def test_open_follow_up_is_labelled(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Bitte nachfassen.",
            follow_up_date="2026-01-01",
        )
        content = self._user_message()
        self.assertIn("Wiedervorlage 01.01.2026", content)
        self.assertIn("ueberfaellig", content)


class DocumentLongSummaryGenerationTests(TestCase):
    """Covers Speicherung, erneutes Erzeugen, Veralterung und Fehlerfall
    fuer die Dokumentebene (#1135). Der Prompt-Inhalt und der Ablauf werden
    geprueft, nicht die Modellantwort selbst.
    """

    def setUp(self):
        self.document = Document.objects.create(
            title="Rechnung 42", text_content="Rechnungsinhalt " * 20
        )

    def test_generates_and_stores_result_with_provenance(self):
        provider = FakeGenerationProvider(
            model="claude-sonnet-5", version="1", reply=_reply("Ein langer Text.")
        )

        document = generate_document_long_summary(
            self.document.pk, generation_provider=provider
        )

        self.assertEqual(len(provider.calls), 1, "genau ein generate()-Call pro Erzeugung")
        self.assertEqual(document.long_summary_status, Document.LongSummaryStatus.READY)
        self.assertEqual(document.long_summary, "Ein langer Text.")
        self.assertIsNotNone(document.long_summary_generated_at)
        self.assertEqual(document.long_summary_ai_model, "claude-sonnet-5")
        self.assertEqual(document.long_summary_ai_model_version, "1")
        self.assertIn("basis_updated_at", document.long_summary_based_on)

    def test_regenerate_replaces_the_stored_text(self):
        generate_document_long_summary(
            self.document.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Erste Fassung.")),
        )
        document = generate_document_long_summary(
            self.document.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Zweite Fassung.")),
        )

        self.assertEqual(document.long_summary, "Zweite Fassung.")
        self.assertEqual(Document.objects.filter(pk=self.document.pk).count(), 1)

    def test_not_stale_immediately_after_generation(self):
        document = generate_document_long_summary(
            self.document.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Text.")),
        )
        self.assertFalse(document_long_summary_is_stale(document))

    def test_stale_after_a_new_comment(self):
        document = generate_document_long_summary(
            self.document.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Text.")),
        )
        DocumentComment.objects.create(document=document, body="Neue Notiz nach der Erzeugung.")

        document.refresh_from_db()
        self.assertTrue(document_long_summary_is_stale(document))

    def test_provider_error_fails_visibly(self):
        class _BoomProvider:
            name = "boom"

            def generate(self, messages, *, stream=False, max_tokens=None):
                raise RuntimeError("Provider weg")

        with self.assertLogs("apps.documents.long_summary", level="ERROR"):
            document = generate_document_long_summary(
                self.document.pk, generation_provider=_BoomProvider()
            )

        self.assertEqual(document.long_summary_status, Document.LongSummaryStatus.FAILED)
        self.assertIn("Provider weg", document.long_summary_error)

    def test_timeout_exception_fails_visibly_and_reraises(self):
        class _TimeoutProvider:
            name = "timeout"

            def generate(self, messages, *, stream=False, max_tokens=None):
                raise TimeoutException("Task exceeded maximum timeout value (600 seconds)")

        with (
            self.assertLogs("apps.documents.long_summary", level="ERROR"),
            self.assertRaises(TimeoutException),
        ):
            generate_document_long_summary(
                self.document.pk, generation_provider=_TimeoutProvider()
            )

        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.long_summary_status, Document.LongSummaryStatus.FAILED)
        self.assertIn("Zeitüberschreitung", document.long_summary_error)

    def test_previous_text_survives_a_failed_regeneration(self):
        generate_document_long_summary(
            self.document.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Erste Fassung.")),
        )

        with self.assertLogs("apps.documents.long_summary", level="ERROR"):
            document = generate_document_long_summary(
                self.document.pk,
                generation_provider=FakeGenerationProvider(reply="kein JSON"),
            )

        self.assertEqual(document.long_summary_status, Document.LongSummaryStatus.FAILED)
        self.assertEqual(document.long_summary, "Erste Fassung.")

    def test_logs_duration_and_attempt_count(self):
        provider = FakeGenerationProvider(reply=_reply("Text."))
        with self.assertLogs("apps.documents.long_summary", level="INFO") as captured:
            generate_document_long_summary(self.document.pk, generation_provider=provider)

        self.assertTrue(
            any(
                "Laufzeit" in message and "generate()-Aufruf" in message
                for message in captured.output
            )
        )


class VorgangLongSummaryTests(TestCase):
    """Covers Prompt-Aufbau, Sichtbarkeitsfilter, Speicherung und
    Veralterung auf Vorgangsebene (#1135).
    """

    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)

        self.vorgang = Vorgang.objects.create(name="Forderung Acme")
        self.correspondent = Correspondent.objects.create(name="Acme GmbH")

        self.visible = self._document(
            "Rechnung 42",
            summary="Acme stellt 129,90 EUR in Rechnung.",
            text_content="Der komplette Volltext, der NICHT in den Vorgangs-Prompt gehoert.",
            visible=True,
        )

    def _document(self, title, *, summary="", text_content="", visible):
        document = Document.objects.create(
            title=title,
            summary=summary,
            text_content=text_content,
            correspondent=self.correspondent,
            visibility=Document.Visibility.DEPARTMENT,
        )
        if visible:
            document.departments.add(self.department)
        else:
            document.departments.add(Department.objects.create(name=f"Fremd {title}"))
        document.vorgaenge.add(self.vorgang)
        return document

    def test_prompt_uses_summary_but_not_full_text(self):
        messages = build_vorgang_long_summary_messages(self.vorgang, [self.visible])
        content = messages[1].content
        self.assertIn("Acme stellt 129,90 EUR in Rechnung.", content)
        self.assertNotIn("Der komplette Volltext", content)

    def test_prompt_includes_existing_document_long_summary(self):
        self.visible.long_summary = "Lange Fassung dieses Dokuments."
        self.visible.long_summary_generated_at = self.visible.created_at
        self.visible.save(update_fields=["long_summary", "long_summary_generated_at"])

        messages = build_vorgang_long_summary_messages(self.vorgang, [self.visible])
        self.assertIn("Lange Fassung dieses Dokuments.", messages[1].content)

    def test_generation_excludes_documents_the_user_cannot_see(self):
        self._document("Geheimes Dokument", summary="Darf Alice nicht sehen.", visible=False)
        provider = FakeGenerationProvider(reply=_reply("Geschichte des Vorgangs."))

        generate_vorgang_long_summary(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertIn("Rechnung 42", prompt)
        self.assertNotIn("Geheimes Dokument", prompt)
        self.assertNotIn("Darf Alice nicht sehen.", prompt)

    def test_generates_and_stores_result_with_provenance(self):
        provider = FakeGenerationProvider(
            model="claude-sonnet-5", version="1", reply=_reply("Geschichte des Vorgangs.")
        )

        vorgang = generate_vorgang_long_summary(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(vorgang.long_summary_status, Vorgang.LongSummaryStatus.READY)
        self.assertEqual(vorgang.long_summary, "Geschichte des Vorgangs.")
        self.assertEqual(vorgang.long_summary_ai_model, "claude-sonnet-5")
        self.assertEqual(vorgang.long_summary_based_on["document_count"], 1)
        self.assertEqual(vorgang.long_summary_based_on["used_count"], 1)

    def test_no_documents_skips_the_generate_call(self):
        empty_vorgang = Vorgang.objects.create(name="Leerer Vorgang")
        provider = FakeGenerationProvider(reply=_reply("Sollte nicht benutzt werden."))

        vorgang = generate_vorgang_long_summary(
            empty_vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(provider.calls, [])
        self.assertEqual(vorgang.long_summary_status, Vorgang.LongSummaryStatus.READY)
        self.assertEqual(vorgang.long_summary, "")

    def test_not_stale_immediately_after_generation(self):
        vorgang = generate_vorgang_long_summary(
            self.vorgang.pk,
            self.user.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Text.")),
        )
        self.assertFalse(vorgang_long_summary_is_stale(vorgang, self.user))

    def test_stale_after_a_new_document(self):
        vorgang = generate_vorgang_long_summary(
            self.vorgang.pk,
            self.user.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Text.")),
        )
        self._document("Mahnung", summary="Zweites Dokument.", visible=True)

        vorgang.refresh_from_db()
        self.assertTrue(vorgang_long_summary_is_stale(vorgang, self.user))
        self.assertEqual(vorgang_long_summary_new_document_count(vorgang, self.user), 1)

    def test_stale_after_a_new_comment_on_a_considered_document(self):
        vorgang = generate_vorgang_long_summary(
            self.vorgang.pk,
            self.user.pk,
            generation_provider=FakeGenerationProvider(reply=_reply("Text.")),
        )
        DocumentComment.objects.create(document=self.visible, body="Neue Notiz.")

        vorgang.refresh_from_db()
        self.assertTrue(vorgang_long_summary_is_stale(vorgang, self.user))

    def test_timeout_exception_fails_visibly_and_reraises(self):
        class _TimeoutProvider:
            name = "timeout"

            def generate(self, messages, *, stream=False, max_tokens=None):
                raise TimeoutException("Task exceeded maximum timeout value (600 seconds)")

        with (
            self.assertLogs("apps.documents.long_summary", level="ERROR"),
            self.assertRaises(TimeoutException),
        ):
            generate_vorgang_long_summary(
                self.vorgang.pk, self.user.pk, generation_provider=_TimeoutProvider()
            )

        vorgang = Vorgang.objects.get(pk=self.vorgang.pk)
        self.assertEqual(vorgang.long_summary_status, Vorgang.LongSummaryStatus.FAILED)
        self.assertIn("Zeitüberschreitung", vorgang.long_summary_error)
