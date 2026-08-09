from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Department

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
