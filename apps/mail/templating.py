"""Renders a named mail template into subject + text/HTML bodies.

A template is a directory `mail/<name>/` under any app's `templates/`
dir, holding `subject.txt` + `body.txt` (required) and `body.html`
(optional) -- ordinary Django templates, rendered with the caller's
context dict.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string


@dataclass(frozen=True)
class RenderedMail:
    subject: str
    text_body: str
    html_body: str | None


def render_mail(template_name: str, context: dict) -> RenderedMail:
    # Header-safe: a template with a trailing newline or line break must
    # not smuggle a CRLF into the Subject header.
    subject = " ".join(
        render_to_string(f"mail/{template_name}/subject.txt", context).split()
    )
    text_body = render_to_string(f"mail/{template_name}/body.txt", context)
    try:
        html_body = render_to_string(f"mail/{template_name}/body.html", context)
    except TemplateDoesNotExist:
        html_body = None
    return RenderedMail(subject=subject, text_body=text_body, html_body=html_body)
