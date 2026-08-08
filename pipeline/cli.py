#!/usr/bin/env python3
"""
adpipe — raw Reddit VOC in, launch-ready static ads out.

    ./adpipe ingest  raw_voc.txt        filter + dedupe                (code)
    ./adpipe segment                    discover + assign + evidence   (code + model)
    ./adpipe extract <segment>          skills 07-26, batched + cached (model)
    ./adpipe picc    <segment>          barriers, PICC card, 5 angles  (model)
    ./adpipe concepts <segment>         10 concepts + hooks + layouts  (model)
    ./adpipe brief   <segment>          production briefs              (model)
    ./adpipe qa      <segment>          compliance gate                (code)
    ./adpipe render  <segment>          composite PNGs                 (code)
    ./adpipe run     <segment>          extract -> ... -> render

Every model stage prints a cost estimate and waits for confirmation; pass --yes to
skip the prompt. -p/--project selects the project (default: montisella).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import paths  # noqa: E402
import presets  # noqa: E402
from llm import BatchResult, NO_RETRY_STOP_REASONS  # noqa: E402

SKILLS = os.path.join(ROOT, "skills")

# Skills 07-26 all read the evidence file and write one dimension each.
EXTRACTORS = list(range(7, 27))
EMPTY_EXTRACTION_RETRIES = 3

# Extraction depth presets. Keep these definitions as the single source of truth
# for both the CLI and Studio so the selected label always matches the jobs run.
PRESETS = {
    "fast": [7, 8, 9, 12, 14, 16, 18, 19, 20, 24],
    "standard": [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                 21, 22, 24, 25],
    "deep": EXTRACTORS,
}
SKILL_TITLES = {
    7: "pain points", 8: "pain moments", 9: "desired outcomes",
    10: "emotional states", 11: "psychological drivers", 12: "beliefs",
    13: "limiting beliefs", 14: "failed solutions", 15: "assumed solutions",
    16: "buying triggers", 17: "buying criteria", 18: "objections",
    19: "mechanisms", 20: "desired proof", 21: "product mentions",
    22: "competitor mentions", 23: "offers", 24: "representative VOC",
    25: "terminology", 26: "slang",
}


def chosen_extractors(args):
    """--skills wins; else the requested preset; else all 20 extractors."""
    if getattr(args, "skills", None):
        want = []
        for tok in args.skills.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if not tok.isdigit() or int(tok) not in EXTRACTORS:
                sys.exit(f"--skills: {tok!r} is not an extractor (07-26)")
            want.append(int(tok))
        return sorted(set(want))
    return PRESETS[getattr(args, "preset", None) or "deep"]


# ------------------------------------------------------------------ project

def load_project(name):
    p = os.path.join(ROOT, "projects", name, "project.json")
    if not os.path.exists(p):
        avail = os.listdir(os.path.join(ROOT, "projects"))
        sys.exit(f"No project {name!r}. Available: {', '.join(sorted(avail))}")
    cfg = json.load(open(p, encoding="utf-8"))
    cfg["_dir"] = os.path.dirname(p)
    # Fold a flat project into research/products/assets the first time it is
    # touched, so an old checkout keeps working without a manual step.
    moved = paths.migrate(name)
    if moved:
        print(f"  [{name}] layout updated: " + ", ".join(moved))
    return cfg


def pdir(cfg, *parts):
    d = os.path.join(cfg["_dir"], *parts)
    os.makedirs(d if not os.path.splitext(d)[1] else os.path.dirname(d), exist_ok=True)
    return d


def provenance_path(cfg):
    return paths.evidence(cfg["_dir"], "_provenance.json")


def read_provenance(cfg):
    p = provenance_path(cfg)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def record_provenance(cfg, segment, origin, detail):
    """Every evidence file records where it came from. Without this an imported
    file is indistinguishable from one the pipeline produced, and downstream
    output silently inherits an unknown lineage."""
    prov = read_provenance(cfg)
    prov[segment] = {"origin": origin, "detail": detail,
                     "recorded_at": datetime.datetime.now().isoformat(timespec="seconds")}
    os.makedirs(os.path.dirname(provenance_path(cfg)), exist_ok=True)
    json.dump(prov, open(provenance_path(cfg), "w", encoding="utf-8"), indent=2)


def warn_if_imported(cfg, segment):
    entry = read_provenance(cfg).get(segment)
    if entry is None:
        print(f"  ! {segment}: UNKNOWN PROVENANCE — this evidence file was not "
              f"produced by an ingest/segment run in this project.")
        print(f"    Anything built on it inherits that. Run ingest + segment, or "
              f"accept it knowingly.")
    elif entry["origin"] == "imported":
        print(f"  ! {segment}: IMPORTED ({entry['detail']}) — not produced by this "
              f"project's pipeline.")


def skill(n):
    """Read skill NN_*.md — the instruction for one pipeline stage."""
    for f in sorted(os.listdir(SKILLS)):
        if f.startswith(f"{n:02d}_") and f.endswith(".md"):
            with open(os.path.join(SKILLS, f), encoding="utf-8") as fh:
                return f[:-3], fh.read()
    sys.exit(f"Skill {n:02d} not found in {SKILLS}")


def client(cfg, args):
    """Pick the backend. --provider/--model win, then project.json, then Anthropic."""
    m = cfg.get("model", {})
    provider = (getattr(args, "provider", None) or m.get("provider") or "anthropic").lower()
    effort = args.effort or m.get("effort", "high")
    name = getattr(args, "model", None) or m.get("id")
    if provider == "openrouter":
        import openrouter
        return openrouter.Client(model=name or openrouter.DEFAULT_MODEL, effort=effort)
    from llm import Client
    return Client(model=name or "claude-opus-5", effort=effort)


PREAMBLE = (
    "You are running one stage of a voice-of-customer research pipeline for a "
    "direct-response advertising team.\n\n"
    "The document below is the SEGMENT EVIDENCE FILE: real, verbatim customer "
    "comments with source URLs and assignment metadata. It is the only permitted "
    "source of customer truth.\n\n"
    "Absolute rules:\n"
    "- Never invent a quote, a count, a prevalence figure, or a customer claim.\n"
    "- Every quote you output must appear verbatim in the evidence file below.\n"
    "- Every count you state must be one you actually derived from these items.\n"
    "- If the evidence does not support a field, write 'insufficient evidence' "
    "rather than filling it in plausibly.\n"
)


# ------------------------------------------------------------------ 01-02

POST_HDR = re.compile(r"^\s*(?:POST\s+)?URL:\s*(\S+)", re.I | re.M)
TITLE_HDR = re.compile(r"^\s*TITLE:\s*(.+)$", re.I | re.M)
# Reddit dumps commonly number comments "[1] ", "[2] " — optionally with a
# classification tag the previous pipeline stage added.
COMMENT_MARK = re.compile(r"^\s*\[\d+\]\s*(?:\[[A-Z_]+\]\s*)?", re.M)


def parse_voc(raw):
    """Split a raw dump into individual comments, carrying post URL/title onto each.

    One evidence item must be one comment. If whole threads come through as single
    items, every prevalence count downstream is inflated by thread length and the
    segment reports become meaningless — so try a structured parse first and only
    fall back to blank-line blocks when there is no post structure to find.
    """
    posts = []
    marks = list(POST_HDR.finditer(raw))
    if marks:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            chunk = raw[m.start():end]
            t = TITLE_HDR.search(chunk)
            posts.append({"url": m.group(1),
                          "title": (t.group(1).strip() if t else ""),
                          "body": chunk})
    else:
        posts = [{"url": "", "title": "", "body": raw}]

    items = []
    for p in posts:
        body = p["body"]
        # Drop the header lines so they don't land inside the first comment.
        body = POST_HDR.sub("", body)
        body = TITLE_HDR.sub("", body)
        body = re.sub(r"^\s*(SUBREDDIT|KEPT COMMENTS/REPLIES|COMMENTS?):.*$", "",
                      body, flags=re.I | re.M)
        parts = COMMENT_MARK.split(body) if COMMENT_MARK.search(body) else \
            re.split(r"\n\s*\n", body)
        for part in parts:
            s = part.strip(" \t\n=-_")
            if s:
                items.append({"text": s, "url": p["url"], "title": p["title"]})
    return items


# Skill 01 defines a CLOSED vocabulary of reason codes; the schema used to accept
# any string, so the model was free to compose prose instead of classifying. That
# cost output tokens, invited deeper reasoning for what is a labelling job, and
# left the reasons unaggregatable downstream. Mirrored from
# skills/01_filter_voc.md § Reason codes — tests/test_filter_budget.py fails if
# these two lists ever drift apart.
REJECTION_REASONS = [
    "interface_chrome", "bot_boilerplate", "empty_record", "malformed_record",
    "spam", "affiliate_promotion", "self_promotion", "link_only",
    "generic_acknowledgement", "reaction_only", "joke_without_signal",
    "insult_without_signal", "off_topic", "exact_duplicate",
    "quotation_without_new_evidence", "insufficient_information",
]
RETENTION_REASONS = [
    "first_person_experience", "third_person_observation", "specific_problem",
    "specific_context", "attempted_solution", "product_experience",
    "competitor_experience", "outcome", "belief", "objection", "buying_trigger",
    "buying_criterion", "desired_proof", "offer_response", "customer_terminology",
    "customer_slang", "comparison", "workaround", "emotional_signal",
]

FILTER_SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer"},
            "decision": {"type": "string", "enum": ["retain", "reject"]},
            "retention_reasons": {"type": "array",
                                  "items": {"type": "string",
                                            "enum": RETENTION_REASONS}},
            "rejection_reasons": {"type": "array",
                                  "items": {"type": "string",
                                            "enum": REJECTION_REASONS}},
        },
        "required": ["evidence_id", "decision", "retention_reasons", "rejection_reasons"],
        "additionalProperties": False}}},
    "required": ["records"], "additionalProperties": False,
}

DEDUP_SCHEMA = {
    "type": "object",
    "properties": {"groups": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "canonical_id": {"type": "integer"},
            "duplicate_ids": {"type": "array", "items": {"type": "integer"}},
            "duplicate_type": {"type": "string",
                               "enum": ["exact_duplicate", "quoted_duplicate",
                                        "cross_post_duplicate", "semantic_duplicate"]},
            "rationale": {"type": "string"},
        },
        "required": ["canonical_id", "duplicate_ids", "duplicate_type", "rationale"],
        "additionalProperties": False}}},
    "required": ["groups"], "additionalProperties": False,
}


class BatchOutputError(RuntimeError):
    """A paid model batch completed but did not satisfy its data contract."""


# Tokens to reserve for reasoning in a per-record budget. `max_tokens` caps
# reasoning AND the answer together, so a budget sized only for the answer hands
# the whole margin to reasoning — which is exactly how skill 01 came back empty.
REASONING_RESERVE = 2000


def _record_max_tokens(chunk, per_record_tokens, reserve=REASONING_RESERVE):
    """Size a per-record batch's output budget from its schema, not by eye.

    answer + reasoning reserve + 15% margin. Reserving the answer's room FIRST is
    what makes the failure structural rather than probabilistic: whatever the
    model spends thinking, the tokens needed to write one verdict per record were
    never the model's to spend. `pipeline/profile_filter.py` prints the same
    arithmetic against a real corpus.
    """
    return int((chunk * per_record_tokens + reserve) * 1.15)


def _json_object(text):
    """Read a JSON object out of a model reply.

    Delegates to presets.extract_json — the hardened parser already used by the
    single-call stages. It strips code fences, tolerates prose around the object,
    scans to the object's real closing brace rather than the first stray '}', and
    reports a truncated reply AS truncated instead of blaming a comma at the
    character where the output happened to stop. Sharing it is what keeps the
    batched stages and the single-call stages agreeing on what valid output is.
    """
    try:
        obj = presets.extract_json(text)
    except presets.PresetError as e:
        raise ValueError(str(e)) from e
    if not isinstance(obj, dict):
        raise ValueError("top-level response is not an object")
    return obj


def _schema_issue(value, schema, path="$"):
    """Return the first useful JSON-schema error for our pipeline schemas.

    Providers should enforce these schemas, but this local check catches a route
    that ignored response_format and lets the recovery pass repair it safely.
    """
    if not schema:
        return None
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(kind, True)
    if not valid:
        return f"{path} must be {kind}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not an allowed value"
    if kind == "object":
        for name in schema.get("required", []):
            if name not in value:
                return f"{path}.{name} is required"
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                return f"{path} has unexpected field {sorted(extra)[0]!r}"
        for name, child in value.items():
            if name in props:
                issue = _schema_issue(child, props[name], f"{path}.{name}")
                if issue:
                    return issue
    elif kind == "array" and "items" in schema:
        for n, child in enumerate(value):
            issue = _schema_issue(child, schema["items"], f"{path}[{n}]")
            if issue:
                return issue
    return None


def _coverage_issue(rows, expected_ids):
    """Describe missing, unknown, or repeated evidence IDs, if any."""
    got = [r.get("evidence_id") for r in rows if isinstance(r, dict)]
    counts = Counter(got)
    missing = sorted(set(expected_ids) - set(got))
    extra = sorted(set(got) - set(expected_ids), key=str)
    duplicate = sorted((i for i, n in counts.items() if n > 1), key=str)
    bits = []
    if missing:
        bits.append(f"{len(missing)} missing")
    if extra:
        bits.append(f"{len(extra)} unknown")
    if duplicate:
        bits.append(f"{len(duplicate)} duplicated")
    if len(got) != len(rows):
        bits.append("non-object rows")
    return ", ".join(bits) if bits else None


def _decode_job_rows(job, raw, key):
    payload = _json_object(raw)
    issue = _schema_issue(payload, job.schema)
    if issue:
        raise ValueError(issue)
    try:
        rows = payload[key]
    except KeyError as e:
        raise ValueError(f"missing top-level {key!r}") from e
    if not isinstance(rows, list):
        raise ValueError(f"{key!r} is not a list")
    if job.expected_ids is not None:
        issue = _coverage_issue(rows, job.expected_ids)
        if issue:
            raise ValueError(f"incomplete record coverage ({issue})")
    return rows


def _raw_text(value):
    if isinstance(value, BatchResult):
        return value.text
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _as_result(value):
    """Normalise whatever a backend returned into a BatchResult.

    Backends hand back a BatchResult; tests and older call sites hand back a
    bare string. A bare string carries no stop reason, so it can only be judged
    on its content — which is exactly the blind spot this wrapper exists to make
    visible everywhere else.
    """
    if isinstance(value, BatchResult):
        return value
    return BatchResult(text=_raw_text(value))


# The single-call path (presets.model_json) retries an output-budget failure at
# 3x the budget. Batched stages use the same factor so a stage behaves the same
# whether it ran as one call or eighty.
BUDGET_RETRY_FACTOR = 3


def _failure_reason(result, parse_error):
    """Name the real failure rather than the symptom.

    A provider that stopped at its output budget before writing anything did not
    emit malformed JSON — it emitted nothing. Reporting the parser's
    "Expecting value: line 1 column 1 (char 0)" for that case is what turns a
    solvable budget problem into a hunt for a JSON bug that does not exist.
    """
    stop = result.stop_reason
    if result.out_of_budget:
        where = "before writing any output" if not result.text.strip() else "mid-output"
        return (f"provider stopped at its output token budget {where} "
                f"(stop reason {stop!r}, {len(result.text)} chars returned)")
    if stop in NO_RETRY_STOP_REASONS:
        return f"provider refused the request (stop reason {stop!r})"
    if not result.text.strip():
        detail = f", stop reason {stop!r}" if stop else ""
        return f"provider returned an empty response{detail}"
    return parse_error


def _save_failure(diagnostics_dir, job, result, reason, suffix):
    """Persist a failed response with the metadata needed to diagnose it.

    The stop reason and the response length belong in the file: without them an
    empty reply and a prose reply look identical on disk, which is precisely the
    ambiguity that made this failure hard to read.
    """
    os.makedirs(diagnostics_dir, exist_ok=True)
    with open(os.path.join(diagnostics_dir, f"{job.id}.{suffix}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(f"WHY SAVED: {reason}\n"
                 f"STOP REASON: {result.stop_reason or 'unknown'}\n"
                 f"MAX TOKENS: {job.max_tokens}\n"
                 f"RESPONSE CHARS: {len(result.text)}\n\n{result.text}")


def _rerun_batch(client_, corpus, preamble, failures, factor):
    """Re-issue the ORIGINAL requests with a larger output budget.

    The right recovery when a provider never finished writing: there is no prior
    output worth repairing, and the repair prompt is strictly LONGER than the
    request that already exhausted the budget — so repairing a budget failure at
    the same budget is close to guaranteed to fail the same way.
    """
    from llm import Job

    jobs = [Job(id=job.id, prompt=job.prompt,
                max_tokens=job.max_tokens * factor, schema=job.schema,
                expected_ids=job.expected_ids)
            for job, _result, _reason in failures]
    return client_.batch(corpus, preamble, jobs)


def _repair_batch(client_, corpus, preamble, failures, key):
    """Reformat failed model outputs in one structured recovery batch."""
    from llm import Job

    jobs = []
    for job, raw, reason in failures:
        raw = _raw_text(raw)
        prior = raw if raw.strip() else "[No response was returned. Re-run the request.]"
        prompt = (
            "RECOVERY TASK: The previous model response could not be consumed by "
            f"the pipeline because: {reason}.\n\n"
            "Return JSON only and obey the supplied JSON schema exactly. Preserve "
            "every decision, reason, group, score, cue, and assignment that is "
            "already present in the previous response; this is data-shape repair, "
            "not a chance to reconsider those judgments. Use the original request "
            "only to restore an omitted input row or to re-run the job when no "
            "response was returned. The final top-level array must be named "
            f"{key!r}. Do not add commentary or Markdown.\n\n"
            "ORIGINAL REQUEST:\n\n" + job.prompt +
            "\n\nPREVIOUS RESPONSE:\n\n" + prior)
        jobs.append(Job(id=job.id, prompt=prompt, max_tokens=job.max_tokens,
                        schema=job.schema, expected_ids=job.expected_ids))
    return client_.batch(corpus, preamble, jobs)


def _decode_into(decoded, job, result, key):
    """Decode one reply into `decoded`, or return the reason it could not be.

    Returns None on success. The reason is derived from the provider's stop
    reason first and the parser's complaint only second, so an unfinished reply
    is never reported as a JSON syntax error.
    """
    try:
        decoded[job.id] = _decode_job_rows(job, result.text, key)
        return None
    except (ValueError, TypeError) as e:
        return _failure_reason(result, str(e))


def _batch_rows(results, jobs, key, diagnostics_dir, repair=None, rerun=None):
    """Decode all jobs, recovering failed responses before failing the stage.

    Two failure modes need two different recoveries, and conflating them is what
    turns one bad batch into a dead stage:

      the model never finished writing  — an output-budget stop, an empty reply,
        or a transport failure. There is no output to repair, so `rerun`
        re-issues the ORIGINAL request with a larger budget.
      the model wrote the wrong shape   — text came back but it isn't the JSON
        the contract asked for. `repair` hands the model its own output back and
        asks for the shape to be fixed, which preserves the judgements already made.

    A refusal is neither: it is a decision, and re-asking only spends money to be
    refused again, so it goes straight to the error.

    `rerun` receives (job, BatchResult, reason) tuples plus a budget multiplier;
    `repair` receives (job, raw_text, reason) tuples. Both return replacement
    responses keyed by job id. Every response along the way — original, re-run
    and repaired — is written to `diagnostics_dir`; nothing is silently discarded.
    """
    by_id = {j.id: j for j in jobs}
    failures, decoded = [], {}
    # Keep how each job FIRST failed. Recovery replaces the carried reply with
    # whatever the retry produced, so without this the final report would
    # classify a job by its re-run's empty reply and lose the budget stop that
    # is the thing the operator actually has to fix.
    first_result = {}
    for jid, job in sorted(by_id.items()):
        result = _as_result(results.get(jid, ""))
        reason = ("no response returned" if jid not in results
                  else _decode_into(decoded, job, result, key))
        if reason:
            first_result[jid] = result
            failures.append((job, result, reason))

    unexpected = sorted(set(results) - set(by_id))
    for job, result, reason in failures:
        _save_failure(diagnostics_dir, job, result, reason, "original")

    # ---- unfinished replies: re-run the original request with more room ------
    if failures and rerun is not None:
        starved = [f for f in failures if f[1].retryable]
        if starved:
            print(f"  ! {len(starved)}/{len(jobs)} {key} response(s) never finished "
                  f"(output budget, empty or failed request) — re-running those "
                  f"requests at {BUDGET_RETRY_FACTOR}x the token budget")
            replies = rerun(starved, BUDGET_RETRY_FACTOR)
            recovered, still_bad = set(), []
            for job, result, original_reason in starved:
                retried = _as_result(replies.get(job.id, ""))
                _save_failure(diagnostics_dir, job, retried,
                              f"re-run of: {original_reason}", "rerun")
                if job.id not in replies:
                    still_bad.append((job, result,
                                      f"{original_reason}; re-run returned no response"))
                    continue
                reason = _decode_into(decoded, job, retried, key)
                if reason:
                    # Carry the re-run's reply forward: it, not the empty
                    # original, is what a shape repair has to work from.
                    still_bad.append((job, retried,
                                      f"{original_reason}; re-run: {reason}"))
                else:
                    recovered.add(job.id)
            print(f"    {len(recovered)}/{len(starved)} recovered on re-run")
            failures = [f for f in failures if not f[1].retryable] + still_bad

    # ---- wrong-shape replies: ask the model to fix the shape ----------------
    if failures and repair is not None:
        fixable = [f for f in failures
                   if f[1].stop_reason not in NO_RETRY_STOP_REASONS]
        if fixable:
            print(f"  ! repairing {len(fixable)} malformed {key} response(s) "
                  "in one additional structured batch")
            repaired = repair([(job, result.text, reason)
                               for job, result, reason in fixable])
            still_bad = []
            for job, result, original_reason in fixable:
                fixed = _as_result(repaired.get(job.id, ""))
                _save_failure(diagnostics_dir, job, fixed,
                              f"repair of: {original_reason}", "repaired")
                if job.id not in repaired:
                    still_bad.append((job, result,
                                      f"original: {original_reason}; "
                                      "repair: repair returned no response"))
                    continue
                reason = _decode_into(decoded, job, fixed, key)
                if reason:
                    still_bad.append((job, result,
                                      f"original: {original_reason}; repair: {reason}"))
            failures = [f for f in failures
                        if f[1].stop_reason in NO_RETRY_STOP_REASONS] + still_bad

    if unexpected:
        os.makedirs(diagnostics_dir, exist_ok=True)
        for jid in unexpected:
            with open(os.path.join(diagnostics_dir, f"{jid}.unexpected.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(_raw_text(results[jid]))

    if failures or unexpected:
        descriptions = [(job.id, reason) for job, _result, reason in failures]
        descriptions.extend((jid, "unexpected response id") for jid in unexpected)
        sample = "; ".join(f"{jid}: {reason}" for jid, reason in descriptions[:3])
        # Lead with the dominant mode. "Violated the JSON contract" is true of
        # every failure here and actionable for none of them; "ran out of output
        # budget" tells the operator to raise max_tokens or pick another model.
        modes = Counter(
            "output budget exhausted" if first.out_of_budget else
            "refused" if first.stop_reason in NO_RETRY_STOP_REASONS else
            "empty response" if not first.text.strip() else "malformed JSON"
            for first in (first_result[job.id] for job, _r, _reason in failures))
        if unexpected:
            modes["unexpected response id"] += len(unexpected)
        breakdown = ", ".join(f"{n} {name}" for name, n in modes.most_common())
        raise BatchOutputError(
            f"{len(descriptions)}/{len(jobs)} batch response(s) could not be used "
            f"after recovery — {breakdown} ({sample}). Original, re-run and repaired "
            f"responses were saved to {diagnostics_dir}. No stage output was written.")
    return [row for jid in sorted(decoded) for row in decoded[jid]]


def _require_exact_ids(rows, expected_ids, label):
    """Ensure a per-record model stage judged every input exactly once."""
    issue = _coverage_issue(rows, expected_ids)
    if issue:
        raise BatchOutputError(
            f"{label} returned incomplete record coverage ({issue}). "
            "No stage output was written.")

# Interface chrome. Skill 01 explicitly permits stripping junk AROUND a comment,
# so removing these deterministically costs nothing and never touches customer words.
BOILER = re.compile(
    r"welcome to reddit|become a redditor|create an account|sign up|log in|"
    r"this is an archived post|i am a bot|automoderator|permalinkembedsave|"
    r"sharesavehidereport|privacy policy|user agreement|content policy|"
    r"submission guidelines|weekly thread|link to wiki", re.I)


def cmd_ingest(cfg, args):
    """Skills 01 (filter) then 02 (deduplicate).

    Both are judgement skills, not regex. Skill 01 wants a retention or rejection
    REASON on every record; skill 02 detects four duplicate types, of which a hash
    only catches one — and warns that the cardinal sin is the false merge, treating
    two different people describing the same pain as one voice.

    A deterministic pre-pass removes interface chrome and byte-identical captures
    first (both explicitly skill 01's job and free), so the model only judges what
    actually needs judging. --rules-only stops there and skips the model entirely.
    """
    from llm import Job, confirm

    with open(args.source, encoding="utf-8", errors="ignore") as fh:
        raw = fh.read()
    blocks = parse_voc(raw)
    voc = paths.voc(cfg["_dir"]); os.makedirs(voc, exist_ok=True)

    # ---- deterministic pre-pass: chrome + byte-identical captures ------------
    pre, seen, dropped = [], {}, Counter()
    for item in blocks:
        text = re.sub(r"\s+", " ", item["text"]).strip()
        if len(text.split()) < cfg["filter"].get("min_words", 8):
            dropped["too_short"] += 1; continue
        if BOILER.search(text):
            dropped["interface_chrome"] += 1; continue
        h = hashlib.sha256(text.lower().encode()).hexdigest()
        if h in seen:
            dropped["exact_duplicate"] += 1; continue
        seen[h] = True
        pre.append({"id": len(pre) + 1, "text": text,
                    "url": item.get("url", ""), "title": item.get("title", "")})

    print(f"  parsed {len(blocks):,} records")
    for k, n in dropped.most_common():
        print(f"    pre-pass dropped {k:18} {n:,}")
    print(f"  {len(pre):,} records to judge")
    if not pre:
        sys.exit("  Nothing survived the pre-pass — check the input format.")

    if args.rules_only:
        _write_jsonl(os.path.join(voc, "filtered_voc.jsonl"), pre)
        print(f"\n  --rules-only: skipped skills 01/02 (no model judgement applied)")
        print(f"  -> {voc}/filtered_voc.jsonl")
        return

    c = client(cfg, args)
    ctx = (f"MARKET CONTEXT: {cfg.get('product', '')} — {cfg.get('market', '')}\n")

    # ---- skill 01 -----------------------------------------------------------
    _, s01 = skill(1)
    CHUNK = 60
    chunks = [pre[i:i + CHUNK] for i in range(0, len(pre), CHUNK)]
    jobs = [Job(id=f"f{n:04d}",
                prompt=("Apply skill 01 to each record below. Decide retain or reject "
                        "and give the reason(s). Keep concrete first-person experience "
                        "by default. Never rewrite, tidy or correct customer words — "
                        "you are only deciding what gets in. Return JSON only as "
                        "{\"records\":[...]}, with exactly one object per input record "
                        "and the numeric [id] copied to evidence_id. Leave the unused "
                        "reason array empty. Reasons must be codes from skill 01's "
                        "Reason codes list, verbatim — classify against that list, "
                        "don't write prose.\n\nRECORDS:\n\n"
                        + "\n\n".join(f"[{r['id']}] {r['text']}" for r in ch)),
                # ~40 tokens covers a worst-case verdict: an id, a decision and
                # two of the longest reason codes skill 01 defines.
                max_tokens=_record_max_tokens(len(ch), 40), schema=FILTER_SCHEMA,
                expected_ids=tuple(r["id"] for r in ch),
                # Skill 01 is the bouncer at the door: retain or reject, with
                # reasons drawn from a closed list. It is judgement, but it is
                # not deep reasoning, and paying synthesis-depth reasoning 83
                # times over is what left no budget to answer with.
                effort="low")
            for n, ch in enumerate(chunks)]

    prefix = f"{s01}\n\n---\n\n{ctx}"
    print(f"\n  01 filter: {len(pre):,} records in {len(jobs)} batched chunks")
    if not confirm(c.estimate(prefix, PREAMBLE, jobs, batched=True), args.yes):
        return
    c.prewarm(prefix, PREAMBLE)
    records = _batch_rows(
        c.batch(prefix, PREAMBLE, jobs), jobs, "records",
        os.path.join(voc, "_model_failures", "01_filter"),
        repair=lambda failed: _repair_batch(c, prefix, PREAMBLE, failed, "records"),
        rerun=lambda failed, factor: _rerun_batch(c, prefix, PREAMBLE, failed, factor))
    _require_exact_ids(records, {r["id"] for r in pre}, "skill 01 filter")
    verdicts = {r["evidence_id"]: r for r in records}

    retained = [{**r, **verdicts[r["id"]]} for r in pre
                if verdicts.get(r["id"], {}).get("decision") == "retain"]
    rejected = [{**r, **verdicts[r["id"]]} for r in pre
                if verdicts.get(r["id"], {}).get("decision") == "reject"]
    _write_jsonl(os.path.join(voc, "retained_voc.jsonl"), retained)
    _write_jsonl(os.path.join(voc, "rejected_voc.jsonl"), rejected)
    print(f"  01 filter: {len(retained):,} retained · {len(rejected):,} rejected")

    # ---- skill 02 -----------------------------------------------------------
    _, s02 = skill(2)
    DCHUNK = 80
    dchunks = [retained[i:i + DCHUNK] for i in range(0, len(retained), DCHUNK)]
    djobs = [Job(id=f"d{n:04d}",
                 prompt=("Apply skill 02 to the records below. Group only genuine "
                         "duplicates — same experience from the same source. Ten "
                         "different people describing the same pain is ten data "
                         "points, not one; keyword overlap is never enough. When "
                         "unsure, do not merge. Return only groups you are "
                         "confident in. Return JSON only as {\"groups\":[...]}; an "
                         "empty groups list is a valid answer.\n\n"
                         "RECORDS:\n\n"
                         + "\n\n".join(f"[{r['id']}] {r['text']}" for r in ch)),
                 max_tokens=6000, schema=DEDUP_SCHEMA)
             for n, ch in enumerate(dchunks)]

    dprefix = f"{s02}\n\n---\n\n{ctx}"
    print(f"\n  02 deduplicate: {len(retained):,} records in {len(djobs)} chunks")
    if not confirm(c.estimate(dprefix, PREAMBLE, djobs, batched=True), args.yes):
        return
    c.prewarm(dprefix, PREAMBLE)
    drop = set()
    groups = _batch_rows(
        c.batch(dprefix, PREAMBLE, djobs), djobs, "groups",
        os.path.join(voc, "_model_failures", "02_deduplicate"),
        repair=lambda failed: _repair_batch(c, dprefix, PREAMBLE, failed, "groups"),
        rerun=lambda failed, factor: _rerun_batch(c, dprefix, PREAMBLE, failed, factor))
    for g in groups:
        drop.update(i for i in g["duplicate_ids"] if i != g["canonical_id"])

    deduped = [r for r in retained if r["id"] not in drop]
    _write_jsonl(os.path.join(voc, "deduplicated_voc.jsonl"), deduped)
    # The segment stage reads filtered_voc.jsonl — keep that name as the contract.
    _write_jsonl(os.path.join(voc, "filtered_voc.jsonl"), deduped)
    with open(os.path.join(voc, "duplicate_groups.jsonl"), "w", encoding="utf-8") as fh:
        for g in groups:
            fh.write(json.dumps(g) + "\n")

    types = Counter(g["duplicate_type"] for g in groups)
    print(f"  02 deduplicate: {len(drop):,} merged away "
          + ("(" + " · ".join(f"{k} {v}" for k, v in types.most_common()) + ")"
             if types else ""))
    print(f"\n  {len(deduped):,} deduplicated evidence items -> {voc}/deduplicated_voc.jsonl")
    print(f"  (also written as filtered_voc.jsonl — the input the segment stage reads)")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------------------ 03-06

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "name": {"type": "string"},
            "definition": {"type": "string"},
            "inclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "exclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "scores": {
                "type": "object",
                "properties": {
                    "evidence_volume": {"type": "integer"},
                    "context_distinctiveness": {"type": "integer"},
                    "pain_distinctiveness": {"type": "integer"},
                    "desired_outcome_distinctiveness": {"type": "integer"},
                    "messaging_distinctiveness": {"type": "integer"},
                    "commercial_actionability": {"type": "integer"}},
                "required": ["evidence_volume", "context_distinctiveness",
                             "pain_distinctiveness", "desired_outcome_distinctiveness",
                             "messaging_distinctiveness", "commercial_actionability"],
                "additionalProperties": False},
            "score_total": {"type": "integer"},
            "confidence": {"type": "string",
                           "enum": ["Validated", "Probable", "Emerging", "Weak", "Rejected"]},
            "representative_evidence_ids": {"type": "array", "items": {"type": "integer"}},
            "merged_aliases": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slug", "name", "definition", "inclusion_criteria",
                     "exclusion_criteria", "scores", "score_total", "confidence",
                     "representative_evidence_ids", "merged_aliases"],
        "additionalProperties": False}}},
    "required": ["candidates"], "additionalProperties": False,
}

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["validated", "merged", "split_required",
                                "needs_more_research", "rejected"]},
            "rationale": {"type": "string"},
            "merged_into": {"type": "string"},
            "split_into": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slug", "status", "rationale", "merged_into", "split_into"],
        "additionalProperties": False}}},
    "required": ["decisions"], "additionalProperties": False,
}

ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {"assignments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer"},
            "primary_segment_id": {"type": "string"},
            "score": {"type": "integer"},
            "winning_margin": {"type": "integer"},
            "cue_types": {"type": "array", "items": {"type": "string"}},
            "primary_cues": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
            "assignment_status": {"type": "string",
                                  "enum": ["assigned", "unassigned_ambiguous",
                                           "unassigned_insufficient_evidence",
                                           "unassigned_no_matching_segment"]},
            "secondary_attributes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["evidence_id", "primary_segment_id", "score", "winning_margin",
                     "cue_types", "primary_cues", "rationale", "assignment_status",
                     "secondary_attributes"],
        "additionalProperties": False}}},
    "required": ["assignments"], "additionalProperties": False,
}


def _corpus_text(items, limit=None):
    rows = items if limit is None else items[:limit]
    return "\n\n".join(f"[{i['id']}] {i['text']}" for i in rows)


def cmd_segment(cfg, args):
    """Skills 03 -> 04 -> 05 -> 06, each writing its contract file.

    All four are model stages. Skill 05 in particular CANNOT be done with keyword
    matching: its own rubric scores `incidental_keyword` at 0 and lists
    "assignment rested on keyword overlap alone" as a failed run. Every item is
    judged against every validated segment by cue TYPE, with the >=6 score and
    >=2 margin thresholds enforced, and unassigned recorded as a valid outcome.
    """
    from llm import Job, confirm

    src = (getattr(args, "source", None)
           or paths.voc(cfg["_dir"], "filtered_voc.jsonl"))
    if not os.path.exists(src):
        sys.exit(f"No VOC source at {src}. Run: adpipe -p {cfg['name']} ingest <raw.txt>")
    items = [json.loads(l) for l in open(src, encoding="utf-8")]
    by_id = {i["id"]: i for i in items}
    voc = paths.voc(cfg["_dir"]); os.makedirs(voc, exist_ok=True)
    c = client(cfg, args)
    corpus = _corpus_text(items)
    print(f"  {len(items):,} deduplicated evidence items")

    # ---------------------------------------------------------------- skill 03
    cand_p = os.path.join(voc, "candidate_segments.json")
    if os.path.exists(cand_p) and not args.rediscover:
        candidates = json.load(open(cand_p, encoding="utf-8"))
        print(f"  03 discover: reusing {len(candidates)} candidates (--rediscover to redo)")
    else:
        _, s03 = skill(3)
        prompt = (f"{s03}\n\n---\n\nApply skill 03 to the complete corpus above. "
                  "Score every candidate 0-5 on all six dimensions and set "
                  "score_total to their sum. Assign confidence honestly — a "
                  "candidate needs score_total >= 18 to be Validated-grade. Give "
                  "inclusion AND exclusion criteria for each, and cite "
                  "representative_evidence_ids using the [n] numbers shown. "
                  "Use merged_aliases (possibly empty) to record any candidates you "
                  "merged. Never build a segment from a single comment.")
        if not confirm(c.estimate(corpus, PREAMBLE,
                                  [Job(id="03", prompt=prompt, max_tokens=16000)]),
                       args.yes):
            return
        candidates = json.loads(
            c.one(corpus, PREAMBLE, prompt, 16000, CANDIDATE_SCHEMA))["candidates"]
        json.dump(candidates, open(cand_p, "w", encoding="utf-8"), indent=2)
        print(f"  03 discover: {len(candidates)} candidates -> {cand_p}")

    # ---------------------------------------------------------------- skill 04
    val_p = os.path.join(voc, "validated_segments.json")
    if os.path.exists(val_p) and not args.rediscover:
        decisions = json.load(open(val_p, encoding="utf-8"))
        print("  04 validate: reusing decisions")
    else:
        _, s04 = skill(4)
        prompt = (f"{s04}\n\n---\n\nHere are the discovered candidates:\n\n"
                  f"{json.dumps(candidates, indent=2)}\n\n"
                  "Apply skill 04. Give EVERY candidate one of the five decisions "
                  "with a written rationale. Default to doubt. Volume is one input "
                  "among nine — never the only one. Watch for a candidate whose "
                  "evidence all traces to a single thread; that is a conversation, "
                  "not an audience. Leave merged_into as \"\" and split_into as [] "
                  "unless the decision is merged or split_required.")
        if not confirm(c.estimate(corpus, PREAMBLE,
                                  [Job(id="04", prompt=prompt, max_tokens=12000)]),
                       args.yes):
            return
        decisions = json.loads(
            c.one(corpus, PREAMBLE, prompt, 12000, VALIDATION_SCHEMA))["decisions"]
        json.dump(decisions, open(val_p, "w", encoding="utf-8"), indent=2)
        tally = Counter(d["status"] for d in decisions)
        print(f"  04 validate: " + " · ".join(f"{k} {v}" for k, v in tally.most_common())
              + f" -> {val_p}")

    by_slug = {c_["slug"]: c_ for c_ in candidates}
    validated = [by_slug[d["slug"]] for d in decisions
                 if d["status"] == "validated" and d["slug"] in by_slug]
    if not validated:
        sys.exit("  No segment survived validation. Nothing to build.\n"
                 f"  Review {val_p} — every candidate has a written rationale.")
    print(f"  {len(validated)} validated segment(s): "
          + ", ".join(s["slug"] for s in validated))

    # ---------------------------------------------------------------- skill 05
    asg_p = os.path.join(voc, "segment_assignments.jsonl")
    if os.path.exists(asg_p) and not args.reassign:
        rows = [json.loads(l) for l in open(asg_p, encoding="utf-8")]
        print(f"  05 assign: reusing {len(rows):,} assignments (--reassign to redo)")
    else:
        _, s05 = skill(5)
        # The validated segments are the same for every chunk, so they go in the
        # cached prefix; only the evidence chunk varies.
        seg_defs = json.dumps(
            [{k: s[k] for k in ("slug", "name", "definition",
                                "inclusion_criteria", "exclusion_criteria")}
             for s in validated], indent=2)
        prefix = (f"{s05}\n\n---\n\nTHE VALIDATED SEGMENTS:\n\n{seg_defs}\n")

        CHUNK = 40
        chunks = [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)]
        jobs = [Job(id=f"chunk{n:03d}",
                    prompt=("Assign each evidence item below. Score by CUE TYPE using "
                            "skill 05's rubric — explicit_self_identification 5, "
                            "dominant_context_match 5, segment_specific_problem 4, "
                            "segment_specific_constraint 3, "
                            "segment_specific_failed_solution 3, incidental_keyword 0. "
                            "A passing keyword mention on its own earns NOTHING. "
                            "Assign only when the winning segment scores >= 6 AND beats "
                            "the runner-up by >= 2; otherwise use the matching "
                            "unassigned_* status and set primary_segment_id to \"\". "
                            "Unassigned is a valid, correct outcome — never force a fit. "
                            "Record secondary_attributes separately; they must not "
                            "influence the primary choice. Return JSON only as "
                            "{\"assignments\":[...]}, with exactly one object per input "
                            "and the numeric [id] copied to evidence_id.\n\nEVIDENCE:\n\n"
                            + "\n\n".join(f"[{i['id']}] {i['text']}" for i in ch)),
                    max_tokens=16000, schema=ASSIGN_SCHEMA,
                    expected_ids=tuple(i["id"] for i in ch))
                for n, ch in enumerate(chunks)]

        print(f"  05 assign: {len(items):,} items in {len(jobs)} batched chunks")
        if not confirm(c.estimate(prefix, PREAMBLE, jobs, batched=True), args.yes):
            return
        c.prewarm(prefix, PREAMBLE)
        results = c.batch(prefix, PREAMBLE, jobs)

        rows = _batch_rows(
            results, jobs, "assignments",
            os.path.join(voc, "_model_failures", "05_assign"),
            repair=lambda failed: _repair_batch(
                c, prefix, PREAMBLE, failed, "assignments"),
            rerun=lambda failed, factor: _rerun_batch(
                c, prefix, PREAMBLE, failed, factor))
        _require_exact_ids(rows, {i["id"] for i in items}, "skill 05 assignment")
        with open(asg_p, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        tally = Counter(r["assignment_status"] for r in rows)
        print("  05 assign: " + " · ".join(f"{k} {v}" for k, v in tally.most_common()))

    # ---------------------------------------------------------------- skill 06
    build_evidence_files(cfg, validated, rows, by_id, voc)


def build_evidence_files(cfg, validated, rows, by_id, voc):
    """Skill 06 — pure assembly, deterministic, plus the audit set.

    The skill file is loaded and written beside the output as the build contract:
    06 is the one stage whose rules are wholly mechanical (join, group, sort, emit,
    audit), so it is implemented in code rather than sent to the model — but the
    spec it implements travels with the artefacts it produced.

    One evidence item -> exactly one primary segment, or unassigned. Never two
    files. An item carrying more than one active assignment fails the build.
    """
    ev = paths.evidence(cfg["_dir"]); os.makedirs(ev, exist_ok=True)
    _, s06 = skill(6)
    open(os.path.join(voc, "06_build_contract.md"), "w", encoding="utf-8").write(s06)
    seen, conflicts, missing = {}, [], []
    grouped = defaultdict(list)
    unassigned = []
    valid_slugs = {s["slug"] for s in validated}

    for r in rows:
        eid = r["evidence_id"]
        if eid not in by_id:
            missing.append(r); continue
        if r["assignment_status"] != "assigned":
            unassigned.append(r); continue
        if eid in seen and seen[eid] != r["primary_segment_id"]:
            conflicts.append({"evidence_id": eid,
                              "assignments": [seen[eid], r["primary_segment_id"]]})
            continue
        if r["primary_segment_id"] not in valid_slugs:
            missing.append({**r, "_reason": "unknown segment"}); continue
        seen[eid] = r["primary_segment_id"]
        grouped[r["primary_segment_id"]].append(r)

    if conflicts:
        p = os.path.join(voc, "assignment_conflicts.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            for x in conflicts:
                fh.write(json.dumps(x) + "\n")
        sys.exit(f"  BUILD FAILED — {len(conflicts)} item(s) carry more than one "
                 f"active primary assignment.\n  Audited to {p}")

    report = []
    for s in validated:
        rs = grouped.get(s["slug"], [])
        if not rs:
            print(f"  ! {s['slug']}: zero assigned evidence (validated but empty)")
            report.append((s["slug"], 0, 0))
            continue
        # Deterministic: score desc, then margin desc, then evidence id asc.
        rs.sort(key=lambda r: (-r["score"], -r["winning_margin"], r["evidence_id"]))
        threads = {by_id[r["evidence_id"]].get("url", "") for r in rs}
        p = os.path.join(ev, f"{s['slug']}.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"{s['name'].upper()}\n{'=' * 72}\n\n")
            fh.write(f"Segment ID: {s['slug']}\nSegment slug: {s['slug']}\n")
            fh.write("Validation status: validated\n")
            fh.write(f"Evidence items: {len(rs)}\nUnique threads: {len(threads)}\n\n")
            fh.write(f"SEGMENT DEFINITION\n{'-' * 72}\n{s['definition']}\n\n")
            fh.write("INCLUSION\n" + "".join(f"  - {x}\n" for x in s["inclusion_criteria"]))
            fh.write("EXCLUSION\n" + "".join(f"  - {x}\n" for x in s["exclusion_criteria"]))
            fh.write("\nEach item appears in this segment only once.\n\n")
            fh.write(f"EVIDENCE ITEMS\n{'-' * 72}\n")
            for r in rs:
                it = by_id[r["evidence_id"]]
                fh.write(f"[{r['evidence_id']}] TYPE: comment\n")
                fh.write(f"TITLE: {it.get('title', '')}\nURL: {it.get('url', '')}\n")
                fh.write(f"ASSIGNMENT SCORE: {r['score']}\n")
                fh.write(f"WINNING MARGIN: {r['winning_margin']}\n")
                fh.write(f"PRIMARY CUES: {', '.join(r['primary_cues'])}\n")
                fh.write(f"CUE TYPES: {', '.join(r['cue_types'])}\n")
                fh.write(f"RATIONALE: {r['rationale']}\n")
                fh.write(f"TEXT: {it['text']}\n\n")
        print(f"  {s['slug']:38} {len(rs):>6,} items -> {p}")
        record_provenance(cfg, s["slug"], "pipeline",
                          f"skills 01-06, {len(rs)} assigned items")
        report.append((s["slug"], len(rs), len(threads)))

    # ------------------------------------------------------------- audit set
    with open(os.path.join(voc, "unassigned_evidence.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Unassigned evidence — {len(unassigned)} item(s)\n\n")
        fh.write("Unassigned is a valid outcome. Forcing an ambiguous comment into a "
                 "segment is worse than leaving it out.\n\n")
        for r in unassigned:
            it = by_id.get(r["evidence_id"], {})
            fh.write(f"## [{r['evidence_id']}] {r['assignment_status']}\n")
            fh.write(f"- Rationale: {r['rationale']}\n- URL: {it.get('url', '')}\n\n")
            fh.write(f"{it.get('text', '')}\n\n---\n\n")

    if missing:
        with open(os.path.join(voc, "missing_evidence.jsonl"), "w", encoding="utf-8") as fh:
            for x in missing:
                fh.write(json.dumps(x) + "\n")

    with open(os.path.join(voc, "segment_evidence_manifest.yaml"), "w",
              encoding="utf-8") as fh:
        fh.write("segments:\n")
        for slug, n, t in report:
            fh.write(f"  - slug: {slug}\n    evidence_file: evidence/{slug}.txt\n")
            fh.write(f"    evidence_count: {n}\n    thread_count: {t}\n")
        fh.write(f"unassigned_count: {len(unassigned)}\n")
        fh.write(f"missing_count: {len(missing)}\n")

    total = sum(n for _, n, _ in report)
    print(f"\n  06 build: {total:,} assigned · {len(unassigned):,} unassigned"
          + (f" · {len(missing)} missing" if missing else ""))
    print(f"  audit set -> {voc}/")


# ------------------------------------------------------------------ 07-26

def evidence_path(cfg, segment):
    p = paths.evidence(cfg["_dir"], f"{segment}.txt")
    if not os.path.exists(p):
        p2 = os.path.join(ROOT, "evidence", f"{segment}.txt")
        if os.path.exists(p2):
            return p2
        avail = []
        for d in (paths.evidence(cfg["_dir"]), os.path.join(ROOT, "evidence")):
            if os.path.isdir(d):
                avail += [f[:-4] for f in os.listdir(d) if f.endswith(".txt")]
        sys.exit(f"No evidence file for {segment!r}. Available: {', '.join(sorted(set(avail))) or 'none'}")
    return p


def cmd_extract(cfg, args):
    """Skills 07-26 against one evidence file. The corpus is identical across all
    20, so it goes in a cached system block and only the skill varies — then the
    whole set fans out as one batch at 50%."""
    from llm import Job, confirm
    warn_if_imported(cfg, args.segment)
    c = client(cfg, args)
    with open(evidence_path(cfg, args.segment), encoding="utf-8") as fh:
        corpus = fh.read()
    out = paths.extractions(cfg["_dir"], args.segment); os.makedirs(out, exist_ok=True)

    picked = chosen_extractors(args)
    print(f"  {len(picked)} of {len(EXTRACTORS)} dimensions: "
          + ", ".join(SKILL_TITLES.get(n, str(n)) for n in picked))
    jobs, names = [], {}
    for n in picked:
        name, body = skill(n)
        dest = os.path.join(out, f"{name}.md")
        # A zero-byte/whitespace artefact is a failed extraction, not completed
        # work. Pick it up automatically even without --force.
        present = False
        if os.path.exists(dest):
            try:
                with open(dest, encoding="utf-8") as fh:
                    present = bool(fh.read().strip())
            except OSError:
                present = False
        if present and not args.force:
            continue
        names[name] = dest
        jobs.append(Job(id=name, prompt=(
            f"{body}\n\n---\n\nApply this skill to the evidence file. Output only the "
            f"skill's specified artefact in Markdown — no preamble, no meta-commentary."
        ), max_tokens=16000))

    if not jobs:
        print(f"  all {len(picked)} selected extractions already present "
              f"(--force to redo)")
        return
    print(f"  {len(jobs)} extractions to run")

    if not confirm(c.estimate(corpus, PREAMBLE, jobs, batched=True), args.yes):
        return

    c.prewarm(corpus, PREAMBLE)   # so batch members read rather than each re-write
    results = c.batch(corpus, PREAMBLE, jobs)

    # A provider can report a successful request while returning no text. Never
    # turn that into a misleading 0-byte extraction. Retry only the affected
    # skill immediately, keeping successful batch results intact.
    failed = []
    for job in jobs:
        result = _as_result(results.get(job.id, ""))
        results[job.id] = result.text
        if result.text.strip():
            continue
        # An empty reply because the budget went on reasoning needs more ROOM,
        # not another identical attempt. Same lesson as the batched JSON stages,
        # and the stop reason is what makes it knowable here too.
        budget = (job.max_tokens * BUDGET_RETRY_FACTOR if result.out_of_budget
                  else job.max_tokens)
        why = ("hit its output token budget before writing anything"
               if result.out_of_budget else "returned no content")
        print(f"  ! {job.id} {why}; retrying immediately at {budget:,} tokens, "
              f"up to {EMPTY_EXTRACTION_RETRIES} times")
        for attempt in range(1, EMPTY_EXTRACTION_RETRIES + 1):
            print(f"    {job.id}: retry {attempt}/{EMPTY_EXTRACTION_RETRIES}",
                  flush=True)
            text = c.one(
                corpus, PREAMBLE, job.prompt, budget, job.schema,
                job_id=job.id,
                operation=f"extraction_empty_retry_{attempt}")
            if text and text.strip():
                results[job.id] = text
                print(f"    {job.id}: recovered on retry {attempt}")
                break
        else:
            results.pop(job.id, None)
            failed.append(job.id)

    written = 0
    for job in jobs:
        text = results.get(job.id, "")
        if not text or not text.strip():
            continue
        with open(names[job.id], "w", encoding="utf-8") as fh:
            fh.write(text)
        written += 1
    print(f"\n  {written}/{len(jobs)} written -> {out}   (${c.actual_usd():.2f})")
    if failed:
        labels = ", ".join(failed)
        raise SystemExit(
            f"  Extraction failed after {EMPTY_EXTRACTION_RETRIES} retries: {labels}.\n"
            "  Run the failed skill again from Pipeline > Extract > "
            "Individual skill rerun, or use --skills NUMBER --force.")


def read_extractions(cfg, segment, *want):
    d = paths.extractions(cfg["_dir"], segment)
    if not os.path.isdir(d):
        sys.exit(f"No extractions. Run: adpipe -p {cfg['name']} extract {segment}")
    parts = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".md") and (not want or any(w in f for w in want)):
            parts.append(f"## {f[:-3]}\n\n{open(os.path.join(d, f), encoding='utf-8').read()}")
    return "\n\n---\n\n".join(parts)


# ------------------------------------------------------------------ synthesis

def synth(cfg, args, stage, prompt, dest, max_tokens=16000, schema=None, corpus=None):
    """Run one synthesis stage against the evidence corpus + prior outputs."""
    from llm import confirm
    c = client(cfg, args)
    corpus = corpus if corpus is not None else open(
        evidence_path(cfg, args.segment), encoding="utf-8").read()
    if os.path.exists(dest) and not args.force:
        print(f"  {os.path.basename(dest)} exists (--force to redo)")
        return open(dest, encoding="utf-8").read()
    if not confirm(c.estimate(corpus, PREAMBLE,
                              [type("J", (), {"prompt": prompt, "max_tokens": max_tokens})()]),
                   args.yes):
        sys.exit(1)
    text = c.one(corpus, PREAMBLE, prompt, max_tokens, schema)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, "w", encoding="utf-8").write(text)
    print(f"  -> {dest}   (${c.actual_usd():.2f})")
    return text


RAMP_RULES = """
RAMP rules (no ad has won yet — one segment, one awareness stage, no factory):

Research dimensions are SELECTORS, not copy. Nine dimensions never appear as words
in the ad; they decide how the visible parts are built. Only five things become
visible copy: headline, support line, proof element, CTA, visual idea.
  pain (07/08)         -> selects the hook + the visual
  desired outcome (09) -> selects the promise / after-state language
  failed solution (14) -> selects the ANGLE + the contrast copy
  objection (18)       -> selects the proof element + the reassurance line
  emotional state (10) -> selects the tone + the entry point
  undercurrent (10/11) -> selects the subtext, the "this is me" resonance
  driver (11/16)       -> selects urgency / why-now
  bias                 -> selects proof style + ordering + presentation
  segment              -> selects which ad exists at all
If a dimension is showing up as literal words in the headline, that is the classic
mistake — put it back as a selector.

COMPLIANCE (health-adjacent wellness product on Meta) — non-negotiable:
  Say: felt experience — tension that won't switch off, waking up stiff, the day's
  tightness following you to bed, sleeping through, waking recovered.
  Never say: corrects posture, realigns spine/neck, relieves nerve compression,
  treats or cures any named condition, or any medical-causation claim.
  Mechanism (19) and proof (20) are where overclaim risk concentrates — deploy the
  FELT mechanism, not the medical one. If a barrier can only be answered with a
  claim that cannot be substantiated, FLAG IT — that means the angle is wrong, not
  that you may overclaim.
"""


def cmd_picc(cfg, args):
    """Skill 27 + the quick PICC card + 5 angles."""
    _, s27 = skill(27)
    prior = read_extractions(cfg, args.segment)
    cr = cfg["creative"]
    seg_ctx = segment_context(cfg, args.segment, getattr(args, "product", None))
    prompt = f"""{s27}

{product_context(cfg, product=getattr(args, "product", None))}

---
{(seg_ctx + chr(10) + chr(10) + '---' + chr(10)) if seg_ctx else ''}

Below are this segment's completed extraction outputs (skills 07-26).

{prior}

---

Do two things, in order.

STEP 1 — Apply skill 27 to rank this segment's buying barriers. Skill 27 is a
synthesis skill: read the extraction outputs above, never re-count the raw corpus.

STEP 2 — Fill the quick PICC card for ONE segment at ONE awareness stage
({cr['awareness']}, {cr['traffic']} traffic), then write 5 angles.

{RAMP_RULES}

Card fields: segment, avatar, awareness, traffic temp, pain, pain moment,
emotional state, limiting belief, assumed solution, solution doubt, mechanism
reframe, primary buying barrier (from your step 1), driver, bias, primary angle,
communication style, representative VOC phrase (VERBATIM from skill 24 — it must
appear word-for-word in the evidence file), proof, objection handled, hook
direction, CTA, destination.

Then 5 distinct angles, one line each. An angle is which truth from the research
the ad leads with — a strategic message, not copy yet. Usual families: pain-led,
failed-solution, desired-outcome, mechanism, objection-busting.

Output Markdown: the barrier ranking, then the card as a table, then the angles.
"""
    synth(cfg, args, "picc", prompt,
          paths.assets(cfg["_dir"], args.segment, "01_picc_card.md"), 16000)


CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "segment": {"type": "string"},
        "awareness": {"type": "string"},
        "evidence_file": {"type": "string"},
        "concepts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "angle": {"type": "string"},
                "style": {"type": "string"},
                "framework": {"type": "string"},
                "template": {"type": "string"},
                "format": {"type": "string"},
                "hooks": {"type": "array", "items": {"type": "string"}},
                "slots": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["id", "angle", "style", "framework", "template", "format",
                         "hooks", "slots"],
            "additionalProperties": False}},
    },
    "required": ["segment", "awareness", "evidence_file", "concepts"],
    "additionalProperties": False,
}


def facts_path(cfg):
    """Approved facts for this project.

    Per-project by default. These used to live at pipeline/facts.json, shared by
    every project — which meant a second product silently inherited the first
    one's approved numbers and claim language. An explicit "facts" key still
    wins, for a project that deliberately points elsewhere.
    """
    p = cfg.get("facts")
    return os.path.join(ROOT, p) if p else os.path.join(cfg["_dir"], "facts.json")


def sheet_path(cfg, product=None):
    """The active product's sheet, rendered from its product.json by the Product
    tab. A project may hold several products, so this resolves which one: an
    explicit --product wins, a project with exactly one needs no argument."""
    p = cfg.get("product_sheet")
    if p:
        return os.path.join(ROOT, p)
    import products
    try:
        products.migrate_legacy(cfg["name"])
        prod = products.resolve_product(cfg["name"], product)
    except products.ProductError:
        return os.path.join(cfg["_dir"], "product_sheet.md")   # legacy layout
    return products.sheet_path(cfg["name"], prod)


def segment_context(cfg, segment, product=None):
    """This segment's Customer Truth and strategy, if the Product tab has any.

    Segment-scoped on purpose: one segment's research must not reach another's
    ads. Returns "" when the segment has no sheet yet, so the pipeline still
    runs on research alone rather than failing closed on an optional layer.
    """
    import products
    p = os.path.join(cfg["_dir"], "segments", f"{segment}.md")   # legacy layout
    try:
        prod = products.resolve_product(cfg["name"], product)
        cand = products.segment_sheet_path(cfg["name"], prod, segment)
        if os.path.exists(cand):
            p = cand
    except products.ProductError:
        pass
    if not os.path.exists(p):
        return ""
    return ("SEGMENT STRATEGY — this segment's customer truth and the strategy "
            "built from it. It applies to THIS segment only; do not carry it to "
            "another.\n\n" + open(p, encoding="utf-8").read())


def product_context(cfg, sheet=True, product=None):
    """What is being sold, for the stages that decide strategy and write copy.

    Until this existed, only the brief stage knew the product: picc and concepts
    ranked barriers, chose angles and wrote headlines without ever being told
    what the ad was for, which is how you get plausible copy for a product that
    does not exist.

    The two sources are kept apart on purpose. facts.json is the only thing that
    licenses a number or a product claim — qa.py fails anything else — while the
    product sheet is background for choosing an angle. Merging them would turn
    240 lines of unconfirmed research into apparent permission to make claims,
    so the prompt says which is which.
    """
    parts = [f"PRODUCT\n{cfg.get('product') or '(unspecified)'}",
             f"MARKET\n{cfg.get('market') or '(unspecified)'}"]

    p = facts_path(cfg)
    if os.path.exists(p):
        parts.append(
            "APPROVED PRODUCT FACTS — the ONLY numbers and product claims that "
            "may appear in an ad. Anything marked NEEDS INPUT is unconfirmed: "
            "do not use it, and do not invent a value for it.\n\n"
            + open(p, encoding="utf-8").read())

    if sheet:
        p = sheet_path(cfg, product)
        if os.path.exists(p):
            parts.append(
                "PRODUCT SHEET — background for strategy: what the product is, how "
                "it actually works, who it suits, what it competes with, and where "
                "the objections are. This is NOT a claims source. Any number or "
                "claim here that is absent from the approved facts above is "
                "unconfirmed and must not appear in an ad.\n\n"
                + open(p, encoding="utf-8").read())

    return "\n\n---\n\n".join(parts)


def picc_path(cfg, segment, chosen=None):
    """Which PICC card the concepts stage should build on.

    Defaults to the segment's own card, but `--picc` takes any card in the
    project — you may have rewritten the card, kept a variant, or want to run
    this segment's language against a card you prefer. The choice is explicit
    rather than inferred, so re-running concepts cannot silently swap strategy
    underneath you.
    """
    default = paths.assets(cfg["_dir"], segment, "01_picc_card.md")
    if not chosen:
        if not os.path.exists(default):
            sys.exit(f"No PICC card. Run: adpipe -p {cfg['name']} picc {segment}")
        return default

    p = chosen if os.path.isabs(chosen) else os.path.join(ROOT, chosen)
    p = os.path.realpath(p)
    # Keep it inside the project: the card decides everything downstream, so it
    # is not a path to accept from anywhere on disk.
    if os.path.commonpath([p, os.path.realpath(cfg["_dir"])]) != os.path.realpath(cfg["_dir"]):
        sys.exit(f"--picc must point inside projects/{cfg['name']}/ — got {chosen}")
    if not os.path.exists(p):
        sys.exit(f"PICC card not found: {chosen}")
    return p


def cmd_concepts(cfg, args):
    """10 concepts, 2-3 in-image hooks each, each mapped to a real layout."""
    card_p = picc_path(cfg, args.segment, getattr(args, "picc", None))
    card = open(card_p, encoding="utf-8").read()
    rel = os.path.relpath(card_p, ROOT)
    print(f"  PICC card: {rel}")
    # The card is the strategy, but it is one model's compression of 20
    # dimensions. Pain, desired outcome, mechanism and desired proof go in raw
    # alongside it so a lossy card cannot quietly drop what the ad is about;
    # 24/25/26 supply the segment's own words, 14/18 the contrast and rebuttal.
    lang = read_extractions(cfg, args.segment, "terminology", "slang",
                            "representative_voc", "failed_solutions", "objections",
                            "pain_points", "desired_outcomes", "mechanisms",
                            "desired_proof")
    templates = _templates()
    n = getattr(args, "concepts", None) or cfg["creative"]["concepts_per_run"]
    hooks_n = getattr(args, "hooks", None) or 3

    seg_ctx = segment_context(cfg, args.segment, getattr(args, "product", None))
    prompt = f"""{product_context(cfg, sheet=False, product=getattr(args, "product", None))}

---
{(seg_ctx + chr(10) + chr(10) + '---' + chr(10)) if seg_ctx else ''}
Here is the completed PICC card and 5 angles for this segment:

{card}

---

And the underlying research the card was built from — pain points (07), desired
outcomes (09), mechanisms (19) and desired proof (20), the segment's own language
(24/25/26), plus failed solutions (14) and objections (18). Where the card and
these disagree, the card decides strategy but these decide the wording, and
anything the card dropped is still fair game here:

{lang}

---

Produce {n} ad concepts as JSON matching the provided schema.

A concept = how an angle is expressed visually AND verbally. Cross the 5 angles
with communication styles (educational / demonstration / comparison / story /
UGC-static) until you have {n} coherent ones. Kill any that feel forced — {n} real
concepts beat 20 padded.

For each concept:
- "angle": which of the 5 angles it leads with
- "style": the communication style
- "framework": Pain->Promise | Mistake->Fix | Before->After | Claim->Proof |
  Objection->Reframe | Problem->Mechanism->Benefit | Comparison->Advantage->CTA
- "template": EXACTLY one of these layouts, and the slots must match it:
{templates}
- "format": "4x5"
- "hooks": exactly {hooks_n} in-image hook options in the SEGMENT'S OWN WORDS (use the
  terminology and slang above — their language, not ours). Match hook style to
  awareness + angle. Draw on the in-image hook templates: misattribution/reframe,
  contrarian, direct callout, curiosity gap, warning, comparison, confession,
  before-you-buy.
