"""Ableitung des Dokumentdatums (#1141).

Geprüft wird ausschließlich die Ableitungslogik, nie eine Modellantwort: die
Testfälle bilden nach, was ein Modell für ein realistisches Dokument liefert
(der Text steht jeweils als Zitat im Docstring), und behaupten, welches Datum
der Code daraus wählt. Genau darum liegt die Auswahl im Code -- so bleibt sie
über einen Modellwechsel hinweg prüfbar.
"""

import datetime

from django.test import SimpleTestCase, override_settings

from .document_dates import (
    DateCandidate,
    DateKind,
    candidates_from_reply,
    normalize_kind,
    parse_date,
    period_bounds,
    resolve_document_date,
)


class ParseDateTests(SimpleTestCase):
    def test_parses_iso_format(self):
        self.assertEqual(parse_date("2023-11-30"), datetime.date(2023, 11, 30))

    def test_parses_german_format(self):
        """Die betroffenen Dokumente schreiben "30.11.2023"; ein Modell

        reicht das trotz Prompt gelegentlich unverändert durch.
        """
        self.assertEqual(parse_date("30.11.2023"), datetime.date(2023, 11, 30))

    def test_returns_none_for_prose_and_empty(self):
        for value in ("", None, "irgendwann letzten Monat", "2023-13-45"):
            with self.subTest(value=value):
                self.assertIsNone(parse_date(value))


class NormalizeKindTests(SimpleTestCase):
    def test_maps_synonyms(self):
        self.assertEqual(normalize_kind("Rechnungsdatum"), DateKind.BELEG)
        self.assertEqual(normalize_kind("Zeitraum-Ende"), DateKind.ZEITRAUM_ENDE)
        self.assertEqual(normalize_kind("Gedruckt am"), DateKind.ERSTELLT)

    def test_unknown_kind_is_dropped(self):
        self.assertEqual(normalize_kind("posteingang"), "")
        self.assertEqual(normalize_kind(None), "")


class CandidatesFromReplyTests(SimpleTestCase):
    def test_model_document_date_becomes_a_candidate(self):
        candidates = candidates_from_reply([], model_date="2026-08-01")

        self.assertEqual(
            candidates, [DateCandidate(DateKind.MODELL, datetime.date(2026, 8, 1))]
        )

    def test_unknown_kinds_and_unparsable_values_are_skipped(self):
        candidates = candidates_from_reply(
            [
                {"kind": "posteingang", "value": "2026-01-01"},
                {"kind": "belegdatum", "value": "kein Datum"},
                {"kind": "belegdatum", "value": "2026-03-04"},
                "kein Objekt",
            ]
        )

        self.assertEqual(
            candidates, [DateCandidate(DateKind.BELEG, datetime.date(2026, 3, 4))]
        )

    def test_non_list_reply_is_tolerated(self):
        self.assertEqual(candidates_from_reply(None), [])
        self.assertEqual(candidates_from_reply("2026-01-01"), [])


class PeriodBoundsTests(SimpleTestCase):
    def test_returns_outer_frame_of_all_bounds(self):
        candidates = [
            DateCandidate(DateKind.ZEITRAUM_BEGINN, datetime.date(2023, 11, 1)),
            DateCandidate(DateKind.ZEITRAUM_BEGINN, datetime.date(2023, 11, 15)),
            DateCandidate(DateKind.ZEITRAUM_ENDE, datetime.date(2023, 11, 14)),
            DateCandidate(DateKind.ZEITRAUM_ENDE, datetime.date(2023, 11, 30)),
        ]

        self.assertEqual(
            period_bounds(candidates),
            (datetime.date(2023, 11, 1), datetime.date(2023, 11, 30)),
        )

    def test_without_period_both_bounds_are_none(self):
        candidates = [DateCandidate(DateKind.BELEG, datetime.date(2026, 3, 4))]

        self.assertEqual(period_bounds(candidates), (None, None))


