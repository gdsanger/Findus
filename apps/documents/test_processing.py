from django.conf import settings
from django.test import TestCase, override_settings

from apps.ai.providers.fake import FakeEmbeddingProvider

from .chunking import chunk_markdown, chunk_text
from .models import Chunk, Document
from .processing import process_document

DIMENSIONS = settings.FINDUS_EMBEDDING_DIMENSIONS


class ChunkTextTests(TestCase):
    def test_blank_text_returns_no_chunks(self):
        self.assertEqual(chunk_text("   \n\n  ", chunk_size=10, overlap=2), [])

    def test_short_text_fits_in_one_chunk(self):
        chunks = chunk_text("one two three", chunk_size=10, overlap=2)
        self.assertEqual(chunks, ["one two three"])

    def test_stable_order_and_overlap_between_consecutive_chunks(self):
        text = " ".join(f"w{i}" for i in range(10))

        chunks = chunk_text(text, chunk_size=4, overlap=1)

        self.assertEqual(
            chunks,
            [
                "w0 w1 w2 w3",
                "w3 w4 w5 w6",
                "w6 w7 w8 w9",
            ],
        )

    def test_no_overlap_produces_disjoint_chunks(self):
        text = " ".join(f"w{i}" for i in range(6))

        chunks = chunk_text(text, chunk_size=3, overlap=0)

        self.assertEqual(chunks, ["w0 w1 w2", "w3 w4 w5"])

    def test_paragraph_break_is_preserved_as_join_point(self):
        chunks = chunk_text("para one here\n\npara two here", chunk_size=20, overlap=0)

        self.assertEqual(chunks, ["para one here\n\npara two here"])

    def test_rejects_non_positive_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_text("text", chunk_size=0, overlap=0)

    def test_rejects_overlap_not_smaller_than_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_text("text", chunk_size=5, overlap=5)


class ChunkMarkdownTests(TestCase):
    """`chunk_markdown()` (#1149) -- the counterpart to `chunk_text()` for
    `Document.content_format == "markdown"`: table rows and a heading's
    section must survive chunking intact (CLAUDE.md, "KI-Vision-
    Neuextraktion").
    """

    def test_blank_text_returns_no_chunks(self):
        self.assertEqual(chunk_markdown("   \n\n  ", chunk_size=10, overlap=2), [])

    def test_short_text_fits_in_one_chunk(self):
        chunks = chunk_markdown("one two three", chunk_size=10, overlap=2)
        self.assertEqual(chunks, ["one two three"])

    def test_table_rows_are_never_split_across_chunks(self):
        table = "\n".join(
            [
                "| Bezeichnung | Wert |",
                "| --- | --- |",
                "| A | 1 |",
                "| B | 2 |",
                "| C | 3 |",
            ]
        )

        chunks = chunk_markdown(table, chunk_size=8, overlap=0)

        # Jede Zeile bleibt vollstaendig innerhalb genau eines Chunks --
        # keine Tabellenzeile ist ueber zwei Chunks hinweg zerrissen.
        for row in ["| A | 1 |", "| B | 2 |", "| C | 3 |"]:
            matches = [chunk for chunk in chunks if row in chunk]
            self.assertEqual(len(matches), 1, f"Zeile {row!r} nicht genau einmal gefunden")
            self.assertIn("| Bezeichnung | Wert |", matches[0])

    def test_heading_stays_with_its_section(self):
        text = "## Seite 2\n\nEin Absatz, der zur Ueberschrift gehoert."

        chunks = chunk_markdown(text, chunk_size=20, overlap=0)

        self.assertEqual(len(chunks), 1)
        self.assertIn("## Seite 2", chunks[0])
        self.assertIn("Ein Absatz", chunks[0])

    def test_oversized_plain_paragraph_still_splits(self):
        text = " ".join(f"w{i}" for i in range(10))

        chunks = chunk_markdown(text, chunk_size=4, overlap=0)

        self.assertEqual(chunks, ["w0 w1 w2 w3", "w4 w5 w6 w7", "w8 w9"])

    def test_rejects_non_positive_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_markdown("text", chunk_size=0, overlap=0)

    def test_rejects_overlap_not_smaller_than_chunk_size(self):
        with self.assertRaises(ValueError):
            chunk_markdown("text", chunk_size=5, overlap=5)


