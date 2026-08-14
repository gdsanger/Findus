"""Wiedervorlage-Erinnerungen (#1125): täglicher Job, der fällige, noch nie

erinnerte Kommentare gebündelt pro Autor per Mail verschickt.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.mail.service import send_notification

from .models import DocumentComment

REMINDER_MAIL_TEMPLATE = "document_comment_reminder"


def _document_url(document):
    return settings.FINDUS_BASE_URL.rstrip("/") + reverse("documents:detail", args=[document.pk])


def send_due_comment_reminders():
    """Django-Q2 worker entry point, scheduled daily (see `schedules.py`).

    Gruppiert alle fälligen, noch nicht erinnerten Kommentare je Autor und
    verschickt genau eine Mail pro Autor -- ein Autor mit drei fälligen
    Wiedervorlagen bekommt eine Mail mit drei Punkten, nicht drei Mails.
    `reminded_at` wird erst nach dem `send_notification`-Aufruf gesetzt: ein
    Absturz mitten im Versand lässt die Wiedervorlage lieber offen (nächster
    Lauf versucht es erneut) statt sie fälschlich als erinnert zu markieren.
    """
    due_comments = (
        DocumentComment.objects.pending_reminders()
        .select_related("document", "author")
        .order_by("author_id", "follow_up_date")
    )

    by_author = {}
    for comment in due_comments:
        if comment.author is None or not comment.author.email:
            continue
        by_author.setdefault(comment.author, []).append(comment)

    for author, comments in by_author.items():
        items = [
            {
                "document_title": comment.document.title,
                "body": comment.body,
                "follow_up_date": comment.follow_up_date,
                "document_url": _document_url(comment.document),
            }
            for comment in comments
        ]
        send_notification(
            REMINDER_MAIL_TEMPLATE,
            [author.email],
            {"items": items, "count": len(items)},
        )
        DocumentComment.objects.filter(pk__in=[comment.pk for comment in comments]).update(
            reminded_at=timezone.now()
        )
