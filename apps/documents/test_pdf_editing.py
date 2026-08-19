"""Die reine Seitenlogik der Scan-Korrektur (#1155): Reihenfolge,
Seitenauswahl, Drehung, Randfälle kaputter Dateien.

Bewusst ohne Datenbank und ohne Storage -- was `apps.documents.pdf_editing`
tut, hängt an keinem der beiden, und die festgelegte Reihenfolge
(drehen -> löschen -> aufteilen) soll ohne Django prüfbar sein.
"""

import io

from django.test import SimpleTestCase

from .pdf_editing import (
    PdfEditError,
    PdfEditPlan,
    inspect_pdf,
    iter_edited_parts,
    part_filename,
    split_page_groups,
    summarize_plan,
)
from .test_extraction import _make_pdf


def _plan(*, rotations=None, deletions=(), splits=()) -> PdfEditPlan:
    return PdfEditPlan(
        rotations=rotations or {}, deletions=tuple(deletions), splits=tuple(splits)
    )


def _page_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    return [(page.extract_text() or "").strip() for page in PdfReader(io.BytesIO(data)).pages]


def _rotations(data: bytes) -> list[int]:
    from pypdf import PdfReader

    return [page.get("/Rotate", 0) for page in PdfReader(io.BytesIO(data)).pages]


class SplitPageGroupsTests(SimpleTestCase):
    """Die Reihenfolge in Reinform: erst fallen die gelöschten Seiten weg,
    dann wirken die Schnittmarken auf den Rest.
    """

    def test_without_a_plan_everything_stays_one_document(self):
        self.assertEqual(split_page_groups(3, _plan()), [[1, 2, 3]])

    def test_deletions_drop_pages_without_splitting(self):
        self.assertEqual(split_page_groups(4, _plan(deletions=(2,))), [[1, 3, 4]])

    def test_a_split_marker_starts_a_new_part_before_that_page(self):
        self.assertEqual(split_page_groups(4, _plan(splits=(3,))), [[1, 2], [3, 4]])

    def test_deletion_is_applied_before_the_split(self):
        """Der eigentliche Reihenfolge-Test: die Marke sitzt vor Seite 3, die
        gelöscht wird -- sie schneidet deshalb vor der nächsten übrig
        gebliebenen Seite und geht nicht verloren.
        """
        self.assertEqual(
            split_page_groups(4, _plan(deletions=(3,), splits=(3,))), [[1, 2], [4]]
        )

    def test_a_marker_before_the_first_remaining_page_is_dropped(self):
        """Ein Teil ohne Seiten wäre kein Dokument -- die Marke verfällt,
        statt ein leeres erstes Teil zu erzeugen."""
        self.assertEqual(split_page_groups(3, _plan(deletions=(1,), splits=(2,))), [[2, 3]])

    def test_several_markers_produce_several_parts(self):
        self.assertEqual(
            split_page_groups(6, _plan(splits=(3, 5))), [[1, 2], [3, 4], [5, 6]]
        )


class ApplyPlanTests(SimpleTestCase):
    """Was tatsächlich in der Datei landet."""

    def test_pages_are_deleted_from_the_written_file(self):
        source = io.BytesIO(_make_pdf(["Seite eins", "Seite zwei", "Seite drei"]))

        parts = list(iter_edited_parts(source, _plan(deletions=(2,))))

        self.assertEqual(len(parts), 1)
        pages, buffer = parts[0]
        self.assertEqual(pages, [1, 3])
        texts = _page_texts(buffer.read())
        self.assertEqual(len(texts), 2)
        self.assertIn("Seite eins", texts[0])
        self.assertIn("Seite drei", texts[1])

    def test_rotation_is_written_into_the_file(self):
        """Nicht nur in der Anzeige gedreht: `/Rotate` steht dauerhaft in der
        Datei, damit jeder Rasterer (Vorschau, OCR, Vision) die gerade Seite
        sieht -- erst dadurch entsteht der fachliche Nutzen.
        """
        source = io.BytesIO(_make_pdf(["Seite eins", "Seite zwei"]))

        _pages, buffer = next(iter_edited_parts(source, _plan(rotations={2: 90})))

        self.assertEqual(_rotations(buffer.read()), [0, 90])

    def test_all_four_rotations_are_supported(self):
        for angle in (90, 180, 270):
            with self.subTest(angle=angle):
                source = io.BytesIO(_make_pdf(["Seite eins"]))
                _pages, buffer = next(iter_edited_parts(source, _plan(rotations={1: angle})))
                self.assertEqual(_rotations(buffer.read()), [angle])

    def test_rotation_is_applied_before_deletion(self):
        """Beide Operationen zusammen, in der festgelegten Reihenfolge: die
        Drehung meint Seite 3 der *Originalnummerierung*, auch wenn Seite 1
        vorher entfernt wird.
        """
        source = io.BytesIO(_make_pdf(["eins", "zwei", "drei"]))

        _pages, buffer = next(
            iter_edited_parts(source, _plan(rotations={3: 180}, deletions=(1,)))
        )

        data = buffer.read()
        self.assertEqual(_rotations(data), [0, 180])
        self.assertIn("zwei", _page_texts(data)[0])

    def test_split_produces_one_part_per_section(self):
        source = io.BytesIO(_make_pdf(["eins", "zwei", "drei", "vier"]))

        parts = list(iter_edited_parts(source, _plan(splits=(3,))))

        self.assertEqual([pages for pages, _buffer in parts], [[1, 2], [3, 4]])
        self.assertEqual(len(_page_texts(parts[0][1].read())), 2)
        self.assertEqual(len(_page_texts(parts[1][1].read())), 2)

    def test_deleting_every_page_is_refused(self):
        source = io.BytesIO(_make_pdf(["eins", "zwei"]))

        with self.assertRaises(PdfEditError):
            list(iter_edited_parts(source, _plan(deletions=(1, 2))))


