"""Pluggable AI provider layer: embeddings + generation + vision, provider-neutral.

See Architektur.md, "LLM-Anbindung" and "Provider-Neutralität &
Lock-in-Hedges". Callers should only ever import from this package,
never a concrete adapter module -- that's what makes the provider a
config choice instead of a code change:

    from apps.ai.providers import get_embedding_provider, Message

    provider = get_embedding_provider()  # picks OpenAI/Claude/Gemini/Ollama/fake
    result = provider.embed(["some text"])
    # result.vectors, result.model, result.version -> Chunk fields

`describe_image()` (via `get_vision_provider()`) is the third
capability, added on top of the tested embed/generate base rather than
folded into it -- it's config-selectable independently
(`FINDUS_AI_VISION_PROVIDER`), e.g. local llava/Ollama for images that
must never leave the container, while embed/generate stay on whatever
provider was already configured. Only providers with a real image-input
API are registered for it (`openai`, `ollama`, `fake`); Anthropic/Gemini
vision is left for a later issue, same as Anthropic embeddings today.

LiteLLM-as-common-base was considered per the issue and rejected for
now: LiteLLM's chat-completions-shaped abstraction covers generation
well but has no first-class batch-embeddings-with-model/version
contract, which is the one piece this layer actually needs to get
right (see `EmbeddingResult`). Four thin, ~100-line HTTP adapters are
easier to audit for "does this leak the key" than a large dependency
whose retry/logging internals aren't ours to review line by line.
Revisit if a fifth provider is added and the adapter count starts to
hurt.
"""

from .base import (
    EmbeddingProvider,
    EmbeddingResult,
    GenerationChunk,
    GenerationOutput,
    GenerationProvider,
    GenerationResult,
    ImageInput,
    Message,
    ProviderError,
    Usage,
    UsageHook,
    VisionProvider,
    VisionResult,
)
from .registry import (
    get_embedding_provider,
    get_generation_provider,
    get_vision_provider,
    set_usage_hook,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResult",
    "GenerationChunk",
    "GenerationOutput",
    "GenerationProvider",
    "GenerationResult",
    "ImageInput",
    "Message",
    "ProviderError",
    "Usage",
    "UsageHook",
    "VisionProvider",
    "VisionResult",
    "get_embedding_provider",
    "get_generation_provider",
    "get_vision_provider",
    "set_usage_hook",
]