class ProcessDocumentMarkdownRoutingTests(TestCase):
    """`process_document()` (#1149) waehlt den Chunker nach
    `Document.content_format`, nicht danach, wie `text_content` aussieht --
    ein Markdown-Dokument bekommt `chunk_markdown()`, alles andere weiter
    `chunk_text()`.
    """

    def setUp(self):
        self.provider = FakeEmbeddingProvider(dimensions=DIMENSIONS)

    @override_settings(FINDUS_CHUNK_SIZE_TOKENS=8, FINDUS_CHUNK_OVERLAP_TOKENS=0)
    def test_markdown_content_format_keeps_table_rows_intact(self):
        table = "\n".join(
            [
                "| Bezeichnung | Wert |",
                "| --- | --- |",
                "| A | 1 |",
                "| B | 2 |",
                "| C | 3 |",
            ]
        )
        document = Document.objects.create(
            title="Kontoauszug",
            text_content=table,
            content_format=Document.ContentFormat.MARKDOWN,
        )

        process_document(document.id, embedding_provider=self.provider)

        chunk_contents = [c.content for c in Chunk.objects.filter(document=document)]
        for row in ["| A | 1 |", "| B | 2 |", "| C | 3 |"]:
            matches = [content for content in chunk_contents if row in content]
            self.assertEqual(len(matches), 1)


class _RaisingEmbeddingProvider:
    def embed(self, texts):
        raise RuntimeError("boom")


class ProcessDocumentTests(TestCase):
    def setUp(self):
        self.provider = FakeEmbeddingProvider(dimensions=DIMENSIONS)

    @override_settings(FINDUS_CHUNK_SIZE_TOKENS=4, FINDUS_CHUNK_OVERLAP_TOKENS=1)
    def test_creates_chunks_with_embeddings_and_marks_ready(self):
        document = Document.objects.create(
            title="Doc",
            text_content=" ".join(f"w{i}" for i in range(10)),
        )

        result = process_document(document.id, embedding_provider=self.provider)

        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(result.processing_error, "")
        chunks = list(Chunk.objects.filter(document=document).order_by("position"))
        self.assertEqual([c.position for c in chunks], [0, 1, 2])
        self.assertTrue(all(c.embedding_model == self.provider.model for c in chunks))
        self.assertTrue(all(c.embedding_model_version == self.provider.version for c in chunks))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.provider.calls[0]), 3)

    def test_blank_text_marks_ready_with_no_chunks(self):
        document = Document.objects.create(title="Empty", text_content="")

        result = process_document(document.id, embedding_provider=self.provider)

        self.assertEqual(result.processing_status, Document.ProcessingStatus.READY)
        self.assertEqual(Chunk.objects.filter(document=document).count(), 0)
        self.assertEqual(self.provider.calls, [])

    @override_settings(
        FINDUS_CHUNK_SIZE_TOKENS=2,
        FINDUS_CHUNK_OVERLAP_TOKENS=0,
        FINDUS_CHUNK_EMBEDDING_BATCH_SIZE=2,
    )
    def test_batches_embedding_calls(self):
        document = Document.objects.create(
            title="Doc",
            text_content="\n\n".join(f"paragraph {i}" for i in range(5)),
        )

        process_document(document.id, embedding_provider=self.provider)

        self.assertEqual(Chunk.objects.filter(document=document).count(), 5)
        self.assertEqual([len(call) for call in self.provider.calls], [2, 2, 1])

    def test_provider_failure_marks_failed_and_reraises(self):
        document = Document.objects.create(title="Doc", text_content="some text")

        with self.assertRaisesMessage(RuntimeError, "boom"):
            process_document(document.id, embedding_provider=_RaisingEmbeddingProvider())

        document.refresh_from_db()
        self.assertEqual(document.processing_status, Document.ProcessingStatus.FAILED)
        self.assertIn("boom", document.processing_error)
        self.assertEqual(Chunk.objects.filter(document=document).count(), 0)

    def test_reprocessing_replaces_previous_chunks(self):
        document = Document.objects.create(title="Doc", text_content="first version")
        process_document(document.id, embedding_provider=self.provider)
        first_chunk_ids = list(
            Chunk.objects.filter(document=document).values_list("id", flat=True)
        )

        document.text_content = "second version now"
        document.save(update_fields=["text_content"])
        process_document(document.id, embedding_provider=self.provider)

        chunks = list(Chunk.objects.filter(document=document))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "second version now")
        self.assertNotIn(chunks[0].id, first_chunk_ids)

    def test_failed_embed_does_not_clobber_existing_chunks(self):
        document = Document.objects.create(title="Doc", text_content="first version")
        process_document(document.id, embedding_provider=self.provider)

        with self.assertRaises(RuntimeError):
            process_document(document.id, embedding_provider=_RaisingEmbeddingProvider())

        self.assertEqual(Chunk.objects.filter(document=document).count(), 1)
