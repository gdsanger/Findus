import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department
from apps.ai.providers.fake import FakeGenerationProvider

from .models import DEFAULT_LETTER_LAYOUT, LetterTemplate, LetterTemplatePlaceholder

User = get_user_model()


def template_payload(**overrides):
    payload = {
        "name": "Antwort ans Finanzamt",
        "description": "Wenn ein Bescheid beantwortet werden muss.",
        "category": "Antwortschreiben",
        "instructions": "## Ton\nSachlich, knapp.",
        "signature": "Christian Angermeier",
        "layout_letterhead": "",
        "layout_date_place": "",
        "layout_closing": "Mit freundlichen Grüßen",
    }
    payload.update(overrides)
    return payload


class LetterTemplateListViewTests(TestCase):
    """Sichtbarkeits-Scoping der Vorlagen-Liste (#1094) -- deckungsgleich mit
    `LetterTemplate.visible_to`.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")

        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

        self.dept_template = LetterTemplate.objects.create(
            name="Widerspruch", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        self.dept_template.departments.add(self.dept_a)

        self.other_template = LetterTemplate.objects.create(
            name="Fremde Abteilung", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        self.other_template.departments.add(self.dept_b)

        self.private_template = LetterTemplate.objects.create(
            name="Privates Anschreiben", visibility=LetterTemplate.Visibility.PRIVATE
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("documents:letter_template_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_only_visible_templates_are_listed(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("documents:letter_template_list"))

        self.assertContains(response, "Widerspruch")
        self.assertNotContains(response, "Fremde Abteilung")
        self.assertNotContains(response, "Privates Anschreiben")

    def test_foreign_template_is_not_reachable_by_id(self):
        self.client.force_login(self.user_a)
        response = self.client.get(
            reverse("documents:letter_template_detail", args=[self.other_template.pk])
        )
        self.assertEqual(response.status_code, 404)


class LetterTemplateCreateViewTests(TestCase):
    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.user_a = User.objects.create_user(username="alice", password="x")
        self.user_a.departments.add(self.dept_a)

    def test_creates_template_scoped_to_owner_departments(self):
        self.client.force_login(self.user_a)
        response = self.client.post(
            reverse("documents:letter_template_create"), template_payload()
        )

        template = LetterTemplate.objects.get(name="Antwort ans Finanzamt")
        self.assertRedirects(
            response, reverse("documents:letter_template_detail", args=[template.pk])
        )
        self.assertEqual(template.owner, self.user_a)
        self.assertIn(self.dept_a, template.departments.all())
        self.assertEqual(template.visibility, LetterTemplate.Visibility.DEPARTMENT)
        self.assertEqual(template.category, "Antwortschreiben")
        self.assertIn("Sachlich, knapp.", template.instructions)

    def test_department_less_user_creates_private_template(self):
        user = User.objects.create_user(username="carol", password="x")
        self.client.force_login(user)
        self.client.post(reverse("documents:letter_template_create"), template_payload())

        template = LetterTemplate.objects.get(name="Antwort ans Finanzamt")
        self.assertEqual(template.visibility, LetterTemplate.Visibility.PRIVATE)
        self.assertEqual(list(template.departments.all()), [])

    def test_new_template_starts_from_the_default_letter_layout(self):
        self.client.force_login(self.user_a)
        self.client.post(
            reverse("documents:letter_template_create"),
            template_payload(layout_letterhead="Perculasoft e.K."),
        )

        template = LetterTemplate.objects.get(name="Antwort ans Finanzamt")
        self.assertEqual(template.layout["letterhead"], "Perculasoft e.K.")
        self.assertEqual(template.layout["format"], DEFAULT_LETTER_LAYOUT["format"])
        self.assertTrue(template.layout["show_recipient_block"])


class LetterTemplateDetailViewTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        self.template = LetterTemplate.objects.create(
            name="Widerspruch", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        self.template.departments.add(self.dept)

    def test_detail_page_offers_instructions_layout_and_bindings(self):
        response = self.client.get(
            reverse("documents:letter_template_detail", args=[self.template.pk])
        )

        self.assertContains(response, "Anleitung (Markdown)")
        self.assertContains(response, "Grußformel")
        self.assertContains(response, "Platzhalter / Daten-Bindungen")
        self.assertContains(response, 'value="document.keyfacts.due_date"')
        self.assertContains(response, 'value="Antwortschreiben"')

    def test_create_page_renders_the_form(self):
        response = self.client.get(reverse("documents:letter_template_create"))
        self.assertContains(response, "Neue Brief-Vorlage")
        self.assertContains(response, "Kategorie")

    def test_edit_updates_fields_and_layout_without_losing_unknown_keys(self):
        self.template.layout = {"format": "din5008", "kuenftige_option": "bleibt"}
        self.template.save()

        self.client.post(
            reverse("documents:letter_template_detail", args=[self.template.pk]),
            template_payload(
                name="Widerspruch (kurz)",
                layout_closing="Freundliche Grüße",
                layout_date_place="Musterstadt",
            ),
        )

        self.template.refresh_from_db()
        self.assertEqual(self.template.name, "Widerspruch (kurz)")
        self.assertEqual(self.template.layout["closing"], "Freundliche Grüße")
        self.assertEqual(self.template.layout["date_place"], "Musterstadt")
        self.assertEqual(self.template.layout["kuenftige_option"], "bleibt")

    def test_delete_removes_template(self):
        response = self.client.post(
            reverse("documents:letter_template_delete", args=[self.template.pk])
        )
        self.assertRedirects(response, reverse("documents:letter_template_list"))
        self.assertFalse(LetterTemplate.objects.filter(pk=self.template.pk).exists())


class LetterTemplatePlaceholderViewTests(TestCase):
    """Die Inline-Pflege der Daten-Bindungen (#1094)."""

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

        self.template = LetterTemplate.objects.create(name="Widerspruch")
        self.template.departments.add(self.dept)

    def add_url(self):
        return reverse(
            "documents:letter_template_placeholder_add", args=[self.template.pk]
        )

    def test_add_binds_placeholder_to_an_internal_source(self):
        response = self.client.post(
            self.add_url(),
            {
                "key": "empfaenger_adresse",
                "label": "Empfänger-Adresse",
                "source": "kontakt.address",
                "required": "on",
            },
        )

        placeholder = self.template.placeholders.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(placeholder.source, "kontakt.address")
        self.assertTrue(placeholder.required)
        self.assertContains(response, "Empfänger (Kontakt) · Adresse")

    def test_add_rejects_unknown_source(self):
        response = self.client.post(
            self.add_url(), {"key": "kunde", "label": "", "source": "crm.kunde"}
        )

        self.assertEqual(self.template.placeholders.count(), 0)
        self.assertContains(response, "keine gültige Auswahl")

    def test_add_rejects_invalid_key_and_duplicate_key(self):
        self.client.post(self.add_url(), {"key": "betrag", "source": "document.keyfacts.amount"})

        invalid = self.client.post(self.add_url(), {"key": "Mit Leerzeichen", "source": "manual"})
        self.assertContains(invalid, "Kleinbuchstaben")

        duplicate = self.client.post(self.add_url(), {"key": "betrag", "source": "manual"})
        self.assertContains(duplicate, "schon vergeben")
        self.assertEqual(self.template.placeholders.count(), 1)

    def test_update_changes_source_and_required(self):
        placeholder = LetterTemplatePlaceholder.objects.create(
            template=self.template, key="betrag", source="document.keyfacts.amount", required=True
        )

        self.client.post(
            reverse(
                "documents:letter_template_placeholder_update",
                args=[self.template.pk, placeholder.pk],
            ),
            {"key": "betrag", "label": "Betrag", "source": "manual"},
        )

        placeholder.refresh_from_db()
        self.assertEqual(placeholder.source, "manual")
        self.assertFalse(placeholder.required)

    def test_move_swaps_order_and_delete_removes_row(self):
        first = LetterTemplatePlaceholder.objects.create(
            template=self.template, key="a", source="self.name", order=1
        )
        second = LetterTemplatePlaceholder.objects.create(
            template=self.template, key="b", source="heute", order=2
        )

        self.client.post(
            reverse(
                "documents:letter_template_placeholder_move",
                args=[self.template.pk, second.pk, "up"],
            )
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual([second.order, first.order], [1, 2])

        self.client.post(
            reverse(
                "documents:letter_template_placeholder_delete",
                args=[self.template.pk, first.pk],
            )
        )
        self.assertEqual([p.key for p in self.template.placeholders.all()], ["b"])

    def test_unknown_move_direction_is_404(self):
        placeholder = LetterTemplatePlaceholder.objects.create(
            template=self.template, key="a", source="self.name"
        )
        response = self.client.post(
            reverse(
                "documents:letter_template_placeholder_move",
                args=[self.template.pk, placeholder.pk, "sideways"],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_placeholder_of_foreign_template_is_not_reachable(self):
        other = LetterTemplate.objects.create(name="Fremd")
        other.departments.add(Department.objects.create(name="Dept B"))
        placeholder = LetterTemplatePlaceholder.objects.create(
            template=other, key="a", source="self.name"
        )

        response = self.client.post(
            reverse(
                "documents:letter_template_placeholder_delete",
                args=[self.template.pk, placeholder.pk],
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(LetterTemplatePlaceholder.objects.filter(pk=placeholder.pk).exists())


def draft_reply(placeholders=None, **overrides):
    payload = {
        "anleitung": "## Ton\nSachlich-bestimmt.\n\n## Aufbau\n1. Bezug nehmen.",
        "platzhalter": placeholders
        if placeholders is not None
        else [
            {"key": "aktenzeichen", "label": "Aktenzeichen", "quelle": "manual", "pflicht": True},
            {
                "key": "empfaenger_adresse",
                "label": "Empfängeradresse",
                "quelle": "kontakt.address",
                "pflicht": False,
            },
        ],
        "name": "Widerspruch Inkasso",
        "kategorie": "Widerspruch",
        "beschreibung": "Gegen eine Inkasso-Forderung.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def suggestion_payload(index=0, key="aktenzeichen", label="Aktenzeichen", source="manual", take=True):
    payload = {
        "suggested_key": key,
        "suggested_label": label,
        "suggested_source": source,
    }
    if take:
        payload["suggested_take"] = str(index)
    return payload


class LetterTemplateDraftViewTests(TestCase):
    """„Mit KI erstellen" (#1097): der Entwurf füllt das Formular vor und
    speichert nichts.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)
        self.url = reverse("documents:letter_template_draft")

    def _draft(self, data, *, reply=None, provider=None):
        provider = provider or FakeGenerationProvider(reply=reply or draft_reply())
        with patch(
            "apps.documents.letter_template_ai.get_generation_provider",
            return_value=provider,
        ):
            response = self.client.post(self.url, data, headers={"hx-request": "true"})
        return response, provider

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(self.url, {"intent": "Widerspruch"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_draft_prefills_the_form_without_saving_anything(self):
        response, provider = self._draft({"intent": "Widerspruch gegen Inkasso"})

        self.assertEqual(len(provider.calls), 1)
        self.assertContains(response, "Sachlich-bestimmt.")
        self.assertContains(response, "Widerspruch Inkasso")
        self.assertContains(response, "Gegen eine Inkasso-Forderung.")
        self.assertContains(response, 'value="aktenzeichen"')
        self.assertContains(response, 'value="empfaenger_adresse"')
        self.assertContains(response, "Gespeichert ist noch nichts")
        self.assertFalse(LetterTemplate.objects.exists())
        self.assertFalse(LetterTemplatePlaceholder.objects.exists())

    def test_incoming_request_is_logged_even_before_the_ai_call(self):
        """Regressionsguard für #1102: kommt der Klick clientseitig nie an
        (oder scheitert vor dem KI-Call), war das Log bislang komplett leer
        -- "kommt nichts an" ließ sich so nicht von einem stillen Erfolg
        unterscheiden.
        """
        with self.assertLogs(
            "apps.documents.letter_template_views", level="INFO"
        ) as logs:
            self._draft({"intent": "Widerspruch gegen Inkasso"})

        self.assertTrue(
            any("KI-Entwurf für Brief-Vorlage angefragt" in message for message in logs.output)
        )

    def test_response_carries_a_client_side_fallback_for_transport_errors(self):
        """`draft_error` deckt nur Fehler ab, die den View-Code erreichen und
        dort als `LetterTemplateDraftError` landen. CSRF-Ablehnung,
        Netzwerkfehler oder eine unerwartete Exception zeigen sich htmx
        gegenüber als Nicht-2xx-Antwort, auf die es *nicht* swapt -- ohne
        client-seitiges Gegenstück bliebe das ein stilles Nichts (#1102).
        """
        response, _ = self._draft({"intent": "Widerspruch gegen Inkasso"})

        self.assertContains(response, 'id="letter-template-draft-transport-error"')
        self.assertContains(response, "hx-on::response-error")

    def test_draft_keeps_values_the_user_already_typed(self):
        response, _ = self._draft(
            {
                "intent": "Widerspruch gegen Inkasso",
                "name": "Mein eigener Name",
                "signature": "Christian Angermeier",
                "instructions": "wird durch den Entwurf ersetzt",
            }
        )

        self.assertContains(response, "Mein eigener Name")
        self.assertContains(response, "Christian Angermeier")
        self.assertContains(response, "Sachlich-bestimmt.")
        self.assertNotContains(response, "wird durch den Entwurf ersetzt")

    def test_missing_intent_shows_a_field_error_and_makes_no_call(self):
        response, provider = self._draft({"intent": "   "})

        self.assertEqual(provider.calls, [])
        self.assertContains(response, "Mit KI erstellen")
        self.assertContains(response, "Dieses Feld ist zwingend erforderlich.")

    def test_failed_draft_is_shown_instead_of_failing_silently(self):
        response, _ = self._draft(
            {"intent": "Widerspruch"}, reply="Kein JSON, nur Prosa."
        )

        self.assertContains(response, "Der Entwurf konnte nicht erstellt werden")
        self.assertFalse(LetterTemplate.objects.exists())

    def test_unknown_source_is_reported_and_bound_to_manual(self):
        response, _ = self._draft(
            {"intent": "Widerspruch"},
            reply=draft_reply(
                placeholders=[{"key": "aktenzeichen", "quelle": "document.aktenzeichen"}]
            ),
        )

        self.assertContains(response, "document.aktenzeichen")
        self.assertContains(response, "Manuelle Eingabe")

    def test_draft_for_existing_template_marks_keys_that_already_exist(self):
        template = LetterTemplate.objects.create(
            name="Widerspruch", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        template.departments.add(self.dept)
        LetterTemplatePlaceholder.objects.create(
            template=template, key="aktenzeichen", source="manual"
        )

        response, _ = self._draft(
            {"intent": "Widerspruch", "template_pk": str(template.pk)}
        )

        self.assertContains(response, "Schlüssel existiert in dieser Vorlage bereits")
        self.assertEqual(template.placeholders.count(), 1)

    def test_draft_for_foreign_template_is_404(self):
        other = LetterTemplate.objects.create(name="Fremd")
        other.departments.add(Department.objects.create(name="Dept B"))

        response, provider = self._draft(
            {"intent": "Widerspruch", "template_pk": str(other.pk)}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(provider.calls, [])


class LetterTemplateSuggestedPlaceholderTests(TestCase):
    """Die Übernahme der vorgeschlagenen Platzhalter (#1097) -- sie
    entstehen erst beim Speichern der Vorlage.
    """

    def setUp(self):
        self.dept = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept)
        self.client.force_login(self.user)

    def test_create_adopts_checked_suggestions_only(self):
        payload = template_payload()
        payload.update(
            {
                "suggested_key": ["aktenzeichen", "empfaenger_adresse"],
                "suggested_label": ["Aktenzeichen", "Empfängeradresse"],
                "suggested_source": ["manual", "kontakt.address"],
                "suggested_take": ["0"],
                "suggested_required": ["0"],
            }
        )

        self.client.post(reverse("documents:letter_template_create"), payload)

        template = LetterTemplate.objects.get(name="Antwort ans Finanzamt")
        placeholders = list(template.placeholders.all())
        self.assertEqual([p.key for p in placeholders], ["aktenzeichen"])
        self.assertEqual(placeholders[0].label, "Aktenzeichen")
        self.assertEqual(placeholders[0].source, "manual")
        self.assertTrue(placeholders[0].required)

    def test_invalid_suggestion_blocks_the_save_with_a_message(self):
        payload = template_payload()
        payload.update(suggestion_payload(key="Nicht Erlaubt"))

        response = self.client.post(reverse("documents:letter_template_create"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Platzhalter-Vorschlag")
        self.assertFalse(LetterTemplate.objects.exists())

    def test_duplicate_suggested_keys_block_the_save(self):
        payload = template_payload()
        payload.update(
            {
                "suggested_key": ["aktenzeichen", "aktenzeichen"],
                "suggested_label": ["", ""],
                "suggested_source": ["manual", "manual"],
                "suggested_take": ["0", "1"],
            }
        )

        response = self.client.post(reverse("documents:letter_template_create"), payload)

        self.assertContains(response, "doppelt vergeben")
        self.assertFalse(LetterTemplate.objects.exists())

    def test_edit_appends_suggestions_behind_the_existing_placeholders(self):
        template = LetterTemplate.objects.create(
            name="Widerspruch", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        template.departments.add(self.dept)
        LetterTemplatePlaceholder.objects.create(
            template=template, key="absender", source="self.name", order=1
        )

        payload = template_payload(name="Widerspruch")
        payload.update(suggestion_payload(key="aktenzeichen"))
        self.client.post(
            reverse("documents:letter_template_detail", args=[template.pk]), payload
        )

        self.assertEqual(
            [p.key for p in template.placeholders.all()], ["absender", "aktenzeichen"]
        )

    def test_suggestion_colliding_with_an_existing_key_blocks_the_save(self):
        template = LetterTemplate.objects.create(
            name="Widerspruch", visibility=LetterTemplate.Visibility.DEPARTMENT
        )
        template.departments.add(self.dept)
        LetterTemplatePlaceholder.objects.create(
            template=template, key="aktenzeichen", source="manual"
        )

        payload = template_payload(name="Neuer Name")
        payload.update(suggestion_payload(key="aktenzeichen", source="kontakt.address"))
        response = self.client.post(
            reverse("documents:letter_template_detail", args=[template.pk]), payload
        )

        self.assertContains(response, "schon vergeben")
        template.refresh_from_db()
        self.assertEqual(template.name, "Widerspruch")
        self.assertEqual(template.placeholders.count(), 1)

    def test_unchecked_suggestions_survive_a_validation_error(self):
        payload = template_payload(name="")
        payload.update(
            {
                "suggested_key": ["aktenzeichen", "empfaenger_adresse"],
                "suggested_label": ["Aktenzeichen", "Empfängeradresse"],
                "suggested_source": ["manual", "kontakt.address"],
                "suggested_take": ["1"],
            }
        )

        response = self.client.post(reverse("documents:letter_template_create"), payload)

        self.assertFalse(LetterTemplate.objects.exists())
        self.assertContains(response, 'value="aktenzeichen"')
        self.assertContains(response, 'value="empfaenger_adresse"')
        # Nur die zweite Zeile war angehakt -- und ist es nach dem
        # Neu-Rendern immer noch.
        content = response.content.decode()
        self.assertIsNotNone(
            re.search(r'id="suggested-take-1"\s+checked', content),
            "Der Haken der zweiten Vorschlagszeile ging verloren.",
        )
        self.assertIsNone(re.search(r'id="suggested-take-0"\s+checked', content))
