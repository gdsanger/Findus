"""Backend-neutral types for mail sending.

Callers depend only on `OutgoingMessage` / `MailBackend` -- never on a
concrete SMTP/Graph module -- so the backend is the steckbare Kante
(same pattern as `apps.ai.providers.base`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class MailBackendError(Exception):
    """Raised when a mail backend fails after exhausting retries.

    Built from the exception type and backend name only -- never from
    request/response bodies, so an SMTP/Graph error message can't leak a
    password or bearer token into a log or debug page.
    """


@dataclass(frozen=True)
class OutgoingMessage:
    to: list[str]
    subject: str
    text_body: str
    html_body: str | None = None
    reply_to: str | None = field(default=None)


@runtime_checkable
class MailBackend(Protocol):
    def send(self, message: OutgoingMessage) -> None:
        """Send `message`, raising `MailBackendError` on failure."""
        ...


def mask_secret(value: str | None) -> str:
    if not value:
        return "<unset>"
    return "***"


def with_retry(
    fn: Callable[[], object],
    *,
    retries: int,
    backoff_seconds: float,
    backend: str,
    retriable_exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """Call `fn`, retrying on `retriable_exceptions` with exponential
    backoff. Never logs the exception message -- only its type -- so a
    transient SMTP/Graph error can't echo a credential into the logs.
    """

    attempt = 0
    while True:
        try:
            return fn()
        except retriable_exceptions as exc:
            attempt += 1
            if attempt > retries:
                raise MailBackendError(
                    f"{backend}: giving up after {attempt} attempt(s), "
                    f"last error was {type(exc).__name__}"
                ) from exc
            sleep_seconds = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s: send failed (%s), attempt %s/%s, retrying in %.1fs",
                backend,
                type(exc).__name__,
                attempt,
                retries,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
