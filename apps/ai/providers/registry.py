"""Config-driven factory: turns `settings.FINDUS_AI_*` into a concrete
provider instance. This is the only place that knows which adapter
class backs which provider name -- callers only ever import
`get_embedding_provider` / `get_generation_provider` and never a
concrete adapter, so switching providers is a config change, not a
code change.
"""

from __future__ import annotations

from typing import Callable, Optional

from django.conf import settings

from .anthropic import AnthropicProvider
from .base import EmbeddingProvider, GenerationProvider, ProviderError, UsageHook, VisionProvider
from .fake import FakeEmbeddingProvider, FakeGenerationProvider, FakeVisionProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider

_usage_hook: Optional[UsageHook] = None


def set_usage_hook(hook: Optional[UsageHook]) -> None:
    """Register the token/cost-counting hook: called as
    `hook(capability, provider_name, model, usage)` after every
    embed()/generate() call that returns usage data. `None` disables it.
    """
    global _usage_hook
    _usage_hook = hook


def _common_kwargs() -> dict:
    return {
        "timeout": settings.FINDUS_AI_TIMEOUT_SECONDS,
        "max_retries": settings.FINDUS_AI_MAX_RETRIES,
        "retry_backoff_seconds": settings.FINDUS_AI_RETRY_BACKOFF_SECONDS,
        "usage_hook": _usage_hook,
    }


def _provider_config(name: str) -> dict:
    providers = getattr(settings, "FINDUS_AI_PROVIDERS", {})
    if name not in providers:
        raise ProviderError(
            f"No settings.FINDUS_AI_PROVIDERS entry for provider '{name}'"
        )
    return providers[name]


def _build_openai(config: dict, common: dict) -> OpenAIProvider:
    return OpenAIProvider(
        api_key=config["api_key"],
        base_url=config["base_url"],
        embedding_model=config["embedding_model"],
        embedding_model_version=config["embedding_model_version"],
        generation_model=config["generation_model"],
        generation_model_version=config["generation_model_version"],
        vision_model=config.get("vision_model", ""),
        vision_model_version=config.get("vision_model_version", ""),
        **common,
    )


def _build_anthropic(config: dict, common: dict) -> AnthropicProvider:
    return AnthropicProvider(
        api_key=config["api_key"],
        base_url=config["base_url"],
        generation_model=config["generation_model"],
        generation_model_version=config["generation_model_version"],
        max_tokens=config["max_tokens"],
        **common,
    )


def _build_gemini(config: dict, common: dict) -> GeminiProvider:
    return GeminiProvider(
        api_key=config["api_key"],
        base_url=config["base_url"],
        embedding_model=config["embedding_model"],
        embedding_model_version=config["embedding_model_version"],
        generation_model=config["generation_model"],
        generation_model_version=config["generation_model_version"],
        **common,
    )


def _build_ollama(config: dict, common: dict) -> OllamaProvider:
    return OllamaProvider(
        base_url=config["base_url"],
        embedding_model=config["embedding_model"],
        embedding_model_version=config["embedding_model_version"],
        generation_model=config["generation_model"],
        generation_model_version=config["generation_model_version"],
        api_key=config.get("api_key", ""),
        vision_model=config.get("vision_model", ""),
        vision_model_version=config.get("vision_model_version", ""),
        **common,
    )


def _build_fake_embedding(config: dict, common: dict) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(dimensions=settings.FINDUS_EMBEDDING_DIMENSIONS)


def _build_fake_generation(config: dict, common: dict) -> FakeGenerationProvider:
    return FakeGenerationProvider()


def _build_fake_vision(config: dict, common: dict) -> FakeVisionProvider:
    return FakeVisionProvider()


# "fake" is a real, selectable provider (via FINDUS_AI_*_PROVIDER=fake) so
# tests exercise the exact same config-switch path callers use in prod --
# not a special code branch that only exists for tests.
_EMBEDDING_BUILDERS: dict[str, Callable[[dict, dict], EmbeddingProvider]] = {
    "openai": _build_openai,
    "gemini": _build_gemini,
    "ollama": _build_ollama,
    "fake": _build_fake_embedding,
}

_GENERATION_BUILDERS: dict[str, Callable[[dict, dict], GenerationProvider]] = {
    "openai": _build_openai,
    "anthropic": _build_anthropic,
    "gemini": _build_gemini,
    "ollama": _build_ollama,
    "fake": _build_fake_generation,
}

# Vision is deliberately narrower than embed/generate: only providers
# with a real image-input API are registered, so selecting e.g.
# "anthropic" here fails with the same clear config error as selecting
# it for embeddings, rather than deep inside a request.
_VISION_BUILDERS: dict[str, Callable[[dict, dict], VisionProvider]] = {
    "openai": _build_openai,
    "ollama": _build_ollama,
    "fake": _build_fake_vision,
}


def get_embedding_provider(name: Optional[str] = None) -> EmbeddingProvider:
    name = name or settings.FINDUS_AI_EMBEDDING_PROVIDER
    builder = _EMBEDDING_BUILDERS.get(name)
    if builder is None:
        raise ProviderError(
            f"Unknown embedding provider '{name}'. Available: {sorted(_EMBEDDING_BUILDERS)}"
        )
    config = {} if name == "fake" else _provider_config(name)
    return builder(config, _common_kwargs())


def get_generation_provider(name: Optional[str] = None) -> GenerationProvider:
    name = name or settings.FINDUS_AI_GENERATION_PROVIDER
    builder = _GENERATION_BUILDERS.get(name)
    if builder is None:
        raise ProviderError(
            f"Unknown generation provider '{name}'. Available: {sorted(_GENERATION_BUILDERS)}"
        )
    config = {} if name == "fake" else _provider_config(name)
    return builder(config, _common_kwargs())


def get_vision_provider(name: Optional[str] = None) -> VisionProvider:
    name = name or settings.FINDUS_AI_VISION_PROVIDER
    builder = _VISION_BUILDERS.get(name)
    if builder is None:
        raise ProviderError(
            f"Unknown vision provider '{name}'. Available: {sorted(_VISION_BUILDERS)}"
        )
    config = {} if name == "fake" else _provider_config(name)
    return builder(config, _common_kwargs())
