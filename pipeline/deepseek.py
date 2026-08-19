#!/usr/bin/env python3
"""
DeepSeek's own API, rather than the same models reached through OpenRouter.

Same wire format — OpenAI chat completions — so this inherits the transport
from `openrouter.Client` and changes only what belongs to the provider: where
the request goes, which headers it carries, how reasoning is selected, and how
a structured stage asks for JSON.

Four differences, each of which would be a silent failure if it were left as
OpenRouter's:

  * **The endpoint.** api.deepseek.com, not openrouter.ai.
  * **The headers.** `HTTP-Referer`, `X-Title` and `X-OpenRouter-Metadata` mean
    nothing here.
  * **Reasoning.** OpenRouter takes a `reasoning` object in the body; DeepSeek
    chooses by model id — `deepseek-reasoner` reasons, `deepseek-chat` does not.
    Sending the key is at best ignored and at worst a 400, and either way the
    effort setting is a lie: the way to reason here is to name that model.
  * **Structured output.** OpenRouter takes a full `json_schema`; DeepSeek's
    JSON mode is `{"type": "json_object"}`, which constrains the output to
    valid JSON and nothing further. So the schema goes into the prompt as well,
    because a model that cannot be handed the shape has to be told it. The
    local validation in cli.py was already the real gate — the wire schema was
    belt and braces — so the braces move rather than disappear.

Every default here is overridable by an environment variable, and deliberately
so: this was written without access to DeepSeek's documentation, so the
endpoint, the model and the JSON mode are best knowledge rather than verified
fact. If one of them is wrong it should be a variable someone sets, not a
release someone waits for.
"""

from __future__ import annotations

import json
import os

import openrouter

# Overridable because they are not verified. `/chat/completions` under the bare
# host is DeepSeek's documented path; their OpenAI-SDK compatibility also
# accepts a `/v1` prefix, so either spelling can be set here.
API = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")

# `deepseek-chat` is the non-reasoning model, `deepseek-reasoner` the reasoning
# one. The default is the first because extraction wants its whole output
# budget spent on the answer: the run that prompted all this spent 2,545 of
# 16,000 tokens thinking and was cut off mid-list.
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# The JSON mode to ask for. Named rather than hard-coded so that a provider
# that later accepts a full schema can be switched to it without a deploy.
JSON_MODE = os.environ.get("DEEPSEEK_JSON_MODE", "json_object")

# USD per million tokens — planning placeholders, exactly as OpenRouter's are.
# The real numbers are on the DeepSeek dashboard, and they publish a discount
# for a cache hit that this deliberately does not assume.
PRICING = {
    "deepseek-chat":     {"in": 0.27, "out": 1.10},
    "deepseek-reasoner": {"in": 0.55, "out": 2.19},
    "_default":          {"in": 0.50, "out": 1.50},
}

SCHEMA_INSTRUCTION = (
    "\n\nRespond with a single JSON object and nothing else — no prose before "
    "or after it, no markdown fence. It must validate against this JSON "
    "Schema:\n\n{schema}\n")


class Client(openrouter.Client):
    """The chat-completions transport, pointed at DeepSeek."""

    PROVIDER = "deepseek"
    ENDPOINT = API
    KEY_HELP = ("No DeepSeek key. Add one on the Settings tab of the studio, or:\n"
                "  export DEEPSEEK_API_KEY=sk-...\n"
                "Get one at https://platform.deepseek.com/api_keys")

    ESTIMATE_LABEL = "DeepSeek"
    ESTIMATE_WHERE = "platform.deepseek.com"
    # DeepSeek caches context automatically and bills a hit at a lower rate, but
    # the estimate does not count on it: a first run over a fresh corpus is all
    # misses, and an estimate that assumed hits would under-state the one number
    # somebody approves a spend against.
    CACHING_NOTE = ("re-sent on EACH request (their cache may discount a repeat, "
                    "not counted here)")

    @classmethod
    def default_model(cls):
        return DEFAULT_MODEL

    @classmethod
    def endpoint(cls):
        return API

    @classmethod
    def pricing(cls):
        return PRICING

    def _extra_headers(self):
        return {}

    def _apply_reasoning(self, body, reasoning):
        """Nothing to apply. Reasoning here is the model id, not a parameter."""

    def _apply_schema(self, body, messages, schema):
        body["response_format"] = {"type": JSON_MODE}
        # json_object constrains the output to valid JSON and says nothing about
        # its shape, so the shape has to be in the prompt. Appended to the last
        # user turn rather than the system block, because the system block is
        # the corpus and is what a cache would key on.
        for message in reversed(messages):
            if message.get("role") == "user":
                message["content"] = (message.get("content", "")
                                      + SCHEMA_INSTRUCTION.format(
                                          schema=json.dumps(schema, indent=2)))
                break
