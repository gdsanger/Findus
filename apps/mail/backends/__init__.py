"""Pluggable mail-send backends: SMTP + Microsoft Graph API.

Mirrors the config-driven-factory shape of `apps.ai.providers` (see that
package's docstring): callers should only ever import from here, never a
concrete backend module, so switching SMTP <-> Graph is a config change:

    from apps.mail.backends import get_mail_backend, OutgoingMessage

    backend = get_mail_backend()  # picks SMTP/Graph/fake from settings
    backend.send(OutgoingMessage(to=["a@b.de"], subject="...", text_body="..."))

This package only sends mail. Mail *ingest* (reading a Graph mailbox via
IMAP/Graph) is a separate, later concern -- see Architektur.md, "Build-Plan
Step 1", point 4.
"""

from .base import MailBackend, MailBackendError, OutgoingMessage
from .registry import get_mail_backend

__all__ = [
    "MailBackend",
    "MailBackendError",
    "OutgoingMessage",
    "get_mail_backend",
]
