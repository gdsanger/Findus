import datetime
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Department
from apps.ai.providers.fake import FakeGenerationProvider

from .models import (
    Correspondent,
    Document,
    DocumentComment,
    Vorgang,
    VorgangRecommendation,
    VorgangRecommendationRun,
)
from .recommendations import (
    build_vorgang_comment_context,
    generate_vorgang_recommendations,
    is_stale_for,
)

User = get_user_model()


def _reply(*, sources, lage="Der Vorgang laeuft."):
    return json.dumps(
        {
            "lage": lage,
            "empfehlungen": [
                {
                    "titel": "Mahnung fristgerecht beantworten",
                    "begruendung": "Die Mahnung setzt eine Frist zum 15.09.2026.",
                    "frist": "2026-09-15",
                    "prioritaet": "hoch",
                    "quellen": sources,
                }
            ],
        }
    )


# Am Output-Token-Limit abgeschnitten, mitten im Satz und mitten im Objekt --
# nachgebaut nach dem gemeldeten Creditreform-Vorgang (#1096). `repair_json`
# wuerde daraus klaglos ein Objekt ohne "empfehlungen" machen.
_CUT_OFF_REPLY = (
    '{"lage": "Die Verjaehrungseinrede ist erhoben, ein Vergleichsentwurf liegt '
    'vor und ist laut Entwurf bereits fuer den 15.08.2026 vorgesehen, was angesichts'
)


