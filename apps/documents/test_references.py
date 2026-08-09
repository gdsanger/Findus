from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Department

from .models import (
    Correspondent,
    Document,
    DocumentReference,
    normalize_reference_value,
)
from .references import (
    SCOPE_KONTAKT,
    SCOPE_VORGANG,
    documents_with_reference,
    scope_for_type,
    set_reference,
    shared_reference_groups,
    shared_reference_matches,
)

User = get_user_model()


class NormalizeReferenceValueTests(TestCase):
    """Der Matching-Schlüssel (#1099) -- hier entscheidet sich, ob "Az.

    123/45" und "123 / 45" als dieselbe Kennung erkannt werden.
    """

    def test_strips_label_prefix(self):
        self.assertEqual(normalize_reference_value("Az. 123/45"), "123/45")
        self.assertEqual(normalize_reference_value("Aktenzeichen: 123/45"), "123/45")
        self.assertEqual(normalize_reference_value("Kd.-Nr. 4711"), "4711")
        self.assertEqual(normalize_reference_value("Kundennummer 4711"), "4711")
        self.assertEqual(normalize_reference_value("Nr. 4711"), "4711")

    def test_same_key_for_different_spellings(self):
        self.assertEqual(
            normalize_reference_value("AZ 123/45"),
            normalize_reference_value("123 / 45"),
        )

    def test_iban_loses_grouping_spaces(self):
        self.assertEqual(
            normalize_reference_value("IBAN DE02 1203 0000 0000 2020 51"),
            "DE02120300000000202051",
        )

    def test_keeps_separators_inside_the_value(self):
        self.assertEqual(normalize_reference_value("2026-100/7"), "2026-100/7")

    def test_label_without_separator_is_not_stripped(self):
        """"AZUBI4711" fängt zufällig mit "AZ" an -- daraus darf kein
        "UBI4711" werden.
        """
        self.assertEqual(normalize_reference_value("AZUBI4711"), "AZUBI4711")

    def test_value_that_is_only_a_label_survives(self):
        self.assertEqual(normalize_reference_value("AZ"), "AZ")

    def test_empty_stays_empty(self):
        self.assertEqual(normalize_reference_value("   "), "")
        self.assertEqual(normalize_reference_value(None), "")


class ReferenceTypeScopeTests(TestCase):
    """Typ→Scope-Mapping (#1099) -- Grundlage der späteren
    Auto-Gruppierung, deshalb schon hier festgenagelt.
    """

    def test_case_related_types_point_at_a_vorgang(self):
        for reference_type in (
            DocumentReference.Type.AKTENZEICHEN,
            DocumentReference.Type.FORDERUNGSNUMMER,
            DocumentReference.Type.VERTRAGSNUMMER,
            DocumentReference.Type.BELEGNUMMER,
            DocumentReference.Type.RECHNUNGSNUMMER,
        ):
            self.assertEqual(scope_for_type(reference_type), SCOPE_VORGANG)

    def test_party_related_types_point_at_a_kontakt(self):
        self.assertEqual(
            scope_for_type(DocumentReference.Type.KUNDENNUMMER), SCOPE_KONTAKT
        )
        self.assertEqual(scope_for_type(DocumentReference.Type.IBAN), SCOPE_KONTAKT)

    def test_ambiguous_types_have_no_scope(self):
        self.assertIsNone(scope_for_type(DocumentReference.Type.IHR_ZEICHEN))
        self.assertIsNone(scope_for_type(DocumentReference.Type.UNSER_ZEICHEN))
        self.assertIsNone(scope_for_type(DocumentReference.Type.SONSTIGE))
        self.assertIsNone(scope_for_type("gibt-es-nicht"))


