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
    Vorgang,
    VorgangRecommendation,
    VorgangRecommendationRun,
)
from .recommendations import generate_vorgang_recommendations, is_stale_for

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

            def generate(self, messages, *, stream=False):
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

    def test_empty_basis_costs_no_generate_call(self):
        empty_vorgang = Vorgang.objects.create(name="Leerer Vorgang")
        provider = FakeGenerationProvider(reply=_reply(sources=[]))

        run = generate_vorgang_recommendations(
            empty_vorgang.pk, self.user.pk, generation_provider=provider
        )

        self.assertEqual(provider.calls, [])
        self.assertEqual(run.status, VorgangRecommendationRun.Status.READY)
        self.assertEqual(run.recommendations.count(), 0)


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
