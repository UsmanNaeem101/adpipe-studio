#!/usr/bin/env python3
"""
OpenRouter backend — same interface as llm.Client, different economics.

Why this is a separate class rather than a base_url swap: the Anthropic path in
llm.py leans on two Anthropic-specific features that OpenRouter's OpenAI-compatible
endpoint does not expose —

  prompt caching   `cache_control` on a system block, so 20 extractions over one
                   540KB corpus pay for that corpus roughly once. Here the corpus
                   is re-sent in full on every request. Some OpenRouter models do
                   their own implicit caching; none of it is controllable from the
                   request, so this class does not pretend to manage it.
  batch API        50% off for async fan-out. No equivalent, so `batch()` runs the
                   jobs concurrently with threads to recover the wall-clock time —
                   but not the discount.

The upshot: on a cheap model the total is usually still far below the cached
Anthropic path, but the *shape* of the cost is different — it scales with
(jobs x corpus) instead of (corpus + jobs). Cheap models make that trade fine;
expensive ones do not. The estimate reflects it honestly.

Standard library only.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

# USD per million tokens. OpenRouter prices move and vary per model, so these are
# planning placeholders only — the real number is on your OpenRouter dashboard.
PRICING = {
    "deepseek/deepseek-v4-flash": {"in": 0.10, "out": 0.30},
    "deepseek/deepseek-chat":     {"in": 0.14, "out": 0.28},
    "_default":                   {"in": 0.50, "out": 1.50},
}


class Estimate:
    def __init__(self, jobs, corpus_tokens, prompt_tokens, out_tokens, model):
        self.jobs, self.model = jobs, model
        self.corpus_tokens = corpus_tokens
        self.prompt_tokens = prompt_tokens
        self.out_tokens = out_tokens

    @property
    def usd(self):
        p = PRICING.get(self.model, PRICING["_default"])
        # No caching: every job re-sends the whole corpus.
        return ((self.corpus_tokens * self.jobs + self.prompt_tokens) * p["in"]
                + self.out_tokens * p["out"]) / 1e6

    def explain(self):
        return (
            f"  provider         OpenRouter\n"
            f"  model            {self.model}\n"
            f"  requests         {self.jobs}  (no batch discount available)\n"
            f"  corpus           {self.corpus_tokens:,} tokens, re-sent on EACH "
            f"request (no prompt caching)\n"
            f"  instructions     {self.prompt_tokens:,} tokens\n"
            f"  est. output      {self.out_tokens:,} tokens\n"
            f"  ESTIMATE         ${self.usd:,.2f}   (list prices move — verify on "
            f"openrouter.ai)")


class Client:
    """Drop-in for llm.Client. Same method names so cli.py doesn't branch."""

    def __init__(self, model=DEFAULT_MODEL, effort="high", verbose=True, api_key=None):
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.verbose = verbose
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.key:
            sys.exit(
                "No OpenRouter key. Add one on the Settings tab of the studio, or:\n"
                "  export OPENROUTER_API_KEY=sk-or-...\n"
                "Get one at https://openrouter.ai/keys"
            )
        self.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}

    # ------------------------------------------------------------- internals

    def _post(self, messages, max_tokens, schema=None, retries=3):
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if schema:
            # Require a route that supports structured output. Without
            # require_parameters OpenRouter may select a provider that silently
            # drops response_format, leaving the caller with prose instead of the
            # JSON contract it asked for.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema}}
            body["provider"] = {"require_parameters": True}
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json",
                     "HTTP-Referer": "https://localhost/adpipe",
                     "X-Title": "adpipe"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    payload = json.load(r)
                u = payload.get("usage") or {}
                self.spent["in"] += u.get("prompt_tokens", 0)
                self.spent["out"] += u.get("completion_tokens", 0)
                return payload["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:400]
                unsupported_schema = (
                    schema and e.code in (400, 404, 422)
                    and any(word in detail.lower() for word in
                            ("response_format", "json_schema", "structured",
                             "requested parameters", "unsupported parameter")))
                if unsupported_schema:
                    # Some model routes have no endpoint that can enforce
                    # response_format. Still get the model's answer: put the
                    # schema in the prompt, then let cli.py validate and, if
                    # necessary, run its audited repair pass.
                    fallback = [dict(m) for m in messages]
                    fallback[-1] = dict(fallback[-1])
                    fallback[-1]["content"] = (
                        fallback[-1].get("content", "")
                        + "\n\nReturn JSON only, matching this schema exactly:\n"
                        + json.dumps(schema, separators=(",", ":")))
                    if self.verbose:
                        print("  ! this OpenRouter route cannot enforce JSON schema; "
                              "retrying with prompt-level schema and local validation")
                    return self._post(fallback, max_tokens, schema=None, retries=retries)
                if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                    time.sleep(2 ** attempt * 5); continue
                if e.code == 401:
                    sys.exit("  401 — the OpenRouter key was rejected.")
                if e.code == 402:
                    sys.exit("  402 — OpenRouter account has insufficient credit.")
                if e.code == 404:
                    sys.exit(f"  404 — model {self.model!r} not found on OpenRouter. "
                             f"Check the exact id at https://openrouter.ai/models")
                sys.exit(f"  HTTP {e.code}: {detail}")
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(5); continue
                sys.exit(f"  Network error: {e.reason}")
        sys.exit("  Gave up after retries.")

    @staticmethod
    def _tokens(text):
        """No count_tokens endpoint here — approximate at ~4 chars/token. Good
        enough for an estimate, and labelled as approximate wherever shown."""
        return max(len(text) // 4, 1)

    # -------------------------------------------------------------- interface

    def actual_usd(self):
        p = PRICING.get(self.model, PRICING["_default"])
        return (self.spent["in"] * p["in"] + self.spent["out"] * p["out"]) / 1e6

    def estimate(self, corpus, preamble, jobs, batched=False):
        return Estimate(
            jobs=len(jobs),
            corpus_tokens=self._tokens(corpus) + self._tokens(preamble),
            prompt_tokens=sum(self._tokens(j.prompt) for j in jobs),
            out_tokens=sum(int(j.max_tokens * 0.55) for j in jobs),
            model=self.model)

    def prewarm(self, corpus, preamble):
        """No-op: there is no controllable cache to warm here."""
        if self.verbose:
            print("  (no prompt cache on OpenRouter — nothing to warm)")

    def one(self, corpus, preamble, prompt, max_tokens=16000, schema=None):
        msgs = [{"role": "system", "content": f"{preamble}\n\n{corpus}"},
                {"role": "user", "content": prompt}]
        return self._post(msgs, max_tokens, schema)

    def batch(self, corpus, preamble, jobs, poll_seconds=0):
        """No batch endpoint — run concurrently to recover wall-clock time.
        Modest concurrency so a rate limit doesn't take the whole run down."""
        system = f"{preamble}\n\n{corpus}"
        out = {}

        def run(j):
            return j.id, self._post(
                [{"role": "system", "content": system},
                 {"role": "user", "content": j.prompt}], j.max_tokens, j.schema)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run, j) for j in jobs]
            for i, f in enumerate(concurrent.futures.as_completed(futures), 1):
                try:
                    cid, text = f.result()
                    out[cid] = text
                except SystemExit:
                    raise
                except Exception as e:
                    print(f"  ! a request failed: {e}")
                if self.verbose:
                    print(f"    {i}/{len(jobs)} done", flush=True)
        return out
