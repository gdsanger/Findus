"""Registers the daily Wiedervorlage-Erinnerungslauf (#1125) as a django-q2

`Schedule` row so it survives a fresh DB build/deploy instead of living only
as a manually created row -- same `post_migrate`-signal wiring as
`apps.ingest.schedules.sync_mail_ingest_schedule`.
"""

from __future__ import annotations

DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME = "document-comment-reminders"


def sync_document_comment_reminder_schedule(sender=None, **kwargs):
    from django_q.models import Schedule

    Schedule.objects.update_or_create(
        name=DOCUMENT_COMMENT_REMINDER_SCHEDULE_NAME,
        defaults={
            "func": "apps.documents.reminders.send_due_comment_reminders",
            "schedule_type": Schedule.DAILY,
            "repeats": -1,
        },
    )
