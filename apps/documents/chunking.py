"""Splits `Document.text_content` into ordered, overlapping chunks.

Paragraph breaks (blank lines) are preserved as join points so a chunk
reads like prose rather than a word salad, but chunk boundaries are
otherwise driven by a word count ("token" = whitespace-split word, not a
vendor tokenizer -- see apps.ai.providers on why the embedding provider,
and therefore its tokenizer, is a config choice this module must not
depend on). A single paragraph longer than `chunk_size` is split
mid-paragraph rather than dropped or embedded oversized.

`chunk_markdown()` (#1149) is the counterpart for `Document.content_format
== "markdown"` (the KI-Vision-Markdown-Neuextraktion, `apps.documents.
extraction.reextract_document_with_vision`): plain `chunk_text()` would
cut a Markdown table row in half or strand a `## Seite N`-heading in its
own chunk, away from the section it introduces -- exactly the structure
this feature exists to preserve (see CLAUDE.md, "KI-Vision-Neuextraktion").
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^#{1,6}\s")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _paragraphs(text: str) -> list[str]:
    return [p for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """Pack `text` into chunks of at most `chunk_size` words each, with the
    last `overlap` words of one chunk repeated at the start of the next so
    semantic search doesn't lose context at a chunk boundary. Returns an
    empty list for blank `text`. Order is stable and matches the
    `Chunk.position` a caller assigns to each returned string.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and less than chunk_size")

    # (word, starts_new_paragraph) -- flattened so the word-count window
    # below can slide across paragraph boundaries uniformly, while
    # `_join` still renders those boundaries as blank lines.
    units: list[tuple[str, bool]] = [
        (word, index == 0)
        for paragraph in _paragraphs(text)
        for index, word in enumerate(paragraph.split())
    ]
    if not units:
        return []

    chunks = []
    start = 0
    total = len(units)
    while start < total:
        end = min(start + chunk_size, total)
        chunks.append(_join(units[start:end]))
        if end >= total:
            break
        start = end - overlap
    return chunks


def _join(units: list[tuple[str, bool]]) -> str:
    parts: list[str] = []
    for word, starts_new_paragraph in units:
        if parts:
            parts.append("\n\n" if starts_new_paragraph else " ")
        parts.append(word)
    return "".join(parts)


def _is_heading_only(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    return len(lines) == 1 and bool(_HEADING_RE.match(lines[0]))


def _is_table_block(block: str) -> bool:
    return any(_TABLE_ROW_RE.match(line) for line in block.splitlines())


def _table_header(lines: list[str]) -> list[str]:
    if len(lines) >= 2 and _TABLE_ROW_RE.match(lines[0]) and _TABLE_SEPARATOR_RE.match(lines[1]):
        return lines[:2]
    return []


def _markdown_blocks(text: str) -> list[str]:
    """Blank-line-separated blocks, with a heading-only block merged into
    the block that follows it -- a lone `## Seite N` would otherwise be
    free to end up in its own chunk, separated from the section it
    introduces, purely because the packing step below only ever looks at
    whole blocks and doesn't know a heading needs its body nearby.
    """
    blocks = _paragraphs(text)
    merged: list[str] = []
    for block in blocks:
        if merged and _is_heading_only(merged[-1]):
            merged[-1] = merged[-1] + "\n\n" + block
        else:
            merged.append(block)
    return merged


def _split_table_block(block: str, *, chunk_size: int) -> list[str]:
    """Split an oversized Markdown table by whole rows, repeating the
    header/separator row in every continuation piece so each one stays a
    readable, self-contained table instead of a headerless row fragment.
    """
    lines = [line for line in block.splitlines() if line.strip()]
    header = _table_header(lines)
    body = lines[len(header):]
    if not body:
        return [block]

    pieces: list[str] = []
    current = list(header)
    current_words = sum(len(line.split()) for line in current)
    for line in body:
        line_words = len(line.split())
        if len(current) > len(header) and current_words + line_words > chunk_size:
            pieces.append("\n".join(current))
            current = list(header)
            current_words = sum(len(line.split()) for line in current)
        current.append(line)
        current_words += line_words
    if len(current) > len(header):
        pieces.append("\n".join(current))
    return pieces


def _split_oversized_block(block: str, *, chunk_size: int) -> list[str]:
    words = block.split()
    return [
        " ".join(words[start : start + chunk_size]) for start in range(0, len(words), chunk_size)
    ]


def chunk_markdown(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    """Like `chunk_text()`, but for `Document.content_format == "markdown"`
    (#1149): packs whole blocks (paragraphs, headings-with-their-section,
    table rows) into chunks of at most `chunk_size` words, never cutting a
    table row or separating a heading from its section. Overlap is
    approximated by carrying whole trailing blocks/rows forward rather
    than an exact word count, for the same reason -- a partial row at a
    chunk's start would be meaningless on its own.

    Only a block that itself exceeds `chunk_size` is split at all, and
    then along its own structure (table rows, or -- for anything else --
    plain word boundaries as a last resort).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and less than chunk_size")

    pieces: list[str] = []
    for block in _markdown_blocks(text):
        if len(block.split()) <= chunk_size:
            pieces.append(block)
        elif _is_table_block(block):
            pieces.extend(_split_table_block(block, chunk_size=chunk_size))
        else:
            pieces.extend(_split_oversized_block(block, chunk_size=chunk_size))
    if not pieces:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for piece in pieces:
        piece_words = len(piece.split())
        if current and current_words + piece_words > chunk_size:
            chunks.append("\n\n".join(current))
            overlap_pieces: list[str] = []
            overlap_words = 0
            for previous in reversed(current):
                previous_words = len(previous.split())
                if overlap_words + previous_words > overlap:
                    break
                overlap_pieces.insert(0, previous)
                overlap_words += previous_words
            current = overlap_pieces
            current_words = overlap_words
        current.append(piece)
        current_words += piece_words
    if current:
        chunks.append("\n\n".join(current))
    return chunks
