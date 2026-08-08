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

import auditlog
import credentials
from llm import BatchResult

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
        try:
            # An explicit constructor value is useful to callers that already
            # resolved a key. Otherwise use the same env -> private-store
            # precedence as the Studio backend and every other CLI provider.
            self.key = api_key or credentials.resolve("openrouter")
        except credentials.CredentialStoreError as error:
            sys.exit(str(error))
        if not self.key:
            sys.exit(
                "No OpenRouter key. Add one on the Settings tab of the studio, or:\n"
                "  export OPENROUTER_API_KEY=sk-or-...\n"
                "Get one at https://openrouter.ai/keys"
            )
        self.spent = {"in": 0, "out": 0, "reasoning": 0,
                      "cache_write": 0, "cache_read": 0}

    # ------------------------------------------------------------- internals

    # OpenRouter's reasoning control takes low/medium/high; the pipeline's own
    # effort vocabulary (Anthropic's) also has xhigh and max. Map rather than
    # send a level the route will reject.
    REASONING_EFFORT = {"low": "low", "medium": "medium", "high": "high",
                        "xhigh": "high", "max": "high"}

    def _post(self, messages, max_tokens, schema=None, retries=3,
              job_id=None, operation="completion", effort=None,
              send_reasoning=True, reasoning_max_tokens=None):
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        # ONE reasoning policy, three mutually exclusive wire shapes, chosen by
        # precedence from the job's settings. Because the shape is selected here
        # rather than declared by the caller, a caller that loses `effort` and
        # `reasoning_max_tokens` does not get an error — it silently falls
        # through to `self.effort` and gets a DIFFERENT policy. That is exactly
        # what a hand-rebuilt recovery Job did: a stage pinned to a 2,000-token
        # ceiling re-ran at {"effort": "high"} with no ceiling at all. Job is a
        # dataclass and every rebuild now goes through dataclasses.replace, so
        # the settings travel with the job instead of being re-specified.
        want = (effort or self.effort or "").lower()
        reasoning = None
        if want == "none":
            reasoning = {"enabled": False}
        elif reasoning_max_tokens:
            # A ceiling beats a level: `max_tokens` caps reasoning and content
            # together, so bounding the reasoning half is what guarantees the
            # answer still has somewhere to go. A real run spent 8,083 reasoning
            # tokens against an 8,000 budget and returned zero bytes of JSON.
            reasoning = {"max_tokens": reasoning_max_tokens}
        elif self.REASONING_EFFORT.get(want):
            reasoning = {"effort": self.REASONING_EFFORT[want]}
        if reasoning and send_reasoning:
            # Without this the route reasons at its own default. This is the
            # setting whose absence broke stage 01: `effort` was accepted by the
            # constructor and then never sent.
            body["reasoning"] = reasoning
        if schema:
            # Require a route that supports structured output. Without
            # require_parameters OpenRouter may select a provider that silently
            # drops response_format, leaving the caller with prose instead of the
            # JSON contract it asked for.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema}}
            body["provider"] = {"require_parameters": True}
        audit = auditlog.start("openrouter", self.model, operation, body, job_id,
                               {"endpoint": API, "retries": retries})
        req = urllib.request.Request(
            API, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json",
                     # Observability only: this surfaces the selected endpoint
                     # and fallback attempts without influencing routing.
                     "X-OpenRouter-Metadata": "enabled",
                     "HTTP-Referer": "https://localhost/adpipe",
                     "X-Title": "adpipe"})
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    payload = json.load(r)
                u = payload.get("usage") or {}
                self.spent["in"] += u.get("prompt_tokens", 0)
                self.spent["out"] += u.get("completion_tokens", 0)
                # Reasoning is billed out of the same completion allowance as the
                # answer, so it is the number that decides whether a budget is
                # survivable — and it was invisible here. Providers report it as
                # completion_tokens_details.reasoning_tokens; some flatten it.
                reasoned = (
                    (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
                    or u.get("reasoning_tokens") or 0)
                self.spent["reasoning"] += reasoned
                # Tolerate a reply with no choices at all rather than raising an
                # IndexError: an empty response is a failure mode the caller can
                # classify and re-run, a traceback is one it can only abort on.
                choice = (payload.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                finish = choice.get("finish_reason")
                routing = payload.get("openrouter_metadata") or {}
                endpoints = (routing.get("endpoints") or {}).get("available") or []
                selected = next((endpoint.get("provider") for endpoint in endpoints
                                 if endpoint.get("selected")), None)
                audit.response(payload, text=content, usage=u,
                               finish_reason=finish)
                result = BatchResult(content, finish, reasoned,
                                     u.get("completion_tokens", 0),
                                     selected or payload.get("provider"))
                if finish == "length" and self.verbose:
                    # Reasoning models spend max_tokens on reasoning first, so
                    # the budget can be gone before a single JSON byte is
                    # written. Report the split, not just the fact: "hit the
                    # budget" was printed for empty replies only, so a run whose
                    # replies were truncated mid-JSON said nothing at all until
                    # the totals came in 83 requests later.
                    print(f"  ! {job_id or 'request'}: hit the {max_tokens:,}-token "
                          f"output budget — {result.budget_note or 'no usage reported'}")
                return result
            except urllib.error.HTTPError as e:
                detail_full = e.read().decode("utf-8", "replace")
                detail = detail_full[:400]
                audit.event("http_error", detail_full, status=e.code,
                            attempt=attempt + 1, will_retry=(
                                e.code in (429, 500, 502, 503) and attempt < retries - 1))
                # Not every route accepts the reasoning control. Drop it and keep
                # the request rather than failing the stage — the sized budget
                # and the audited re-run still cover us if the route then
                # over-reasons. Checked before the schema fallback so a reply
                # complaining about both loses reasoning first (the cheaper
                # capability to give up).
                if (send_reasoning and reasoning and e.code in (400, 404, 422)
                        and "reasoning" in detail.lower()):
                    if self.verbose:
                        print("  ! this OpenRouter route does not accept the "
                              "reasoning parameter; retrying without it")
                    return self._post(messages, max_tokens, schema=schema,
                                      retries=retries, job_id=job_id,
                                      operation=operation + "_no_reasoning",
                                      effort=effort, send_reasoning=False,
                                      reasoning_max_tokens=reasoning_max_tokens)
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
                    return self._post(fallback, max_tokens, schema=None, retries=retries,
                                      job_id=job_id, operation=operation + "_schema_fallback",
                                      effort=effort, send_reasoning=send_reasoning,
                                      reasoning_max_tokens=reasoning_max_tokens)
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
                audit.event("network_error", str(e.reason), attempt=attempt + 1,
                            will_retry=attempt < retries - 1)
                if attempt < retries - 1:
                    time.sleep(5); continue
                sys.exit(f"  Network error: {e.reason}")
            except Exception as e:
                audit.error(e, attempt=attempt + 1)
                raise
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

    def one_result(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
                   job_id="single", operation="pipeline_single", effort=None,
                   reasoning_max_tokens=None):
        """Single request with stop reason and usage preserved for recovery."""
        msgs = [{"role": "system", "content": f"{preamble}\n\n{corpus}"},
                {"role": "user", "content": prompt}]
        return self._post(msgs, max_tokens, schema, job_id=job_id,
                          operation=operation, effort=effort,
                          reasoning_max_tokens=reasoning_max_tokens)

    def one(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
            job_id="single", operation="pipeline_single", effort=None,
            reasoning_max_tokens=None):
        """Backward-compatible text-only single request."""
        return self.one_result(
            corpus, preamble, prompt, max_tokens, schema, job_id, operation,
            effort, reasoning_max_tokens).text

    def batch(self, corpus, preamble, jobs, poll_seconds=0):
        """No batch endpoint — run concurrently to recover wall-clock time.
        Modest concurrency so a rate limit doesn't take the whole run down."""
        system = f"{preamble}\n\n{corpus}"
        out = {}

        def run(j):
            return j.id, self._post(
                [{"role": "system", "content": system},
                 {"role": "user", "content": j.prompt}], j.max_tokens, j.schema,
                job_id=j.id, operation="pipeline_batch_job", effort=j.effort,
                reasoning_max_tokens=j.reasoning_max_tokens)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(run, j): j for j in jobs}
            for i, f in enumerate(concurrent.futures.as_completed(futures), 1):
                job = futures[f]
                try:
                    cid, result = f.result()
                    out[cid] = result
                except SystemExit:
                    raise
                except Exception as e:
                    # Record which job died. Silently omitting it leaves the
                    # caller reporting a "missing response" with no cause, and
                    # unable to tell a dead request from an empty one.
                    print(f"  ! {job.id} failed: {e}")
                    out[job.id] = BatchResult("", f"request_error:{type(e).__name__}")
                if self.verbose:
                    print(f"    {i}/{len(jobs)} done", flush=True)
        return out
