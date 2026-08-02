"""Caller-facing entry points for sending a notification mail.

`send_notification` does the actual send and is meant to run on the
Django-Q2 worker (see tasks.py) -- not in the request/response cycle.
`queue_notification` is the request-cycle-safe entry point: it only
enqueues and returns immediately.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .backends import OutgoingMessage, get_mail_backend
from .templating import render_mail


def send_notification(
    template_name: str,
    to: Iterable[str],
    context: Optional[dict] = None,
    *,
    backend_name: Optional[str] = None,
) -> None:
    """Render `template_name` with `context` and send it to `to` now.

    Runs the real backend I/O (SMTP/Graph) -- call this from the
    background worker, not from a request handler.
    """

    rendered = render_mail(template_name, context or {})
    message = OutgoingMessage(
        to=list(to),
        subject=rendered.subject,
        text_body=rendered.text_body,
        html_body=rendered.html_body,
    )
    get_mail_backend(backend_name).send(message)


def queue_notification(
    template_name: str,
    to: Iterable[str],
    context: Optional[dict] = None,
    *,
    backend_name: Optional[str] = None,
) -> str:
    """Enqueue `send_notification` on the Django-Q2 worker and return the
    task id immediately -- safe to call from a request handler.
    """

    from django_q.tasks import async_task

    from .tasks import send_notification_task

    return async_task(
        send_notification_task,
        template_name,
        list(to),
        context or {},
        backend_name,
    )
