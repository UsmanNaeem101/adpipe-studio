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
  50% off. Live Stage 03E diagnostics showed that a synchronous cache write was
  not read by the first Message Batch, even with the same structured request
  shape. Anthropic's serial one-request Stage 03E waves therefore let the first
  real Batch seed the shared prefix and subsequent Batches read it.

Everything is costed with count_tokens before it runs; nothing spends money
without the caller seeing the estimate.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field

import auditlog
import credentials

MODEL = "claude-opus-5"

# USD per million tokens. Multipliers per shared/prompt-caching.md.
PRICING = {
    "claude-opus-5":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
}
CACHE_WRITE_1H = 2.00   # 1-hour TTL write premium
CACHE_READ = 0.10       # cache read discount
BATCH = 0.50            # Batch API discount

CACHE_MIN_TOKENS = 512  # Opus 5 minimum cacheable prefix; below this it silently won't cache
HAIKU_THINKING_BUDGET = 4096


@dataclass
class Job:
    """One unit of work: a skill instruction applied to the shared corpus."""
    id: str
    prompt: str
    max_tokens: int = 16000
    schema: dict | None = None
    # Per-record stages use this to prove that a response covered the complete
    # input chunk before any output is accepted or written.
    expected_ids: tuple[int, ...] | None = None
    # Reasoning depth for THIS job, overriding the client's setting. Stages are
    # not equally hard: skill 01 is a bouncer deciding retain/reject against a
    # closed list of reasons, and reasoning at the depth a synthesis stage needs
    # is what emptied its output budget. None = use the client's effort;
    # "none" disables reasoning where the provider supports it.
    effort: str | None = None
    # A hard ceiling on the reasoning half of `max_tokens`, where the provider
    # can enforce one. This is what turns "the answer will probably fit" into
    # "the answer's room was never the model's to spend".
    reasoning_max_tokens: int | None = None


# Providers spell "I ran out of output room" differently: Anthropic returns a
# stop_reason of "max_tokens", OpenAI-compatible routes a finish_reason of
# "length". Defined once here because llm.py, openrouter.py and presets.py all
# have to recognise the same condition.
BUDGET_STOP_REASONS = ("max_tokens", "length")

# A refusal or a content-filter block is a decision, not a shortfall. Re-running
# it spends money to be blocked again, so neither is ever retryable.
NO_RETRY_STOP_REASONS = ("refusal", "content_filter")


