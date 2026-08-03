from django.test import TestCase

from .text_sanitize import clean_json, clean_text


class CleanTextTests(TestCase):
    def test_strips_nul_bytes_without_touching_surrounding_text(self):
        self.assertEqual(clean_text("Rech\x00nung ueber 42\x00 EUR"), "Rechnung ueber 42 EUR")

    def test_strips_other_c0_control_characters(self):
        self.assertEqual(clean_text("A\x01B\x1fC\x7fD"), "ABCD")

    def test_keeps_tab_newline_and_carriage_return(self):
        text = "Zeile 1\nZeile 2\r\n\tEingerueckt"
        self.assertEqual(clean_text(text), text)

    def test_text_without_forbidden_characters_is_unchanged(self):
        text = "Ein ganz normaler Rechnungstext mit Umlauten: ae oe ue ss."
        self.assertEqual(clean_text(text), text)


class CleanJsonTests(TestCase):
    def test_cleans_strings_nested_in_dicts_and_lists(self):
        parsed = {
            "title": "Rech\x00nung",
            "key_facts": {"sender_name": "Acme\x00 GmbH"},
            "tag_suggestions": [{"name": "Dring\x00end", "confidence": 0.9}],
        }

        cleaned = clean_json(parsed)

        self.assertEqual(cleaned["title"], "Rechnung")
        self.assertEqual(cleaned["key_facts"]["sender_name"], "Acme GmbH")
        self.assertEqual(cleaned["tag_suggestions"][0]["name"], "Dringend")
        self.assertEqual(cleaned["tag_suggestions"][0]["confidence"], 0.9)

    def test_leaves_non_string_values_untouched(self):
        parsed = {"confidence": 0.4, "count": 3, "ok": True, "nothing": None}

        self.assertEqual(clean_json(parsed), parsed)
