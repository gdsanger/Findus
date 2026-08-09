"""Kennungen an Vorgang/Kontakt und der Zuordnungs-Vorschlag (#1100)."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department

from .models import (
    Correspondent,
    CorrespondentReference,
    Document,
    DocumentReference,
    OwnerReference,
    Vorgang,
    VorgangReference,
)
from .reference_matching import (
    assignment_suggestions,
    auto_assign_from_references,
    learn_references_from_document,
    set_owner_reference,
    types_for_scope,
)
from .references import SCOPE_KONTAKT, SCOPE_VORGANG, set_reference

User = get_user_model()


class OwnerReferenceBasicsTests(TestCase):
    """`set_owner_reference()` -- Dedup, Normalisierung und die Grenze, die
    das Typ->Scope-Mapping zieht.
    """

    def setUp(self):
        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        self.correspondent = Correspondent.objects.create(name="Stadtwerke")

    def test_creates_with_normalized_and_raw_value(self):
        reference = set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )

        self.assertEqual(reference.value_raw, "Az. 123/45")
        self.assertEqual(reference.value_normalized, "123/45")
        self.assertEqual(reference.source, OwnerReference.Source.MANUAL)

    def test_same_key_twice_creates_no_duplicate(self):
        first = set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )
        second = set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123 / 45",
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.vorgang.references.count(), 1)

    def test_manual_entry_upgrades_a_learned_row(self):
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
            source=OwnerReference.Source.LEARNED,
        )
        reference = set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        self.assertEqual(reference.source, OwnerReference.Source.MANUAL)

    def test_learning_never_downgrades_a_manual_row(self):
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        reference = set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
            source=OwnerReference.Source.LEARNED,
        )

        self.assertEqual(reference.source, OwnerReference.Source.MANUAL)

    def test_party_related_type_does_not_belong_on_a_vorgang(self):
        """Eine Kundennummer am Vorgang wäre eine wirkungslose Eingabe --
        der Abgleich lässt sie per Typ->Scope-Mapping nur gegen Kontakte
        laufen.
        """
        self.assertIsNone(
            set_owner_reference(
                self.vorgang,
                reference_type=DocumentReference.Type.KUNDENNUMMER,
                value="4711",
            )
        )
        self.assertIsNone(
            set_owner_reference(
                self.correspondent,
                reference_type=DocumentReference.Type.AKTENZEICHEN,
                value="123/45",
            )
        )

    def test_scopeless_type_belongs_nowhere(self):
        self.assertIsNone(
            set_owner_reference(
                self.vorgang,
                reference_type=DocumentReference.Type.IHR_ZEICHEN,
                value="123/45",
            )
        )

    def test_empty_value_creates_nothing(self):
        self.assertIsNone(
            set_owner_reference(
                self.vorgang,
                reference_type=DocumentReference.Type.AKTENZEICHEN,
                value="   ",
            )
        )
        self.assertEqual(self.vorgang.references.count(), 0)

    def test_type_choices_are_scoped(self):
        vorgang_types = [value for value, _label in types_for_scope(SCOPE_VORGANG)]
        kontakt_types = [value for value, _label in types_for_scope(SCOPE_KONTAKT)]

        self.assertIn(DocumentReference.Type.AKTENZEICHEN, vorgang_types)
        self.assertNotIn(DocumentReference.Type.KUNDENNUMMER, vorgang_types)
        self.assertIn(DocumentReference.Type.IBAN, kontakt_types)
        self.assertNotIn(DocumentReference.Type.AKTENZEICHEN, kontakt_types)


class LearnReferencesTests(TestCase):
    """Auto-Lernen: die Zuordnung eines Dokuments *ist* die Aussage
    "diese Nummern gehören hierher".
    """

    def setUp(self):
        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        self.correspondent = Correspondent.objects.create(name="Inkasso AG")
        self.document = Document.objects.create(title="Mahnung")
        self.document.vorgaenge.add(self.vorgang)
        self.document.correspondent = self.correspondent
        self.document.save(update_fields=["correspondent"])

        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="Kd.-Nr. 4711",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.IHR_ZEICHEN,
            value="XY-9",
        )

    def test_case_related_goes_to_the_vorgang_party_related_to_the_kontakt(self):
        learn_references_from_document(self.document)

        self.assertEqual(
            [(r.type, r.value_normalized) for r in self.vorgang.references.all()],
            [(DocumentReference.Type.AKTENZEICHEN, "123/45")],
        )
        self.assertEqual(
            [(r.type, r.value_normalized) for r in self.correspondent.references.all()],
            [(DocumentReference.Type.KUNDENNUMMER, "4711")],
        )

    def test_learned_rows_carry_source_and_origin(self):
        learn_references_from_document(self.document)

        reference = self.vorgang.references.get()
        self.assertEqual(reference.source, OwnerReference.Source.LEARNED)
        self.assertEqual(reference.learned_from_id, self.document.pk)

    def test_scopeless_reference_is_not_learned(self):
        learn_references_from_document(self.document)

        self.assertFalse(
            VorgangReference.objects.filter(
                type=DocumentReference.Type.IHR_ZEICHEN
            ).exists()
        )

    def test_second_document_with_the_same_reference_creates_no_duplicate(self):
        learn_references_from_document(self.document)

        other = Document.objects.create(title="Klage")
        other.vorgaenge.add(self.vorgang)
        set_reference(
            other,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123 / 45",
        )
        learn_references_from_document(other)

        self.assertEqual(self.vorgang.references.count(), 1)

    def test_unassigned_document_teaches_nobody(self):
        loose = Document.objects.create(title="Streuner")
        set_reference(
            loose,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="999/99",
        )

        self.assertEqual(learn_references_from_document(loose), [])
        self.assertEqual(VorgangReference.objects.count(), 0)


class AssignmentSuggestionTests(TestCase):
    """Der Abgleich: exakt, `visible_to`-gescoped, und immer nur ein
    Vorschlag.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept_a)

        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        self.correspondent = Correspondent.objects.create(name="Inkasso AG")

        self.known = self._document("Erste Mahnung", self.dept_a)
        self.known.vorgaenge.add(self.vorgang)
        self.known.correspondent = self.correspondent
        self.known.save(update_fields=["correspondent"])
        set_reference(
            self.known,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )
        set_reference(
            self.known,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )
        learn_references_from_document(self.known)

        self.new = self._document("Zweite Mahnung", self.dept_a)

    def _document(self, title, department):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT
        )
        document.departments.add(department)
        return document

    def test_matching_reference_suggests_the_vorgang(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123 / 45",
        )

        suggestions = assignment_suggestions(self.user, self.new)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["scope"], SCOPE_VORGANG)
        self.assertEqual(suggestions[0]["target"], self.vorgang)
        self.assertEqual(suggestions[0]["reference"].value_normalized, "123/45")

    def test_matching_party_reference_suggests_the_kontakt(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )

        suggestions = assignment_suggestions(self.user, self.new)

        self.assertEqual([s["target"] for s in suggestions], [self.correspondent])
        self.assertEqual(suggestions[0]["scope"], SCOPE_KONTAKT)

    def test_same_value_under_another_type_does_not_match(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.RECHNUNGSNUMMER,
            value="123/45",
        )

        self.assertEqual(assignment_suggestions(self.user, self.new), [])

    def test_already_assigned_vorgang_is_not_suggested_again(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        self.new.vorgaenge.add(self.vorgang)

        self.assertEqual(assignment_suggestions(self.user, self.new), [])

    def test_a_set_kontakt_is_never_replaced_by_a_suggestion(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )
        self.new.correspondent = Correspondent.objects.create(name="Jemand anderes")
        self.new.save(update_fields=["correspondent"])

        self.assertEqual(assignment_suggestions(self.user, self.new), [])

    def test_two_matching_references_yield_one_suggestion_per_target(self):
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.FORDERUNGSNUMMER,
            value="F-1",
        )
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.FORDERUNGSNUMMER,
            value="F-1",
        )

        suggestions = assignment_suggestions(self.user, self.new)

        self.assertEqual([s["target"] for s in suggestions], [self.vorgang])

    def test_reference_learned_from_an_invisible_document_suggests_nothing(self):
        """Sonst wäre der Vorschlag ein Fenster auf genau das Dokument, das
        der Nutzer nicht sehen darf.
        """
        foreign_vorgang = Vorgang.objects.create(name="Fremdakte")
        foreign = self._document("Fremdes Schreiben", self.dept_b)
        foreign.vorgaenge.add(foreign_vorgang)
        set_reference(
            foreign,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="777/77",
        )
        learn_references_from_document(foreign)

        set_reference(
            self.new,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="777/77",
        )

        self.assertEqual(assignment_suggestions(self.user, self.new), [])
        # ... für jemanden mit Zugriff auf denselben Bestand dagegen schon.
        bob = User.objects.create_user(username="bob", password="x")
        bob.departments.add(self.dept_a, self.dept_b)
        self.assertEqual(
            [s["target"] for s in assignment_suggestions(bob, self.new)],
            [foreign_vorgang],
        )

    def test_manually_maintained_reference_needs_no_visible_origin(self):
        hand_kept = Vorgang.objects.create(name="Von Hand gepflegt")
        set_owner_reference(
            hand_kept,
            reference_type=DocumentReference.Type.VERTRAGSNUMMER,
            value="V-2024-9",
        )
        set_reference(
            self.new,
            reference_type=DocumentReference.Type.VERTRAGSNUMMER,
            value="V-2024-9",
        )

        self.assertEqual(
            [s["target"] for s in assignment_suggestions(self.user, self.new)],
            [hand_kept],
        )

    def test_document_without_references_has_no_suggestions(self):
        self.assertEqual(assignment_suggestions(self.user, self.new), [])