class SetReferenceTests(TestCase):
    def setUp(self):
        self.document = Document.objects.create(title="Bescheid")

    def test_creates_with_normalized_and_raw_value(self):
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123/45",
        )

        self.assertEqual(reference.value_raw, "Az. 123/45")
        self.assertEqual(reference.value_normalized, "123/45")
        self.assertEqual(reference.source, DocumentReference.Source.MANUAL)

    def test_same_value_twice_updates_instead_of_duplicating(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="Az. 123 / 45",
            role=DocumentReference.Role.MINE,
        )

        self.assertEqual(self.document.references.count(), 1)
        self.assertEqual(reference.value_raw, "Az. 123 / 45")
        self.assertEqual(reference.role, DocumentReference.Role.MINE)

    def test_unknown_type_falls_back_to_sonstige(self):
        reference = set_reference(
            self.document, reference_type="mondzahl", value="42"
        )

        self.assertEqual(reference.type, DocumentReference.Type.SONSTIGE)

    def test_unknown_role_is_dropped_instead_of_guessed(self):
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
            role="vielleicht",
        )

        self.assertEqual(reference.role, "")

    def test_empty_value_creates_nothing(self):
        self.assertIsNone(
            set_reference(
                self.document,
                reference_type=DocumentReference.Type.AKTENZEICHEN,
                value="   ",
            )
        )
        self.assertEqual(self.document.references.count(), 0)


