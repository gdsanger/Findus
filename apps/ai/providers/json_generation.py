"""Robust JSON extraction on top of `GenerationProvider.generate()` (bug fix
to #1020, tracked as #1028; extended for truncation in #1096).

LLMs occasionally return near-valid JSON: a missing comma, a trailing
comma, or the payload wrapped in prose/code fences. `generate_json()` is
the one place that deals with that, so callers get a parsed `dict` back,
or a clear `JSONGenerationError` carrying the raw model output for
logging.

The one thing it must *not* do is repair a reply that was cut off by the
output-token ceiling. `repair_json` happily closes a half-written object
-- which turns "the model was still writing the recommendations list"
into "the recommendations key is absent", i.e. a silent, plausible-looking
empty result (#1096). So a cut-off reply is a *failure*, detected from the
provider's finish reason (`GenerationResult.truncated`) and, for providers
that report none, from the payload's own brace/quote balance. The retry
then goes out with a bigger budget and an instruction to be brief; only a
reply that is complete *and* carries the keys the caller asked for is
handed back.

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
from typing import Iterable, Optional, Sequence

from json_repair import repair_json

from .base import GenerationProvider, GenerationResult, Message

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Why a reply couldn't be used as it stands -- each one gets its own retry
# instruction, because "you were cut off" and "that wasn't JSON" call for
# different corrections from the model.
_TRUNCATED = "truncated"
_UNPARSEABLE = "unparseable"
_MISSING_KEYS = "missing_keys"

_RETRY_INSTRUCTION = (
    "Deine letzte Antwort war kein gueltiges JSON. Antworte jetzt "
    "ausschliesslich mit einem einzigen, syntaktisch validen JSON-Objekt -- "
    "kein Markdown, keine Code-Fences, kein Text davor oder danach."
)

_TRUNCATION_RETRY_INSTRUCTION = (
    "Deine letzte Antwort wurde abgeschnitten, weil sie zu lang war. "
    "Antworte jetzt erneut mit dem vollstaendigen JSON-Objekt und halte "
    "dich dabei deutlich kuerzer: knappe Formulierungen, keine "
    "Wiederholungen, keine Ausschmueckungen. Wichtiger als ausfuehrliche "
    "Fliesstexte ist, dass das JSON-Objekt vollstaendig geschlossen ist "
    "und alle geforderten Schluessel enthaelt."
)

# The retried call gets this multiple of the original budget. A cut-off
# reply says the ceiling was too low, and asking the model to be terser
# alone is a request, not a guarantee -- the budget is the part we control.
_TRUNCATION_RETRY_FACTOR = 2

# Kept short on purpose: this only ever ends up in a log line, not in
# `Document.metadata`, so it just needs to be enough to diagnose the case.
_RAW_OUTPUT_LOG_CHARS = 2000


class JSONGenerationError(Exception):
    """Raised when a reply is still unusable after the repair pass and one
    retry. `raw_text` is the (already-truncated) last model output, so every
    caller logs the same excerpt instead of re-deriving it.

    `truncated` distinguishes "the model hit the output-token ceiling twice"
    from "the model can't produce JSON" -- the first is an operator problem
    (raise the budget), the second a prompt problem, and the message says so.
    """

    def __init__(self, message: str, *, raw_text: str, truncated: bool = False):
        super().__init__(message)
        self.raw_text = raw_text
        self.truncated = truncated


@dataclass(frozen=True)
class JSONGenerationResult:
    data: dict
    model: str
    version: str
    attempts: int


def _extract_json_object(text: str) -> str:
    match = _JSON_OBJECT_RE.search(text)
    return match.group(0) if match else text


def _looks_incomplete(payload: str) -> bool:
    """True when `payload` stops mid-object: a brace/bracket is still open
    or a string was never closed.

    This is the fallback for providers that report no finish reason (and
    for OpenAI-compatible endpoints that omit it). Structural, not
    heuristic: valid JSON is always balanced, so this can only fire on
    something that genuinely isn't a finished object.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in payload:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return in_string or depth > 0