@override_settings(FINDUS_DOCUMENT_DATE_UPLOAD_TOLERANCE_DAYS=1)
class ResolveDocumentDateTests(SimpleTestCase):
    """Die vier Belegarten aus #1141, jeweils als das, was ein Modell für den

    zitierten Text zurückgibt.
    """

    def test_kontoauszug_uses_end_of_period_not_the_download_day(self):
        """"Kontoauszug vom 01.11.2023 bis 30.11.2023" / "Erstellt am:

        15.08.2026" -- der Auslöser des Tickets: hochgeladen am 15.08.2026,
        maßgeblich ist trotzdem das Ende des Abrechnungszeitraums.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "zeitraum_beginn", "value": "2023-11-01"},
                    {"kind": "zeitraum_ende", "value": "2023-11-30"},
                    {"kind": "erstellt", "value": "2026-08-15"},
                ],
                model_date="2026-08-15",
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2023, 11, 30))
        self.assertEqual(resolved.kind, DateKind.ZEITRAUM_ENDE)
        self.assertFalse(resolved.upload_conflict)

    def test_rechnung_keeps_its_invoice_date_despite_a_print_date(self):
        """"Rechnungsdatum: 04.03.2026" / "Gedruckt am 06.03.2026"."""
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "belegdatum", "value": "2026-03-04"},
                    {"kind": "erstellt", "value": "2026-03-06"},
                ],
                model_date="2026-03-04",
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2026, 3, 4))
        self.assertEqual(resolved.kind, DateKind.BELEG)

    def test_anschreiben_uses_the_letterhead_date(self):
        """"Landshut, den 4. März 2026" -- ein Anschreiben ohne Belegdatum."""
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "briefkopf", "value": "2026-03-04"},
                    {"kind": "erstellt", "value": "2026-03-05"},
                ]
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2026, 3, 4))
        self.assertEqual(resolved.kind, DateKind.BRIEFKOPF)

    def test_only_a_created_date_is_still_used(self):
        """"Stand: 12.01.2026" und sonst nichts -- das Erstelldatum ist die

        letzte Wahl, aber besser als gar kein Datum.
        """
        resolved = resolve_document_date(
            candidates_from_reply([{"kind": "stand", "value": "2026-01-12"}]),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2026, 1, 12))
        self.assertEqual(resolved.kind, DateKind.ERSTELLT)

    def test_no_dates_at_all_resolves_to_nothing(self):
        resolved = resolve_document_date([], uploaded_on=datetime.date(2026, 8, 15))

        self.assertIsNone(resolved.date)
        self.assertEqual(resolved.kind, "")

    def test_plausibility_check_overrides_a_date_on_the_upload_day(self):
        """Das Modell hält das Abrufdatum für das Belegdatum -- die Rangfolge

        allein hilft dann nicht, weil die Fehlwahl schon ganz oben steht.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "belegdatum", "value": "2026-08-15"},
                    {"kind": "zeitraum_ende", "value": "2023-11-30"},
                ]
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2023, 11, 30))
        self.assertEqual(resolved.kind, DateKind.ZEITRAUM_ENDE)
        self.assertTrue(resolved.upload_conflict)
        self.assertEqual(resolved.rejected.date, datetime.date(2026, 8, 15))
        self.assertEqual(resolved.rejected.kind, DateKind.BELEG)

    def test_plausibility_check_covers_the_day_before_the_upload(self):
        """Der Auszug wurde am Vorabend gezogen und am Folgetag hochgeladen --

        dasselbe Muster, ein Tag daneben.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "belegdatum", "value": "2026-08-14"},
                    {"kind": "zeitraum_ende", "value": "2023-11-30"},
                ]
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2023, 11, 30))
        self.assertTrue(resolved.upload_conflict)

    def test_plausibility_check_keeps_the_date_without_an_alternative(self):
        """Nur ein Erstelldatum, und das liegt am Upload-Tag: es bleibt (ein

        Datum ist besser als keins), die Herkunft macht die Unsicherheit
        sichtbar.
        """
        resolved = resolve_document_date(
            candidates_from_reply([{"kind": "erstellt", "value": "2026-08-15"}]),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2026, 8, 15))
        self.assertEqual(resolved.kind, DateKind.ERSTELLT)
        self.assertFalse(resolved.upload_conflict)

    def test_plausibility_check_needs_an_alternative_off_the_upload_day(self):
        """Alle Quellen nennen den Upload-Tag -- dann gibt es nichts zu

        korrigieren, und die Rangfolge entscheidet wie sonst auch.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "belegdatum", "value": "2026-08-15"},
                    {"kind": "erstellt", "value": "2026-08-15"},
                ]
            ),
            uploaded_on=datetime.date(2026, 8, 15),
        )

        self.assertEqual(resolved.date, datetime.date(2026, 8, 15))
        self.assertEqual(resolved.kind, DateKind.BELEG)
        self.assertFalse(resolved.upload_conflict)

    def test_without_an_upload_day_only_the_ranking_applies(self):
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "belegdatum", "value": "2026-08-15"},
                    {"kind": "zeitraum_ende", "value": "2023-11-30"},
                ]
            )
        )

        self.assertEqual(resolved.date, datetime.date(2026, 8, 15))
        self.assertFalse(resolved.upload_conflict)

    def test_period_start_is_never_the_document_date(self):
        """Ein Zeitraum datiert auf sein Ende; sein Beginn taucht in der

        Rangfolge gar nicht auf -- fehlt das Ende, gewinnt lieber das
        Erstelldatum.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "zeitraum_beginn", "value": "2023-11-01"},
                    {"kind": "erstellt", "value": "2026-01-12"},
                ]
            )
        )

        self.assertEqual(resolved.date, datetime.date(2026, 1, 12))
        self.assertEqual(resolved.kind, DateKind.ERSTELLT)

    def test_model_guess_ranks_above_the_print_date(self):
        resolved = resolve_document_date(
            candidates_from_reply(
                [{"kind": "erstellt", "value": "2026-03-06"}],
                model_date="2026-03-04",
            )
        )

        self.assertEqual(resolved.date, datetime.date(2026, 3, 4))
        self.assertEqual(resolved.kind, DateKind.MODELL)

    def test_latest_period_end_wins_over_an_earlier_one(self):
        """Eine mehrseitige Abrechnung wiederholt die Zeitraum-Kopfzeile; das

        Dokument datiert auf das späteste Ende, nicht auf das erstgenannte.
        """
        resolved = resolve_document_date(
            candidates_from_reply(
                [
                    {"kind": "zeitraum_ende", "value": "2023-11-14"},
                    {"kind": "zeitraum_ende", "value": "2023-11-30"},
                ]
            )
        )

        self.assertEqual(resolved.date, datetime.date(2023, 11, 30))


@override_settings(FINDUS_DOCUMENT_DATE_UPLOAD_TOLERANCE_DAYS=0)
class UploadToleranceSettingTests(SimpleTestCase):
    def test_tolerance_zero_limits_the_check_to_the_upload_day(self):
        candidates = candidates_from_reply(
            [
                {"kind": "belegdatum", "value": "2026-08-14"},
                {"kind": "zeitraum_ende", "value": "2023-11-30"},
            ]
        )

        resolved = resolve_document_date(
            candidates, uploaded_on=datetime.date(2026, 8, 15)
        )

        self.assertEqual(resolved.date, datetime.date(2026, 8, 14))
        self.assertFalse(resolved.upload_conflict)