class VorgangRecommendationGenerationTests(TestCase):
    """Covers the on-demand Vorgang-Beurteilung (#1093): one generate() call
    over the documents' summaries/key-facts, result persisted as a run plus
    per-recommendation rows with their source documents.
    """

    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)

        self.vorgang = Vorgang.objects.create(name="Forderung Acme")
        self.correspondent = Correspondent.objects.create(name="Acme GmbH")

        self.older = self._document(
            "Rechnung 42",
            datetime.date(2026, 7, 1),
            summary="Acme stellt 129,90 EUR in Rechnung.",
            key_facts={"document_type": "Rechnung", "amount": "129.90"},
        )
        self.newer = self._document(
            "Mahnung zu Rechnung 42",
            datetime.date(2026, 8, 1),
            summary="Acme mahnt die offene Rechnung an.",
            key_facts={"document_type": "Mahnung", "due_date": "2026-09-15"},
        )

    def _document(self, title, document_date, *, summary="", key_facts=None, visible=True):
        document = Document.objects.create(
            title=title,
            document_date=document_date,
            summary=summary,
            key_facts=key_facts or {},
            correspondent=self.correspondent,
            text_content="Der komplette Volltext, der gerade NICHT in den Prompt gehoert.",
            visibility=Document.Visibility.DEPARTMENT,
        )
        if visible:
            document.departments.add(self.department)
        else:
            document.departments.add(Department.objects.create(name=f"Fremd {title}"))
        document.vorgaenge.add(self.vorgang)
        return document

    def test_generates_situation_and_recommendations_with_sources(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(len(provider.calls), 1, "genau ein generate()-Call pro Generierung")
        self.assertEqual(run.status, VorgangRecommendationRun.Status.READY)
        self.assertEqual(run.situation, "Der Vorgang laeuft.")
        self.assertIsNotNone(run.generated_at)

        recommendation = run.recommendations.get()
        self.assertEqual(recommendation.title, "Mahnung fristgerecht beantworten")
        self.assertEqual(recommendation.due_date, datetime.date(2026, 9, 15))
        self.assertEqual(recommendation.priority, VorgangRecommendation.Priority.HIGH)
        self.assertEqual(recommendation.status, VorgangRecommendation.Status.OPEN)
        self.assertIsNone(recommendation.task)
        self.assertEqual([doc.pk for doc in recommendation.documents.all()], [self.newer.pk])

    def test_prompt_uses_summaries_and_key_facts_but_not_full_text(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertIn("Acme mahnt die offene Rechnung an.", prompt)
        self.assertIn("document_type=Mahnung", prompt)
        self.assertNotIn("NICHT in den Prompt", prompt)

    def test_prompt_lists_documents_chronologically(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertLess(
            prompt.index(f"[Dokument {self.older.pk}]"),
            prompt.index(f"[Dokument {self.newer.pk}]"),
        )

    def test_basis_is_scoped_to_visible_documents(self):
        hidden = self._document(
            "Interne Notiz", datetime.date(2026, 7, 15), summary="Nur fremde Abteilung.", visible=False
        )
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertNotIn("Nur fremde Abteilung.", prompt)
        self.assertNotIn(hidden.pk, run.based_on["document_ids"])

    def test_sources_outside_the_prompt_are_dropped(self):
        """Halluzinierte/unsichtbare Quell-IDs werden nicht verlinkt."""
        hidden = self._document(
            "Fremdakte", datetime.date(2026, 7, 20), summary="Fremd.", visible=False
        )
        provider = FakeGenerationProvider(
            reply=_reply(sources=[self.newer.pk, hidden.pk, 999999])
        )

        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        recommendation = run.recommendations.get()
        self.assertEqual([doc.pk for doc in recommendation.documents.all()], [self.newer.pk])

    def test_based_on_records_the_document_basis(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(run.based_on["document_count"], 2)
        self.assertEqual(run.based_on["used_count"], 2)
        self.assertFalse(run.based_on["truncated"])
        self.assertEqual(
            run.based_on["considered_document_ids"], sorted([self.older.pk, self.newer.pk])
        )
        self.assertEqual(run.based_on["latest_document_date"], "2026-08-01")

    @override_settings(FINDUS_VORGANG_RECOMMENDATION_MAX_DOCUMENTS=1)
    def test_document_limit_keeps_the_newest_and_is_reported(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        with self.assertLogs("apps.documents.recommendations", level="WARNING") as logs:
            run = generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=provider
            )

        self.assertTrue(run.based_on["truncated"])
        self.assertEqual(run.based_on["document_ids"], [self.newer.pk])
        self.assertEqual(run.based_on["document_count"], 2)
        self.assertTrue(any("Datenbasis" in message for message in logs.output))

    @override_settings(FINDUS_VORGANG_RECOMMENDATION_MAX_ITEMS=1)
    def test_item_limit_truncates_and_logs(self):
        provider = FakeGenerationProvider(
            reply=json.dumps(
                {
                    "lage": "Viel los.",
                    "empfehlungen": [
                        {"titel": "Erstes", "quellen": [self.newer.pk]},
                        {"titel": "Zweites", "quellen": [self.newer.pk]},
                    ],
                }
            )
        )

        with self.assertLogs("apps.documents.recommendations", level="WARNING"):
            run = generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=provider
            )

        self.assertEqual([item.title for item in run.recommendations.all()], ["Erstes"])

    def test_regeneration_replaces_the_previous_result(self):
        first = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk], lage="Erste Lage."))
        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=first
        )

        second = FakeGenerationProvider(
            reply=json.dumps(
                {
                    "lage": "Zweite Lage.",
                    "empfehlungen": [{"titel": "Neuer Schritt", "quellen": [self.older.pk]}],
                }
            )
        )
        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=second
        )

        self.assertEqual(VorgangRecommendationRun.objects.count(), 1)
        self.assertEqual(run.situation, "Zweite Lage.")
        self.assertEqual([item.title for item in run.recommendations.all()], ["Neuer Schritt"])

    def test_malformed_json_is_repaired_instead_of_failing(self):
        broken = _reply(sources=[self.newer.pk]).replace('"lage"', 'lage', 1)
        provider = FakeGenerationProvider(reply=f"Hier das Ergebnis:\n```json\n{broken}\n```")

        run = generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.READY)
        self.assertEqual(run.recommendations.count(), 1)

    def test_unparseable_reply_fails_visibly_instead_of_silently(self):
        provider = FakeGenerationProvider(reply="Dazu kann ich nichts sagen.")

        with self.assertLogs("apps.documents.recommendations", level="ERROR"):
            run = generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=provider
            )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.FAILED)
        self.assertTrue(run.error)

    def test_provider_error_fails_visibly(self):
        class _BoomProvider:
            name = "boom"

            def generate(self, messages, *, stream=False, max_tokens=None):
                raise RuntimeError("Provider weg")

        with self.assertLogs("apps.documents.recommendations", level="ERROR"):
            run = generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=_BoomProvider()
            )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.FAILED)
        self.assertIn("Provider weg", run.error)

    def test_previous_recommendations_survive_a_failed_regeneration(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))
        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        with self.assertLogs("apps.documents.recommendations", level="ERROR"):
            run = generate_vorgang_recommendations(
                self.vorgang.pk,
                self.user.pk,
                generation_provider=FakeGenerationProvider(reply="kein JSON"),
            )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.FAILED)
        self.assertEqual(run.recommendations.count(), 1)

    def test_generate_call_uses_the_configured_output_budget(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        with override_settings(FINDUS_VORGANG_RECOMMENDATION_MAX_OUTPUT_TOKENS=4000):
            generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=provider
            )

        self.assertEqual(provider.max_tokens_calls, [4000])

    def test_truncated_reply_is_retried_instead_of_read_as_no_recommendations(self):
        """Der gemeldete Bug (#1096): die Antwort brach mitten in der
        Lage-Einschaetzung ab, die Empfehlungsliste kam nie an -- und das
        Panel behauptete daraufhin "keine Empfehlungen".
        """
        provider = FakeGenerationProvider(
            replies=[_CUT_OFF_REPLY, _reply(sources=[self.newer.pk])],
            truncated=[True, False],
        )

        with override_settings(FINDUS_VORGANG_RECOMMENDATION_MAX_OUTPUT_TOKENS=4000):
            with self.assertLogs("apps.ai.providers.json_generation", level="WARNING"):
                run = generate_vorgang_recommendations(
                    self.vorgang.pk, self.user.pk, generation_provider=provider
                )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.READY)
        self.assertEqual(run.recommendations.count(), 1)
        self.assertEqual(run.situation, "Der Vorgang laeuft.")
        self.assertEqual(provider.max_tokens_calls, [4000, 8000], "Retry mit groesserem Budget")

    def test_reply_still_truncated_after_the_retry_fails_visibly(self):
        provider = FakeGenerationProvider(reply=_CUT_OFF_REPLY, truncated=True)

        with self.assertLogs("apps.documents.recommendations", level="ERROR"):
            run = generate_vorgang_recommendations(
                self.vorgang.pk, self.user.pk, generation_provider=provider
            )

        self.assertEqual(run.status, VorgangRecommendationRun.Status.FAILED)
        self.assertIn("abgeschnitten", run.error)
        # Weder der angefangene Satz noch eine leere Liste wird gespeichert.
        self.assertEqual(run.situation, "")
        self.assertEqual(run.recommendations.count(), 0)

    def test_prompt_keeps_the_situation_short_and_asks_for_recommendations_first(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        system_prompt = provider.calls[0][0].content
        self.assertLess(
            system_prompt.index('"empfehlungen"'),
            system_prompt.index('"lage"'),
            "Empfehlungen vor der Lage -- geht das Budget aus, fehlt der weniger "
            "wichtige Teil",
        )
        self.assertIn("hoechstens 3 kurze Saetze", system_prompt)

    def test_prompt_includes_comments_in_their_own_labeled_section(self):
        DocumentComment.objects.create(
            document=self.newer, body="Bereits am 12.08.2026 vollstaendig bezahlt."
        )
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertIn("Notizen des Nutzers zu diesen Dokumenten", prompt)
        self.assertIn("Bereits am 12.08.2026 vollstaendig bezahlt.", prompt)
        self.assertLess(
            prompt.index("Dokumente des Vorgangs"),
            prompt.index("Notizen des Nutzers"),
            "Notizen stehen als eigener Abschnitt hinter den Dokumentbloecken",
        )

    def test_comment_of_a_hidden_document_never_reaches_the_prompt(self):
        hidden = self._document(
            "Interne Notiz", datetime.date(2026, 7, 15), summary="geheim", visible=False
        )
        DocumentComment.objects.create(document=hidden, body="Darf niemand sehen.")
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        prompt = provider.calls[0][-1].content
        self.assertNotIn("Darf niemand sehen.", prompt)

    def test_system_prompt_gives_notes_precedence_over_documents(self):
        provider = FakeGenerationProvider(reply=_reply(sources=[self.newer.pk]))

        generate_vorgang_recommendations(
            self.vorgang.pk, self.user.pk, generation_provider=provider
        )

        system_prompt = provider.calls[0][0].content
        self.assertIn("gilt die Notiz", system_prompt)
        self.assertIn("bereits erledigt, empfiehl sie nicht erneut", system_prompt)

    def test_empty_basis_costs_no_generate_call(self):
        empty_vorgang = Vorgang.objects.create(name="Leerer Vorgang")
        provider = FakeGenerationProvider(reply=_reply(sources=[]))

        run = generate_vorgang_recommendations(
            empty_vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(provider.calls, [])
        self.assertEqual(run.status, VorgangRecommendationRun.Status.READY)
        self.assertEqual(run.recommendations.count(), 0)


class VorgangCommentContextTests(TestCase):
    """Covers `build_vorgang_comment_context` (#1132) in isolation --

    Kontextaufbau, Kuerzung und Sichtbarkeit lassen sich hier pruefen, ohne
    ein Modell aufzurufen (der Prompt-Inhalt wird geprueft, nicht die
    Modellantwort).
    """

    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)

        self.vorgang = Vorgang.objects.create(name="Forderung Acme")
        self.document = Document.objects.create(
            title="Rechnung 42",
            document_date=datetime.date(2026, 7, 1),
            visibility=Document.Visibility.DEPARTMENT,
        )
        self.document.departments.add(self.department)
        self.document.vorgaenge.add(self.vorgang)

    def test_no_comments_yield_no_section(self):
        self.assertEqual(build_vorgang_comment_context([self.document], 30), [])

    def test_comment_is_dated_titled_and_attributed_to_its_document(self):
        DocumentComment.objects.create(
            document=self.document, body="Am 12.08.2026 ueberwiesen."
        )

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertIn("Notizen des Nutzers zu diesen Dokumenten", lines[1])
        self.assertTrue(any("Am 12.08.2026 ueberwiesen." in line for line in lines))
        self.assertTrue(any(self.document.title in line for line in lines))

    def test_overdue_follow_up_is_labeled(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Nachfassen.",
            follow_up_date=timezone.localdate() - datetime.timedelta(days=3),
        )

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertTrue(any("(ueberfaellig)" in line for line in lines))

    def test_follow_up_due_today_is_labeled(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Heute entscheiden.",
            follow_up_date=timezone.localdate(),
        )

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertTrue(any("(heute faellig)" in line for line in lines))

    def test_future_follow_up_is_labeled_planned(self):
        DocumentComment.objects.create(
            document=self.document,
            body="Rueckmeldung abwarten.",
            follow_up_date=timezone.localdate() + datetime.timedelta(days=5),
        )

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertTrue(any("(geplant)" in line for line in lines))

    def test_comments_beyond_the_limit_are_dropped(self):
        old = DocumentComment.objects.create(document=self.document, body="Alte Notiz.")
        DocumentComment.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=90)
        )
        DocumentComment.objects.create(document=self.document, body="Neue Notiz.")

        lines = build_vorgang_comment_context([self.document], 1)

        self.assertFalse(any("Alte Notiz." in line for line in lines))
        self.assertTrue(any("Neue Notiz." in line for line in lines))

    def test_open_follow_up_survives_the_limit_regardless_of_age(self):
        old_planned = DocumentComment.objects.create(
            document=self.document,
            body="Alte, aber geplante Notiz.",
            follow_up_date=timezone.localdate() + datetime.timedelta(days=1),
        )
        DocumentComment.objects.filter(pk=old_planned.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=90)
        )
        DocumentComment.objects.create(document=self.document, body="Neuere Notiz.")
        DocumentComment.objects.create(document=self.document, body="Noch neuere Notiz.")

        lines = build_vorgang_comment_context([self.document], 1)

        self.assertTrue(any("Alte, aber geplante Notiz." in line for line in lines))

    def test_author_is_omitted_when_there_is_only_one(self):
        DocumentComment.objects.create(document=self.document, body="Notiz.", author=self.user)

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertFalse(any(self.user.get_username() in line for line in lines))

    def test_author_is_shown_when_several_contributed(self):
        other = User.objects.create_user(username="bob", password="x")
        DocumentComment.objects.create(document=self.document, body="Notiz A.", author=self.user)
        DocumentComment.objects.create(document=self.document, body="Notiz B.", author=other)

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertTrue(any(self.user.get_username() in line for line in lines))
        self.assertTrue(any(other.get_username() in line for line in lines))

    def test_comments_of_documents_outside_the_basis_are_excluded(self):
        other_document = Document.objects.create(
            title="Anderes Dokument", visibility=Document.Visibility.DEPARTMENT
        )
        other_document.departments.add(self.department)
        other_document.vorgaenge.add(self.vorgang)
        DocumentComment.objects.create(document=other_document, body="Fremde Notiz.")

        lines = build_vorgang_comment_context([self.document], 30)

        self.assertFalse(any("Fremde Notiz." in line for line in lines))


