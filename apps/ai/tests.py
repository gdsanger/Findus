"""Unit tests for the AI provider layer (apps.ai.providers).

Every test here runs offline: the registry tests use the built-in
`fake` provider (see providers/fake.py), and the adapter tests patch
`requests.request` so no socket is ever opened, per this issue's
acceptance criterion ("kein echter Netz-Call im Test").
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from apps.ai.providers import (
    GenerationChunk,
    GenerationResult,
    Message,
    ProviderError,
    get_embedding_provider,
    get_generation_provider,
)
from apps.ai.providers.anthropic import AnthropicProvider
from apps.ai.providers.base import Usage, with_retry
from apps.ai.providers.fake import FakeEmbeddingProvider, FakeGenerationProvider
from apps.ai.providers.gemini import GeminiProvider
from apps.ai.providers.ollama import OllamaProvider
from apps.ai.providers.openai import OpenAIProvider


class RegistrySwitchTests(SimpleTestCase):
    """Provider per Config umschaltbar, ohne Aufrufer-Code zu ändern."""

    @override_settings(FINDUS_AI_EMBEDDING_PROVIDER="fake")
    def test_embedding_provider_selected_from_settings(self):
        provider = get_embedding_provider()
        self.assertIsInstance(provider, FakeEmbeddingProvider)

    @override_settings(FINDUS_AI_GENERATION_PROVIDER="fake")
    def test_generation_provider_selected_from_settings(self):
        provider = get_generation_provider()
        self.assertIsInstance(provider, FakeGenerationProvider)

    def test_explicit_name_overrides_settings(self):
        provider = get_embedding_provider("fake")
        self.assertIsInstance(provider, FakeEmbeddingProvider)

    @override_settings(
        FINDUS_AI_PROVIDERS={
            "openai": {
                "api_key": "unused-in-this-test",
                "base_url": "https://api.openai.com/v1",
                "embedding_model": "text-embedding-3-small",
                "embedding_model_version": "1",
                "generation_model": "gpt-4o-mini",
                "generation_model_version": "1",
            },
        }
    )
    def test_same_caller_code_gets_different_classes_per_config(self):
        """One call site (`get_embedding_provider()`), two different
        concrete classes depending only on settings -- no branching in
        the caller.
        """

        def caller():
            return type(get_embedding_provider()).__name__

        with override_settings(FINDUS_AI_EMBEDDING_PROVIDER="fake"):
            self.assertEqual(caller(), "FakeEmbeddingProvider")

        with override_settings(FINDUS_AI_EMBEDDING_PROVIDER="openai"):
            self.assertEqual(caller(), "OpenAIProvider")

    def test_unknown_provider_raises_clear_error(self):
        with self.assertRaises(ProviderError):
            get_embedding_provider("not-a-real-provider")

    def test_anthropic_not_registered_for_embeddings(self):
        # Anthropic has no embeddings API -- must not be selectable here.
        with self.assertRaises(ProviderError):
            get_embedding_provider("anthropic")

    def test_missing_provider_config_raises_clear_error(self):
        with override_settings(FINDUS_AI_PROVIDERS={}):
            with self.assertRaises(ProviderError):
                get_embedding_provider("ollama")


class FakeProviderBehaviourTests(SimpleTestCase):
    def test_embed_returns_vector_model_and_version(self):
        provider = FakeEmbeddingProvider(dimensions=4, model="fake-embed", version="2024-01")
        result = provider.embed(["hello", "a longer piece of text"])
        self.assertEqual(len(result.vectors), 2)
        self.assertTrue(all(len(vector) == 4 for vector in result.vectors))
        self.assertEqual(result.model, "fake-embed")
        self.assertEqual(result.version, "2024-01")

    def test_generate_non_streaming_returns_text(self):
        provider = FakeGenerationProvider()
        result = provider.generate([Message(role="user", content="hi there")], stream=False)
        self.assertIsInstance(result, GenerationResult)
        self.assertIn("hi there", result.text)

    def test_generate_streaming_yields_chunks_ending_in_done(self):
        provider = FakeGenerationProvider(reply="a b c")
        chunks = list(
            provider.generate([Message(role="user", content="irrelevant")], stream=True)
        )
        self.assertTrue(all(isinstance(chunk, GenerationChunk) for chunk in chunks))
        self.assertTrue(chunks[-1].done)
        streamed_text = "".join(chunk.delta for chunk in chunks)
        self.assertEqual(streamed_text.strip(), "a b c")


class RetryBackoffTests(SimpleTestCase):
    @patch("apps.ai.providers.base.time.sleep")
    def test_succeeds_after_transient_failures(self, mock_sleep):
        attempts = {"count": 0}

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = with_retry(flaky, retries=3, backoff_seconds=0.1, provider="test")
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("apps.ai.providers.base.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep):
        def always_fails():
            raise ConnectionError("nope")

        with self.assertRaises(ProviderError):
            with_retry(always_fails, retries=2, backoff_seconds=0.1, provider="test")
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("apps.ai.providers.base.time.sleep")
    def test_provider_error_never_contains_original_exception_text(self, mock_sleep):
        secret = "sk-super-secret-token"

        def fails_with_secret_in_message():
            raise ConnectionError(f"auth failed for header Authorization: Bearer {secret}")

        with self.assertRaises(ProviderError) as ctx:
            with_retry(fails_with_secret_in_message, retries=1, backoff_seconds=0.0, provider="test")
        self.assertNotIn(secret, str(ctx.exception))


class KeyNotLoggedTests(SimpleTestCase):
    def test_openai_repr_masks_api_key(self):
        provider = OpenAIProvider(
            api_key="sk-should-not-appear",
            base_url="https://api.openai.com/v1",
            embedding_model="m",
            embedding_model_version="1",
            generation_model="m",
            generation_model_version="1",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )
        self.assertNotIn("sk-should-not-appear", repr(provider))
        self.assertIn("***", repr(provider))

    def test_anthropic_repr_masks_api_key(self):
        provider = AnthropicProvider(
            api_key="sk-ant-should-not-appear",
            base_url="https://api.anthropic.com",
            generation_model="m",
            generation_model_version="1",
            max_tokens=16,
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )
        self.assertNotIn("sk-ant-should-not-appear", repr(provider))

    def test_gemini_repr_masks_api_key(self):
        provider = GeminiProvider(
            api_key="gemini-should-not-appear",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            embedding_model="m",
            embedding_model_version="1",
            generation_model="m",
            generation_model_version="1",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )
        self.assertNotIn("gemini-should-not-appear", repr(provider))

    @patch("apps.ai.providers.base.logger")
    def test_retry_warning_log_never_includes_exception_message(self, mock_logger):
        secret = "sk-should-not-be-logged"

        def fails_once():
            fails_once.calls += 1
            if fails_once.calls == 1:
                raise ConnectionError(f"leaked {secret}")
            return "ok"

        fails_once.calls = 0
        with patch("apps.ai.providers.base.time.sleep"):
            with_retry(fails_once, retries=1, backoff_seconds=0.0, provider="test")

        for call in mock_logger.warning.call_args_list:
            args = call.args
            rendered = args[0] % args[1:] if args else ""
            self.assertNotIn(secret, rendered)


def _mock_response(json_body=None, lines=None):
    response = Mock()
    response.raise_for_status = Mock()
    if json_body is not None:
        response.json.return_value = json_body
    if lines is not None:
        response.iter_lines.return_value = iter(lines)
    return response


class OpenAIAdapterTests(SimpleTestCase):
    def _provider(self):
        return OpenAIProvider(
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            embedding_model="text-embedding-3-small",
            embedding_model_version="1",
            generation_model="gpt-4o-mini",
            generation_model_version="1",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("apps.ai.providers.http.requests.request")
    def test_embed_parses_vectors_in_index_order(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={
                "data": [
                    {"index": 1, "embedding": [0.2, 0.2]},
                    {"index": 0, "embedding": [0.1, 0.1]},
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 0},
            }
        )
        result = self._provider().embed(["first", "second"])
        self.assertEqual(result.vectors, [[0.1, 0.1], [0.2, 0.2]])
        self.assertEqual(result.model, "text-embedding-3-small")
        self.assertEqual(result.version, "1")
        called_headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(called_headers["Authorization"], "Bearer sk-test")

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_non_streaming(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={
                "choices": [{"message": {"content": "hello there"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )
        result = self._provider().generate([Message(role="user", content="hi")], stream=False)
        self.assertEqual(result.text, "hello there")

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_streaming_yields_deltas(self, mock_request):
        sse_lines = [
            'data: {"choices": [{"delta": {"content": "hel"}}]}',
            'data: {"choices": [{"delta": {"content": "lo"}}]}',
            "data: [DONE]",
        ]
        mock_request.return_value = _mock_response(lines=sse_lines)
        chunks = list(
            self._provider().generate([Message(role="user", content="hi")], stream=True)
        )
        text = "".join(c.delta for c in chunks)
        self.assertEqual(text, "hello")
        self.assertTrue(chunks[-1].done)

    @patch("apps.ai.providers.http.requests.request")
    def test_usage_hook_called_on_embed(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={
                "data": [{"index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 0},
            }
        )
        seen = []
        provider = self._provider()
        provider._usage_hook = lambda capability, name, model, usage: seen.append(
            (capability, name, model, usage)
        )
        provider.embed(["x"])
        self.assertEqual(len(seen), 1)
        capability, name, model, usage = seen[0]
        self.assertEqual(capability, "embed")
        self.assertEqual(name, "openai")
        self.assertEqual(usage, Usage(prompt_tokens=7, completion_tokens=0))


class AnthropicAdapterTests(SimpleTestCase):
    def _provider(self):
        return AnthropicProvider(
            api_key="sk-ant-test",
            base_url="https://api.anthropic.com",
            generation_model="claude-sonnet-5",
            generation_model_version="1",
            max_tokens=64,
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_splits_system_message(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        self._provider().generate(
            [
                Message(role="system", content="be terse"),
                Message(role="user", content="hello"),
            ],
            stream=False,
        )
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["system"], "be terse")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hello"}])
        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], "sk-ant-test")

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_streaming(self, mock_request):
        lines = [
            'event: content_block_delta',
            'data: {"type": "content_block_delta", "delta": {"text": "hi"}}',
            'event: message_stop',
            'data: {"type": "message_stop"}',
        ]
        mock_request.return_value = _mock_response(lines=lines)
        chunks = list(
            self._provider().generate([Message(role="user", content="hi")], stream=True)
        )
        text = "".join(c.delta for c in chunks)
        self.assertEqual(text, "hi")
        self.assertTrue(chunks[-1].done)


class GeminiAdapterTests(SimpleTestCase):
    def _provider(self):
        return GeminiProvider(
            api_key="gemini-test",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            embedding_model="text-embedding-004",
            embedding_model_version="1",
            generation_model="gemini-2.5-flash",
            generation_model_version="1",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("apps.ai.providers.http.requests.request")
    def test_embed_sends_key_as_header_not_query(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={"embeddings": [{"values": [0.1, 0.2]}]}
        )
        result = self._provider().embed(["hello"])
        self.assertEqual(result.vectors, [[0.1, 0.2]])
        url = mock_request.call_args.args[1]
        self.assertNotIn("gemini-test", url)
        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-goog-api-key"], "gemini-test")

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_maps_assistant_role_to_model(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )
        self._provider().generate(
            [
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
            ],
            stream=False,
        )
        payload = mock_request.call_args.kwargs["json"]
        roles = [c["role"] for c in payload["contents"]]
        self.assertEqual(roles, ["user", "model"])


class OllamaAdapterTests(SimpleTestCase):
    def _provider(self):
        return OllamaProvider(
            base_url="http://localhost:11434",
            embedding_model="nomic-embed-text",
            embedding_model_version="1",
            generation_model="llama3.1",
            generation_model_version="1",
            timeout=5,
            max_retries=0,
            retry_backoff_seconds=0,
        )

    @patch("apps.ai.providers.http.requests.request")
    def test_embed_batch(self, mock_request):
        mock_request.return_value = _mock_response(
            json_body={"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
        )
        result = self._provider().embed(["a", "b"])
        self.assertEqual(result.vectors, [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(mock_request.call_args.kwargs["headers"], {})

    @patch("apps.ai.providers.http.requests.request")
    def test_generate_streaming_ndjson(self, mock_request):
        lines = [
            json.dumps({"message": {"content": "hel"}, "done": False}),
            json.dumps({"message": {"content": "lo"}, "done": False}),
            json.dumps({"message": {"content": ""}, "done": True}),
        ]
        mock_request.return_value = _mock_response(lines=lines)
        chunks = list(
            self._provider().generate([Message(role="user", content="hi")], stream=True)
        )
        text = "".join(c.delta for c in chunks)
        self.assertEqual(text, "hello")
        self.assertTrue(chunks[-1].done)
