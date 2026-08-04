"""Registers the periodic `watch_mail_ingest --once` trigger as a
django-q2 `Schedule` row so it survives a fresh DB build/deploy instead of
living only as a manually created row (#1060). Wired up via the
`post_migrate` signal in `apps.py` -- fires after every `migrate`, not at
process start, so it can never run before the `django_q_schedule` table
itself exists on a first-time deploy.
"""

from __future__ import annotations

from django.conf import settings

MAIL_INGEST_SCHEDULE_NAME = "mail-ingest"


def sync_mail_ingest_schedule(sender=None, **kwargs):
    from django_q.models import Schedule

    Schedule.objects.update_or_create(
        name=MAIL_INGEST_SCHEDULE_NAME,
        defaults={
            "func": "django.core.management.call_command",
            "args": repr(("watch_mail_ingest", "--once")),
            "schedule_type": Schedule.MINUTES,
            "minutes": settings.FINDUS_MAIL_POLL_MINUTES,
            "repeats": -1,
        },
    )
