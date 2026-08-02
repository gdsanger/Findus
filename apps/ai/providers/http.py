"""Thin HTTP helpers shared by the provider adapters.

Deliberately not a client SDK: each adapter builds its own request body
and parses its own response shape, this module only carries the bits
that are identical across all four backends (timeout/retry wiring and
line-based streaming parsers).
"""

from __future__ import annotations

from typing import Iterator

import requests

from .base import ProviderError, with_retry

# Network errors are worth retrying; HTTP 4xx/5xx are surfaced via
# raise_for_status() as requests.HTTPError, also retriable (a 429/5xx
# from the provider is exactly the transient case retries are for).
RETRIABLE_EXCEPTIONS = (requests.RequestException,)


def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict,
    json: dict,
    timeout: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict:
    def _call():
        response = requests.request(method, url, headers=headers, json=json, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return with_retry(
        _call,
        retries=max_retries,
        backoff_seconds=retry_backoff_seconds,
        provider=provider,
        retriable_exceptions=RETRIABLE_EXCEPTIONS,
    )


def stream_lines(
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict,
    json: dict,
    timeout: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> Iterator[str]:
    """Open a streaming request and yield raw response lines.

    Retries apply only to *establishing* the connection -- once bytes
    have started flowing, a mid-stream drop surfaces as a ProviderError
    rather than silently restarting and duplicating output already
    handed to the caller.
    """

    def _open():
        response = requests.request(
            method, url, headers=headers, json=json, timeout=timeout, stream=True
        )
        response.raise_for_status()
        return response

    response = with_retry(
        _open,
        retries=max_retries,
        backoff_seconds=retry_backoff_seconds,
        provider=provider,
        retriable_exceptions=RETRIABLE_EXCEPTIONS,
    )
    try:
        for line in response.iter_lines(decode_unicode=True):
            if line:
                yield line
    except requests.RequestException as exc:
        raise ProviderError(f"{provider}: stream interrupted ({type(exc).__name__})") from exc
