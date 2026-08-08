#!/usr/bin/env python3
"""A/B two reasoning policies on the same records, and report the difference.

The open question is whether stage 01 should run with reasoning disabled
(`none`) or reduced with a ceiling (`low` + a 2,000-token cap). Both arguments
are plausible from the armchair — it is high-volume structured classification,
which argues for `none`; a borderline retain/reject is a real judgement call,
which argues for keeping some. Nothing offline settles it, so this runs both
against the SAME records and prints what actually differs.

    ./.venv/bin/python pipeline/ab_reasoning.py --records 120
    ./.venv/bin/python pipeline/ab_reasoning.py --records 120 --arms none,low,medium
    ./.venv/bin/python pipeline/ab_reasoning.py --stage 02 --arms none,low,high

This SPENDS MONEY and needs OPENROUTER_API_KEY. It prints the per-arm cost and
asks before running unless --yes is given.

What it compares
----------------
  retain/reject agreement   the decision that actually gates the corpus
  reason-code agreement     mean Jaccard over each record's code set
  disagreements             every record the arms judged differently, in full,
                            because that list is the quality question — an
                            aggregate number cannot tell you whether the arm
                            that dropped a record was right to
  reasoning tokens          the term that caused the original failure
  completion tokens         what you are billed for
  latency                   wall clock per arm
  schema failures / retries how often each arm needed the recovery path

Read the disagreements before reading the totals. If the arms agree on the
decisions and differ only in cost, prefer the cheap one. If they disagree on
records you would have kept, the cheap one is not free.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli  # noqa: E402
import llm  # noqa: E402

DEFAULT_CORPUS = os.path.join(
    ROOT, "projects", "montisella", "research", "voc", "filtered_voc.jsonl")


def load(path, n):
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    # Take a contiguous slice rather than a sample: every arm must see exactly
    # the same records in the same order, and a seeded sample is one more thing
    # that can silently differ between runs.
    return [{"id": i + 1, "text": r["text"], "url": r.get("url", ""),
             "title": r.get("title", "")} for i, r in enumerate(rows[:n])]


def build_jobs(records, chunk, effort, cap, schema, prompt_of):
    from llm import Job
    chunks = [records[i:i + chunk] for i in range(0, len(records), chunk)]
    return [Job(id=f"a{n:04d}", prompt=prompt_of(ch),
                max_tokens=cli._record_max_tokens(len(ch), 40),
                schema=schema, expected_ids=tuple(r["id"] for r in ch),
                effort=effort,
                # `none` disables reasoning outright, so a ceiling is moot; the
                # cap is what makes the `low` arm's budget a guarantee.
                reasoning_max_tokens=None if effort == "none" else cap)
            for n, ch in enumerate(chunks)]


def run_arm(client, prefix, preamble, jobs, key):
    """One arm. Returns rows keyed by evidence_id plus the observed cost."""
    before = dict(client.spent)
    started = time.monotonic()
    recovery = {"rerun": 0, "repair": 0}

    def rerun(failed, factor):
        recovery["rerun"] += len(failed)
        return cli._rerun_batch(client, prefix, preamble, failed, factor)

    def repair(failed):
        recovery["repair"] += len(failed)
        return cli._repair_batch(client, prefix, preamble, failed, key)

    error = None
    rows = []
    try:
        rows = cli._batch_rows(
            client.batch(prefix, preamble, jobs), jobs, key,
            os.path.join(ROOT, "logs", "ab", key), repair=repair, rerun=rerun)
    except cli.BatchOutputError as e:
        error = str(e)
    return {
        "rows": {r.get("evidence_id"): r for r in rows},
        "seconds": time.monotonic() - started,
        "tokens": {k: client.spent[k] - before.get(k, 0) for k in client.spent},
        "recovery": recovery,
        "error": error,
    }


def jaccard(a, b):
    a, b = set(a or []), set(b or [])
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def compare(records, arms, results):
    base_name = arms[0]
    base = results[base_name]["rows"]
    print(f"\n{'':22}", "".join(f"{a:>14}" for a in arms))
    row = lambda label, f: print(
        f"  {label:20}", "".join(f"{f(results[a]):>14}" for a in arms))

    row("reasoning tokens", lambda r: f"{r['tokens'].get('reasoning', 0):,}")
    row("completion tokens", lambda r: f"{r['tokens'].get('out', 0):,}")
    row("prompt tokens", lambda r: f"{r['tokens'].get('in', 0):,}")
    row("wall clock", lambda r: f"{r['seconds']:.1f}s")
    row("re-runs", lambda r: r["recovery"]["rerun"])
    row("repairs", lambda r: r["recovery"]["repair"])
    row("records judged", lambda r: f"{len(r['rows']):,}")
    row("stage aborted", lambda r: "YES" if r["error"] else "no")

    print(f"\n  Agreement vs {base_name!r}:")
    for arm in arms[1:]:
        other = results[arm]["rows"]
        common = [r["id"] for r in records if r["id"] in base and r["id"] in other]
        if not common:
            print(f"    {arm:12} no overlapping records to compare")
            continue
        same = [i for i in common
                if base[i].get("decision") == other[i].get("decision")]
        codes = [jaccard(base[i].get("retention_reasons", [])
                         + base[i].get("rejection_reasons", []),
                         other[i].get("retention_reasons", [])
                         + other[i].get("rejection_reasons", []))
                 for i in common]
        print(f"    {arm:12} decision {100*len(same)/len(common):5.1f}% "
              f"({len(common)-len(same)} differ)   "
              f"reason-code overlap {100*statistics.mean(codes):5.1f}%")

    # The list that actually answers the quality question.
    for arm in arms[1:]:
        other = results[arm]["rows"]
        diff = [r for r in records
                if r["id"] in base and r["id"] in other
                and base[r["id"]].get("decision") != other[r["id"]].get("decision")]
        if not diff:
            print(f"\n  No decision disagreements between {base_name!r} and {arm!r}.")
            continue
        print(f"\n  DISAGREEMENTS — {base_name!r} vs {arm!r} "
              f"({len(diff)}). Read these; the percentages above cannot tell you "
              f"which arm was right:")
        for r in diff[:25]:
            b, o = base[r["id"]], other[r["id"]]
            print(f"\n    [{r['id']}] {r['text'][:150]}"
                  f"{'…' if len(r['text']) > 150 else ''}")
            for label, v in ((base_name, b), (arm, o)):
                reasons = v.get("retention_reasons") or v.get("rejection_reasons") or []
                print(f"        {label:10} {v.get('decision'):7} {', '.join(reasons)}")
        if len(diff) > 25:
            print(f"\n    …and {len(diff) - 25} more.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--project", default="montisella")
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--records", type=int, default=120)
    ap.add_argument("--chunk", type=int, default=cli.FILTER_CHUNK)
    ap.add_argument("--stage", choices=["01", "02"], default="01")
    ap.add_argument("--arms", default="none,low",
                    help="reasoning policies to compare, first is the baseline")
    ap.add_argument("--cap", type=int, default=cli.REASONING_RESERVE)
    ap.add_argument("--provider", default="openrouter")
    ap.add_argument("--model", default=None)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    records = load(args.corpus, args.records)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    cfg = cli.load_project(args.project)
    ctx = f"MARKET CONTEXT: {cfg.get('product', '')} — {cfg.get('market', '')}\n"

    if args.stage == "01":
        prefix = (cli.skill_sections(1, cli.FILTER_SKILL_SECTIONS,
                                     include_intro=False)
                  + f"\n\n---\n\n{ctx}")
        preamble, schema, key = cli.FILTER_PREAMBLE, cli.FILTER_SCHEMA, "records"
        prompt_of = lambda ch: (
            "Classify each record below: retain or reject, with reason "
            "codes.\n\nRECORDS:\n\n"
            + "\n\n".join(f"[{r['id']}] {r['text']}" for r in ch))
    else:
        _, s02 = cli.skill(2)
        prefix = f"{s02}\n\n---\n\n{ctx}"
        preamble, schema, key = cli.PREAMBLE, cli.DEDUP_SCHEMA, "groups"
        prompt_of = lambda ch: (
            "Apply skill 02 to the records below. Group only genuine "
            "duplicates. Return JSON only as {\"groups\":[...]}; an empty "
            "groups list is a valid answer.\n\nRECORDS:\n\n"
            + "\n\n".join(f"[{r['id']}] {r['text']}" for r in ch))

    print(f"\n  A/B stage {args.stage}: {len(records):,} records x {len(arms)} arms "
          f"({', '.join(arms)}) at {args.chunk}/batch")
    setattr(args, "effort", None)
    client = cli.client(cfg, args)
    jobs = build_jobs(records, args.chunk, arms[0], args.cap, schema, prompt_of)
    est = client.estimate(prefix, preamble, jobs, batched=False)
    print(est.explain())
    print(f"  x{len(arms)} arms  ->  roughly ${est.usd * len(arms):,.2f} total")
    if not llm.confirm(est, args.yes):
        return

    results = {}
    for arm in arms:
        print(f"\n  --- arm {arm!r}")
        results[arm] = run_arm(
            client, prefix, preamble,
            build_jobs(records, args.chunk, arm, args.cap, schema, prompt_of), key)
        if results[arm]["error"]:
            print(f"    ! {results[arm]['error'][:200]}")

    compare(records, arms, results)
    out = os.path.join(ROOT, "logs", "ab",
                       f"stage{args.stage}_{'_vs_'.join(arms)}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({a: {k: v for k, v in r.items() if k != "rows"}
                   | {"rows": {str(i): row for i, row in r["rows"].items()}}
                   for a, r in results.items()}, fh, indent=2)
    print(f"\n  full per-record output -> {out}\n")


if __name__ == "__main__":
    main()