class SharedReferenceTests(TestCase):
    """Der exakte Match (#1099) inklusive `visible_to` -- eine geteilte
    Kennung darf kein Fenster auf ein fremdes Dokument sein.
    """

    def setUp(self):
        self.dept_a = Department.objects.create(name="Dept A")
        self.dept_b = Department.objects.create(name="Dept B")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.dept_a)

        self.document = self._document("Mahnung", self.dept_a)
        self.same_case = self._document("Klageschrift", self.dept_a)
        self.foreign = self._document("Fremdakte", self.dept_b)

        for document in (self.document, self.same_case, self.foreign):
            set_reference(
                document,
                reference_type=DocumentReference.Type.AKTENZEICHEN,
                value="Az. 123/45",
            )

    def _document(self, title, department):
        document = Document.objects.create(
            title=title, visibility=Document.Visibility.DEPARTMENT
        )
        document.departments.add(department)
        return document

    def test_groups_by_reference_and_finds_exact_match(self):
        groups = shared_reference_groups(self.user, self.document)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["reference"].value_normalized, "123/45")
        self.assertEqual([d.pk for d in groups[0]["documents"]], [self.same_case.pk])

    def test_differently_spelled_reference_still_matches(self):
        other = self._document("Anwaltsschreiben", self.dept_a)
        set_reference(
            other,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123 / 45",
        )

        groups = shared_reference_groups(self.user, self.document)

        self.assertIn(other.pk, [d.pk for d in groups[0]["documents"]])

    def test_same_value_under_a_different_type_does_not_match(self):
        other = self._document("Rechnung", self.dept_a)
        set_reference(
            other,
            reference_type=DocumentReference.Type.RECHNUNGSNUMMER,
            value="123/45",
        )

        groups = shared_reference_groups(self.user, self.document)

        self.assertNotIn(other.pk, [d.pk for d in groups[0]["documents"]])

    def test_invisible_documents_are_never_returned(self):
        groups = shared_reference_groups(self.user, self.document)

        self.assertNotIn(self.foreign.pk, [d.pk for d in groups[0]["documents"]])

    def test_document_without_references_has_no_groups(self):
        plain = self._document("Ohne Kennung", self.dept_a)

        self.assertEqual(shared_reference_groups(self.user, plain), [])

    @override_settings(FINDUS_REFERENCE_MATCH_LIMIT=1)
    def test_reports_truncation_instead_of_hiding_it(self):
        extra = self._document("Noch eine Akte", self.dept_a)
        set_reference(
            extra,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        groups = shared_reference_groups(self.user, self.document)

        self.assertEqual(len(groups[0]["documents"]), 1)
        self.assertTrue(groups[0]["truncated"])

    def test_matches_list_one_pair_per_counterpart(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )
        set_reference(
            self.same_case,
            reference_type=DocumentReference.Type.KUNDENNUMMER,
            value="4711",
        )

        matches = shared_reference_matches(self.user, self.document)

        self.assertEqual([other.pk for other, _ in matches], [self.same_case.pk])

    def test_documents_with_reference_is_scoped_and_exact(self):
        found = documents_with_reference(
            self.user, DocumentReference.Type.AKTENZEICHEN, "Az. 123/45"
        )

        self.assertEqual(
            sorted(found.values_list("pk", flat=True)),
            sorted([self.document.pk, self.same_case.pk]),
        )


class DocumentReferenceViewTests(TestCase):
    """Manuelles Pflegen im Dokument-Detail (#1099)."""

    def setUp(self):
        self.department = Department.objects.create(name="Dept A")
        self.user = User.objects.create_user(username="alice", password="x")
        self.user.departments.add(self.department)
        self.client.force_login(self.user)

        self.document = Document.objects.create(
            title="Mahnung", visibility=Document.Visibility.DEPARTMENT
        )
        self.document.departments.add(self.department)

        self.other_user = User.objects.create_user(username="bob", password="x")
        self.other_user.departments.add(Department.objects.create(name="Dept B"))

    def _url(self, name, *args):
        return reverse(f"documents:{name}", args=[self.document.pk, *args])

    def test_detail_shows_the_reference_block(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        response = self.client.get(self._url("detail"))

        self.assertContains(response, "Verwandte Dokumente über gemeinsame Kennungen")
        self.assertContains(response, "123/45")

    def test_create_adds_a_manual_reference(self):
        response = self.client.post(
            self._url("reference_create"),
            {"type": DocumentReference.Type.KUNDENNUMMER, "value": "Kd.-Nr. 4711"},
        )

        self.assertEqual(response.status_code, 200)
        reference = self.document.references.get()
        self.assertEqual(reference.type, DocumentReference.Type.KUNDENNUMMER)
        self.assertEqual(reference.value_normalized, "4711")
        self.assertEqual(reference.source, DocumentReference.Source.MANUAL)

    def test_create_without_a_value_reports_an_error(self):
        response = self.client.post(
            self._url("reference_create"),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "  "},
        )

        self.assertContains(response, "Bitte eine Kennung eingeben.")
        self.assertEqual(self.document.references.count(), 0)

    def test_update_corrects_value_type_and_role(self):
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.SONSTIGE,
            value="123/54",
        )

        self.client.post(
            self._url("reference_update", reference.pk),
            {
                "type": DocumentReference.Type.AKTENZEICHEN,
                "value": "123/45",
                "role": DocumentReference.Role.THEIRS,
            },
        )

        corrected = self.document.references.get()
        self.assertEqual(corrected.type, DocumentReference.Type.AKTENZEICHEN)
        self.assertEqual(corrected.value_normalized, "123/45")
        self.assertEqual(corrected.role, DocumentReference.Role.THEIRS)

    def test_update_with_an_empty_value_changes_nothing(self):
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        response = self.client.post(
            self._url("reference_update", reference.pk),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": ""},
        )

        self.assertContains(response, "Bitte eine Kennung eingeben.")
        self.assertEqual(self.document.references.get().value_normalized, "123/45")

    def test_update_merges_into_an_existing_identical_reference(self):
        first = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        second = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/54",
        )

        self.client.post(
            self._url("reference_update", second.pk),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "123/45"},
        )

        # Die korrigierte Zeile geht in der bereits vorhandenen auf, statt
        # an der UniqueConstraint zu scheitern.
        self.assertEqual(self.document.references.count(), 1)
        self.assertFalse(DocumentReference.objects.filter(pk=second.pk).exists())
        self.assertTrue(DocumentReference.objects.filter(pk=first.pk).exists())

    def test_delete_removes_the_reference(self):
        reference = set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        self.client.post(self._url("reference_delete", reference.pk))

        self.assertEqual(self.document.references.count(), 0)

    def test_reference_of_another_document_cannot_be_deleted(self):
        foreign_document = Document.objects.create(title="Fremd")
        foreign_reference = set_reference(
            foreign_document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="999/99",
        )

        response = self.client.post(
            self._url("reference_delete", foreign_reference.pk)
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            DocumentReference.objects.filter(pk=foreign_reference.pk).exists()
        )

    def test_invisible_document_is_not_editable(self):
        self.client.force_login(self.other_user)

        response = self.client.post(
            self._url("reference_create"),
            {"type": DocumentReference.Type.AKTENZEICHEN, "value": "123/45"},
        )

        self.assertEqual(response.status_code, 404)

    def test_related_document_of_another_department_is_not_listed(self):
        set_reference(
            self.document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )
        foreign_document = Document.objects.create(
            title="Streng geheime Fremdakte",
            visibility=Document.Visibility.DEPARTMENT,
            correspondent=Correspondent.objects.create(name="Fremd GmbH"),
        )
        foreign_document.departments.add(Department.objects.create(name="Dept C"))
        set_reference(
            foreign_document,
            reference_type=DocumentReference.Type.AKTENZEICHEN,
            value="123/45",
        )

        response = self.client.get(self._url("references"))

        self.assertNotContains(response, "Streng geheime Fremdakte")