@dataclass
class BatchResult:
    """One job's reply, plus the reason generation stopped.

    The stop reason is the whole difference between "the model wrote bad JSON"
    and "the model never got as far as the JSON". Without it a decode site sees
    an empty string, reports `Expecting value: line 1 column 1 (char 0)`, and
    sends the reader hunting for a parser bug that isn't there.
    """
    text: str = ""
    stop_reason: str | None = None
    # How the output budget was actually spent. `max_tokens` caps reasoning and
    # answer together, so "ran out of room" and "reasoned instead of answering"
    # are the same stop reason and different problems — and only these numbers
    # tell them apart. Without them the last fix rested on an assumption that
    # could not be checked: that the provider honoured the reasoning ceiling it
    # was sent. It did not, and nothing in the run said so.
    reasoning_tokens: int = 0
    completion_tokens: int = 0
    # Actual route used for aggregators such as OpenRouter.  This is telemetry,
    # never a routing instruction; provider selection remains unchanged.
    provider: str | None = None
    # Structured provider failures belong beside the empty response. In
    # particular, an Anthropic Message Batch `errored` result contains the real
    # invalid_request_error one level below the outer result type. Keeping it
    # here prevents downstream recovery from reducing that contract failure to
    # the misleading phrase "provider returned an empty response".
    provider_error: dict | None = None

    @property
    def budget_note(self) -> str:
        """One phrase describing where the budget went, or "" if not reported."""
        if not self.completion_tokens and not self.reasoning_tokens:
            return ""
        answer = max(self.completion_tokens - self.reasoning_tokens, 0)
        return (f"{self.completion_tokens:,} completion tokens "
                f"({self.reasoning_tokens:,} reasoning, {answer:,} answer)")

    @property
    def out_of_budget(self) -> bool:
        return self.stop_reason in BUDGET_STOP_REASONS

    @property
    def retryable(self) -> bool:
        """True when re-running the ORIGINAL request is the right recovery.

        That is the case when the provider never finished writing: an
        output-budget stop, an empty reply, or a transport/batch-level failure.
        A reply that has text but the wrong shape is cheaper to repair than to
        regenerate, and a refusal is not a shortfall at all.
        """
        if self.stop_reason in NO_RETRY_STOP_REASONS:
            return False
        if ((self.provider_error or {}).get("type") == "invalid_request_error"):
            return False
        return self.out_of_budget or not (self.text or "").strip()

    @property
    def repairable(self) -> bool:
        """True when a shape-repair pass has something to work with.

        Repair shows the model its own output and asks for the shape to be
        fixed. An empty reply has no shape to fix — handing "" to that prompt
        is the original bug in a different costume, and it burns a paid request
        to re-derive an answer the model never wrote. A blocked reply is not
        repairable either: the block was the provider's decision, not a
        formatting slip.
        """
        if self.stop_reason in NO_RETRY_STOP_REASONS:
            return False
        return bool((self.text or "").strip())


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

    # Used by Stage 03E only. Its native Anthropic rolling waves contain one
    # request each, so the first real Message Batch can seed the cache before
    # the next starts. A synchronous prewarm did not populate that Batch cache
    # in the live proof and would only add a discarded paid generation.
    batch_cache_self_seeds = True
    # Capability marker used only by Stage 05's scheduler. OpenRouter also has
    # a ``batch`` method, but it is a local four-thread fan-out rather than one
    # Anthropic Message Batch and therefore must not inherit Anthropic's grammar
    # compilation pacing policy.
    native_anthropic_batches = True

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
        try:
            key = credentials.resolve("anthropic")
        except credentials.CredentialStoreError as error:
            sys.exit(str(error))
        # Supplying the shared resolver's result enables the private AdPipe
        # store. With no API key, retain the SDK's existing auth-token/profile
        # support rather than narrowing accepted Anthropic credentials.
        self.client = (anthropic.Anthropic(api_key=key) if key
                       else anthropic.Anthropic())
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

    @staticmethod
    def _normalise_system(system):
        """Return valid Anthropic system content, or ``None`` when absent.

        Anthropic rejects a text block whose text is empty.  Keep strings as
        strings and structured blocks as blocks so counting and inference use
        the caller's real request shape; only impossible empty text blocks are
        removed.
        """
        if system is None:
            return None
        if isinstance(system, str):
            return system if system.strip() else None

        blocks = []
        for block in system:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and not str(block.get("text") or "").strip()):
                continue
            blocks.append(dict(block) if isinstance(block, dict) else block)
        return blocks or None

    def _system(self, corpus: str, preamble: str):
        """Stable cached prefix. Nothing volatile may appear before the breakpoint —
        no timestamps, no per-job ids — or every job pays full price."""
        blocks = []
        if preamble and preamble.strip():
            blocks.append({"type": "text", "text": preamble})
        if corpus and corpus.strip():
            blocks.append({"type": "text", "text": corpus})
        if not blocks:
            return None
        # The cache breakpoint belongs on the last real block. This caches the
        # complete stable prefix whether it consists of preamble + corpus or
        # only one of them, and never creates an empty placeholder block.
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return blocks

    def _params(self, system, prompt, max_tokens, schema=None, effort=None,
                reasoning_max_tokens=None):
        p = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            # Adaptive is the default on Opus 5; set it explicitly so the intent
            # survives a model swap. No temperature/top_p — rejected on Opus 5.
            "thinking": {"type": "adaptive"},
        }
        # Haiku 4.5 rejects adaptive thinking at inference time (including in
        # Message Batches), even though count_tokens accepts the same body. Keep
        # reasoning enabled, but express it with Haiku's supported explicit
        # budget. `reasoning_max_tokens` remains the stage-specific override.
        is_haiku_45 = self.model.startswith("claude-haiku-4-5")
        supports_effort = not is_haiku_45
        thinking_disabled = (effort or "").lower() == "none"
        if is_haiku_45 and not thinking_disabled:
            requested = reasoning_max_tokens or HAIKU_THINKING_BUDGET
            # Anthropic requires at least 1,024 thinking tokens and room for an
            # answer inside max_tokens. Stage 03E uses 12k, so its default is
            # the proven 4,096-token configuration rather than the invalid
            # adaptive form seen in the live request.
            budget = min(max(int(requested), 1024), max(max_tokens - 1, 1024))
            if budget >= max_tokens:
                raise ValueError(
                    f"Anthropic Haiku thinking budget {budget} must be below "
                    f"max_tokens {max_tokens}")
            p["thinking"] = {"type": "enabled", "budget_tokens": budget}
        output_config = ({"effort": (effort or self.effort)}
                         if supports_effort else {})
        system = self._normalise_system(system)
        if system is not None:
            p["system"] = system
        if thinking_disabled:
            # Opus 5 accepts disabled thinking only at effort `high` or below,
            # so pin the level too rather than emit a combination the API 400s.
            # Note this is the documented-riskier setting on Opus 5 (it can leak
            # internal tags into the response); low effort is the safer default
            # and is what stage 01 ships with.
            if supports_effort:
                output_config["effort"] = "high"
            p["thinking"] = {"type": "disabled"}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            p["output_config"] = output_config
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

    def count(self, system, prompt, schema=None, effort=None, max_tokens=16000,
              reasoning_max_tokens=None) -> int:
        """Use Anthropic's native counter with the real inference input shape.

        ``count_tokens`` accepts the same system/messages/thinking/output-config
        inputs as ``messages.create`` but does not accept ``max_tokens``. Build
        through ``_params`` so estimation cannot drift from inference, remove
        only that output-only field, and omit ``system`` entirely when absent.
        """
        params = self._params(system, prompt, max_tokens, schema, effort,
                              reasoning_max_tokens)
        params.pop("max_tokens")
        return self.client.messages.count_tokens(**params).input_tokens

    def estimate(self, corpus, preamble, jobs, batched=False) -> Estimate:
        """Price a stage before running it. Counts the corpus once and each job's
        instruction separately, so the cached/uncached split is real rather than
        a guess."""
        system = self._system(corpus, preamble)
        # Isolate the shared system contribution using two valid native counts.
        # The no-system baseline is omitted from the request rather than encoded
        # as the invalid [{"type": "text", "text": ""}] placeholder.
        baseline_tokens = self.count(None, "x")
        corpus_tokens = max(self.count(system, "x") - baseline_tokens, 0)
        est = Estimate(jobs=len(jobs), cached_tokens=corpus_tokens,
                       batched=batched, model=self.model)
        for j in jobs:
            # Count the exact system + prompt + schema + effort structure the
            # inference request will use, then allocate the stable prefix to the
            # cached side of the estimate.
            full_tokens = self.count(
                system, j.prompt, j.schema, j.effort, j.max_tokens,
                j.reasoning_max_tokens)
            est.uncached_in += max(full_tokens - corpus_tokens, 0)
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
        system = self._system(corpus, preamble)
        if system is None:
            if self.verbose:
                print("  no non-empty system prefix — skipping cache warm-up")
            return
        params = {"model": self.model, "max_tokens": 1,
                  "system": system,
                  "messages": [{"role": "user", "content": "warmup"}]}
        audit = auditlog.start("anthropic", self.model, "cache_prewarm", params,
                               job_id="warmup")
        try:
            r = self.client.messages.create(**params)
            audit.response(r, text="", usage=getattr(r, "usage", None))
        except Exception as e:
            audit.error(e)
            raise
        self._track(r.usage)
        if self.verbose:
            print(f"  cache warmed: {r.usage.cache_creation_input_tokens:,} tokens written")

    def one_result(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
                   job_id="single", operation="pipeline_single", effort=None,
                   reasoning_max_tokens=None) -> BatchResult:
        """Single request retaining stop reason and usage for recovery.

        Haiku's explicit thinking mode can enforce `reasoning_max_tokens`;
        adaptive-capable Anthropic models retain their existing behaviour.
        """
        params = self._params(self._system(corpus, preamble), prompt, max_tokens,
                              schema, effort, reasoning_max_tokens)
        audit = auditlog.start("anthropic", self.model, operation, params,
                               job_id=job_id)
        try:
            with self.client.messages.stream(**params) as stream:
                msg = stream.get_final_message()
        except Exception as e:
            audit.error(e)
            raise
        self._track(msg.usage)

        text = "".join(b.text for b in msg.content if b.type == "text")
        audit.response(msg, text=text, usage=msg.usage, stop_reason=msg.stop_reason)

        return BatchResult(
            text, msg.stop_reason,
            _anthropic_thinking_tokens(msg.usage),
            getattr(msg.usage, "output_tokens", 0) or 0,
            "Anthropic")

    def one(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
            job_id="single", operation="pipeline_single", effort=None,
            reasoning_max_tokens=None) -> str:
        """Backward-compatible text-only single request."""
        result = self.one_result(
            corpus, preamble, prompt, max_tokens, schema, job_id, operation,
            effort, reasoning_max_tokens)
        if result.stop_reason == "refusal":
            raise RuntimeError("Request refused by safety classifiers.")
        if result.stop_reason == "max_tokens":
            print(f"  ! output hit max_tokens ({max_tokens}) and is truncated — raise it")
        return result.text

    def batch(self, corpus, preamble, jobs, poll_seconds=30) -> dict:
        """Fan out independent jobs at 50%. Results come back keyed by custom_id in
        arbitrary order — never by position."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        system = self._system(corpus, preamble)
        params_by_id = {j.id: self._params(
            system, j.prompt, j.max_tokens, j.schema, j.effort,
            j.reasoning_max_tokens) for j in jobs}
        audits = {j.id: auditlog.start(
            "anthropic", self.model, "pipeline_batch_job", params_by_id[j.id],
            job_id=j.id) for j in jobs}
        try:
            batch = self.client.messages.batches.create(requests=[
                Request(custom_id=j.id,
                        params=MessageCreateParamsNonStreaming(**params_by_id[j.id]))
                for j in jobs
            ])
        except Exception as e:
            for audit in audits.values():
                audit.error(e, phase="batch_submit")
            raise
        if self.verbose:
            print(f"  batch {batch.id} submitted ({len(jobs)} requests)")

        try:
            while True:
                b = self.client.messages.batches.retrieve(batch.id)
                if b.processing_status == "ended":
                    break
                if self.verbose:
                    c = b.request_counts
                    print(f"    {b.processing_status}: {c.succeeded} done, "
                          f"{c.processing} running, {c.errored} errored", flush=True)
                time.sleep(poll_seconds)
        except Exception as e:
            for audit in audits.values():
                audit.error(e, phase="batch_poll", batch_id=batch.id)
            raise

        out, failed = {}, []
        try:
            for res in self.client.messages.batches.results(batch.id):
                if res.result.type == "succeeded":
                    m = res.result.message
                    self._track(m.usage)
                    text = "".join(b.text for b in m.content if b.type == "text")
                    audits[res.custom_id].response(
                        m, text=text, usage=m.usage, stop_reason=m.stop_reason,
                        batch_id=batch.id)
                    if m.stop_reason == "refusal":
                        failed.append(f"{res.custom_id}: refused by safety classifiers")
                    elif m.stop_reason == "max_tokens":
                        failed.append(
                            f"{res.custom_id}: hit max_tokens before finishing")
                    # Record the reply either way, refusal included. Dropping it
                    # here would leave the caller unable to tell a refusal from a
                    # request that never came back, and it would retry the refusal.
                    out[res.custom_id] = BatchResult(
                        text, m.stop_reason,
                        # Anthropic bills thinking inside output_tokens too, and
                        # reports it separately only when it is asked for.
                        _anthropic_thinking_tokens(m.usage),
                        getattr(m.usage, "output_tokens", 0) or 0,
                        "Anthropic")
                else:
                    raw = _provider_plain(res.result)
                    detail = _anthropic_batch_error(
                        raw, custom_id=res.custom_id, batch_id=batch.id)
                    audits[res.custom_id].response(
                        res.result, text="", stop_reason=f"batch_{res.result.type}",
                        batch_id=batch.id, custom_id=res.custom_id,
                        provider_error=detail)
                    message = detail.get("message") or "no provider message"
                    failed.append(
                        f"{res.custom_id}: {detail['type']}: {message} "
                        f"(batch {batch.id})")
                    if self.verbose:
                        print("\n  ANTHROPIC BATCH ERROR")
                        print(f"  custom_id: {res.custom_id}")
                        print(f"  batch_id: {batch.id}")
                        print(f"  type: {detail['type']}")
                        print(f"  message: {message}")
                        if detail.get("code") is not None:
                            print(f"  code: {detail['code']}")
                        if detail.get("request_id"):
                            print(f"  request_id: {detail['request_id']}")
                        if detail.get("details"):
                            print("  details: " + json.dumps(
                                detail["details"], ensure_ascii=False,
                                sort_keys=True))
                        print(f"  diagnostics: {audits[res.custom_id].relative_path}")
                    out[res.custom_id] = BatchResult(
                        text="", stop_reason=f"batch_{res.result.type}",
                        provider="Anthropic", provider_error=detail)
        except Exception as e:
            for audit in audits.values():
                audit.error(e, phase="batch_results", batch_id=batch.id)
            raise

        for f in failed:
            print(f"  ! {f}")
        return out


def _provider_plain(value):
    """Convert SDK result objects to JSON-shaped data for diagnostics."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _provider_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_provider_plain(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _provider_plain(dump())
    if hasattr(value, "__dict__"):
        return _provider_plain(vars(value))
    return repr(value)


