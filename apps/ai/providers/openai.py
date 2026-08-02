"""OpenAI adapter -- embeddings + chat completions over the plain HTTP API.

No `openai` SDK dependency on purpose (see package `__init__.py` for the
LiteLLM-vs-thin-adapters decision): a handful of JSON requests don't
justify pulling in and pinning a vendor SDK.
"""

from __future__ import annotations

import json
from typing import Iterable, Iterator, Optional

from .base import (
    EmbeddingResult,
    GenerationChunk,
    GenerationOutput,
    GenerationResult,
    Message,
    Usage,
    UsageHook,
    mask_secret,
)
from .http import request_json, stream_lines


class OpenAIProvider:
    """Embeddings + generation via `api.openai.com/v1` (or an
    OpenAI-compatible endpoint, e.g. Azure OpenAI, via `base_url`).
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        embedding_model: str,
        embedding_model_version: str,
        generation_model: str,
        generation_model_version: str,
        timeout: float,
        max_retries: int,
        retry_backoff_seconds: float,
        usage_hook: Optional[UsageHook] = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._embedding_model = embedding_model
        self._embedding_model_version = embedding_model_version
        self._generation_model = generation_model
        self._generation_model_version = generation_model_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._usage_hook = usage_hook

    def __repr__(self) -> str:
        return f"OpenAIProvider(base_url={self._base_url!r}, api_key={mask_secret(self._api_key)})"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _report_usage(self, capability: str, model: str, usage_body: dict) -> None:
        if not self._usage_hook or not usage_body:
            return
        usage = Usage(
            prompt_tokens=usage_body.get("prompt_tokens", 0),
            completion_tokens=usage_body.get("completion_tokens", 0),
        )
        self._usage_hook(capability, self.name, model, usage)

    def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        texts = list(texts)
        body = request_json(
            "POST",
            f"{self._base_url}/embeddings",
            provider=self.name,
            headers=self._headers(),
            json={"model": self._embedding_model, "input": texts},
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        ordered = sorted(body["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in ordered]
        self._report_usage("embed", self._embedding_model, body.get("usage", {}))
        return EmbeddingResult(
            vectors=vectors,
            model=self._embedding_model,
            version=self._embedding_model_version,
        )

    def generate(
        self, messages: Iterable[Message], *, stream: bool = False
    ) -> GenerationOutput:
        payload = {
            "model": self._generation_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
        }
        if stream:
            return self._stream(payload)

        body = request_json(
            "POST",
            f"{self._base_url}/chat/completions",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        text = body["choices"][0]["message"]["content"]
        self._report_usage("generate", self._generation_model, body.get("usage", {}))
        return GenerationResult(
            text=text,
            model=self._generation_model,
            version=self._generation_model_version,
        )

    def _stream(self, payload: dict) -> Iterator[GenerationChunk]:
        for line in stream_lines(
            "POST",
            f"{self._base_url}/chat/completions",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        ):
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {}).get("content") or ""
            if delta:
                yield GenerationChunk(delta=delta)
        yield GenerationChunk(delta="", done=True)
