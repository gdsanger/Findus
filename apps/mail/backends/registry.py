"""Config-driven factory: turns `settings.FINDUS_MAIL_*` into a concrete
backend instance. This is the only place that knows which class backs
which backend name -- callers only ever import `get_mail_backend` and
never a concrete backend module, so switching SMTP <-> Graph is a
config change, not a code change.
"""

from __future__ import annotations

from typing import Callable, Optional

from django.conf import settings

from .base import MailBackend, MailBackendError
from .fake import FakeMailBackend
from .graph import GraphBackend
from .smtp import SMTPBackend


def _common_kwargs() -> dict:
    return {
        "from_address": settings.FINDUS_MAIL_FROM_ADDRESS,
        "timeout": settings.FINDUS_MAIL_TIMEOUT_SECONDS,
        "max_retries": settings.FINDUS_MAIL_MAX_RETRIES,
        "retry_backoff_seconds": settings.FINDUS_MAIL_RETRY_BACKOFF_SECONDS,
    }


def _backend_config(name: str) -> dict:
    backends = getattr(settings, "FINDUS_MAIL_BACKENDS", {})
    if name not in backends:
        raise MailBackendError(f"No settings.FINDUS_MAIL_BACKENDS entry for backend '{name}'")
    return backends[name]


def _build_smtp(config: dict, common: dict) -> SMTPBackend:
    return SMTPBackend(
        host=config["host"],
        port=config["port"],
        username=config["username"],
        password=config["password"],
        use_tls=config["use_tls"],
        use_ssl=config["use_ssl"],
        **common,
    )


def _build_graph(config: dict, common: dict) -> GraphBackend:
    return GraphBackend(
        tenant_id=config["tenant_id"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        sender=config["sender"],
        **common,
    )


def _build_fake(config: dict, common: dict) -> FakeMailBackend:
    return FakeMailBackend()


# "fake" is a real, selectable backend (via FINDUS_MAIL_BACKEND=fake) so
# tests exercise the exact same config-switch path callers use in prod --
# not a special code branch that only exists for tests.
_BACKENDS: dict[str, Callable[[dict, dict], MailBackend]] = {
    "smtp": _build_smtp,
    "graph": _build_graph,
    "fake": _build_fake,
}


def get_mail_backend(name: Optional[str] = None) -> MailBackend:
    name = name or settings.FINDUS_MAIL_BACKEND
    builder = _BACKENDS.get(name)
    if builder is None:
        raise MailBackendError(f"Unknown mail backend '{name}'. Available: {sorted(_BACKENDS)}")
    config = {} if name == "fake" else _backend_config(name)
    return builder(config, _common_kwargs())