- "slots": the filled text slots for that template. Put the strongest hook in the
  "hook" slot. Keep every line short enough to read at thumbnail size — a hook is
  at most ~10 words, a subhead at most ~20.

{RAMP_RULES}

Hard constraints on slot copy:
- Any "quote" slot MUST be verbatim from the evidence file, word for word.
- Use NO numbers, percentages, or durations at all — nothing is substantiated yet.
- Compare against a category ("most foam pillows"), never a named brand.
- Do not fill image/plate slots; those are generated separately.

Set "evidence_file" to "{os.path.relpath(evidence_path(cfg, args.segment), ROOT)}".
"""
    dest = paths.assets(cfg["_dir"], args.segment, "concepts.json")
    text = synth(cfg, args, "concepts", prompt, dest, 32000, CONCEPT_SCHEMA)
    if text:
        doc = json.loads(text)
        md = [f"# Concepts — {doc['segment']} ({doc['awareness']})\n"]
        for c in doc["concepts"]:
            md.append(f"## {c['id']} — {c['angle']}\n")
            md.append(f"- **Style:** {c['style']}  ·  **Framework:** {c['framework']}")
            md.append(f"- **Layout:** `{c['template']}` ({c['format']})")
            md.append("- **Hooks:**")
            md += [f"  {i}. {h}" for i, h in enumerate(c["hooks"], 1)]
            md.append("")
        open(os.path.join(os.path.dirname(dest), "02_concepts.md"), "w",
             encoding="utf-8").write("\n".join(md))
        print(f"  -> {os.path.dirname(dest)}/02_concepts.md")


def _templates():
    d = os.path.join(ROOT, "pipeline", "templates")
    import render
    lines = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".html") and not f.startswith("_"):
            slots = [s for s in render.template_slots(os.path.join(d, f))
                     if s not in ("logo_text",) and "image" not in s and "avatar" not in s]
            lines.append(f"    {f[:-5]} — slots: {', '.join(slots)}")
    return "\n".join(lines)


def cmd_brief(cfg, args):
    """Production briefs for the strongest concepts — the artefact a designer or
    image model builds from."""
    cp = paths.assets(cfg["_dir"], args.segment, "concepts.json")
    if not os.path.exists(cp):
        sys.exit(f"No concepts. Run: adpipe -p {cfg['name']} concepts {args.segment}")
    concepts = open(cp, encoding="utf-8").read()
    n = getattr(args, "briefs", None) or cfg["creative"]["briefs_per_run"]

    prompt = f"""These are the approved concepts:

