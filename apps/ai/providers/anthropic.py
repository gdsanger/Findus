"""Anthropic (Claude) adapter -- generation only.

Anthropic has no embeddings endpoint, so this adapter implements only
`generate()`; it's registered under `FINDUS_AI_GENERATION_PROVIDER`
but can never be selected for embeddings (the registry raises a clear
config error instead of failing deep inside a request).
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator, Optional

from .base import (
    GenerationChunk,
    GenerationOutput,
    GenerationResult,
    Message,
    Usage,
    UsageHook,
    mask_secret,
)
from .http import request_json, stream_lines

ANTHROPIC_VERSION_HEADER = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        generation_model: str,
        generation_model_version: str,
        max_tokens: int,
        timeout: float,
        max_retries: int,
        retry_backoff_seconds: float,
        usage_hook: Optional[UsageHook] = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._generation_model = generation_model
        self._generation_model_version = generation_model_version
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._usage_hook = usage_hook

    def __repr__(self) -> str:
        return f"AnthropicProvider(base_url={self._base_url!r}, api_key={mask_secret(self._api_key)})"

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION_HEADER,
        }

    @staticmethod
    def _split_system(messages: Iterable[Message]):
        system_parts = []
        rest = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                rest.append({"role": message.role, "content": message.content})
        return "\n\n".join(system_parts), rest

    def _report_usage(self, usage_body: dict) -> None:
        if not self._usage_hook or not usage_body:
            return
        usage = Usage(
            prompt_tokens=usage_body.get("input_tokens", 0),
            completion_tokens=usage_body.get("output_tokens", 0),
        )
        self._usage_hook("generate", self.name, self._generation_model, usage)

    def generate(
        self, messages: Iterable[Message], *, stream: bool = False
    ) -> GenerationOutput:
        system, rest = self._split_system(messages)
        payload = {
            "model": self._generation_model,
            "max_tokens": self._max_tokens,
            "messages": rest,
            "stream": stream,
        }
        if system:
            payload["system"] = system

        if stream:
            return self._stream(payload)

        body = request_json(
            "POST",
            f"{self._base_url}/v1/messages",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        text = "".join(
            block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"
        )
        self._report_usage(body.get("usage", {}))
        return GenerationResult(
            text=text,
            model=self._generation_model,
            version=self._generation_model_version,
        )

    def _stream(self, payload: dict) -> Iterator[GenerationChunk]:
        for line in stream_lines(
            "POST",
            f"{self._base_url}/v1/messages",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        ):
            if not line.startswith("data:"):
                continue
            event = json.loads(line[len("data:") :].strip())
            event_type = event.get("type")
            if event_type == "content_block_delta":
                delta = event.get("delta", {}).get("text", "")
                if delta:
                    yield GenerationChunk(delta=delta)
            elif event_type == "message_delta":
                usage_body = event.get("usage")
                if usage_body:
                    self._report_usage(usage_body)
            elif event_type == "message_stop":
                break
        yield GenerationChunk(delta="", done=True)