class VorgangRecommendationStalenessTests(TestCase):
    """Covers the "veraltet"-Hinweis (#1093)."""

    def setUp(self):
        self.department = Department.objects.create(name="Buchhaltung")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)

        self.vorgang = Vorgang.objects.create(name="Forderung Acme")
        self.document = Document.objects.create(
            title="Rechnung", summary="Kurz.", visibility=Document.Visibility.DEPARTMENT
        )
        self.document.departments.add(self.department)
        self.document.vorgaenge.add(self.vorgang)

        self.run = generate_vorgang_recommendations(
            self.vorgang.pk,
            self.user.pk,
            generation_provider=FakeGenerationProvider(
                reply=_reply(sources=[self.document.pk])
            ),
        )

    def test_unchanged_basis_is_not_stale(self):
        self.assertFalse(is_stale_for(self.run, self.user))

    def test_new_document_makes_it_stale(self):
        added = Document.objects.create(
            title="Mahnung", visibility=Document.Visibility.DEPARTMENT
        )
        added.departments.add(self.department)
        added.vorgaenge.add(self.vorgang)

        self.assertTrue(is_stale_for(self.run, self.user))

    def test_changed_document_makes_it_stale(self):
        self.document.summary = "Jetzt anders."
        self.document.save(update_fields=["summary", "updated_at"])

        self.assertTrue(is_stale_for(self.run, self.user))

    def test_running_run_is_never_stale(self):
        self.run.status = VorgangRecommendationRun.Status.RUNNING
        self.run.save(update_fields=["status"])

        self.assertFalse(is_stale_for(self.run, self.user))

    def test_narrower_visibility_does_not_look_stale(self):
        """Ein Kollege, der weniger sieht, bekommt keinen falschen Hinweis."""
        other = User.objects.create_user(username="bob", password="x")
        other.departments.add(Department.objects.create(name="Vertrieb"))

        self.assertFalse(is_stale_for(self.run, other))

    def test_truncated_run_is_not_immediately_stale(self):
        older = Document.objects.create(
            title="Alt",
            document_date=datetime.date(2020, 1, 1),
            visibility=Document.Visibility.DEPARTMENT,
        )
        older.departments.add(self.department)
        older.vorgaenge.add(self.vorgang)

        with override_settings(FINDUS_VORGANG_RECOMMENDATION_MAX_DOCUMENTS=1):
            run = generate_vorgang_recommendations(
                self.vorgang.pk,
                self.user.pk,
                generation_provider=FakeGenerationProvider(
                    reply=_reply(sources=[self.document.pk])
                ),
            )

        self.assertTrue(run.based_on["truncated"])
        self.assertFalse(is_stale_for(run, self.user))

    def test_no_run_is_not_stale(self):
        self.assertFalse(is_stale_for(None, self.user))

    def test_document_touched_before_generation_is_not_stale(self):
        self.run.generated_at = timezone.now() + datetime.timedelta(minutes=5)
        self.run.save(update_fields=["generated_at"])

        self.assertFalse(is_stale_for(self.run, self.user))
