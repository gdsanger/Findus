"""Strips bytes PostgreSQL's `text`/`varchar` columns cannot store (#1061).

Postgres represents `text` values as null-terminated C strings internally,
so a NUL (`\\x00`) that made it into a Python `str` -- regardless of whether
it came from a PDF text layer, OCR, a vision-model transcription, or an
LLM's JSON reply -- makes `save()` raise `psycopg.DataError: PostgreSQL
text fields cannot contain NUL (0x00) bytes` instead of persisting the
document. The other C0 control characters (besides tab/newline/CR) are
stripped alongside it: Postgres tolerates them, but scanners/PDF generators
that emit stray NULs tend to emit these too, and none of them are
meaningful in a document's extracted text.

`clean_text`/`clean_json` are the single sanitizer both `extraction.py` and
`analysis.py` run their results through before `Document.save()`, so every
text-bearing save path is covered from one place.
"""

from __future__ import annotations

import re

_FORBIDDEN_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: str) -> str:
    """Remove NUL and other Postgres-unsafe control characters from `value`.

    Only strips the forbidden bytes -- readable content is left untouched.
    """
    return _FORBIDDEN_CHARS_RE.sub("", value)


def clean_json(value):
    """Recursively apply `clean_text` to every string in a parsed-JSON value.

    Used on the KI-Analyse's parsed model reply, which feeds several
    `Document`/`TagSuggestion`/`VorgangSuggestion`/`Correspondent` text
    fields at once -- cleaning the whole structure once, before any of
    those fields are read off it, covers all of them without repeating the
    call at each field access.
    """
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    return value