class AutoAssignTests(TestCase):
    """Die Opt-in-Auto-Zuordnung -- Default aus, nur stark, nur eindeutig."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        self.document = Document.objects.create(title="Neue Mahnung", owner=self.user)
        self.document.visibility = Document.Visibility.PRIVATE
        self.document.save(update_fields=["visibility"])

    def test_default_is_a_suggestion_only(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        self.assertEqual(auto_assign_from_references(self.document, self.user), [])
        self.assertEqual(list(self.document.vorgaenge.all()), [])

    @override_settings(FINDUS_REFERENCE_AUTO_ASSIGN=True)
    def test_unique_strong_match_assigns(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        applied = auto_assign_from_references(self.document, self.user)

        self.assertEqual([s["target"] for s in applied], [self.vorgang])
        self.assertEqual(list(self.document.vorgaenge.all()), [self.vorgang])

    @override_settings(FINDUS_REFERENCE_AUTO_ASSIGN=True)
    def test_ambiguous_match_stays_a_suggestion(self):
        other = Vorgang.objects.create(name="Zweite Akte, gleiches Aktenzeichen")
        set_owner_reference(
            other,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        self.assertEqual(auto_assign_from_references(self.document, self.user), [])
        self.assertEqual(list(self.document.vorgaenge.all()), [])

    @override_settings(FINDUS_REFERENCE_AUTO_ASSIGN=True)
    def test_weak_reference_type_is_never_enough(self):
        weak_vorgang = Vorgang.objects.create(name="Rechnungsablage")
        set_owner_reference(
            weak_vorgang,
            reference_type=DocumentReference.Type.RECHNUNGSNUMMER,
            value="2024/17",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.RECHNUNGSNUMMER,
            value="2024/17",
        )

        self.assertEqual(auto_assign_from_references(self.document, self.user), [])
        self.assertEqual(list(self.document.vorgaenge.all()), [])

    @override_settings(FINDUS_REFERENCE_AUTO_ASSIGN=True)
    def test_auto_assignment_also_teaches_the_target(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.FORDERUNGSNUMMER,
            value="F-77",
        )

        auto_assign_from_references(self.document, self.user)

        self.assertIn(
            "F-77",
            self.vorgang.references.values_list("value_normalized", flat=True),
        )


class ReferenceSuggestionViewTests(TestCase):
    """Der Ein-Klick-Vorschlag am Dokument-Detail."""

    def setUp(self):
        self.department = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)
        self.client.force_login(self.user)

        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        self.document = Document.objects.create(
            title="Zweite Mahnung", visibility=Document.Visibility.DEPARTMENT
        )
        self.document.departments.add(self.department)
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.FORDERUNGSNUMMER,
            value="F-77",
        )

    def _assign_url(self, scope, target_pk):
        return reverse(
            "documents:reference_assign", args=[self.document.pk, scope, target_pk]
        )

    def test_detail_offers_the_suggestion(self):
        response = self.client.get(
            reverse("documents:detail", args=[self.document.pk])
        )

        self.assertContains(response, "Gehört vermutlich zu Vorgang")
        self.assertContains(response, self._assign_url(SCOPE_VORGANG, self.vorgang.pk))

    def test_one_click_assigns_and_learns(self):
        response = self.client.post(self._assign_url(SCOPE_VORGANG, self.vorgang.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.document.vorgaenge.all()), [self.vorgang])
        self.assertIn(
            "F-77", self.vorgang.references.values_list("value_normalized", flat=True)
        )
        # Zuordnungs-Anzeige wandert als Out-of-Band-Swap mit.
        self.assertContains(response, 'id="document-meta"')
        self.assertNotContains(response, "Gehört vermutlich zu Vorgang")

    def test_a_target_that_was_never_suggested_is_not_assigned(self):
        stranger = Vorgang.objects.create(name="Unbeteiligt")

        response = self.client.post(self._assign_url(SCOPE_VORGANG, stranger.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.document.vorgaenge.all()), [])

    def test_unknown_scope_is_a_404(self):
        response = self.client.post(
            reverse(
                "documents:reference_assign",
                args=[self.document.pk, "tag", self.vorgang.pk],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_invisible_document_cannot_be_assigned(self):
        foreign = Document.objects.create(
            title="Fremd", visibility=Document.Visibility.DEPARTMENT
        )
        foreign.departments.add(Department.objects.create(name="Dept B"))

        response = self.client.post(
            reverse(
                "documents:reference_assign",
                args=[foreign.pk, SCOPE_VORGANG, self.vorgang.pk],
            )
        )

        self.assertEqual(response.status_code, 404)

    def test_assigning_via_the_meta_form_also_teaches(self):
        self.client.post(
            reverse("documents:meta", args=[self.document.pk]),
            {"vorgaenge": [self.vorgang.pk]},
        )

        self.assertIn(
            "F-77", self.vorgang.references.values_list("value_normalized", flat=True)
        )


class OwnerReferenceHubViewTests(TestCase):
    """Der Pflege-Block an Vorgang-Hub (#1040) und Kontakt-Hub (#1041)."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="x")
        self.client.force_login(self.user)
        self.vorgang = Vorgang.objects.create(name="Inkasso Meier")
        self.correspondent = Correspondent.objects.create(name="Stadtwerke")

    def test_vorgang_hub_shows_the_block(self):
        set_owner_reference(
            self.vorgang,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )

        response = self.client.get(
            reverse("documents:vorgang_detail", args=[self.vorgang.pk])
        )

        self.assertContains(response, "Az. 123/45")
        self.assertContains(response, 'id="vorgang-references"')

    def test_correspondent_hub_shows_the_block(self):
        response = self.client.get(
            reverse("documents:correspondent_detail", args=[self.correspondent.pk])
        )

        self.assertContains(response, 'id="correspondent-references"')

    def test_create_and_delete_on_the_vorgang(self):
        self.client.post(
            reverse("documents:vorgang_reference_create", args=[self.vorgang.pk]),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "Az. 123/45"},
        )
        reference = self.vorgang.references.get()
        self.assertEqual(reference.value_normalized, "123/45")
        self.assertEqual(reference.source, OwnerReference.Source.MANUAL)

        self.client.post(
            reverse(
                "documents:vorgang_reference_delete",
                args=[self.vorgang.pk, reference.pk],
            )
        )
        self.assertEqual(self.vorgang.references.count(), 0)

    def test_create_on_the_correspondent(self):
        self.client.post(
            reverse(
                "documents:correspondent_reference_create",
                args=[self.correspondent.pk],
            ),
            {"type": DocumentReference.Type.KUNDENNUMMER, "value": "Kd.-Nr. 4711"},
        )

        self.assertEqual(
            CorrespondentReference.objects.get().value_normalized, "4711"
        )

    def test_empty_value_reports_an_error(self):
        response = self.client.post(
            reverse("documents:vorgang_reference_create", args=[self.vorgang.pk]),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "   "},
        )

        self.assertContains(response, "Bitte eine Kennung eingeben.")
        self.assertEqual(self.vorgang.references.count(), 0)

    def test_type_from_the_wrong_scope_reports_an_error(self):
        response = self.client.post(
            reverse("documents:vorgang_reference_create", args=[self.vorgang.pk]),
            {"type": DocumentReference.Type.KUNDENNUMMER, "value": "4711"},
        )

        self.assertContains(response, "gehört nicht an dieses Ziel")
        self.assertEqual(self.vorgang.references.count(), 0)

    def test_reference_of_another_vorgang_cannot_be_deleted(self):
        other = Vorgang.objects.create(name="Andere Akte")
        reference = set_owner_reference(
            other,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        response = self.client.post(
            reverse(
                "documents:vorgang_reference_delete",
                args=[self.vorgang.pk, reference.pk],
            )
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(other.references.count(), 1)

    def test_login_is_required(self):
        self.client.logout()

        response = self.client.post(
            reverse("documents:vorgang_reference_create", args=[self.vorgang.pk]),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "123/45"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.vorgang.references.count(), 0)
