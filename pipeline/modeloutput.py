#!/usr/bin/env python3
"""One place that decides what an empty model response actually means.

Reasoning models (deepseek-v4 in particular) can spend their entire `max_tokens`
allowance inside a reasoning block and stop before emitting a single character of
content. The provider reports that honestly — empty `content`, `finish_reason`
of 'length'/'max_tokens' — but a caller that only looks at `content` sees `""`,
hands it to a JSON parser, and reports

    Expecting value: line 1 column 1 (char 0)

…which reads like a malformed-JSON problem and sends the reply into a JSON repair
path. There is nothing to repair: the model never wrote anything. The fix is to
re-ask with a larger budget, which is a different action entirely.

That distinction was first made in presets.py (commit 40ae8da) for strategy
synthesis. It lived only there, so the same bug survived independently in the
OpenRouter and Anthropic clients used by the ingest stages. This module holds the
rule once; every request path calls it.

Standard library only.
"""

from __future__ import annotations

# Provider spellings for "I stopped because I hit the output cap".
LENGTH_FINISHES = ("length", "max_tokens")

# What a budget-exhaustion retry multiplies the original allowance by. Enough
# room for the model to finish reasoning AND write the answer.
RETRY_MULTIPLIER = 3


class OutputBudgetExhausted(Exception):
    """Empty content plus a length finish: the budget ran out before any output.

    Carries the budget that failed so a caller can retry with a larger one.
    """

    def __init__(self, message, max_tokens=None, finish_reason=None):
        super().__init__(message)
        self.max_tokens = max_tokens
        self.finish_reason = finish_reason


def is_budget_exhaustion(text, finish_reason):
    """True when the model returned nothing because it ran out of output tokens."""
    return not (text or "").strip() and finish_reason in LENGTH_FINISHES


def budget_message(max_tokens, finish_reason, label=None):
    where = f"{label}: " if label else ""
    return (
        f"{where}the model hit its output token budget ({max_tokens}) during "
        f"reasoning and stopped before writing any content (finish_reason="
        f"{finish_reason!r}). This is not malformed JSON — there is no JSON to "
        f"repair. Retrying with a larger budget usually fixes it.")


def empty_reason(finish_reason):
    """Describe an empty reply that is NOT budget exhaustion.

    Keeps case D honest: report the provider's actual reason rather than letting
    a JSON parser invent a syntax error out of an empty string.
    """
    if finish_reason in ("content_filter", "error"):
        return (f"the model returned empty content and stopped with "
                f"finish_reason={finish_reason!r} — a provider-side refusal or "
                f"error, not a JSON problem")
    return (f"the model returned empty content with "
            f"finish_reason={finish_reason!r} — no JSON was produced, so this is "
            f"a provider/model failure rather than a JSON syntax error")


def check(text, finish_reason, max_tokens, label=None):
    """Raise on budget exhaustion, otherwise return the text unchanged."""
    if is_budget_exhaustion(text, finish_reason):
        raise OutputBudgetExhausted(
            budget_message(max_tokens, finish_reason, label),
            max_tokens=max_tokens, finish_reason=finish_reason)
    return text
