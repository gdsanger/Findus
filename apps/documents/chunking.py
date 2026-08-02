"""Splits `Document.text_content` into ordered, overlapping chunks.

Paragraph breaks (blank lines) are preserved as join points so a chunk
reads like prose rather than a word salad, but chunk boundaries are
otherwise driven by a word count ("token" = whitespace-split word, not a
vendor tokenizer -- see apps.ai.providers on why the embedding provider,
and therefore its tokenizer, is a config choice this module must not
depend on). A single paragraph longer than `chunk_size` is split
mid-paragraph rather than dropped or embedded oversized.
"""

from __future__ import annotations


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