def _anthropic_batch_error(raw_result, *, custom_id, batch_id):
    """Flatten Anthropic's nested Message Batch error without losing detail."""
    raw = _provider_plain(raw_result) or {}
    outer = raw.get("error") if isinstance(raw, dict) else {}
    outer = outer if isinstance(outer, dict) else {}
    inner = outer.get("error")
    inner = inner if isinstance(inner, dict) else {}
    details = inner.get("details", outer.get("details"))
    code = (inner.get("code", inner.get("status_code"))
            if inner else outer.get("code", outer.get("status_code")))
    return {
        "type": (inner.get("type") or outer.get("type")
                 or (raw.get("type") if isinstance(raw, dict) else None)
                 or "unknown_batch_error"),
        "message": inner.get("message") or outer.get("message") or "",
        "code": code,
        "request_id": outer.get("request_id") or inner.get("request_id"),
        "details": details,
        "custom_id": custom_id,
        "batch_id": batch_id,
    }


def _anthropic_thinking_tokens(usage):
    """Read both legacy direct and current nested Anthropic usage telemetry."""
    direct = getattr(usage, "thinking_tokens", None)
    if direct is not None:
        return direct or 0
    details = getattr(usage, "output_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        return details.get("thinking_tokens", 0) or 0
    return getattr(details, "thinking_tokens", 0) or 0


def confirm(estimate: Estimate, assume_yes=False) -> bool:
    print(estimate.explain())
    if assume_yes:
        print("  (--yes given, proceeding)")
        return True
    if not sys.stdin.isatty():
        print("  Not a TTY and --yes not given — refusing to spend. Re-run with --yes.")
        return False
    return input("  Proceed? [y/N] ").strip().lower() in ("y", "yes")
