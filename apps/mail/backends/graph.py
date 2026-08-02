"""Microsoft Graph API backend: sends via `POST /users/{sender}/sendMail`
using an app-only (client-credentials) token.

Graph's sendMail body takes a single `body.contentType` (`Text` or
`HTML`) -- there is no multipart/alternative concept like SMTP's, so a
message with an HTML body is sent as HTML-only and the plain-text
rendering is dropped for that send. Callers that need a guaranteed
plain-text copy should route through the SMTP backend instead.

The same client-credentials token fetch is reusable by the later Graph
*ingest* feature (reading a mailbox) -- see Architektur.md, "Build-Plan
Step 1", point 4.
"""

from __future__ import annotations

import time

import requests

from .base import OutgoingMessage, mask_secret, with_retry

RETRIABLE_EXCEPTIONS = (requests.RequestException,)

TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# Refetch this many seconds before the token's reported expiry, so a
# request never starts with a token that expires mid-flight.
TOKEN_EXPIRY_MARGIN_SECONDS = 60


class GraphBackend:
    name = "graph"

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        sender: str,
        from_address: str,
        timeout: float,
        max_retries: int,
        retry_backoff_seconds: float,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.sender = sender
        self.from_address = from_address
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _fetch_token(self) -> str:
        def _request():
            response = requests.post(
                TOKEN_URL_TEMPLATE.format(tenant_id=self.tenant_id),
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": GRAPH_SCOPE,
                    "grant_type": "client_credentials",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()

        payload = with_retry(
            _request,
            retries=self.max_retries,
            backoff_seconds=self.retry_backoff_seconds,
            backend="graph-token",
            retriable_exceptions=RETRIABLE_EXCEPTIONS,
        )
        self._token = payload["access_token"]
        self._token_expires_at = (
            time.time() + int(payload.get("expires_in", 3600)) - TOKEN_EXPIRY_MARGIN_SECONDS
        )
        return self._token

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    def send(self, message: OutgoingMessage) -> None:
        body = {
            "message": {
                "subject": message.subject,
                "body": {
                    "contentType": "HTML" if message.html_body else "Text",
                    "content": message.html_body or message.text_body,
                },
                "toRecipients": [{"emailAddress": {"address": addr}} for addr in message.to],
            },
            "saveToSentItems": "false",
        }
        if message.reply_to:
            body["message"]["replyTo"] = [{"emailAddress": {"address": message.reply_to}}]

        def _send():
            response = requests.post(
                f"{GRAPH_BASE_URL}/users/{self.sender}/sendMail",
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
            response.raise_for_status()

        with_retry(
            _send,
            retries=self.max_retries,
            backoff_seconds=self.retry_backoff_seconds,
            backend="graph",
            retriable_exceptions=RETRIABLE_EXCEPTIONS,
        )

    def __repr__(self) -> str:
        return (
            f"GraphBackend(tenant_id={self.tenant_id!r}, sender={self.sender!r}, "
            f"client_id={mask_secret(self.client_id)}, client_secret=***)"
        )
