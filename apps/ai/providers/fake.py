"""In-memory fake providers -- no network, deterministic output.

Used by this app's own tests and importable by any later feature
(retrieval service, RAG endpoint, ...) that needs to exercise
embedding/generation call sites without a real provider.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional

from .base import (
    EmbeddingResult,
    GenerationChunk,
    GenerationOutput,
    GenerationResult,
    ImageInput,
    Message,
    VisionResult,
)


class FakeEmbeddingProvider:
    """Deterministic embeddings: each vector is `dimensions` copies of
    the text's length, scaled -- good enough to assert on without
    depending on real model output.
    """

    name = "fake"

    def __init__(self, *, dimensions: int = 8, model: str = "fake-embed", version: str = "1"):
        self.dimensions = dimensions
        self.model = model
        self.version = version
        self.calls: list[list[str]] = []

    def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        texts = list(texts)
        self.calls.append(texts)
        vectors = [[float(len(text) % 7 + 1)] * self.dimensions for text in texts]
        return EmbeddingResult(vectors=vectors, model=self.model, version=self.version)


class FakeGenerationProvider:
    """Echoes the last user message back, optionally split into chunks
    for the streaming path.
    """

    name = "fake"

    def __init__(
        self,
        *,
        model: str = "fake-generate",
        version: str = "1",
        reply: Optional[str] = None,
    ):
        self.model = model
        self.version = version
        self.reply = reply
        self.calls: list[list[Message]] = []

    def _reply_for(self, messages: list[Message]) -> str:
        if self.reply is not None:
            return self.reply
        user_messages = [m for m in messages if m.role == "user"]
        last = user_messages[-1].content if user_messages else ""
        return f"echo: {last}"

    def generate(
        self, messages: Iterable[Message], *, stream: bool = False
    ) -> GenerationOutput:
        messages = list(messages)
        self.calls.append(messages)
        text = self._reply_for(messages)
        if not stream:
            return GenerationResult(text=text, model=self.model, version=self.version)
        return self._stream(text)

    def _stream(self, text: str) -> Iterator[GenerationChunk]:
        for word in text.split(" "):
            yield GenerationChunk(delta=word + " ")
        yield GenerationChunk(delta="", done=True)


class FakeVisionProvider:
    """Records the `(image, prompt)` it was called with and returns a
    deterministic description -- no image is ever decoded/rendered.
    """

    name = "fake"

    def __init__(
        self,
        *,
        model: str = "fake-vision",
        version: str = "1",
        reply: Optional[str] = None,
    ):
        self.model = model
        self.version = version
        self.reply = reply
        self.calls: list[tuple[ImageInput, str]] = []

    def describe_image(self, image: ImageInput, prompt: str) -> VisionResult:
        self.calls.append((image, prompt))
        text = self.reply if self.reply is not None else f"described {len(image.data)} bytes for: {prompt}"
        return VisionResult(text=text, model=self.model, version=self.version)
