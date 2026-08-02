"""SMTP backend: sends through Django's own SMTP mail backend.

Django's `EmailMultiAlternatives` already speaks multipart/alternative
(text + HTML), so this backend is a thin wire-up rather than a protocol
implementation -- unlike Graph, which has no such multipart concept
(see graph.py).
"""

from __future__ import annotations

import smtplib

from django.core.mail import EmailMultiAlternatives, get_connection

from .base import MailBackendError, OutgoingMessage, mask_secret, with_retry

RETRIABLE_EXCEPTIONS = (smtplib.SMTPException, OSError)


class SMTPBackend:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        use_ssl: bool,
        from_address: str,
        timeout: float,
        max_retries: int,
        retry_backoff_seconds: float,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.from_address = from_address
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    def send(self, message: OutgoingMessage) -> None:
        def _send():
            connection = get_connection(
                backend="django.core.mail.backends.smtp.EmailBackend",
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
                use_ssl=self.use_ssl,
                timeout=self.timeout,
            )
            email = EmailMultiAlternatives(
                subject=message.subject,
                body=message.text_body,
                from_email=self.from_address,
                to=list(message.to),
                reply_to=[message.reply_to] if message.reply_to else None,
                connection=connection,
            )
            if message.html_body:
                email.attach_alternative(message.html_body, "text/html")
            sent_count = email.send()
            if not sent_count:
                raise MailBackendError("smtp: send() reported 0 messages accepted")

        with_retry(
            _send,
            retries=self.max_retries,
            backoff_seconds=self.retry_backoff_seconds,
            backend="smtp",
            retriable_exceptions=RETRIABLE_EXCEPTIONS,
        )

    def __repr__(self) -> str:
        return (
            f"SMTPBackend(host={self.host!r}, port={self.port}, "
            f"username={mask_secret(self.username)})"
        )