def _evaluate(
    result: GenerationResult, required_keys: Sequence[str]
) -> tuple[Optional[dict], Optional[str]]:
    """`(parsed, problem)` for one reply -- `problem` is `None` when the
    object is complete and usable.
    """
    if result.truncated:
        # The provider itself said it hit the ceiling. Whatever arrived is
        # a fragment, however parseable it looks.
        return None, _TRUNCATED

    payload = _extract_json_object(result.text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        if _looks_incomplete(payload):
            return None, _TRUNCATED
        try:
            parsed = repair_json(payload, return_objects=True)
        except Exception:
            return None, _UNPARSEABLE

    if not isinstance(parsed, dict):
        return None, _UNPARSEABLE
    if any(key not in parsed for key in required_keys):
        return parsed, _MISSING_KEYS
    return parsed, None


def _missing_keys(parsed: Optional[dict], required_keys: Sequence[str]) -> list[str]:
    parsed = parsed or {}
    return [key for key in required_keys if key not in parsed]


def _retry_instruction(problem: str, parsed: Optional[dict], required_keys: Sequence[str]) -> str:
    if problem == _TRUNCATED:
        return _TRUNCATION_RETRY_INSTRUCTION
    if problem == _MISSING_KEYS:
        missing = ", ".join(f'"{key}"' for key in _missing_keys(parsed, required_keys))
        return (
            "Deiner letzten Antwort fehlten Pflicht-Schluessel: "
            f"{missing}. Antworte jetzt erneut mit einem vollstaendigen "
            "JSON-Objekt, das alle geforderten Schluessel enthaelt -- "
            "notfalls mit leeren Werten, aber nicht weggelassen."
        )
    return _RETRY_INSTRUCTION


def _budget_label(max_tokens: Optional[int]) -> str:
    """Anzeigewert fuer Log und Fehlertext: ohne eigenes Budget gilt der
    Provider-Default -- ein blankes "None" waere dort irrefuehrend.
    """
    return str(max_tokens) if max_tokens else "Provider-Default"


def _generate(provider: GenerationProvider, messages: list[Message], max_tokens: Optional[int]):
    # Only pass the kwarg when there is a budget to pass: a provider
    # implementation that predates the parameter (or a caller's own test
    # double) keeps working unchanged.
    if max_tokens is None:
        return provider.generate(messages)
    return provider.generate(messages, max_tokens=max_tokens)


def generate_json(
    provider: GenerationProvider,
    messages: Iterable[Message],
    *,
    max_tokens: Optional[int] = None,
    required_keys: Sequence[str] = (),
) -> JSONGenerationResult:
    """Call `provider.generate()` and parse the reply as a JSON object.

    Stage 1 (defensive parsing) strips prose/code-fences around the
    payload; stage 2 (repair) fixes common syntax slips such as a missing
    or trailing comma; stage 3 (retry) re-asks the model once, with an
    instruction matched to what went wrong. The happy path costs exactly
    one `generate()` call, same as before this wrapper existed.

    `max_tokens` is the output budget for the first call; a reply cut off
    at that ceiling is retried with `_TRUNCATION_RETRY_FACTOR` times as
    much, never quietly repaired. `required_keys` are the keys the caller
    will read -- a reply missing one is retried too, rather than handed on
    as an "empty" result.
    """
    messages = list(messages)
    required_keys = tuple(required_keys)

    result = _generate(provider, messages, max_tokens)
    parsed, problem = _evaluate(result, required_keys)
    if problem is None:
        return JSONGenerationResult(
            data=parsed, model=result.model, version=result.version, attempts=1
        )

    retry_max_tokens = max_tokens
    if problem == _TRUNCATED:
        if max_tokens:
            retry_max_tokens = max_tokens * _TRUNCATION_RETRY_FACTOR
        logger.warning(
            "generate_json: Antwort war abgeschnitten (Output-Limit), Retry mit "
            "Budget %s statt %s",
            _budget_label(retry_max_tokens),
            _budget_label(max_tokens),
        )
    else:
        logger.warning("generate_json: Antwort war unbrauchbar (%s), versuche einen Retry", problem)

    retry_messages = messages + [
        Message(role="user", content=_retry_instruction(problem, parsed, required_keys))
    ]
    retry_result = _generate(provider, retry_messages, retry_max_tokens)
    retry_parsed, retry_problem = _evaluate(retry_result, required_keys)
    if retry_problem is None:
        return JSONGenerationResult(
            data=retry_parsed,
            model=retry_result.model,
            version=retry_result.version,
            attempts=2,
        )

    if retry_problem == _TRUNCATED:
        message = (
            "KI-Antwort wurde wegen des Output-Token-Limits abgeschnitten und war "
            f"auch im Retry mit groesserem Budget ({_budget_label(retry_max_tokens)}) "
            "unvollstaendig"
        )
    elif retry_problem == _MISSING_KEYS:
        missing = ", ".join(_missing_keys(retry_parsed, required_keys))
        message = f"KI-Antwort fehlen auch nach einem Retry Pflicht-Schluessel: {missing}"
    else:
        message = "KI-Antwort ist auch nach Repair-Pass und Retry kein gueltiges JSON-Objekt"

    raise JSONGenerationError(
        message,
        raw_text=retry_result.text[:_RAW_OUTPUT_LOG_CHARS],
        truncated=retry_problem == _TRUNCATED,
    )
