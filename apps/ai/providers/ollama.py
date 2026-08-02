"""Ollama adapter -- embeddings + generation against a local (or
self-hosted) Ollama instance. This is the Embedding-Lock-in-Hedge path
from Architektur.md: local/open embeddings so a cloud provider outage
or price change never blocks re-indexing.

No API key by default (Ollama has none out of the box); `api_key`, if
set, is sent as a bearer token for setups proxied behind auth.
"""

from __future__ import annotations

import base64
import json
from typing import Iterable, Iterator, Optional

from .base import (
    EmbeddingResult,
    GenerationChunk,
    GenerationOutput,
    GenerationResult,
    ImageInput,
    Message,
    ProviderError,
    UsageHook,
    VisionResult,
)
from .http import request_json, stream_lines


class OllamaProvider:
    """Embeddings + generation + (via `vision_model`, e.g. `llava`)
    vision -- the local/no-cost path for all three capabilities, so a
    cloud outage or price change never blocks re-indexing, generation,
    or the extraction cascade's vision stage (see Architektur.md,
    "Lock-in-Hedges").
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        embedding_model: str,
        embedding_model_version: str,
        generation_model: str,
        generation_model_version: str,
        timeout: float,
        max_retries: int,
        retry_backoff_seconds: float,
        api_key: str = "",
        vision_model: str = "",
        vision_model_version: str = "",
        usage_hook: Optional[UsageHook] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._embedding_model = embedding_model
        self._embedding_model_version = embedding_model_version
        self._generation_model = generation_model
        self._generation_model_version = generation_model_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._api_key = api_key
        self._vision_model = vision_model
        self._vision_model_version = vision_model_version
        self._usage_hook = usage_hook

    def __repr__(self) -> str:
        return f"OllamaProvider(base_url={self._base_url!r})"

    def _headers(self) -> dict:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        texts = list(texts)
        body = request_json(
            "POST",
            f"{self._base_url}/api/embed",
            provider=self.name,
            headers=self._headers(),
            json={"model": self._embedding_model, "input": texts},
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        return EmbeddingResult(
            vectors=body["embeddings"],
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
            f"{self._base_url}/api/chat",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        return GenerationResult(
            text=body["message"]["content"],
            model=self._generation_model,
            version=self._generation_model_version,
        )

    def _stream(self, payload: dict) -> Iterator[GenerationChunk]:
        # Ollama streams newline-delimited JSON objects, not SSE.
        for line in stream_lines(
            "POST",
            f"{self._base_url}/api/chat",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        ):
            event = json.loads(line)
            delta = event.get("message", {}).get("content", "")
            done = bool(event.get("done"))
            if delta:
                yield GenerationChunk(delta=delta)
            if done:
                yield GenerationChunk(delta="", done=True)
                return
        yield GenerationChunk(delta="", done=True)

    def describe_image(self, image: ImageInput, prompt: str) -> VisionResult:
        if not self._vision_model:
            raise ProviderError(f"{self.name}: no vision_model configured for describe_image")

        body = request_json(
            "POST",
            f"{self._base_url}/api/chat",
            provider=self.name,
            headers=self._headers(),
            json={
                "model": self._vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [base64.b64encode(image.data).decode("ascii")],
                    }
                ],
                "stream": False,
            },
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        return VisionResult(
            text=body["message"]["content"],
            model=self._vision_model,
            version=self._vision_model_version,
        )
