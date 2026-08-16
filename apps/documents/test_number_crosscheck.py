from django.test import SimpleTestCase

from .number_crosscheck import find_unmatched_numbers


class FindUnmatchedNumbersTests(SimpleTestCase):
    def test_identical_numbers_report_no_deviation(self):
        self.assertEqual(
            find_unmatched_numbers("Betrag 1.234,56 EUR", "Betrag 1.234,56 EUR"), []
        )

    def test_thousands_separator_and_decimal_style_differences_are_ignored(self):
        # 1.234,56 (deutsch) vs. 1234.56 (englisch) -- gleiche Ziffernfolge.
        self.assertEqual(find_unmatched_numbers("1.234,56", "1234.56"), [])

    def test_currency_symbols_and_surrounding_whitespace_are_ignored(self):
        self.assertEqual(find_unmatched_numbers("1.234,56 €", "  1.234,56  EUR"), [])

    def test_a_genuinely_different_number_is_reported(self):
        self.assertEqual(find_unmatched_numbers("Betrag 999,00 EUR", "Betrag 111,00 EUR"), ["999,00"])

    def test_short_numbers_below_the_significance_threshold_are_ignored(self):
        # Seitenzahlen, Aufzaehlungspunkte -- "1", "12" sind kein Befund.
        self.assertEqual(find_unmatched_numbers("Seite 1 von 12", "vollkommen anderer Text"), [])

    def test_duplicate_unmatched_numbers_are_reported_once(self):
        self.assertEqual(
            find_unmatched_numbers("999,00 und nochmal 999,00", "nichts Passendes"),
            ["999,00"],
        )

    def test_dates_are_compared_like_any_other_number(self):
        self.assertEqual(find_unmatched_numbers("faellig am 31.12.2024", "faellig am 31.12.2024"), [])
        self.assertEqual(
            find_unmatched_numbers("faellig am 31.12.2024", "faellig am 15.06.2023"),
            ["31.12.2024"],
        )

    def test_empty_reference_text_reports_every_significant_number(self):
        self.assertEqual(find_unmatched_numbers("Konto 123456789", ""), ["123456789"])
