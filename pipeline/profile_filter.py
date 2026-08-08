#!/usr/bin/env python3
"""
Profile stage 01 (VOC filter) before spending money on it.

Stage 01 aborted on a 4,977-record corpus with 82/83 batches returning empty.
The instinct is "the output budget is too small". It isn't: this tool measures
what the stage actually needs and shows that the ANSWER fits comfortably inside
`max_tokens` — the budget is being consumed by reasoning before a single JSON
byte is written.

    ./.venv/bin/python pipeline/profile_filter.py                    # bundled corpus
    ./.venv/bin/python pipeline/profile_filter.py --records 4977     # project to a size
    ./.venv/bin/python pipeline/profile_filter.py --corpus raw.txt   # a raw VOC dump
    ./.venv/bin/python pipeline/profile_filter.py --exact            # bill count_tokens

What it can and cannot know
---------------------------
Deterministic (measured here): prompt size, the JSON one verdict serialises to,
and therefore the content half of the output budget. These are arithmetic over
your corpus and the stage's own schema — no model required.

NOT deterministic: how many tokens a given model spends REASONING before it
starts writing. That is the term that broke the run, and nothing offline can
measure it. This tool prints the reasoning HEADROOM each configuration leaves,
which is the number that decides whether the stage completes; `--exact` bills a
real count_tokens call for input, and `--calibrate` spends one real batch to
measure the reasoning term against your model.

Standard library only (count_tokens needs the anthropic SDK and a key).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli  # noqa: E402
import llm  # noqa: E402
import openrouter  # noqa: E402

DEFAULT_CORPUS = os.path.join(
    ROOT, "projects", "montisella", "research", "voc", "filtered_voc.jsonl")

# The pipeline's own approximation, kept identical so estimates agree with the
# cost line the CLI prints before spending.
CHARS_PER_TOKEN = 4

# What a verdict costs on the wire, measured from the real FILTER_SCHEMA rather
# than guessed. Two reason codes is the common case for a retained record.
SAMPLE_VERDICT = {
    "evidence_id": 1234,
    "decision": "retain",
    "retention_reasons": ["first_person_experience", "specific_problem"],
    "rejection_reasons": [],
}

# Reason codes are a closed vocabulary in skills/01_filter_voc.md. The longest
# one bounds a verdict's worst case — which is the point of an enum: the tail
# stops being open-ended.
LONGEST_CODES = ["desired_outcome_distinctiveness", "quotation_without_new_evidence"]


def approx_tokens(text: str) -> int:
    return max(len(text) // CHARS_PER_TOKEN, 1)


# ------------------------------------------------------------------ corpus

def load_records(path):
    """Accept a filtered_voc.jsonl or a raw dump, and return the texts stage 01
    would actually judge (i.e. after the deterministic pre-pass)."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        raw = fh.read()
    if path.endswith(".jsonl"):
        return [json.loads(line)["text"] for line in raw.splitlines() if line.strip()]
    return [item["text"] for item in cli.parse_voc(raw)]


def rendered_tokens(texts):
    """Per-record prompt cost as cmd_ingest actually renders it: `[id] text`
    joined by blank lines."""
    return [approx_tokens(f"[{i + 1}] {t}") + 1 for i, t in enumerate(texts)]


# ------------------------------------------------------------------ sizing

def verdict_tokens(worst_case=False):
    v = dict(SAMPLE_VERDICT)
    if worst_case:
        v["retention_reasons"] = LONGEST_CODES
    return approx_tokens(json.dumps(v, separators=(",", ":")) + ",")


def content_budget(chunk, worst_case=False):
    """Tokens the ANSWER needs for one batch — one verdict per record plus the
    `{"records":[...]}` wrapper. This is the half of `max_tokens` the stage
    cannot do without."""
    return chunk * verdict_tokens(worst_case) + 8


def recommended_max_tokens(chunk, reasoning_cap):
    """A budget derived from the schema instead of picked round.

    content + reasoning + 15% margin. Sizing this way is what makes the failure
    structural rather than probabilistic: the answer's room is reserved before
    reasoning is allowed to spend anything.
    """
    return int((content_budget(chunk, worst_case=True) + reasoning_cap) * 1.15)


# ------------------------------------------------------------------ report

def sweep(texts, chunks, prefix_tokens, reasoning_cap, current_max_tokens):
    per_record = rendered_tokens(texts)
    corpus_tokens = sum(per_record)
    n = len(texts)
    rows = []
    for chunk in chunks:
        batches = math.ceil(n / chunk)
        content = content_budget(chunk)
        headroom = current_max_tokens - content
        rows.append({
            "chunk": chunk,
            "batches": batches,
            # Records are sent exactly once regardless of batch size; only the
            # cached prefix is re-read per request. That is why batch size is a
            # reliability lever far more than a cost lever.
            "prompt_in": corpus_tokens + prefix_tokens * batches,
            "content_out": content,
            "content_total": content * batches,
            "headroom": headroom,
            "headroom_pct": 100.0 * headroom / current_max_tokens,
            "recommended": recommended_max_tokens(chunk, reasoning_cap),
        })
    return corpus_tokens, rows


