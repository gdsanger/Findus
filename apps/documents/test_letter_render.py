import datetime
import shutil
import tempfile
import zipfile
from io import BytesIO

from django.test import TestCase, override_settings

from .letter_render import (
    build_content,
    file_slug,
    plain_text,
    render_docx,
    render_draft_files,
    render_pdf,
)
from .models import LetterDraft, LetterTemplate, default_letter_layout

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="findus-letter-render-media-")
_LOCAL_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _docx_text(data: bytes) -> str:
    """Der sichtbare Text eines .docx -- ein docx ist ein ZIP mit
    `word/document.xml`, und für „steht der Absatz drin?" genügt dessen
    Roh-XML.
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def _draft(**overrides):
    fields = {
        "template_name": "Widerspruch",
        "subject": "Widerspruch gegen Bescheid 4711",
        "body_text": "Sehr geehrte Damen und Herren,\n\nhiermit widerspreche ich.\n\nBitte bestätigen Sie.",
        "letter_date": datetime.date(2026, 8, 9),
        "sender_block": "Anna Beispiel\nHauptstr. 1\n10115 Berlin",
        "recipient_block": "Amt für Alles\nAmtsweg 2\n10117 Berlin",
        "signature": "Anna Beispiel",
        "layout": default_letter_layout(),
    }
    fields.update(overrides)
    return LetterDraft.objects.create(**fields)


class LetterContentTests(TestCase):
    """Der Zusammenbau des Briefs aus dem Entwurfs-Snapshot (#1095) -- die
    eine Struktur, aus der beide Renderer lesen.
    """

    def test_content_carries_every_layout_block(self):
        draft = _draft()
        draft.layout["letterhead"] = "Anna Beispiel – Beratung"
        draft.layout["date_place"] = "Berlin"
        draft.save(update_fields=["layout"])

        content = build_content(draft)

        self.assertEqual(content.letterhead, "Anna Beispiel – Beratung")
        self.assertEqual(content.subject, "Widerspruch gegen Bescheid 4711")
        self.assertIn("Amt für Alles", content.recipient_block)
        self.assertEqual(content.date_line, "Berlin, 9. August 2026")
        self.assertEqual(content.closing, "Mit freundlichen Grüßen")
        self.assertEqual(content.signature, "Anna Beispiel")

    def test_sender_line_is_the_one_line_form_of_the_sender_block(self):
        content = build_content(_draft())

        self.assertEqual(content.sender_line, "Anna Beispiel · Hauptstr. 1 · 10115 Berlin")

    def test_layout_switches_can_drop_blocks(self):
        """Was die Vorlage abgeschaltet hat, darf auch nicht gerendert
        werden -- der Snapshot ist die einzige Quelle dafür.
        """
        draft = _draft()
        draft.layout.update(
            {"show_date": False, "show_subject": False, "show_recipient_block": False}
        )
        draft.save(update_fields=["layout"])

        content = build_content(draft)

        self.assertEqual(content.date_line, "")
        self.assertEqual(content.subject, "")
        self.assertEqual(content.recipient_block, "")

    def test_content_ignores_later_template_edits(self):
        """Snapshot-Prinzip: ein bereits entworfener Brief ändert sich
        nicht, weil jemand die Vorlage angefasst hat.
        """
        template = LetterTemplate.objects.create(name="Widerspruch", signature="Alt")
        draft = _draft(template=template)

        template.layout = {**default_letter_layout(), "closing": "Hochachtungsvoll"}
        template.signature = "Neu"
        template.save()

        content = build_content(draft)

        self.assertEqual(content.closing, "Mit freundlichen Grüßen")
        self.assertEqual(content.signature, "Anna Beispiel")

    def test_plain_text_covers_address_and_body(self):
        text = plain_text(build_content(_draft()))

        self.assertIn("Amt für Alles", text)
        self.assertIn("Widerspruch gegen Bescheid 4711", text)
        self.assertIn("hiermit widerspreche ich.", text)
        self.assertIn("Mit freundlichen Grüßen", text)


class LetterDocxTests(TestCase):
    """Word als editierbarer Master (#1095)."""

    def test_docx_is_a_valid_package_containing_the_letter(self):
        data = render_docx(build_content(_draft()))

        self.assertTrue(data.startswith(b"PK"))
        xml = _docx_text(data)
        for expected in (
            "Amt für Alles",
            "Widerspruch gegen Bescheid 4711",
            "hiermit widerspreche ich.",
            "Mit freundlichen Grüßen",
            "Anna Beispiel",
        ):
            self.assertIn(expected, xml)

    def test_paragraphs_survive_as_separate_paragraphs(self):
        """Absätze sind der Punkt eines editierbaren Masters -- ein Block
        Text mit Zeilenumbrüchen wäre in Word nicht weiterverwendbar.
        """
        xml = _docx_text(render_docx(build_content(_draft())))

        self.assertGreaterEqual(xml.count("<w:p>"), 5)


class LetterPdfTests(TestCase):
    """PDF als Druck-/Sendefassung, direkt gerendert (kein Konverter)."""

    def test_pdf_has_a_pdf_header_and_content(self):
        data = render_pdf(build_content(_draft()))

        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 800)

    def test_characters_outside_cp1252_do_not_break_the_pdf(self):
        """Ein Emoji oder kyrillischer Name im Text darf den Brief nicht
        sprengen -- fpdf2-Kernschriften können nur cp1252.
        """
        draft = _draft(body_text="Grüße 😀 aus Берлин – bis bald.")

        data = render_pdf(build_content(draft))

        self.assertTrue(data.startswith(b"%PDF"))


@override_settings(STORAGES=_LOCAL_STORAGES, MEDIA_ROOT=_TEST_MEDIA_ROOT)
class RenderDraftFilesTests(TestCase):
    """Beide Fassungen am Entwurf ablegen -- und beim erneuten Rendern
    ersetzen, nicht anhäufen.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_TEST_MEDIA_ROOT, ignore_errors=True)

    def test_both_files_are_stored_on_the_draft(self):
        draft = _draft()

        render_draft_files(draft)

        draft.refresh_from_db()
        self.assertTrue(draft.docx_file.name.endswith(".docx"))
        self.assertTrue(draft.pdf_file.name.endswith(".pdf"))
        self.assertTrue(draft.has_files)
        with draft.pdf_file.open("rb") as handle:
            self.assertTrue(handle.read(4) == b"%PDF")

    def test_re_rendering_replaces_instead_of_piling_up(self):
        """„Text ändern und neu rendern" darf keine Halde alter Fassungen im
        Object-Storage hinterlassen -- nach zwei Läufen liegen genau die
        zwei aktuellen Dateien da.
        """
        draft = _draft()
        render_draft_files(draft)
        directory = draft.pdf_file.name.rsplit("/", 1)[0]

        draft.body_text = "Sehr geehrte Damen und Herren,\n\nneue Fassung."
        draft.save(update_fields=["body_text"])
        render_draft_files(draft)

        draft.refresh_from_db()
        slug = file_slug(build_content(draft), draft)
        _dirs, files = draft.pdf_file.storage.listdir(directory)
        own_files = [name for name in files if name.startswith(slug)]
        self.assertEqual(sorted(own_files), [f"{slug}.docx", f"{slug}.pdf"])
        self.assertTrue(draft.pdf_file.storage.exists(draft.pdf_file.name))
        self.assertTrue(draft.docx_file.storage.exists(draft.docx_file.name))

    def test_file_slug_uses_subject_and_draft_id(self):
        draft = _draft(subject="Widerspruch: Bescheid 4711")

        self.assertEqual(
            file_slug(build_content(draft), draft), f"brief-widerspruch-bescheid-4711-{draft.pk}"
        )

    def test_logo_failure_does_not_break_the_render(self):
        """Das Logo liegt im Object-Storage -- ist es weg, kommt der Brief
        eben ohne, statt zu scheitern.
        """
        template = LetterTemplate.objects.create(name="Mit Logo")
        template.logo.name = "letter_templates/logos/does-not-exist.png"
        template.save(update_fields=["logo"])
        draft = _draft(template=template)

        content = build_content(draft)

        self.assertIsNone(content.logo)
        self.assertTrue(render_pdf(content).startswith(b"%PDF"))


class LetterDraftModelTests(TestCase):
    def test_editable_while_not_running_and_not_filed(self):
        draft = _draft(status=LetterDraft.Status.RUNNING)
        self.assertFalse(draft.is_editable)

        draft.status = LetterDraft.Status.READY
        self.assertTrue(draft.is_editable)

        # Auch nach einem Fehlschlag: dann schreibt der Nutzer eben selbst.
        draft.status = LetterDraft.Status.FAILED
        self.assertTrue(draft.is_editable)

        draft.status = LetterDraft.Status.FINALIZED
        self.assertFalse(draft.is_editable)

    def test_layout_value_falls_back_to_the_base_layout(self):
        draft = _draft(layout={})

        self.assertEqual(draft.layout_value("closing"), "Mit freundlichen Grüßen")

    def test_visible_to_scopes_like_every_other_model(self):
        from django.contrib.auth import get_user_model

        from apps.accounts.models import Department

        user_model = get_user_model()
        dept = Department.objects.create(name="Dept A")
        other_dept = Department.objects.create(name="Dept B")
        user = user_model.objects.create_user(username="alice", password="x")
        user.departments.add(dept)

        mine = _draft(subject="Meiner")
        mine.departments.add(dept)
        theirs = _draft(subject="Fremder")
        theirs.departments.add(other_dept)
        _draft(
            subject="Privat – eigener",
            visibility=LetterDraft.Visibility.PRIVATE,
            owner=user,
        )
        _draft(subject="Privat – fremder", visibility=LetterDraft.Visibility.PRIVATE)

        visible = set(LetterDraft.objects.visible_to(user).values_list("subject", flat=True))

        self.assertEqual(visible, {"Meiner", "Privat – eigener"})
