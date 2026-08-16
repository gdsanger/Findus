"""Ausgabekontrakt der KI-Vision-Extraktion nach Markdown (#1148).

Getestet wird hier ausschliesslich der Kontrakt -- Prompt, Normalisierung,
Seitenmarker, Fingerabdruck, Datenschutz-Schalter. Der Lauf selbst (Rendern,
Provideraufruf, Speichern) steht in `test_extraction.py`.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from . import vision_markdown
from .models import Document


class PagePromptContractTests(SimpleTestCase):
    """Die Regeln aus #1148 muessen im Prompt tatsaechlich stehen.

    Bewusst als Test und nicht nur als Kommentar: der Prompt ist die
    einzige Stelle, an der das Modell von den Strukturregeln erfaehrt --
    faellt eine beim Umformulieren raus, merkt es sonst niemand, bis der
    naechste Laborbefund wieder als vier getrennte Spaltenlisten
    zurueckkommt.
    """

    def test_asks_for_real_markdown_tables_with_one_row_per_line(self):
        self.assertIn("Markdown-Tabellen", vision_markdown.PAGE_PROMPT)
        self.assertIn("fachlicher Zeile", vision_markdown.PAGE_PROMPT)
        self.assertIn("Referenzbereich", vision_markdown.PAGE_PROMPT)

    def test_keeps_handwriting_out_of_the_table(self):
        self.assertIn("Handschriftliche Vermerke", vision_markdown.PAGE_PROMPT)
        self.assertIn("NICHT in die Tabelle", vision_markdown.PAGE_PROMPT)

    def test_asks_to_mark_uncertain_readings_instead_of_guessing(self):
        self.assertIn("[unsicher:", vision_markdown.PAGE_PROMPT)
        self.assertIn("[unleserlich]", vision_markdown.PAGE_PROMPT)

    def test_asks_to_report_unreadable_pages_with_the_recognised_marker(self):
        self.assertIn(vision_markdown.UNREADABLE_PAGE_MARKER, vision_markdown.PAGE_PROMPT)
        self.assertIn("Erfinde in diesem Fall keinen Inhalt", vision_markdown.PAGE_PROMPT)

    def test_forbids_commenting_and_interpreting(self):
        self.assertIn("Nicht kommentieren", vision_markdown.PAGE_PROMPT)
        self.assertIn("nicht interpretieren", vision_markdown.PAGE_PROMPT)


class NormalizePageMarkdownTests(SimpleTestCase):
    def test_plain_markdown_is_left_alone(self):
        markdown = "| Wert | Einheit |\n| --- | --- |\n| 5,2 | mmol/l |"

        self.assertEqual(vision_markdown.normalize_page_markdown(markdown), markdown)

    def test_surrounding_code_fences_are_removed(self):
        """Ein in ```-Fences gewickeltes Ergebnis wuerde die ganze Seite als
        Quelltextblock rendern statt als Tabelle -- das haeufigste Abweichen
        vom Kontrakt und das mit dem groessten Schaden.
        """
        fenced = "```markdown\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```"

        self.assertEqual(
            vision_markdown.normalize_page_markdown(fenced),
            "| A | B |\n| --- | --- |\n| 1 | 2 |",
        )

    def test_inner_code_fences_survive(self):
        markdown = "Vorher\n\n```\nCode\n```\n\nNachher"

        self.assertEqual(vision_markdown.normalize_page_markdown(markdown), markdown)

    def test_empty_answer_stays_empty(self):
        self.assertEqual(vision_markdown.normalize_page_markdown("   \n "), "")


class UnreadablePageTests(SimpleTestCase):
    def test_marker_with_reason_is_recognised(self):
        page = f"{vision_markdown.UNREADABLE_PAGE_MARKER} (ueberbelichtet)"

        self.assertTrue(vision_markdown.page_is_unreadable(page))

    def test_ordinary_content_is_not_mistaken_for_an_empty_page(self):
        self.assertFalse(vision_markdown.page_is_unreadable("### Befund\n\nText."))

    def test_failed_page_placeholder_names_the_reason(self):
        placeholder = vision_markdown.failed_page_markdown("ProviderError")

        self.assertIn("ProviderError", placeholder)
        self.assertIn("nicht verarbeitet werden", placeholder)


class JoinPagesAsTextTests(SimpleTestCase):
    def test_single_page_gets_no_page_marker(self):
        self.assertEqual(vision_markdown.join_pages_as_text(["Nur eine Seite."]), "Nur eine Seite.")

    def test_multiple_pages_keep_visible_page_boundaries(self):
        joined = vision_markdown.join_pages_as_text(["Eins", "Zwei"])

        self.assertIn("--- Seite 1 ---", joined)
        self.assertIn("--- Seite 2 ---", joined)
        self.assertLess(joined.index("--- Seite 1 ---"), joined.index("--- Seite 2 ---"))

    def test_no_pages_yields_empty_text(self):
        self.assertEqual(vision_markdown.join_pages_as_text([]), "")


class SourceFingerprintTests(TestCase):
    def test_fingerprint_covers_file_and_prompt_version(self):
        document = Document.objects.create(title="Scan", sha256="abc123")

        fingerprint = vision_markdown.source_fingerprint(document)

        self.assertIn("abc123", fingerprint)
        self.assertIn(vision_markdown.PROMPT_VERSION, fingerprint)

    def test_a_different_file_gets_a_different_fingerprint(self):
        one = Document.objects.create(title="A", sha256="aaa")
        other = Document.objects.create(title="B", sha256="bbb")

        self.assertNotEqual(
            vision_markdown.source_fingerprint(one),
            vision_markdown.source_fingerprint(other),
        )

    def test_a_new_prompt_version_invalidates_an_existing_fingerprint(self):
        """Der eigentliche Zweck der Prompt-Version: nach einer fachlichen
        Prompt-Aenderung darf der Bestand wieder erneuert werden, ohne dass
        jemand von Hand nachhaelt, welches Dokument mit welcher Fassung
        erzeugt wurde.
        """
        document = Document.objects.create(
            title="Scan",
            sha256="abc123",
            vision_reextraction_status=Document.VisionReextractionStatus.READY,
        )
        document.vision_reextraction_fingerprint = vision_markdown.source_fingerprint(document)
        self.assertTrue(vision_markdown.is_up_to_date(document))

        with patch.object(vision_markdown, "PROMPT_VERSION", "99"):
            self.assertFalse(vision_markdown.is_up_to_date(document))


class IsUpToDateTests(TestCase):
    def _document(self, **kwargs):
        return Document.objects.create(title="Scan", sha256="abc123", **kwargs)

    def test_ready_run_over_the_same_file_counts_as_up_to_date(self):
        document = self._document(
            vision_reextraction_status=Document.VisionReextractionStatus.READY
        )
        document.vision_reextraction_fingerprint = vision_markdown.source_fingerprint(document)

        self.assertTrue(vision_markdown.is_up_to_date(document))

    def test_failed_run_is_never_up_to_date(self):
        """Sonst wuerde ein Fehlschlag ausgerechnet den Wiederholungsversuch
        blockieren, um den es geht.
        """
        document = self._document(
            vision_reextraction_status=Document.VisionReextractionStatus.FAILED
        )
        document.vision_reextraction_fingerprint = vision_markdown.source_fingerprint(document)

        self.assertFalse(vision_markdown.is_up_to_date(document))

    def test_document_without_a_run_is_not_up_to_date(self):
        self.assertFalse(vision_markdown.is_up_to_date(self._document()))


@override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE=vision_markdown.AUTO_SCOPE_SCANS)
class AutoScopeTests(TestCase):
    """`FINDUS_VISION_MARKDOWN_AUTO_SCOPE` ist der Datenschutz-Schalter: er
    entscheidet, welche Anhaenge *ohne* Zutun eines Menschen an den
    konfigurierten Vision-Provider gehen.
    """

    def _document(self, *, mime_type="application/pdf", method=Document.ExtractionMethod.OCR):
        return Document.objects.create(
            title="Scan",
            sha256="abc123",
            metadata={"mime_type": mime_type},
            extraction_method=method,
        )

    @override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE=vision_markdown.AUTO_SCOPE_OFF)
    def test_off_lets_nothing_out(self):
        self.assertFalse(vision_markdown.auto_scope_includes(self._document()))

    def test_scans_covers_documents_without_a_usable_text_layer(self):
        self.assertTrue(vision_markdown.auto_scope_includes(self._document()))
        self.assertTrue(
            vision_markdown.auto_scope_includes(
                self._document(mime_type="image/png", method=Document.ExtractionMethod.VISION)
            )
        )

    def test_scans_skips_a_born_digital_pdf(self):
        """Ein PDF mit sauberem Text-Layer hat von der Vision-Transkription
        nichts -- es wuerde nur Tokens kosten und den Text durch eine
        schlechtere Fassung ersetzen.
        """
        self.assertFalse(
            vision_markdown.auto_scope_includes(
                self._document(method=Document.ExtractionMethod.TEXT_LAYER)
            )
        )

    @override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE=vision_markdown.AUTO_SCOPE_ALL)
    def test_all_covers_a_born_digital_pdf_too(self):
        self.assertTrue(
            vision_markdown.auto_scope_includes(
                self._document(method=Document.ExtractionMethod.TEXT_LAYER)
            )
        )

    @override_settings(FINDUS_VISION_MARKDOWN_AUTO_SCOPE=vision_markdown.AUTO_SCOPE_ALL)
    def test_formats_that_cannot_be_rendered_page_by_page_stay_out(self):
        self.assertFalse(
            vision_markdown.auto_scope_includes(
                self._document(mime_type="application/vnd.oasis.opendocument.text")
            )
        )

    # Dass ein fehlender oder vertippter Schalter restriktiv wirkt, ist eine
    # Architekturregel und steht deshalb als Vertragstest in
    # `test_contracts.AutomaticVisionEgressIsOptInTests`.

    def test_up_to_date_documents_are_not_extracted_again(self):
        document = self._document()
        document.vision_reextraction_status = Document.VisionReextractionStatus.READY
        document.vision_reextraction_fingerprint = vision_markdown.source_fingerprint(document)

        self.assertTrue(vision_markdown.auto_scope_includes(document))
        self.assertFalse(vision_markdown.should_extract_automatically(document))
