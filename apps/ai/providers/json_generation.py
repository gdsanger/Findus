"""Robust JSON extraction on top of `GenerationProvider.generate()` (bug fix
to #1020, tracked as #1028).

LLMs occasionally return near-valid JSON: a missing comma, a trailing
comma, or the payload wrapped in prose/code fences. `generate_json()` is
the one place that deals with that, so callers -- currently only
`apps.documents.analysis` -- get a parsed `dict` back, or a clear
`JSONGenerationError` carrying the raw model output for logging.

Deliberately built as a wrapper around the existing `generate()` call
rather than a new provider-protocol method (e.g. OpenAI's
`response_format=json_schema`): that would mean giving every adapter
(OpenAI/Anthropic/Gemini/Ollama) a second, provider-specific code path
to keep in sync, for a payoff -- near-certain valid JSON on the happy
path -- that defensive parsing + one repair pass + one retry already
covers for every provider today. Revisit if production logs show a
persistently high retry rate.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from json_repair import repair_json

from .base import GenerationProvider, Message

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_RETRY_INSTRUCTION = (
    "Deine letzte Antwort war kein gueltiges JSON. Antworte jetzt "
    "ausschliesslich mit einem einzigen, syntaktisch validen JSON-Objekt -- "
    "kein Markdown, keine Code-Fences, kein Text davor oder danach."
)

# Kept short on purpose: this only ever ends up in a log line, not in
# `Document.metadata`, so it just needs to be enough to diagnose the case.
_RAW_OUTPUT_LOG_CHARS = 2000


class JSONGenerationError(Exception):
    """Raised when a reply is still not parseable as JSON after the repair
    pass and one retry. `raw_text` is the (already-truncated) last model
    output, so every caller logs the same excerpt instead of re-deriving it.
    """

    def __init__(self, message: str, *, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


@dataclass(frozen=True)
class JSONGenerationResult:
    data: dict
    model: str
    version: str
    attempts: int


def _extract_json_object(text: str) -> str:
    match = _JSON_OBJECT_RE.search(text)
    return match.group(0) if match else text


def _try_parse(text: str) -> Optional[dict]:
    payload = _extract_json_object(text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        try:
            parsed = repair_json(payload, return_objects=True)
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def generate_json(provider: GenerationProvider, messages: Iterable[Message]) -> JSONGenerationResult:
    """Call `provider.generate()` and parse the reply as a JSON object.

    Stage 1 (defensive parsing) strips prose/code-fences around the
    payload; stage 2 (repair) fixes common syntax slips such as a missing
    or trailing comma; stage 3 (retry) re-asks the model once, with an
    explicit "valid JSON only" instruction, if stages 1-2 didn't produce
    an object. The happy path costs exactly one `generate()` call, same as
    before this wrapper existed.
    """
    messages = list(messages)

    result = provider.generate(messages)
    parsed = _try_parse(result.text)
    if parsed is not None:
        return JSONGenerationResult(data=parsed, model=result.model, version=result.version, attempts=1)

    logger.warning("generate_json: Antwort war kein gueltiges JSON, versuche einen Retry")
    retry_messages = messages + [Message(role="user", content=_RETRY_INSTRUCTION)]
    retry_result = provider.generate(retry_messages)
    parsed = _try_parse(retry_result.text)
    if parsed is not None:
        return JSONGenerationResult(
            data=parsed, model=retry_result.model, version=retry_result.version, attempts=2
        )

    raise JSONGenerationError(
        "KI-Antwort ist auch nach Repair-Pass und Retry kein gueltiges JSON-Objekt",
        raw_text=retry_result.text[:_RAW_OUTPUT_LOG_CHARS],
    )
