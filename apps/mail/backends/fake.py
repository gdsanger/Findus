"""In-memory fake backend -- no network, no real send.

Used by this app's own tests and importable by any caller (e.g. the
document-processing pipeline once it sends real notifications) that
needs to assert "a mail was sent" without a real SMTP/Graph round-trip.
"""

from __future__ import annotations

from .base import OutgoingMessage


class FakeMailBackend:
    name = "fake"

    def __init__(self):
        self.sent: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)
