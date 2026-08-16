from django.test import SimpleTestCase

from .table_plausibility import check_unit_plausibility, find_row_shifts, parse_markdown_tables


class ParseMarkdownTablesTests(SimpleTestCase):
    def test_extracts_header_and_rows(self):
        markdown = (
            "# Titel\n\n"
            "| Bezeichnung | Ergebnis | Referenzbereich | Einheit |\n"
            "| --- | --- | --- | --- |\n"
            "| GOT (ASAT) | < 50 | < 50 | U/l |\n"
            "| GPT (ALAT) | < 50 | < 50 | U/l |\n"
        )
        tables = parse_markdown_tables(markdown)

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].header, ["Bezeichnung", "Ergebnis", "Referenzbereich", "Einheit"])
        self.assertEqual(
            tables[0].rows,
            [
                ["GOT (ASAT)", "< 50", "< 50", "U/l"],
                ["GPT (ALAT)", "< 50", "< 50", "U/l"],
            ],
        )

    def test_missing_cell_stays_empty_instead_of_shifting(self):
        # #1152: eine fehlende Material-Angabe bleibt eine leere Zelle,
        # rueckt nicht die Referenzbereichs-/Einheitsspalte nach vorn.
        markdown = (
            "| Bezeichnung | Ergebnis | Referenzbereich | Einheit | Material |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| Kreatinin | 0,9 | 0,7 - 1,2 | mg/dl |  |\n"
        )
        tables = parse_markdown_tables(markdown)

        self.assertEqual(len(tables[0].rows[0]), 5)
        self.assertEqual(tables[0].rows[0][4], "")

    def test_ignores_prose_paragraphs_outside_tables(self):
        markdown = "Ein Fließtext-Absatz ohne Tabelle.\n\nNoch ein Absatz."
        self.assertEqual(parse_markdown_tables(markdown), [])

    def test_multiple_tables_are_each_extracted(self):
        markdown = (
            "## Seite 1\n\n"
            "| A | B |\n"
            "| --- | --- |\n"
            "| 1 | 2 |\n\n"
            "## Seite 2\n\n"
            "| C | D |\n"
            "| --- | --- |\n"
            "| 3 | 4 |\n"
        )
        tables = parse_markdown_tables(markdown)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[1].header, ["C", "D"])


class CheckUnitPlausibilityTests(SimpleTestCase):
    def test_flags_a_row_whose_unit_deviates_from_the_table_majority(self):
        # Das reale Beispiel aus #1152: eine eingeschobene Zeile traegt
        # "mg/dl" in einer ansonsten durchgaengigen "U/l"-Tabelle.
        markdown = (
            "| Bezeichnung | Ergebnis | Referenzbereich | Einheit |\n"
            "| --- | --- | --- | --- |\n"
            "| GOT (ASAT) | < 50 | > 200 | mg/dl |\n"
            "| GPT (ALAT) | < 50 | < 50 | U/l |\n"
            "| gamma-GT | < 50 | < 60 | U/l |\n"
            "| AP | < 60 | 40 - 130 | U/l |\n"
        )
        tables = parse_markdown_tables(markdown)

        flags = check_unit_plausibility(tables)

        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].row, "GOT (ASAT)")
        self.assertEqual(flags[0].unit, "mg/dl")
        self.assertEqual(flags[0].expected_unit, "U/l")

    def test_no_flags_when_units_are_consistent(self):
        markdown = (
            "| Bezeichnung | Ergebnis | Einheit |\n"
            "| --- | --- | --- |\n"
            "| A | 1 | U/l |\n"
            "| B | 2 | U/l |\n"
        )
        tables = parse_markdown_tables(markdown)
        self.assertEqual(check_unit_plausibility(tables), [])

    def test_no_flags_without_a_clear_majority(self):
        # Zwei Zeilen, zwei unterschiedliche Einheiten -- kein Mehrheitswert,
        # gegen den man eine Abweichung feststellen koennte.
        markdown = (
            "| Bezeichnung | Ergebnis | Einheit |\n"
            "| --- | --- | --- |\n"
            "| A | 1 | U/l |\n"
            "| B | 2 | mg/dl |\n"
        )
        tables = parse_markdown_tables(markdown)
        self.assertEqual(check_unit_plausibility(tables), [])

    def test_ignores_tables_without_a_unit_column(self):
        markdown = "| Bezeichnung | Wert |\n| --- | --- |\n| A | 1 |\n| B | 2 |\n"
        tables = parse_markdown_tables(markdown)
        self.assertEqual(check_unit_plausibility(tables), [])


class FindRowShiftsTests(SimpleTestCase):
    def test_flags_a_row_whose_numbers_are_present_but_far_apart(self):
        # Kuenstlich verschobenes Beispiel: die Zeile behauptet "GOT: < 50,
        # Referenz > 200", aber im Text-Layer stehen "50" und "200" weit
        # auseinander (verschiedene Messwerte, keine gemeinsame Zeile).
        markdown = (
            "| Bezeichnung | Ergebnis | Referenzbereich |\n"
            "| --- | --- | --- |\n"
            "| GOT (ASAT) | 50 | 200 |\n"
        )
        reference_text = (
            "GOT (ASAT) Ergebnis < 50 U/l Normalbereich. "
            + ("Fuellwort " * 40)
            + "Triglyceride erhoeht ( > 200 ) mg/dl am Seitenende."
        )
        tables = parse_markdown_tables(markdown)

        flags = find_row_shifts(tables, reference_text)

        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].row, "GOT (ASAT)")
        self.assertEqual(set(flags[0].numbers), {"50", "200"})

    def test_passes_when_numbers_are_close_together(self):
        markdown = (
            "| Bezeichnung | Ergebnis | Referenzbereich |\n"
            "| --- | --- | --- |\n"
            "| GOT (ASAT) | 50 | 200 |\n"
        )
        reference_text = "GOT (ASAT) 50 Referenz 200 U/l"

        self.assertEqual(find_row_shifts(parse_markdown_tables(markdown), reference_text), [])

    def test_skips_rows_where_a_number_is_missing_entirely_from_the_reference(self):
        # Fehlt eine Zahl komplett, ist das der Fall von
        # `number_crosscheck.find_unmatched_numbers`, kein Zeilenversatz.
        markdown = (
            "| Bezeichnung | Ergebnis | Referenzbereich |\n"
            "| --- | --- | --- |\n"
            "| GOT (ASAT) | 999 | 200 |\n"
        )
        reference_text = "irgendwas mit 200 aber nicht der andere Wert"

        self.assertEqual(find_row_shifts(parse_markdown_tables(markdown), reference_text), [])

    def test_skips_rows_with_fewer_than_two_significant_numbers(self):
        markdown = "| Bezeichnung | Ergebnis |\n| --- | --- |\n| Notiz | ohne Zahl |\n"
        self.assertEqual(
            find_row_shifts(parse_markdown_tables(markdown), "beliebiger Referenztext"), []
        )
