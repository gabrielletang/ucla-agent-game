"""Shared model-calling helper for both games.

`ask()` sends one decision to the model with a strict JSON schema, validates
the answer with a game-supplied `build()` function, retries once with the
validation error, and respects a time budget. If no valid answer arrives in
time it returns None and the caller plays its code-computed fallback, so the
agent never stalls or sends an illegal move.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from typing import Any, Callable

from examples.llm.base import InvalidChoice
from examples.llm.providers import ProviderError, provider_from_env

MIN_ATTEMPT_SECONDS = 2.5


def object_schema(properties: dict) -> dict:
    """Strict JSON schema: every property required, nothing extra."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


class Brain:
    """Thin wrapper around the provider with timing, retry and logging."""

    def __init__(self, provider=None, *, log: Callable[[str], None] = print, clock=time.monotonic):
        self.log = log
        if provider is None:
            try:
                provider = provider_from_env()
            except RuntimeError as exc:
                log("!" * 70)
                log(f"WARNING: {exc}")
                log("The agent will only play code fallback moves. That does NOT meet the course")
                log("requirement that the LLM drives decisions. Fix .env before any real game.")
                log("!" * 70)
                provider = _NoModel()
        if os.environ.get("OPENAI_API_KEY", "").startswith("sk-your"):
            log("WARNING: OPENAI_API_KEY in .env is still the placeholder 'sk-your_key_here'. Paste your real key.")
        self.provider = provider
        self.clock = clock
        self.calls = 0
        self.failures = 0
        try:
            params = inspect.signature(self.provider.complete_structured).parameters.values()
            self._takes_timeout = any(p.name == "timeout" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
        except (TypeError, ValueError, AttributeError):
            self._takes_timeout = False
        self.verbose = os.environ.get("AGENT_VERBOSE", "1") != "0"

    def ask(
        self,
        *,
        kind: str,
        system: str,
        payload: dict,
        schema: dict,
        build: Callable[[dict], Any],
        budget: float | None = None,
        attempts: int = 2,
    ) -> tuple[Any, dict | None]:
        """Return (built_value, raw_answer) or (None, None) on failure/timeout."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        deadline = None if budget is None else self.clock() + budget
        for attempt in range(1, attempts + 1):
            kwargs: dict = {}
            if deadline is not None:
                left = deadline - self.clock()
                if left < MIN_ATTEMPT_SECONDS:
                    self.log(f"[brain] {kind}: out of time ({left:.1f}s left)")
                    break
                if self._takes_timeout:
                    kwargs["timeout"] = left
            self.calls += 1
            started = self.clock()
            try:
                answer = self.provider.complete_structured(messages, kind, schema, **kwargs)
                value = build(answer)
                if self.verbose:
                    self.log(f"[brain] {kind}: answered in {self.clock() - started:.1f}s")
                return value, answer
            except (ProviderError, InvalidChoice, KeyError, TypeError, ValueError) as exc:
                self.failures += 1
                self.log(f"[brain] {kind}: invalid answer (attempt {attempt}/{attempts}): {exc}")
                messages.append({
                    "role": "user",
                    "content": f"That answer was invalid: {exc}. Answer again using only the given options.",
                })
        return None, None


class _NoModel:
    model = "none"

    def complete_structured(self, messages, schema_name, schema, **kwargs):
        raise ProviderError("no OPENAI_API_KEY configured")


def clip_words(text: str, max_words: int, max_chars: int) -> str:
    return " ".join(str(text or "").split()[:max_words])[:max_chars].strip()
