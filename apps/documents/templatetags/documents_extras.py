"""Template filters for the document detail view (#1016)."""

from __future__ import annotations

import markdown as markdown_lib
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_LANGUAGE_LABELS = {
    "de": "Deutsch",
    "en": "Englisch",
    "fr": "Französisch",
    "es": "Spanisch",
    "it": "Italienisch",
    "nl": "Niederländisch",
}

_DIRECTION_BADGE_CLASSES = {
    "eingang": "text-bg-primary",
    "ausgang": "text-bg-success",
    "intern": "text-bg-secondary",
}


@register.filter(name="render_markdown")
def render_markdown(value):
    """Render `Document.markdown` (or the `text_content` fallback) to HTML.

    The source is extraction/OCR/vision output, not author-trusted
    Markdown -- escaping it before conversion strips any embedded HTML
    (e.g. a phishing mail's `<script>`) while leaving the `#`/blank-line
    syntax `apps.documents.extraction.build_markdown()` emits intact.
    """
    if not value:
        return ""
    html = markdown_lib.markdown(escape(value), extensions=["nl2br"])
    return mark_safe(html)


@register.filter(name="language_label")
def language_label(code):
    if not code:
        return "—"
    return _LANGUAGE_LABELS.get(code, code.upper())


@register.filter(name="direction_badge_class")
def direction_badge_class(direction):
    """Bootstrap badge class for `Document.direction` (#1031) -- a solid
    color for a known direction, a subdued outline for `unbekannt` so an
    unclassified document doesn't visually compete with a classified one.
    """
    return _DIRECTION_BADGE_CLASSES.get(direction, "border text-body-secondary")
