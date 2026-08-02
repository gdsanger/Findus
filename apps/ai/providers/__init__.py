"""Pluggable AI provider layer: embeddings + generation, provider-neutral.

See Architektur.md, "LLM-Anbindung" and "Provider-Neutralität &
Lock-in-Hedges". Callers should only ever import from this package,
never a concrete adapter module -- that's what makes the provider a
config choice instead of a code change:

    from apps.ai.providers import get_embedding_provider, Message

    provider = get_embedding_provider()  # picks OpenAI/Claude/Gemini/Ollama/fake
    result = provider.embed(["some text"])
    # result.vectors, result.model, result.version -> Chunk fields

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
    Message,
    ProviderError,
    Usage,
    UsageHook,
)
from .registry import get_embedding_provider, get_generation_provider, set_usage_hook

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResult",
    "GenerationChunk",
    "GenerationOutput",
    "GenerationProvider",
    "GenerationResult",
    "Message",
    "ProviderError",
    "Usage",
    "UsageHook",
    "get_embedding_provider",
    "get_generation_provider",
    "set_usage_hook",
]