{concepts}

---

{product_context(cfg, product=getattr(args, "product", None))}

---

Pick the {n} strongest concepts and write a production brief for each: everything a
designer or an image model needs to build the ad.

Per brief:
- Concept id and why it is one of the strongest (tie it to the ranked barrier)
- Hook (the chosen one) · headline · subhead
- Visual direction — the scene, the mood, what is in frame
- Layout token + which text goes in which zone
- Proof element and the reassurance line
- CTA and destination
- Brand styling notes
- **Visual prompt** — a prompt for an image model that generates the PLATE ONLY:
  background, product, scene. It must contain NO text, NO words, NO lettering, no
  signage, no UI. Text is composited separately; image models cannot spell.

{RAMP_RULES}

Close with a QA checklist for this specific batch: headline readable · correct
product · no hallucinated quotes or stats · mechanism accurate to evidence · no
unsupported medical claim · brand styling · template followed · every visible line
traceable to a real comment or a stated product fact.

Output Markdown.
"""
    synth(cfg, args, "brief", prompt,
          paths.assets(cfg["_dir"], args.segment, "03_production_brief.md"), 24000)


# ------------------------------------------------------------------ code stages

def _script(cfg, args, name, extra=()):
    cp = paths.assets(cfg["_dir"], args.segment, "concepts.json")
    if not os.path.exists(cp):
        sys.exit(f"No concepts.json for {args.segment}.")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "pipeline", name), cp, *extra])
    return r.returncode


def cmd_qa(cfg, args):
    sys.exit(_script(cfg, args, "qa.py"))


def cmd_render(cfg, args):
    if _script(cfg, args, "qa.py") != 0 and not args.force:
        sys.exit("QA failed — fix the copy before rendering (--force to override).")
    sys.exit(_script(cfg, args, "render.py"))


def cmd_studio(cfg, args):
    """Launch the browser UI. Everything the CLI does is reachable from there."""
    import app
    app.main()


def cmd_run(cfg, args):
    for step in (cmd_extract, cmd_picc, cmd_concepts, cmd_brief):
        print(f"\n=== {step.__name__.replace('cmd_', '').upper()} ===")
        step(cfg, args)
    print("\n=== QA ===")
    _script(cfg, args, "qa.py")
    print("\n=== RENDER ===")
    _script(cfg, args, "render.py")


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(prog="adpipe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--project", default="montisella")
    ap.add_argument("--yes", action="store_true", help="skip cost confirmations")
    ap.add_argument("--force", action="store_true", help="redo work that already exists")
    ap.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--provider", choices=["anthropic", "openrouter"],
                    help="which API to run model stages on")
    ap.add_argument("--model", help="model id, e.g. claude-opus-5 or "
                                    "deepseek/deepseek-v4-flash")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        """Repeat the global switches on every subcommand so they work in either
        position — `adpipe --provider x extract seg` and `adpipe extract seg
        --provider x` both read naturally, and argparse only accepts the first
        unless we do this."""
        # SUPPRESS is load-bearing: without it the subparser's None default
        # overwrites a value already parsed from the global position, so
        # `adpipe --provider openrouter extract seg` would silently fall back.
        S = argparse.SUPPRESS
        sp.add_argument("--provider", choices=["anthropic", "openrouter"], default=S)
        sp.add_argument("--model", default=S)
        sp.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                        default=S)
        sp.add_argument("--yes", action="store_true", default=S)
        sp.add_argument("--force", action="store_true", default=S)
        return sp

    s = common(sub.add_parser("ingest")); s.add_argument("source")
    s.add_argument("--rules-only", action="store_true",
                   help="deterministic pre-pass only; skip skills 01/02 (free)")
    s.set_defaults(fn=cmd_ingest)
    s = common(sub.add_parser("segment")); s.add_argument("--rediscover", action="store_true")
    s.add_argument("--reassign", action="store_true")
    s.add_argument("--source", help="VOC file to segment (default: voc/filtered_voc.jsonl)")
    s.set_defaults(fn=cmd_segment)
    s = sub.add_parser("studio", help="open the browser UI"); s.set_defaults(fn=cmd_studio)
    for name, fn in (("extract", cmd_extract), ("picc", cmd_picc), ("concepts", cmd_concepts),
                     ("brief", cmd_brief), ("qa", cmd_qa), ("render", cmd_render),
                     ("run", cmd_run)):
        s = common(sub.add_parser(name)); s.add_argument("segment"); s.set_defaults(fn=fn)
        if name in ("extract", "run"):
            s.add_argument("--preset", choices=sorted(PRESETS),
                           help="extraction depth: fast, standard, or deep (default: deep)")
            s.add_argument("--skills", help="explicit list, e.g. 7,8,14,18,24,25")
        if name in ("picc", "concepts", "brief", "run"):
            s.add_argument("--product", help="which product in this project to "
                                             "build on (default: the only one)")
        if name in ("concepts", "run"):
            s.add_argument("--concepts", type=int, help="how many concepts")
            s.add_argument("--hooks", type=int, help="hooks per concept (default 3)")
            s.add_argument("--picc", help="which PICC card to build on, as a path "
                                          "inside the project (default: this "
                                          "segment's output/<segment>/01_picc_card.md)")
        if name in ("brief", "run"):
            s.add_argument("--briefs", type=int, help="how many production briefs")

    args, extra = ap.parse_known_args()
    if extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    cfg = load_project(args.project)
    os.environ["ADPIPE_PROJECT"] = cfg["name"]
    os.environ["ADPIPE_STAGE"] = args.cmd
    import auditlog
    auditlog.set_context(project=cfg["name"], stage=args.cmd, source="pipeline_cli")
    print(f"[{cfg['name']}] {args.cmd}")

    try:
        args.fn(cfg, args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Completed stages are on disk; re-run to continue.")
    except BatchOutputError as e:
        sys.exit(f"\n  MODEL OUTPUT ERROR — {e}")
    except Exception as e:
        # Turn SDK errors into one actionable line instead of a traceback.
        name = type(e).__name__
        if name == "AuthenticationError":
            sys.exit("Authentication failed — the API key was rejected (401). "
                     "Check ANTHROPIC_API_KEY.")
        if name == "PermissionDeniedError":
            sys.exit("Permission denied (403) — this key cannot use "
                     f"{cfg.get('model', {}).get('id', 'the model')}.")
        if name == "RateLimitError":
            sys.exit("Rate limited (429). Wait and re-run — finished stages are cached "
                     "on disk and will be skipped.")
        if name == "NotFoundError":
            sys.exit(f"Model not found (404): {cfg.get('model', {}).get('id')!r}. "
                     "Check the id in project.json.")
        raise


if __name__ == "__main__":
    main()
