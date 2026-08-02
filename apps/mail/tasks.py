import logging
from typing import Optional

from .backends import MailBackendError
from .service import send_notification

logger = logging.getLogger(__name__)


def send_notification_task(
    template_name: str,
    to: list[str],
    context: Optional[dict] = None,
    backend_name: Optional[str] = None,
) -> None:
    """Django-Q2 worker entry point, queued via `service.queue_notification`.

    Transient send failures are already retried with backoff inside the
    backend (see backends/base.py `with_retry`); if `MailBackendError`
    still reaches here, the backend gave up -- log and re-raise so
    Django-Q2 records the task as failed instead of silently dropping
    the notification.
    """

    try:
        send_notification(template_name, to, context, backend_name=backend_name)
    except MailBackendError:
        logger.exception(
            "Mail-Versand endgültig fehlgeschlagen: template=%s to=%s", template_name, to
        )
        raise