class BrokenFileTests(SimpleTestCase):
    """Randfälle, die nie als 500er-Seite enden dürfen."""

    def test_a_corrupt_file_raises_a_readable_error(self):
        with self.assertRaises(PdfEditError) as ctx:
            inspect_pdf(io.BytesIO(b"keine PDF-Datei"))

        self.assertIn("beschädigt", str(ctx.exception))

    def test_a_password_protected_file_is_refused(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(_make_pdf(["eins"]))).pages:
            writer.add_page(page)
        writer.encrypt("geheim")
        encrypted = io.BytesIO()
        writer.write(encrypted)
        encrypted.seek(0)

        with self.assertRaises(PdfEditError) as ctx:
            inspect_pdf(encrypted)

        self.assertIn("passwortgeschützt", str(ctx.exception))

    def test_inspect_reports_the_page_count(self):
        info = inspect_pdf(io.BytesIO(_make_pdf(["eins", "zwei", "drei"])))

        self.assertEqual(info.page_count, 3)
        self.assertFalse(info.has_signature)


class SummaryTests(SimpleTestCase):
    """Der Bestätigungstext -- er muss in Worten sagen, was passiert."""

    def test_summary_names_deletions_rotations_and_the_split(self):
        lines = summarize_plan(
            _plan(rotations={5: 90}, deletions=(3, 7), splits=(4, 8)), page_count=10
        )

        text = ", ".join(lines)
        self.assertIn("Seite 5 wird um 90° gedreht", text)
        self.assertIn("Seiten 3 und 7 werden entfernt", text)
        self.assertIn("in 3 Dokumente aufgeteilt", text)
        self.assertIn("das Original wird gelöscht", text)

    def test_summary_without_a_split_says_nothing_about_deleting_the_original(self):
        lines = summarize_plan(_plan(deletions=(2,)), page_count=4)

        self.assertNotIn("das Original wird gelöscht", lines)


class PlanSerializationTests(SimpleTestCase):
    """Der Plan überlebt die JSON-Ablage am Lauf unverändert -- der
    Hintergrundjob liest ihn von dort, nicht aus dem Request."""

    def test_roundtrip_through_json_shaped_dict(self):
        plan = _plan(rotations={2: 270}, deletions=(1, 4), splits=(3,))

        restored = PdfEditPlan.from_dict(plan.as_dict())

        self.assertEqual(restored, plan)

    def test_form_data_replays_the_same_field_names(self):
        plan = _plan(rotations={2: 90}, deletions=(1,), splits=(3,))

        self.assertEqual(
            sorted(plan.as_form_data()),
            sorted([("delete_pages", "1"), ("rotate_2", "90"), ("split_before_3", "1")]),
        )


class PartFilenameTests(SimpleTestCase):
    def test_single_part_keeps_the_original_name(self):
        self.assertEqual(part_filename("scan.pdf", 1, 1), "scan.pdf")

    def test_parts_are_numbered(self):
        self.assertEqual(part_filename("scan.pdf", 2, 3), "scan-teil-2.pdf")

    def test_name_without_suffix_still_becomes_a_pdf(self):
        self.assertEqual(part_filename("scan", 2, 3), "scan-teil-2.pdf")
