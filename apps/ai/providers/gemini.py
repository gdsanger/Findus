"""Google Gemini adapter -- embeddings + generation.

Uses the `x-goog-api-key` header rather than the `?key=` query
parameter the REST docs default to: keeping the key out of the URL
means it can never end up in a request-line log line or an exception
message that includes the URL.
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

# Gemini's "assistant" role is spelled "model".
_ROLE_MAP = {"assistant": "model", "user": "user"}


class GeminiProvider:
    name = "gemini"

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
        return f"GeminiProvider(base_url={self._base_url!r}, api_key={mask_secret(self._api_key)})"

    def _headers(self) -> dict:
        return {"x-goog-api-key": self._api_key}

    def _report_usage(self, capability: str, model: str, usage_body: dict) -> None:
        if not self._usage_hook or not usage_body:
            return
        usage = Usage(
            prompt_tokens=usage_body.get("promptTokenCount", 0),
            completion_tokens=usage_body.get("candidatesTokenCount", 0),
        )
        self._usage_hook(capability, self.name, model, usage)

    def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        texts = list(texts)
        model_path = f"models/{self._embedding_model}"
        body = request_json(
            "POST",
            f"{self._base_url}/{model_path}:batchEmbedContents",
            provider=self.name,
            headers=self._headers(),
            json={
                "requests": [
                    {"model": model_path, "content": {"parts": [{"text": text}]}}
                    for text in texts
                ]
            },
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        vectors = [item["values"] for item in body["embeddings"]]
        return EmbeddingResult(
            vectors=vectors,
            model=self._embedding_model,
            version=self._embedding_model_version,
        )

    @staticmethod
    def _contents(messages: Iterable[Message]):
        system_parts = []
        contents = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
            else:
                role = _ROLE_MAP.get(message.role, message.role)
                contents.append({"role": role, "parts": [{"text": message.content}]})
        return "\n\n".join(system_parts), contents

    def generate(
        self, messages: Iterable[Message], *, stream: bool = False
    ) -> GenerationOutput:
        system, contents = self._contents(messages)
        payload = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        if stream:
            return self._stream(payload)

        body = request_json(
            "POST",
            f"{self._base_url}/models/{self._generation_model}:generateContent",
            provider=self.name,
            headers=self._headers(),
            json=payload,
            timeout=self._timeout,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        text = "".join(
            part.get("text", "")
            for part in body["candidates"][0]["content"]["parts"]
        )
        self._report_usage("generate", self._generation_model, body.get("usageMetadata", {}))
        return GenerationResult(
            text=text,
            model=self._generation_model,
            version=self._generation_model_version,
        )

    def _stream(self, payload: dict) -> Iterator[GenerationChunk]:
        for line in stream_lines(
            "POST",
            f"{self._base_url}/models/{self._generation_model}:streamGenerateContent?alt=sse",
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
            candidates = event.get("candidates") or []
            if not candidates:
                continue
            for part in candidates[0].get("content", {}).get("parts", []):
                delta = part.get("text", "")
                if delta:
                    yield GenerationChunk(delta=delta)
            usage_body = event.get("usageMetadata")
            if usage_body:
                self._report_usage("generate", self._generation_model, usage_body)
        yield GenerationChunk(delta="", done=True)
