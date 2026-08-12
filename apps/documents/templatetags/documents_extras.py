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

_SPHERE_BADGE_CLASSES = {
    # Geschäftlich als ruhiges, aber sichtbares Info-Blau, privat als
    # zurückgenommenes Grau -- die Sphäre (#1112) ist eine Einordnung, kein
    # Alarm, und soll das Richtungs-Badge daneben nicht überstrahlen.
    "geschaeftlich": "text-bg-info",
    "privat": "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
}

_TAX_RELEVANCE_BADGE_CLASSES = {
    # Private ESt-Absetzbarkeit (#1113): Ja = grün (positiver Treffer),
    # Vielleicht = gelb (Hedge, prüfen), Nein = neutral-zurückgenommen,
    # "nicht zutreffend" = dezenter Outline (geschäftlich, kein privates
    # Thema). `unbekannt` fällt auf denselben dezenten Default wie eine
    # unklassifizierte Sphäre.
    "ja": "text-bg-success",
    "vielleicht": "text-bg-warning",
    "nein": "bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle",
    "nicht_zutreffend": "border text-body-secondary",
}

_ACTION_STATUS_BADGE_CLASSES = {
    "offen": "text-bg-warning",
    "erledigt": "bg-success-subtle text-success-emphasis border border-success-subtle",
}

_VORGANG_STATUS_BADGE_CLASSES = {
    "open": "text-bg-secondary",
    "in_progress": "text-bg-warning",
    "closed": "text-bg-success",
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


@register.filter(name="sphere_badge_class")
def sphere_badge_class(sphere):
    """Bootstrap badge class for `Document.sphere` (#1112) -- a subdued
    outline for `unbekannt` (same treatment as an unclassified `direction`)
    so a not-yet-classified document doesn't visually compete with a
    classified one.
    """
    return _SPHERE_BADGE_CLASSES.get(sphere, "border text-body-secondary")


@register.filter(name="tax_relevance_badge_class")
def tax_relevance_badge_class(tax_relevance):
    """Bootstrap badge class for `Document.tax_relevance` (#1113) -- private
    ESt-Absetzbarkeit. Ampel-Logik (Ja grün, Vielleicht gelb, Nein neutral),
    `unbekannt` fällt auf den dezenten Outline-Default zurück, wie eine
    unklassifizierte Sphäre/Richtung.
    """
    return _TAX_RELEVANCE_BADGE_CLASSES.get(tax_relevance, "border text-body-secondary")


@register.filter(name="action_status_badge_class")
def action_status_badge_class(action_status):
    """Bootstrap badge class for `Document.action_status` (#1057) --

    `offen` gets a loud solid amber so open follow-ups stand out in the
    list/detail; `erledigt` a subdued green so it doesn't compete for
    attention once done. `keine` has no entry -- templates skip the badge
    entirely for it instead of rendering a neutral one.
    """
    return _ACTION_STATUS_BADGE_CLASSES.get(action_status, "")


@register.filter(name="vorgang_status_badge_class")
def vorgang_status_badge_class(status):
    """Bootstrap badge class for `Vorgang.status` (#1084) -- solid colors
    throughout, including for `closed`, unlike `action_status_badge_class`'s
    subdued `*-subtle` treatment: "Abgeschlossen" must stay a clearly
    visible signal on its own even though the index dims/collapses closed
    rows separately.
    """
    return _VORGANG_STATUS_BADGE_CLASSES.get(status, "text-bg-secondary")