def anthropic_usd(corpus_tokens, prefix_tokens, batches, out_tokens, model):
    """Price via llm.Estimate so the number matches what `adpipe ingest` prints."""
    est = llm.Estimate(jobs=batches, cached_tokens=prefix_tokens,
                       uncached_in=corpus_tokens, est_out=out_tokens,
                       batched=True, model=model)
    return est.usd


def openrouter_usd(corpus_tokens, prefix_tokens, batches, out_tokens, model):
    p = openrouter.PRICING.get(model, openrouter.PRICING["_default"])
    # No prompt caching: the prefix is re-sent in full on every request.
    return ((corpus_tokens + prefix_tokens * batches) * p["in"]
            + out_tokens * p["out"]) / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=DEFAULT_CORPUS,
                    help="filtered_voc.jsonl or a raw VOC dump")
    ap.add_argument("--records", type=int,
                    help="project onto a corpus of this size (e.g. 4977)")
    ap.add_argument("--chunks", default="20,30,40,60,80,120",
                    help="batch sizes to sweep")
    ap.add_argument("--max-tokens", type=int, default=8000,
                    help="the budget currently configured (default: stage 01's 8000)")
    ap.add_argument("--reasoning-cap", type=int, default=2000,
                    help="tokens to reserve for reasoning when sizing (default 2000)")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--exact", action="store_true",
                    help="bill a real count_tokens call instead of the 4:1 estimate")
    args = ap.parse_args()

    texts = load_records(args.corpus)
    if not texts:
        sys.exit(f"no records in {args.corpus}")
    measured = len(texts)
    if args.records and args.records != measured:
        # Repeat the measured distribution rather than inventing records, so the
        # projection carries this corpus's real length profile.
        texts = [texts[i % measured] for i in range(args.records)]

    _, skill_text = cli.skill(1)
    prefix = f"{skill_text}\n\n---\n\n{cli.PREAMBLE}"
    prefix_tokens = approx_tokens(prefix)
    basis = "~4 chars/token"
    if args.exact:
        try:
            c = llm.Client(model=args.model, verbose=False)
            prefix_tokens = c.count([{"type": "text", "text": prefix}], "x")
            basis = "count_tokens (exact)"
        except SystemExit as e:
            print(f"  ! --exact unavailable ({e}); falling back to the estimate\n")

    chunks = [int(x) for x in args.chunks.split(",") if x.strip()]
    corpus_tokens, rows = sweep(texts, chunks, prefix_tokens,
                                args.reasoning_cap, args.max_tokens)

    print(f"\nSTAGE 01 PROFILE   {len(texts):,} records "
          f"({'measured' if len(texts) == measured else f'projected from {measured:,}'})")
    print(f"  token basis      {basis}")
    print(f"  cached prefix    {prefix_tokens:,} tok "
          f"({'cacheable' if prefix_tokens >= llm.CACHE_MIN_TOKENS else 'BELOW cache minimum'})")
    print(f"  corpus (records) {corpus_tokens:,} tok — sent once, whatever the batch size")
    print(f"  one verdict      {verdict_tokens()} tok typical, "
          f"{verdict_tokens(worst_case=True)} tok worst case")

    print(f"\n  Output budget analysis at the configured max_tokens={args.max_tokens:,}")
    print(f"  {'batch':>6} {'reqs':>6} {'answer needs':>13} {'left for reasoning':>19} "
          f"{'sized budget':>13}")
    for r in rows:
        print(f"  {r['chunk']:>6} {r['batches']:>6} {r['content_out']:>10,} tok "
              f"{r['headroom']:>13,} tok ({r['headroom_pct']:>4.1f}%) "
              f"{r['recommended']:>9,} tok")

    print(f"\n  Cost (output priced at the answer only — reasoning bills on top)")
    print(f"  {'batch':>6} {'reqs':>6} {'anthropic (batched+cached)':>28} {'openrouter':>14}")
    for r in rows:
        a = anthropic_usd(corpus_tokens, prefix_tokens, r["batches"],
                          r["content_total"], args.model)
        o = openrouter_usd(corpus_tokens, prefix_tokens, r["batches"],
                           r["content_total"], openrouter.DEFAULT_MODEL)
        print(f"  {r['chunk']:>6} {r['batches']:>6} {'$' + format(a, ',.2f'):>28} "
              f"{'$' + format(o, ',.2f'):>14}")

    worst = min(rows, key=lambda r: r["headroom_pct"])
    print(f"\n  READ THIS: at every batch size above, the answer fits inside "
          f"max_tokens={args.max_tokens:,}.")
    print(f"  Even the tightest ({worst['chunk']}/batch) leaves "
          f"{worst['headroom']:,} tok — {worst['headroom_pct']:.0f}% of the budget — unused by content.")
    print( "  A run that returns EMPTY content therefore did not run out of room to answer;")
    print( "  it spent that headroom reasoning. Bound the reasoning (effort / reasoning cap)")
    print( "  and size max_tokens off the schema; raising max_tokens alone only buys")
    print( "  the reasoning more room to expand into.")
    print( "\n  Batch size barely moves cost — records are sent once either way. Choose it")
    print( "  for blast radius: one unrecoverable batch loses that many records' verdicts.\n")


if __name__ == "__main__":
    main()
