"""Anthropic Messages API wrapper (§4) — retry, token accounting, structured JSON.

The synthesis call is the ONE place judgement lives (§2 fix #10): blocker-vs-escalation
classification, verbatim fidelity, phrasing. Scope is already handled in code before we
get here, so this wrapper is deliberately thin.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from designops.core.config import Settings, get_settings

# Claude 5 / Opus 4.8 pricing (USD per token). Update from the pricing skill before billing.
_PRICE = {
    "claude-opus-4-8": (15 / 1_000_000, 75 / 1_000_000),
    "claude-sonnet-5": (3 / 1_000_000, 15 / 1_000_000),
    "claude-haiku-4-5-20251001": (1 / 1_000_000, 5 / 1_000_000),
}


@dataclass(slots=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    pin, pout = _PRICE.get(model, (0.0, 0.0))
    return round(in_tok * pin + out_tok * pout, 4)


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def synthesize(
        self,
        *,
        system: str,
        user_content: str,
        max_tokens: int = 8000,
        retries: int = 3,
        model: str | None = None,
    ) -> LLMResult:
        if not self.s.anthropic_configured:
            raise RuntimeError("ANTHROPIC_API_KEY not set (env only).")
        # Imported lazily so the module (and the fast test suite) load without the SDK.
        import anthropic

        client = anthropic.Anthropic(api_key=self.s.anthropic_api_key)
        model_id = model or self.s.digest_model
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                msg = client.messages.create(
                    model=model_id,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_content}],
                )
                text = "".join(b.text for b in msg.content if b.type == "text")
                if not text.strip():  # empty completion — retry rather than fail the run
                    raise RuntimeError(f"empty completion (stop_reason={msg.stop_reason})")
                return LLMResult(
                    text=text,
                    input_tokens=msg.usage.input_tokens,
                    output_tokens=msg.usage.output_tokens,
                    cost_usd=_cost(model_id, msg.usage.input_tokens, msg.usage.output_tokens),
                    model=model_id,
                )
            except Exception as e:  # noqa: BLE001 — retry transient API errors + empties
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM synthesis failed after {retries} attempts: {last_err}")


def parse_digest_json(text: str) -> dict:
    """The model is asked for structured JSON (§6.3). Tolerate ```json fences, a prose
    preamble, or trailing text after the object. `raw_decode` reads the first complete
    JSON object from the first `{` and ignores anything after it."""
    t = (text or "").strip()
    if not t:
        raise ValueError("empty LLM response — nothing to parse")
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else ""
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    if not t:
        raise ValueError("empty LLM response after stripping markdown fences")
    start = t.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in LLM response: {t[:120]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[start:])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON from LLM ({e}): {t[start:start + 160]!r}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"LLM JSON was {type(obj).__name__}, expected object")
    return obj
