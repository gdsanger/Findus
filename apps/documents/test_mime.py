from django.test import SimpleTestCase

from .mime import GENERIC_MIME_TYPE, mime_from_filename, resolve_mime_type

_PDF_HEADER = b"%PDF-1.7\n1 0 obj\n"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF"


class ResolveMimeTypeTests(SimpleTestCase):
    def test_octet_stream_pdf_is_recognised_from_content(self):
        # Kernfall #1077: Scanner meldet octet-stream, Inhalt ist ein PDF.
        resolved = resolve_mime_type(
            _PDF_HEADER, filename="scan.pdf", declared="application/octet-stream"
        )
        self.assertEqual(resolved, "application/pdf")

    def test_content_wins_over_wrong_extension(self):
        # Inhalt ist maßgeblich, nicht die (hier falsche) Endung.
        resolved = resolve_mime_type(_PNG_HEADER, filename="bild.pdf", declared="")
        self.assertEqual(resolved, "image/png")

    def test_filename_with_spaces_still_resolves_extension(self):
        # Endungserkennung darf an Leerzeichen im Namen nicht scheitern.
        resolved = resolve_mime_type(
            b"kein bekannter header",
            filename="20260708130035_001 1.pdf",
            declared="application/octet-stream",
        )
        self.assertEqual(resolved, "application/pdf")

    def test_extension_fallback_when_content_unknown(self):
        self.assertEqual(
            resolve_mime_type(b"Hallo Welt", filename="notiz.txt", declared=""),
            "text/plain",
        )

    def test_specific_declared_kept_when_content_and_extension_silent(self):
        resolved = resolve_mime_type(
            b"PK\x03\x04rest", filename="archive.zip", declared="application/zip"
        )
        self.assertEqual(resolved, "application/zip")

    def test_truly_unknown_stays_generic(self):
        resolved = resolve_mime_type(
            b"\x00\x01\x02rohdaten", filename="daten.bin", declared=""
        )
        self.assertEqual(resolved, GENERIC_MIME_TYPE)

    def test_jpeg_from_content(self):
        self.assertEqual(
            resolve_mime_type(_JPEG_HEADER, filename="", declared="application/octet-stream"),
            "image/jpeg",
        )


class MimeFromFilenameTests(SimpleTestCase):
    def test_known_extensions(self):
        cases = {
            "a.pdf": "application/pdf",
            "b.PNG": "image/png",
            "c.jpg": "image/jpeg",
            "d.jpeg": "image/jpeg",
            "e.tif": "image/tiff",
            "f.tiff": "image/tiff",
            "g.txt": "text/plain",
            "h.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        for name, expected in cases.items():
            self.assertEqual(mime_from_filename(name), expected, name)

    def test_unknown_extension_is_empty(self):
        self.assertEqual(mime_from_filename("daten.bin"), "")
