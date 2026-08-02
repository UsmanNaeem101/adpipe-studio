#!/usr/bin/env python3
"""
Claude API layer for the ad pipeline.

The pipeline's shape makes two API features do most of the work:

  Prompt caching — skills 07..27 all read the SAME segment evidence file (up to
  ~540KB) and differ only in the extraction instruction. That is the textbook
  "shared prefix, varying suffix" case: the corpus goes in a cached system block,
  the skill goes in the user turn. Cache reads cost ~0.1x, so 21 extractions over
  one corpus cost roughly one full read plus change instead of 21.

  Batching — those 21 extractions are independent, so they go out as one batch at
  50% off. Batches can't pre-warm their own cache (parallel requests can't read
  what each other is still writing), so we fire a max_tokens=0 warm-up first and
  let the batch read the entry it wrote.

Everything is costed with count_tokens before it runs; nothing spends money
without the caller seeing the estimate.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

MODEL = "claude-opus-5"

# USD per million tokens. Multipliers per shared/prompt-caching.md.
PRICING = {
    "claude-opus-5":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}
CACHE_WRITE_1H = 2.00   # 1-hour TTL write premium
CACHE_READ = 0.10       # cache read discount
BATCH = 0.50            # Batch API discount

CACHE_MIN_TOKENS = 512  # Opus 5 minimum cacheable prefix; below this it silently won't cache


@dataclass
class Job:
    """One unit of work: a skill instruction applied to the shared corpus."""
    id: str
    prompt: str
    max_tokens: int = 16000


@dataclass
class Estimate:
    jobs: int = 0
    cached_tokens: int = 0
    uncached_in: int = 0
    est_out: int = 0
    batched: bool = False
    model: str = MODEL

    @property
    def usd(self) -> float:
        p = PRICING.get(self.model, PRICING[MODEL])
        disc = BATCH if self.batched else 1.0
        # Cache is written once, then read once per job.
        write = self.cached_tokens * CACHE_WRITE_1H * p["in"] / 1e6
        read = self.cached_tokens * self.jobs * CACHE_READ * p["in"] * disc / 1e6
        fresh = self.uncached_in * p["in"] * disc / 1e6
        out = self.est_out * p["out"] * disc / 1e6
        return write + read + fresh + out

    def explain(self) -> str:
        return (
            f"  model            {self.model}\n"
            f"  requests         {self.jobs}{'  (batched, 50% off)' if self.batched else ''}\n"
            f"  cached corpus    {self.cached_tokens:,} tokens "
            f"(written once, read {self.jobs}x at 10%)\n"
            f"  uncached input   {self.uncached_in:,} tokens\n"
            f"  est. output      {self.est_out:,} tokens\n"
            f"  ESTIMATE         ${self.usd:,.2f}"
        )


class Client:
    """Thin wrapper: caching, batching, cost, and a single place for model config."""

    def __init__(self, model=MODEL, effort="high", verbose=True):
        try:
            import anthropic
        except ImportError:
            sys.exit(
                "The anthropic SDK is not installed.\n"
                "  ./.venv/bin/pip install anthropic\n"
                "…or run the CLI via ./.venv/bin/python."
            )
        self._sdk = anthropic
        # Zero-arg constructor resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
        # or an `ant auth login` profile — don't demand an env var here.
        self.client = anthropic.Anthropic()
        # The SDK doesn't fail on construction — it raises deep inside the first
        # request. Check here so a missing key is one clear line, not a traceback.
        if not (getattr(self.client, "api_key", None) or
                getattr(self.client, "auth_token", None)):
            sys.exit(
                "No Anthropic credentials found. Add a key on the Settings tab of the\n"
                "studio, or from a terminal:\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...   (create one at console.anthropic.com)\n\n"
                "Free stages that need no key: `qa`, `render`, and\n"
                "`ingest --rules-only` (deterministic pre-pass, skips skills 01/02).\n"
                "Everything else runs a skill file and needs credentials."
            )
        self.model = model
        self.effort = effort
        self.verbose = verbose
        self.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}

    # ---------------------------------------------------------------- helpers

    def _system(self, corpus: str, preamble: str):
        """Stable cached prefix. Nothing volatile may appear before the breakpoint —
        no timestamps, no per-job ids — or every job pays full price."""
        return [
            {"type": "text", "text": preamble},
            {
                "type": "text",
                "text": corpus,
                # 1h TTL: an extraction batch can take a while, and re-running a
                # stage later in the same session should still hit the entry.
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ]

    def _params(self, system, prompt, max_tokens, schema=None):
        p = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self.effort},
            # Adaptive is the default on Opus 5; set it explicitly so the intent
            # survives a model swap. No temperature/top_p — rejected on Opus 5.
            "thinking": {"type": "adaptive"},
        }
        if schema:
            p["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        return p

    def _track(self, usage):
        self.spent["in"] += getattr(usage, "input_tokens", 0) or 0
        self.spent["out"] += getattr(usage, "output_tokens", 0) or 0
        self.spent["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.spent["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0

    def actual_usd(self) -> float:
        p = PRICING.get(self.model, PRICING[MODEL])
        return (
            self.spent["in"] * p["in"]
            + self.spent["cache_write"] * CACHE_WRITE_1H * p["in"]
            + self.spent["cache_read"] * CACHE_READ * p["in"]
            + self.spent["out"] * p["out"]
        ) / 1e6

    # ---------------------------------------------------------------- costing

    def count(self, system, prompt) -> int:
        return self.client.messages.count_tokens(
            model=self.model, system=system, messages=[{"role": "user", "content": prompt}]
        ).input_tokens

    def estimate(self, corpus, preamble, jobs, batched=False) -> Estimate:
        """Price a stage before running it. Counts the corpus once and each job's
        instruction separately, so the cached/uncached split is real rather than
        a guess."""
        system = self._system(corpus, preamble)
        corpus_tokens = self.count(system, "x")
        est = Estimate(jobs=len(jobs), cached_tokens=corpus_tokens,
                       batched=batched, model=self.model)
        for j in jobs:
            est.uncached_in += max(self.count([{"type": "text", "text": ""}], j.prompt), 0)
            # Assume jobs run to ~55% of their cap; extraction output is verbose.
            est.est_out += int(j.max_tokens * 0.55)
        if corpus_tokens < CACHE_MIN_TOKENS:
            print(f"  ! corpus is {corpus_tokens} tokens, below the {CACHE_MIN_TOKENS}-token "
                  f"cache minimum — it will NOT cache and every job pays full price")
        return est

    # ---------------------------------------------------------------- running

    def prewarm(self, corpus, preamble):
        """Write the cache entry before a fan-out. Parallel requests can't read a
        cache entry that is still being written, so without this every job in a
        batch pays the full corpus price."""
        r = self.client.messages.create(
            model=self.model, max_tokens=0,
            system=self._system(corpus, preamble),
            messages=[{"role": "user", "content": "warmup"}],
        )
        self._track(r.usage)
        if self.verbose:
            print(f"  cache warmed: {r.usage.cache_creation_input_tokens:,} tokens written")

    def one(self, corpus, preamble, prompt, max_tokens=16000, schema=None) -> str:
        """Single request. Always streamed — above ~16k max_tokens a non-streaming
        call risks an HTTP timeout, and these stages run long."""
        params = self._params(self._system(corpus, preamble), prompt, max_tokens, schema)
        with self.client.messages.stream(**params) as stream:
            msg = stream.get_final_message()
        self._track(msg.usage)

        if msg.stop_reason == "refusal":
            cat = getattr(msg.stop_details, "category", None)
            raise RuntimeError(f"Request refused by safety classifiers (category: {cat}).")
        if msg.stop_reason == "max_tokens":
            print(f"  ! output hit max_tokens ({max_tokens}) and is truncated — raise it")

        return "".join(b.text for b in msg.content if b.type == "text")

    def batch(self, corpus, preamble, jobs, poll_seconds=30) -> dict:
        """Fan out independent jobs at 50%. Results come back keyed by custom_id in
        arbitrary order — never by position."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        system = self._system(corpus, preamble)
        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=j.id,
                    params=MessageCreateParamsNonStreaming(
                        **self._params(system, j.prompt, j.max_tokens)))
            for j in jobs
        ])
        if self.verbose:
            print(f"  batch {batch.id} submitted ({len(jobs)} requests)")

        while True:
            b = self.client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            if self.verbose:
                c = b.request_counts
                print(f"    {b.processing_status}: {c.succeeded} done, "
                      f"{c.processing} running, {c.errored} errored", flush=True)
            time.sleep(poll_seconds)

        out, failed = {}, []
        for res in self.client.messages.batches.results(batch.id):
            if res.result.type == "succeeded":
                m = res.result.message
                self._track(m.usage)
                if m.stop_reason == "refusal":
                    failed.append(f"{res.custom_id}: refused by safety classifiers")
                    continue
                out[res.custom_id] = "".join(b.text for b in m.content if b.type == "text")
            else:
                detail = getattr(getattr(res.result, "error", None), "type", res.result.type)
                failed.append(f"{res.custom_id}: {detail}")

        for f in failed:
            print(f"  ! {f}")
        return out


def confirm(estimate: Estimate, assume_yes=False) -> bool:
    print(estimate.explain())
    if assume_yes:
        print("  (--yes given, proceeding)")
        return True
    if not sys.stdin.isatty():
        print("  Not a TTY and --yes not given — refusing to spend. Re-run with --yes.")
        return False
    return input("  Proceed? [y/N] ").strip().lower() in ("y", "yes")
