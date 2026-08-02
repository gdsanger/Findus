"""Provider-neutral types for the AI layer (embeddings + generation).

See Architektur.md, "LLM-Anbindung" / "Provider-Neutralität &
Lock-in-Hedges": the interface is the steckbare Kante -- callers depend
only on what's defined here, never on a concrete provider module.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Literal, Protocol, Union, runtime_checkable

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant"]


class ProviderError(Exception):
    """Raised when a provider adapter fails after exhausting retries.

    The message is built from the exception type and provider/model
    names only -- never from request/response bodies, so a stray
    Authorization header can't end up in a traceback string that gets
    logged or rendered on a Django debug page.
    """


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    """Token counts for one provider call, for the cost-tracking hook.

    `image_tokens` is reported separately from `prompt_tokens` (rather
    than folded in) because vision-capable providers bill/report image
    input differently from text -- callers that want an image-inclusive
    cost estimate need to see the split, not just a combined number.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    image_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens + self.image_tokens


# (capability, provider_name, model, usage) -> None. Register via
# apps.ai.providers.registry.set_usage_hook() to count tokens/cost;
# left as a no-op by default.
UsageHook = Callable[[str, str, str, Usage], None]


@dataclass(frozen=True)
class EmbeddingResult:
    """`model` + `version` travel with the vectors onto `Chunk` -- the
    lock-in hedge that makes a later re-index targeted instead of a
    full-archive guess (see Architektur.md, "Lock-in-Hedges").
    """

    vectors: list[list[float]]
    model: str
    version: str


@dataclass(frozen=True)
class GenerationChunk:
    delta: str
    done: bool = False


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    version: str


GenerationOutput = Union[GenerationResult, Iterator[GenerationChunk]]


@dataclass(frozen=True)
class ImageInput:
    """One image -- or a rendered PDF page, which is just an image by
    the time it reaches a vision provider -- to describe. `data` is raw
    bytes, never a file path: workers/containers calling a provider
    don't necessarily share a filesystem with whatever produced the
    image.
    """

    data: bytes
    mime_type: str = "image/png"


@dataclass(frozen=True)
class VisionResult:
    text: str
    model: str
    version: str


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        """Batch-embed `texts`, in order, into one vector each."""
        ...


@runtime_checkable
class GenerationProvider(Protocol):
    def generate(self, messages: Iterable[Message], *, stream: bool = False) -> GenerationOutput:
        """Return a `GenerationResult`, or an iterator of `GenerationChunk`
        when `stream=True` (SSE-suitable: callers can forward each delta
        to a client as it arrives).
        """
        ...


@runtime_checkable
class VisionProvider(Protocol):
    def describe_image(self, image: ImageInput, prompt: str) -> VisionResult:
        """Transcribe/describe `image` per `prompt`, e.g. "transcribe
        this invoice page" -- the extraction cascade's last-resort
        stage for pages that OCR/text-extraction couldn't read.
        """
        ...


def mask_secret(value: str | None) -> str:
    if not value:
        return "<unset>"
    return "***"


def with_retry(
    fn: Callable[[], object],
    *,
    retries: int,
    backoff_seconds: float,
    provider: str,
    retriable_exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """Call `fn`, retrying on `retriable_exceptions` with exponential
    backoff. Never logs the exception message -- only its type -- so a
    provider SDK/HTTP error that happens to echo request details can't
    leak a key into the logs.
    """

    attempt = 0
    while True:
        try:
            return fn()
        except retriable_exceptions as exc:
            attempt += 1
            if attempt > retries:
                raise ProviderError(
                    f"{provider}: giving up after {attempt} attempt(s), "
                    f"last error was {type(exc).__name__}"
                ) from exc
            sleep_seconds = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s: call failed (%s), attempt %s/%s, retrying in %.1fs",
                provider,
                type(exc).__name__,
                attempt,
                retries,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
