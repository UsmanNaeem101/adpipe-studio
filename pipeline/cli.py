#!/usr/bin/env python3
"""
adpipe — raw Reddit VOC in, launch-ready static ads out.

    ./adpipe ingest  raw_voc.txt        filter + dedupe                (code)
    ./adpipe refine-voc                 lean production + rich audit   (code)
    ./adpipe segment                    research + commercial segments (code + model)
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
import copy
import dataclasses
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import paths  # noqa: E402
import presets  # noqa: E402
import segmentation  # noqa: E402
import commercial  # noqa: E402
import corpus_policy  # noqa: E402
import researchpack  # noqa: E402
import stagereport  # noqa: E402
from llm import BatchResult, NO_RETRY_STOP_REASONS  # noqa: E402

SKILLS = os.path.join(ROOT, "skills")

# Skills 07-26 all read the evidence file and write one dimension each.
EXTRACTORS = list(range(7, 27))
EMPTY_EXTRACTION_RETRIES = 3
PRODUCTION_VOC_FILE = "production_voc.jsonl"
AUDIT_VOC_FILE = "audit_voc.jsonl"
STAGE03E_CATALOGUE_MARKER = "\n\nCANONICAL CANDIDATES:\n"
STAGE03E_EVIDENCE_PREFIX = (
    "Return exactly one match row per evidence item.\n\nEVIDENCE:\n"
)

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


# Which skills each stage actually loads, in the order it loads them.
#
# The sub-skills were invisible everywhere but the source. Stage 03 alone runs
# five files -- an orchestration contract plus four workers that exist because
# the corpus has to be chunked -- and the UI described the whole thing as one
# line, so a skill could be edited, or fail to be edited, with nothing showing
# it was in play. This is the one place that mapping is written down; app.py
# renders from it rather than keeping a second copy that can drift.
STAGE_SKILLS = {
    "ingest": [
        ("01_filter_voc.md", "01 filter", "keep real customer signal, bin the rest"),
        ("02_deduplicate_voc.md", "02 deduplicate",
         "collapse repeats, protect independent voices"),
    ],
    "segment": [
        ("03_discover_segments.md", "03 orchestration",
         "the fixed sequence stage 03 runs"),
        ("03a_discover_segment_candidates.md", "03A harvest",
         "high-recall discovery per chunk, sorted into four registers"),
        ("03b_consolidate_segment_candidates.md", "03B consolidate",
         "merge candidates globally into one map"),
        ("03e_expand_segment_evidence.md", "03E expand",
         "classify all evidence against the candidate map"),
        ("03c_audit_segment_coverage.md", "03C novelty audit",
         "look for audiences the map missed"),
        ("04_validate_segments.md", "04 validate",
         "the sceptic: floors, registers, facets and the segment graph"),
        ("05_assign_primary_segment.md", "05 assign",
         "one comment, one audience -- or none"),
        ("06_build_segment_evidence_files.md", "06 build",
         "one evidence file per audience, assembled in code"),
        ("commercial_07_coalesce.md", "07 coalesce",
         "research clusters into commercial audiences"),
        ("commercial_08a_extract_signals.md", "08A signals",
         "structure the extractions into VOC signals"),
        ("commercial_08b_coalesce_signals.md", "08B synthesise",
         "signals into per-audience themes"),
    ],
    "picc": [
        ("27_rank_buying_barriers.md", "27 buying barriers",
         "one ranked stack of what blocks the sale"),
    ],
}


def stage_skill_manifest(stage):
    """The skills a stage runs, with whether each file is actually present."""
    rows = []
    for filename, label, purpose in STAGE_SKILLS.get(stage, []):
        path = os.path.join(SKILLS, filename)
        rows.append({"file": filename, "label": label, "purpose": purpose,
                     "present": os.path.isfile(path),
                     "lines": (len(open(path, encoding="utf-8").read().split("\n"))
                               if os.path.isfile(path) else 0)})
    return rows


def extractor_skill_manifest():
    """Skills 07-26, the per-dimension extractors, with their real filenames."""
    rows = []
    for number in EXTRACTORS:
        match = next((f for f in sorted(os.listdir(SKILLS))
                      if f.startswith(f"{number:02d}_") and f.endswith(".md")), None)
        path = os.path.join(SKILLS, match) if match else None
        rows.append({
            "n": number, "title": SKILL_TITLES.get(number, str(number)),
            "file": match or "", "present": bool(match),
            "lines": (len(open(path, encoding="utf-8").read().split("\n"))
                      if path else 0)})
    return rows


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


def skill_named(filename):
    """Read one explicitly named skill when a numbered stage has subskills."""
    path = os.path.join(SKILLS, filename)
    if not os.path.isfile(path):
        sys.exit(f"Skill {filename} not found in {SKILLS}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


SECTION_RE = re.compile(r"^## (.+?)\s*$", re.M)


def skill_sections(n, keep, include_intro=True):
    """Render a skill as (optionally) its intro plus only the named `## ` sections.

    A skill file describes the whole STAGE — including work the deterministic
    pre-pass already did, the record shape the pipeline stores, and the report a
    human wants at the end. The model is asked for none of that: it gets one
    schema with four fields and `additionalProperties: false`.

    Sending the whole file therefore hands the model two different output
    contracts and asks it to reconcile them, which it does at length and at our
    expense. This sends the judgement and drops the plumbing. `keep` is checked
    against the file so a restructured skill fails loudly instead of silently
    shipping a prompt with its criteria missing.
    """
    name, text = skill(n)
    marks = list(SECTION_RE.finditer(text))
    found = {m.group(1): (m.start(), m.end()) for m in marks}
    missing = [s for s in keep if s not in found]
    if missing:
        sys.exit(f"{name}: expected section(s) {missing} — the skill was "
                 f"restructured, so the prompt built from it is no longer "
                 f"the reviewed one. Update the section list in cli.py.")
    ends = {m.start(): (marks[i + 1].start() if i + 1 < len(marks) else len(text))
            for i, m in enumerate(marks)}
    out = []
    if include_intro:
        out.append(text[:marks[0].start()].strip() if marks else text.strip())
    for section in keep:
        start, _ = found[section]
        out.append(text[start:ends[start]].strip())
    return "\n\n".join(out)


# The judgement half of skill 01. Everything that decides retain/reject stays;
# what leaves is the record shape the JSON schema now states outright, the
# exact-duplicate pass cmd_ingest already ran in code, and the self-audit and
# report the model has nowhere to put.
FILTER_SKILL_SECTIONS = (
    "The one rule that governs everything",
    "Keep it (retain)",
    "Bin it (reject)",
    "Hard boundaries (what this skill must not do)",
    "Reason codes",
)


def client(cfg, args):
    """Pick the backend. --provider/--model win, then project.json, then Anthropic."""
    m = cfg.get("model", {})
    provider = (getattr(args, "provider", None) or m.get("provider") or "anthropic").lower()
    effort = args.effort or m.get("effort", "high")
    name = getattr(args, "model", None) or m.get("id")
    if provider == "openrouter":
        import openrouter
        # OpenRouter fronts hundreds of third-party routes whose output ceilings
        # are the provider's business and not published anywhere this code can
        # read, so the ceiling is declared per project rather than guessed:
        # "model": {"max_output_tokens": 16384}. Absent, nothing is clamped and
        # an over-large request is reported by the route itself.
        return openrouter.Client(model=name or openrouter.DEFAULT_MODEL, effort=effort,
                                 max_output=m.get("max_output_tokens"))
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
    "- For frequency or prevalence, Core evidence drives the count. Supporting "
    "evidence corroborates or enriches; Context evidence is preserved but does not "
    "increase a core-frequency claim.\n"
    "- If the evidence does not support a field, write 'insufficient evidence' "
    "rather than filling it in plausibly.\n"
)


# ------------------------------------------------------------------ 01-02

# PREAMBLE above is written for skills 07-26, which read a segment evidence file
# and quote from it. At skill 01 none of that is true yet — there is no evidence
# file, no segment, no assignment metadata, and nothing is quoted back. Worse,
# its "write 'insufficient evidence'" rule cannot be satisfied by a schema whose
# reason arrays are enums, so the model is left reconciling an instruction it
# has no legal way to follow. Skill 01 gets its own operating context.
FILTER_PREAMBLE = (
    "You are the filtering stage of a voice-of-customer research pipeline. You "
    "classify Reddit comments as evidence worth keeping or noise worth dropping. "
    "That is the entire job: you are not segmenting, not summarising, not "
    "extracting themes, and not deciding what anything means. You are the "
    "bouncer at the door — signal gets in, chrome and spam don't, and everyone "
    "who gets in keeps their own voice.\n\n"
    "What you are given: numbered records, one comment each. Interface chrome, "
    "bot boilerplate and byte-identical duplicates have ALREADY been removed in "
    "code before you see them — do not re-run that pass, and do not look for "
    "duplicates.\n\n"
    "What you return: one JSON object per input record, in exactly the supplied "
    "schema and nothing else.\n"
    "- The record's numeric [id] is copied to evidence_id. IDs are assigned by "
    "the pipeline; you never mint, change or re-order them.\n"
    "- Reasons are codes from the list below, copied verbatim. Not prose, not "
    "explanations, not new codes.\n"
    "- The array that does not apply to your decision is []. A rejected record "
    "has retention_reasons: [], a retained one has rejection_reasons: [].\n"
    "- Those four fields are the whole record. The pipeline already holds the "
    "comment text and its source, and rejects any record that adds to the "
    "schema.\n\n"
    "You are not asked for counts, totals, rates, a summary or a report, and "
    "there is nowhere in the schema to put one. Judge each record once and move "
    "on: do not re-audit or re-tally decisions you have already made."
)

POST_HDR = re.compile(r"^\s*(?:POST\s+)?URL:\s*(\S+)", re.I | re.M)
TITLE_HDR = re.compile(r"^\s*TITLE:\s*(.+)$", re.I | re.M)
# Reddit dumps commonly number comments "[1] ", "[2] " — optionally with a
# classification tag the previous pipeline stage added.
COMMENT_MARK = re.compile(r"^\s*\[\d+\]\s*(?:\[[A-Z_]+\]\s*)?", re.M)
# What the scraper leaves behind once the [n] marker is split off. 6,013 items in
# the montisella dump opened with it, so every one of those was sent to the model
# with scrape furniture standing where the customer's first words should be.
SCRAPE_PREFIX = re.compile(r"^\?\s*\(score:\s*-?\d+\)\s*", re.I)
# The scraper's own section banners — `--- POST BODY ---`, `--- COMMENTS (21) ---`.
# Left in, blank-line splitting turns each one into its own evidence record.
SECTION_HDR = re.compile(r"^\s*-{2,}\s*[A-Z][A-Z ()\d/]*\s*-{2,}\s*$", re.M)

# old.reddit renders a whole thread as one unbroken line: every comment is
# introduced by a collapse marker, a username, a triple score, a relative time
# and a child count, and closed by its action links. Nothing is on its own line,
# so a blank-line or [n] split sees the entire thread as a single item.
#
# That is not a cosmetic problem. Before this existed, one montisella "evidence
# item" was 107,187 characters containing 184 separate people, and the run-
# together dumps held 76% of the corpus text — which the chrome filter then
# deleted wholesale, because `permalinkembedsave` looked like page furniture
# rather than the separator between two customers speaking.
#
# `_NOT_NEXT` is a tempered token: it lets each part of the header skip over
# usernames, flair and moderator tags without ever running past the start of the
# following comment, which a plain `.*?` will happily do on a malformed dump.
_NOT_NEXT = r"(?:(?!\[[–\-]\]).)"
OLD_REDDIT_HEAD = re.compile(
    r"\[[–\-]\]"                                            # collapse marker
    + _NOT_NEXT + r"{0,200}?"                               # username + flair
    + r"(?:\d+\s*points?|score hidden)"                     # score, or hidden
    + _NOT_NEXT + r"{0,160}?"
    + r"\d+\s*(?:year|month|week|day|hour|minute)s?\s*ago"  # relative time
    + _NOT_NEXT + r"{0,80}?\(\d+\s*child(?:ren)?\)",        # reply count
    re.I | re.S)
# The action links that close a comment, and everything after them up to the next
# comment, are furniture rather than anything a customer wrote.
OLD_REDDIT_TAIL = re.compile(
    r"permalink(?:embed)?(?:save)?(?:parent)?(?:report)?(?:reply)?", re.I)
# Everything before the first comment is the post body followed by the thread's
# own chrome — the comment count, the sort bar, the signup prompt — run together
# on the same line. This marks where the customer stops and the page starts.
# The comment-count is the first token of that furniture run, so it has to be
# matched with the run behind it — a bare `\d+ comments` would also truncate a
# post body that happens to mention reading the comments.
OLD_REDDIT_LEAD_END = re.compile(
    r"(?:SPOILER)?\s*\d+\s*comments?(?=sharesavehidereport)"
    r"|sharesavehidereport|sorted by:|all \d+ comments"
    r"|Want to add to the discussion", re.I)


def split_old_reddit(text):
    """Split one old.reddit run-together dump into post body and comments.

    Returns [] when the text is not that layout, so the caller can keep whatever
    it already had. A dump that yields nothing usable is also [] — never a list
    of empty strings, which would show up downstream as evidence with no text.

    The post body leads the dump and is worth the extra care: it is the original
    poster stating their problem in full, the richest evidence in any thread.
    942 of them were in the montisella scrape.
    """
    heads = list(OLD_REDDIT_HEAD.finditer(text))
    if not heads:
        return []
    comments = []
    lead = text[:heads[0].start()]
    chrome = OLD_REDDIT_LEAD_END.search(lead)
    if chrome:
        lead = lead[:chrome.start()]
    lead = SCRAPE_PREFIX.sub("", lead.strip()).strip()
    if lead:
        comments.append(lead)
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        body = text[head.end():end]
        tail = OLD_REDDIT_TAIL.search(body)
        if tail:
            body = body[:tail.start()]
        body = body.strip()
        if body:
            comments.append(body)
    return comments


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
        body = SECTION_HDR.sub("", body)
        parts = COMMENT_MARK.split(body) if COMMENT_MARK.search(body) else \
            re.split(r"\n\s*\n", body)
        for part in parts:
            s = part.strip(" \t\n=-_")
            if not s:
                continue
            # Second pass, because the two layouts co-occur inside one post: a
            # scrape can carry a [n]-marked post body AND an old.reddit thread
            # dump. Splitting again is additive — a part with no old.reddit
            # header is kept exactly as the first pass produced it.
            for comment in split_old_reddit(s) or [s]:
                comment = SCRAPE_PREFIX.sub("", comment).strip()
                if comment:
                    items.append({"text": comment, "url": p["url"],
                                  "title": p["title"]})
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
# Out-of-scope by audience, not by quality, and therefore not part of the base
# vocabulary: a project researching the whole population must never be offered
# this code. Kept distinct from `off_topic` so a scope decision stays countable
# and reversible — flipping back to `all` should be answerable from the audit
# file rather than another pass over the corpus.
SCOPED_REJECTION_REASONS = REJECTION_REASONS + [corpus_policy.CLINICAL_POPULATION]
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


def filter_schema(cfg):
    """Stage 01's schema for this project's audience scope.

    A project researching the whole population must not be offered
    `clinical_population` as a rejection code. Leaving an unusable code in the
    enum is how a policy gets applied by a stage that was never asked to apply
    it — the model sees a code, infers a rule, and quietly narrows a corpus the
    operator wanted whole.

    The unscoped default returns the shared FILTER_SCHEMA object rather than a
    copy of it. That is load-bearing: callers and test doubles identify this
    stage by `schema is cli.FILTER_SCHEMA`, and handing back an equal-but-
    distinct dict would break them silently.
    """
    if corpus_policy.scope(cfg) == corpus_policy.SCOPE_ALL:
        return FILTER_SCHEMA
    schema = copy.deepcopy(FILTER_SCHEMA)
    schema["properties"]["records"]["items"]["properties"][
        "rejection_reasons"]["items"]["enum"] = list(SCOPED_REJECTION_REASONS)
    return schema

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

# Stage 01 defaults. Both are overridable per project so a different model can be
# tuned without touching code — `"filter": {"chunk": 40, "effort": "none"}` in
# project.json. Deliberately per-stage: the synthesis stages want deep reasoning
# and must not inherit this.
FILTER_CHUNK = 60
FILTER_EFFORT = "low"
DEDUP_EFFORT = None   # None = leave skill 02 exactly as it was

# Stages 01 and 02 learn a persistent output floor while they run. These deliberately
# coarse tiers avoid paying to rediscover that a nearby ceiling is also too low.
# A ceiling is not spend: providers charge generated tokens, not tokens allowed.
ADAPTIVE_TOKEN_TIERS = (12000, 16000, 24000, 32000)
# 03B emits the complete canonical catalogue in one response. Live telemetry
# showed 22,727 answer tokens plus only 1,273 reasoning tokens at a 24k stop, so
# its legitimate answer shape—not reasoning—is larger than the general recovery
# tiers. Keep this policy local to 03B's two consolidation calls.
STAGE03B_TOKEN_TIERS = (64000, 128000)
# Stage 04 asks DeepSeek for one complete validation decision catalogue. Live
# telemetry showed its former 16k ceiling consumed 15,708 reasoning tokens and
# left only 292 answer tokens, so this stage needs a substantially wider,
# independently bounded ladder. Do not share it with the surrounding stages.
STAGE04_TOKEN_TIERS = (64000, 128000, 256000)
# Stage 05 emits one assignment row per evidence item. Its answer can exceed
# 16k, but it does not need Stage 04's catalogue-sized 64k+ ladder.
STAGE05_TOKEN_TIERS = (16000, 24000, 32000)
# Commercial coalescing is one global catalogue response; signal extraction is
# chunk-local, and segment-local coalescing may legitimately be larger.  These
# ladders are isolated from the established Stage 03--06 policies.
STAGE07_TOKEN_TIERS = (64000, 128000)
STAGE08A_TOKEN_TIERS = (12000, 16000, 24000, 32000)
STAGE08B_TOKEN_TIERS = (24000, 32000, 64000)
STAGE05_ANTHROPIC_WAVE_SIZE = 10
STAGE05_ANTHROPIC_BATCH_INTERVAL_SECONDS = 65
STAGE05_GRAMMAR_RETRY_ATTEMPTS = 2
ADAPTIVE_OPENROUTER_WAVE_SIZE = 4
ADAPTIVE_PROGRESS_WIDTH = 20


def _model_ceiling(client_):
    """The client's own output ceiling, or None when it does not publish one."""
    ask = getattr(client_, "max_output_tokens", None)
    try:
        return ask() if callable(ask) else None
    except Exception:
        # A ceiling lookup must never be the thing that fails a stage.
        return None


def _clamp_tiers(tiers, ceiling, stage="", verbose=True):
    """Bound a recovery ladder to what the model will actually accept.

    A ladder is only a ladder if its top rung can be stood on. Stage 04's ladder
    tops out at 256,000 tokens and Stage 03B's and 07's at 128,000 — above what
    several models accept — so escalating into one turns a recoverable budget
    stop into a rejected request, which is the failure mode the ladder exists to
    prevent. Clamping keeps every rung reachable.

    Tiers above the ceiling collapse onto it rather than being dropped, so a
    stage keeps its final attempt at the highest budget available instead of
    quietly losing its last retry. A ceiling of None (an unrecognised model)
    leaves the ladder alone: silently lowering a ceiling the route would have
    honoured is its own truncation bug.
    """
    if not ceiling or not tiers:
        return tuple(tiers)
    if max(tiers) <= ceiling:
        return tuple(tiers)
    clamped = tuple(dict.fromkeys(min(t, ceiling) for t in tiers))
    if verbose:
        label = f"Stage {stage} " if stage else ""
        print(f"  ! {label}ceiling {_token_tier_label(max(tiers))} exceeds this "
              f"model's {_token_tier_label(ceiling)} output limit — using "
              f"{', '.join(_token_tier_label(t) for t in clamped)}")
    return clamped


@dataclasses.dataclass
class AdaptiveStageStats:
    """Small run-local counter set for one adaptive stage's live summary."""
    stage: str
    total: int
    completed: int = 0
    first_pass_successes: int = 0
    budget_retries: int = 0
    json_repairs: int = 0
    terminal_failures: int = 0
    terminal_failure_ids: set[str] = dataclasses.field(default_factory=set)
    budget_promotions: int = 0
    starting_ceiling: int = ADAPTIVE_TOKEN_TIERS[0]
    final_ceiling: int = ADAPTIVE_TOKEN_TIERS[0]
    completion_tokens: list[int] = dataclasses.field(default_factory=list)
    reasoning_tokens: list[int] = dataclasses.field(default_factory=list)
    provider_routes: Counter = dataclasses.field(default_factory=Counter)


def _adaptive_percentage(completed, total):
    """Nearest whole percent without Python's surprising half-even rounding."""
    return int((100 * completed / total) + 0.5) if total else 100


def _adaptive_progress_bar(completed, total, width=ADAPTIVE_PROGRESS_WIDTH):
    fraction = completed / total if total else 1
    filled = min(width, int((width * fraction) + 0.5))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _adaptive_wave_label(jobs):
    ids = [job.id for job in jobs]
    if len(ids) < 2:
        return ids[0] if ids else "none"
    try:
        numbers = [int(jid[1:]) for jid in ids]
    except ValueError:
        return ", ".join(ids)
    if all(b == a + 1 for a, b in zip(numbers, numbers[1:])):
        return f"{ids[0]}–{ids[-1]}"
    return ", ".join(ids)


def _print_adaptive_progress(stats):
    pct = _adaptive_percentage(stats.completed, stats.total)
    bar = _adaptive_progress_bar(stats.completed, stats.total)
    print(f"  Stage {stats.stage}  {bar} {stats.completed}/{stats.total} "
          f"batches ({pct}%)")
    print(f"  Current ceiling: {_token_tier_label(stats.final_ceiling)}")
    print(f"  First-pass successes: {stats.first_pass_successes}  ·  "
          f"Budget retries: {stats.budget_retries}  ·  "
          f"Repairs: {stats.json_repairs}  ·  Failures: {stats.terminal_failures}")


def _token_distribution(values):
    ordered = sorted(values)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return min(ordered), statistics.median(ordered), p95, max(ordered)


def _format_token_value(value):
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _approx_prompt_tokens(text):
    """Match the OpenRouter planning estimate used elsewhere in the CLI."""
    return max(len(text) // 4, 1)


def _stage03e_prompt_composition(skill_text, catalogue_json, jobs,
                                 evidence_payloads):
    """Estimate rendered 03E prompt components without changing the request.

    Providers expose an actual token count only for the complete prompt, not for
    individual prompt regions. This diagnostic therefore uses the same labelled
    ~4-characters/token estimate as the OpenRouter cost preview, but measures the
    exact strings that Stage 03E is about to send.
    """
    if not jobs or len(jobs) != len(evidence_payloads):
        raise ValueError("03E composition requires one evidence payload per job")

    instructions = _approx_prompt_tokens(
        SEGMENT_PREAMBLE + "\n\n" + skill_text +
        STAGE03E_EVIDENCE_PREFIX.removesuffix("EVIDENCE:\n"))
    catalogue = _approx_prompt_tokens(catalogue_json)
    evidence_tokens = [_approx_prompt_tokens(text) for text in evidence_payloads]
    item_counts = [len(job.expected_ids or ()) for job in jobs]

    schema_other = []
    for job in jobs:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True,
                            "schema": job.schema},
        }
        rendered = (STAGE03E_CATALOGUE_MARKER + "EVIDENCE:\n" +
                    json.dumps(response_format, ensure_ascii=False,
                               separators=(",", ":")))
        schema_other.append(_approx_prompt_tokens(rendered))

    median_evidence = statistics.median(evidence_tokens)
    representative = min(
        range(len(jobs)),
        key=lambda index: (abs(evidence_tokens[index] - median_evidence), index))
    totals = [instructions + catalogue + evidence_tokens[index] +
              schema_other[index] for index in range(len(jobs))]
    return {
        "job_id": jobs[representative].id,
        "instructions": instructions,
        "candidate_catalogue": catalogue,
        "evidence_batch": evidence_tokens[representative],
        "schema_other": schema_other[representative],
        "total": totals[representative],
        "candidate_count": len(json.loads(catalogue_json)),
        "batch_count": len(jobs),
        "evidence_items": item_counts[representative],
        "evidence_item_range": (min(item_counts), statistics.median(item_counts),
                                max(item_counts)),
        "evidence_token_range": (min(evidence_tokens),
                                 statistics.median(evidence_tokens),
                                 max(evidence_tokens)),
        "total_token_range": (min(totals), statistics.median(totals), max(totals)),
    }


def _print_stage03e_prompt_composition(composition):
    low_items, median_items, high_items = composition["evidence_item_range"]
    low_tokens, median_tokens, high_tokens = composition["evidence_token_range"]
    low_total, median_total, high_total = composition["total_token_range"]
    candidate_label = ("candidate" if composition["candidate_count"] == 1
                       else "candidates")
    print("\n  03E prompt composition per request (~4 chars/token):")
    print(f"    representative:      {composition['job_id']} "
          f"({composition['evidence_items']} evidence items)")
    print(f"    instructions:        {composition['instructions']:,} tokens")
    print(f"    candidate catalogue: {composition['candidate_catalogue']:,} tokens "
          f"({composition['candidate_count']} {candidate_label}; repeated per request)")
    print(f"    evidence batch:      {composition['evidence_batch']:,} tokens")
    print(f"    schema/other:        {composition['schema_other']:,} tokens")
    print(f"    total:               {composition['total']:,} tokens")
    print(f"    requests:            {composition['batch_count']}")
    print(f"    evidence items:      {low_items}/{_format_token_value(median_items)}/"
          f"{high_items} min/median/max")
    print(f"    evidence tokens:     {low_tokens:,}/"
          f"{_format_token_value(median_tokens)}/{high_tokens:,} min/median/max")
    print(f"    total tokens:        {low_total:,}/"
          f"{_format_token_value(median_total)}/{high_total:,} min/median/max")


def _print_token_distribution(label, values, total):
    if not values:
        return
    low, median, p95, high = _token_distribution(values)
    coverage = (f" ({len(values)}/{total} batches reported)"
                if len(values) != total else "")
    print(f"  {label}:{coverage}")
    print(f"    min:    {_format_token_value(low)}")
    print(f"    median: {_format_token_value(median)}")
    print(f"    p95:    {_format_token_value(p95)}")
    print(f"    max:    {_format_token_value(high)}")


def _print_adaptive_summary(stats):
    print(f"\n  Stage {stats.stage} complete")
    print(f"  Batches:              {stats.completed}/{stats.total}")
    print(f"  First-pass successes: {stats.first_pass_successes}")
    print(f"  Budget retries:        {stats.budget_retries}")
    print(f"  JSON repairs:          {stats.json_repairs}")
    print(f"  Terminal failures:     {stats.terminal_failures}")
    print(f"  Starting ceiling:      {_token_tier_label(stats.starting_ceiling)}")
    print(f"  Final ceiling:         {_token_tier_label(stats.final_ceiling)}")
    print(f"  Budget promotions:     {stats.budget_promotions}")
    _print_token_distribution("Completion tokens", stats.completion_tokens,
                              stats.total)
    _print_token_distribution("Reasoning tokens", stats.reasoning_tokens,
                              stats.total)
    if stats.provider_routes:
        routes = " · ".join(f"{name}: {count}" for name, count in
                            stats.provider_routes.most_common())
        print(f"  Provider routes:       {routes}")


def filter_chunk(cfg, args):
    return int(getattr(args, "chunk", None)
               or cfg.get("filter", {}).get("chunk") or FILTER_CHUNK)


def dedup_effort(cfg, args):
    """Reasoning depth for skill 02 only.

    Defaults to None — the client's own setting, i.e. unchanged. Skill 02 is
    semantic duplicate detection: deciding that two differently-worded comments
    are the same person's same experience is exactly the judgement reasoning
    helps with, and its own skill file warns that the cardinal sin is the false
    merge. It gets a knob so the question can be measured, not a new default.
    """
    return (getattr(args, "dedup_effort", None)
            or cfg.get("dedup", {}).get("effort") or DEDUP_EFFORT)


def filter_effort(cfg, args):
    """Reasoning depth for skill 01 only.

    `none` disables reasoning outright where the provider allows it. It is not
    the default: a borderline retain/reject is a real judgement call, and the
    measured problem was reasoning spent reconciling contradictory instructions
    rather than reasoning spent judging. Fix the instructions first; turn the
    reasoning off only if your own spot-check says the quality holds.
    """
    return (getattr(args, "filter_effort", None)
            or cfg.get("filter", {}).get("effort") or FILTER_EFFORT)


def _token_tier_label(tokens):
    return f"{tokens // 1000}k"


def _adaptive_wave_size(client_):
    """OpenRouter can safely keep four calls in flight; Anthropic stays serial.

    Anthropic's client method submits its whole argument as one remote Batch API
    job and only yields after the batch ends. One request per wave is the simple
    safe choice there: a learned floor can then govern every request not started.
    OpenRouter's `batch` already owns a four-thread pool, so four is both its
    existing concurrency and the natural rolling-wave boundary.
    """
    try:
        import openrouter
        return (ADAPTIVE_OPENROUTER_WAVE_SIZE
                if isinstance(client_, openrouter.Client) else 1)
    except ImportError:
        return 1


def _adaptive_validation_error(job, result, key, row_validator=None,
                               row_cleaner=None):
    """Return why a completed adaptive-stage reply is not a success."""
    stop = result.stop_reason or ""
    if stop in NO_RETRY_STOP_REASONS or stop.startswith(("request_error", "batch_")):
        return _failure_reason(result, "provider did not complete the request")
    try:
        _decode_job_rows(job, result.text, key, row_validator=row_validator,
                         row_cleaner=row_cleaner)
    except (ValueError, TypeError) as e:
        return _failure_reason(result, str(e))
    return None


def _run_adaptive_stage(client_, corpus, preamble, jobs, diagnostics_dir, *,
                        stage, key, wave_size=1, stats=None, debug=False,
                        row_validator=None, row_cleaner=None,
                        token_tiers=ADAPTIVE_TOKEN_TIERS, batch_runner=None,
                        success_callback=None, first_pass_exclusions=None):
    """Run one structured stage with a live, persistent output-token floor.

    New work is launched in rolling waves. A wave is wholly in flight before any
    result is inspected, so every member may finish at the old ceiling. At the
    boundary, only an output-budget stop promotes the global floor. A successful
    response never promotes, regardless of utilisation. Budget-exhausted jobs are
    retried before any never-started job, which lets their next result promote
    again before more work is launched.

    Returns raw results plus the exact Job variant used for each final first-pass
    response. Shape/provider failures deliberately remain raw so the caller's
    `_batch_rows` call can use existing recovery paths without teaching the
    generic recovery code about this state machine.
    """
    if not jobs:
        return {}, []
    if wave_size < 1:
        raise ValueError(f"Stage {stage} wave_size must be at least 1")
    stats = stats or AdaptiveStageStats(stage=stage, total=len(jobs))
    if first_pass_exclusions is None:
        first_pass_exclusions = set()
    if stats.total != len(jobs):
        raise ValueError(f"Stage {stage} stats total does not match the job count")
    if stats.stage != stage:
        raise ValueError(f"Stage {stage} stats were labelled for Stage {stats.stage}")

    # Bound the ladder before anything is scheduled, so a stage never escalates
    # into a ceiling the model will reject.
    token_tiers = _clamp_tiers(token_tiers, _model_ceiling(client_), stage=stage)
    requested_start = min(max(job.max_tokens for job in jobs), max(token_tiers))
    tier_index = next(
        (index for index, tier in enumerate(token_tiers)
         if tier >= requested_start), len(token_tiers) - 1)
    pending = list(jobs)
    retrying = []
    results = {}
    final_jobs = {}
    attempts = defaultdict(list)
    stats.starting_ceiling = token_tiers[tier_index]
    stats.final_ceiling = stats.starting_ceiling
    start = _token_tier_label(stats.starting_ceiling)
    print(f"  Stage {stage} starting ceiling: {start}")
    if debug:
        print(f"  Scheduler: {wave_size}-request rolling waves; promotions require "
              "finish_reason='length' or 'max_tokens'")
    _print_adaptive_progress(stats)

    while retrying or pending:
        is_retry_wave = bool(retrying)
        queue = retrying if retrying else pending
        originals = queue[:wave_size]
        del queue[:wave_size]
        ceiling = token_tiers[tier_index]
        wave = [dataclasses.replace(job, max_tokens=ceiling) for job in originals]
        print(f"  Current wave: {_adaptive_wave_label(wave)}")
        if debug:
            kind = "budget retry" if is_retry_wave else "untouched work"
            print(f"  Scheduler: launching {kind} at {_token_tier_label(ceiling)}; "
                  f"{len(retrying)} retry and {len(pending)} untouched queued")
        replies = ((batch_runner or client_.batch)(corpus, preamble, wave))

        exhausted = []
        for job in wave:
            result = _as_result(replies.get(job.id, ""))
            if result.provider:
                stats.provider_routes[result.provider] += 1
            attempts[job.id].append((job, result))
            label = _token_tier_label(job.max_tokens)
            if result.out_of_budget:
                reason = _failure_reason(result, "output budget exhausted")
                _save_failure(diagnostics_dir, job, result, reason,
                              f"budget_{job.max_tokens}")
                if debug:
                    print(f"    stop_reason={result.stop_reason!r} · "
                          f"{result.budget_note or 'usage metadata unavailable'}")
                    artifact = os.path.join(
                        diagnostics_dir, f"{job.id}.budget_{job.max_tokens}.txt")
                    print(f"    diagnostics: {artifact}")
                exhausted.append(job)
                continue

            validation_error = _adaptive_validation_error(
                job, result, key, row_validator=row_validator,
                row_cleaner=row_cleaner)
            results[job.id] = result
            final_jobs[job.id] = job
            stats.completed += 1
            if validation_error:
                print(f"    ! {job.id}  recovery required  {validation_error}")
                if debug:
                    print(f"      stop_reason={result.stop_reason!r}; ceiling={label}")
                continue

            if (len(attempts[job.id]) == 1
                    and job.id not in first_pass_exclusions):
                stats.first_pass_successes += 1
            usage = result.completion_tokens
            utilisation = usage / job.max_tokens if usage else 0
            if usage:
                stats.completion_tokens.append(usage)
                print(f"    ✓ {job.id}  success  {usage:,}/{job.max_tokens:,} tokens "
                      f"({utilisation:.2%})")
            else:
                print(f"    ✓ {job.id}  success  usage metadata unavailable")
            if result.reasoning_tokens:
                stats.reasoning_tokens.append(result.reasoning_tokens)
            if success_callback is not None:
                success_callback(job, result)
            if debug:
                answer = max(result.completion_tokens - result.reasoning_tokens, 0)
                print(f"      stop_reason={result.stop_reason!r}; reasoning="
                      f"{result.reasoning_tokens:,}; answer={answer:,}; provider="
                      f"{result.provider or 'unreported'}")

        if exhausted and tier_index == len(token_tiers) - 1:
            stats.terminal_failures += len(exhausted)
            stats.terminal_failure_ids.update(job.id for job in exhausted)
            failed = ", ".join(job.id for job in exhausted)
            detail = "; ".join(
                f"{job.id}: {attempts[job.id][-1][1].budget_note or 'usage not reported'}"
                for job in exhausted)
            print(f"\n  STAGE {stage} FAILED")
            for job in exhausted:
                print(f"  {job.id} exhausted the maximum "
                      f"{_token_tier_label(token_tiers[-1])} ceiling")
            if success_callback is None:
                print(f"  No Stage {stage} output was written.")
            else:
                print(f"  Completed Stage {stage} chunk artifacts remain reusable.")
            print(f"  Diagnostics saved to: {diagnostics_dir}")
            raise BatchOutputError(
                f"Stage {stage} hard ceiling exhausted: {failed} reached "
                f"{_token_tier_label(ceiling)} and still ended for output budget "
                f"({detail}). Attempts and truncated replies were saved to "
                f"{diagnostics_dir}. " +
                ("No stage output was written." if success_callback is None else
                 "Completed chunk artifacts were preserved for resume."))

        if exhausted:
            old = ceiling
            tier_index += 1
            new = token_tiers[tier_index]
            stats.budget_promotions += 1
            stats.final_ceiling = new
            stats.budget_retries += len(exhausted)
            print("\n  BUDGET EXHAUSTED")
            for job in exhausted:
                print(f"  {job.id} hit {_token_tier_label(old)} before completing "
                      "the response")
            print("  Retry:")
            for job in exhausted:
                print(f"  {job.id}  {_token_tier_label(old)} → "
                      f"{_token_tier_label(new)}")
            print(f"  New floor for untouched batches: {_token_tier_label(new)}")
            if debug:
                print("  Scheduler: budget retries are placed ahead of all "
                      "untouched batches")
        else:
            stats.final_ceiling = ceiling

        # An exhausted request is always resolved before never-started work. Put
        # the just-failed jobs at the front even if a larger retry wave was
        # waiting, so another insufficiency can promote the shared floor first.
        if exhausted:
            retrying[:0] = exhausted

        _print_adaptive_progress(stats)

    ordered_jobs = [final_jobs[job.id] for job in jobs]
    return results, ordered_jobs


def _run_stage01_adaptive(client_, corpus, preamble, jobs, diagnostics_dir,
                          wave_size=1, stats=None, debug=False):
    return _run_adaptive_stage(
        client_, corpus, preamble, jobs, diagnostics_dir, stage="01",
        key="records", wave_size=wave_size, stats=stats, debug=debug)


def _run_stage02_adaptive(client_, corpus, preamble, jobs, diagnostics_dir,
                          wave_size=1, stats=None, debug=False):
    return _run_adaptive_stage(
        client_, corpus, preamble, jobs, diagnostics_dir, stage="02",
        key="groups", wave_size=wave_size, stats=stats, debug=debug)


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


def _single_call_result(client_, corpus, preamble, job, operation):
    """Run one Job without discarding the provider's failure metadata.

    `one()` predates BatchResult and intentionally returns text for the many
    unstructured synthesis callers. Real clients now expose `one_result()` for
    structured callers; the fallback keeps small test/dummy clients compatible.
    """
    result_call = getattr(client_, "one_result", None)
    if result_call is not None:
        return _as_result(result_call(
            corpus, preamble, job.prompt, job.max_tokens, job.schema,
            job_id=job.id, operation=operation, effort=job.effort,
            reasoning_max_tokens=job.reasoning_max_tokens))
    return _as_result(client_.one(
        corpus, preamble, job.prompt, job.max_tokens, job.schema))


def _single_call_tiers(starting_ceiling, token_tiers=ADAPTIVE_TOKEN_TIERS):
    """The current ceiling followed by this stage's coarse tiers above it."""
    return (starting_ceiling,) + tuple(
        tier for tier in token_tiers if tier > starting_ceiling)


def _run_single_structured(client_, corpus, preamble, job, diagnostics_dir,
                           token_tiers=ADAPTIVE_TOKEN_TIERS,
                           response_validator=None, repair_guidance=None,
                           validation_error_handler=None, initial_result=None,
                           repair_merger=None, attempt_reporter=None):
    """Run one structured Job with failure-aware, bounded recovery.

    Budget stops retry the identical Job at the next coarse ceiling. Refusals,
    filters and unexplained empty replies fail immediately. A complete but
    malformed/schema-invalid reply gets one shape-only repair. At no point does
    an empty budget-starved response reach the JSON parser.
    """
    # Same clamp as the batched path: the job's own starting budget is bounded
    # too, since a stage can open at a tier the model would already reject.
    ceiling = _model_ceiling(client_)
    tiers = _clamp_tiers(_single_call_tiers(job.max_tokens, token_tiers), ceiling)
    tier_index = 0
    current = job if job.max_tokens <= tiers[0] else dataclasses.replace(
        job, max_tokens=tiers[0])
    repairing = False
    pending_result = (_as_result(initial_result)
                      if initial_result is not None else None)
    repair_source = None

    while True:
        operation = ("single_structured_repair" if repairing
                     else "single_structured")
        if pending_result is not None:
            result = pending_result
            pending_result = None
        else:
            result = _single_call_result(
                client_, corpus, preamble, current, operation)

        if attempt_reporter is not None:
            attempt_reporter(current, result, repairing)

        if result.out_of_budget:
            reason = _failure_reason(result, "output budget exhausted")
            _save_failure(
                diagnostics_dir, current, result, reason,
                f"budget_{current.max_tokens}")
            if tier_index + 1 >= len(tiers):
                print(f"\n  STRUCTURED CALL FAILED")
                print(f"  {job.id} exhausted the maximum "
                      f"{_token_tier_label(current.max_tokens)} ceiling")
                print(f"  {result.budget_note or 'Usage metadata unavailable.'}")
                print(f"  Diagnostics saved to: {diagnostics_dir}")
                raise BatchOutputError(
                    f"{job.id} stopped at its output token budget "
                    f"(finish_reason={result.stop_reason!r}) at "
                    f"{current.max_tokens:,} tokens; no complete structured "
                    f"response was produced. Diagnostics: {diagnostics_dir}")
            old = current.max_tokens
            tier_index += 1
            current = dataclasses.replace(current, max_tokens=tiers[tier_index])
            print(f"\n  OUTPUT BUDGET EXHAUSTED")
            print(f"  {job.id}: {_token_tier_label(old)} → "
                  f"{_token_tier_label(current.max_tokens)}")
            print(f"  {result.budget_note or 'Usage metadata unavailable.'}")
            print("  Retrying the same structured request with more room")
            continue

        if result.stop_reason in NO_RETRY_STOP_REASONS:
            reason = _failure_reason(result, "provider refused the request")
            _save_failure(diagnostics_dir, current, result, reason, "blocked")
            raise BatchOutputError(
                f"{job.id} provider/refusal failure: {reason}. "
                f"Diagnostics: {diagnostics_dir}")

        if not result.text.strip():
            reason = _failure_reason(result, "provider returned no content")
            _save_failure(diagnostics_dir, current, result, reason, "empty")
            raise BatchOutputError(
                f"{job.id} provider failure: {reason}. "
                f"Diagnostics: {diagnostics_dir}")

        recovery_error = None
        decoded = None
        try:
            decoded = _json_object(result.text)
            if repairing and repair_merger is not None and repair_source is not None:
                decoded = repair_merger(repair_source, decoded)
            # Stage-specific semantic/provenance contracts run before the
            # general schema check so they can report useful source identifiers
            # instead of only an anonymous enum path. Validators must tolerate
            # missing/wrong-shape fields and leave those to `_schema_issue`.
            if response_validator is not None:
                response_validator(decoded)
            issue = _schema_issue(decoded, current.schema) if current.schema else None
            if issue:
                raise ValueError(f"schema validation failed: {issue}")
            return decoded
        except (ValueError, TypeError) as parse_error:
            recovery_error = parse_error
            if (not repairing and repair_merger is not None and decoded is not None
                    and isinstance(
                        parse_error, segmentation.ConsolidationContractError)):
                repair_source = decoded
            reason = _failure_reason(result, str(parse_error))
            if validation_error_handler is not None:
                validation_error_handler(
                    parse_error, repairing=repairing,
                    diagnostics_dir=diagnostics_dir)
            _save_failure(
                diagnostics_dir, current, result, reason,
                "repair" if repairing else
                ("contract" if isinstance(
                    parse_error, segmentation.ConsolidationContractError)
                 else "malformed"))
            if repairing:
                if not isinstance(
                        parse_error, segmentation.ConsolidationContractError):
                    raise BatchOutputError(
                        f"{job.id} returned malformed/schema-invalid JSON; its "
                        f"shape-repair response also failed ({reason}). "
                        f"Diagnostics: {diagnostics_dir}") from parse_error
                raise BatchOutputError(
                    f"{job.id} returned a structured response that still "
                    f"violated its contract after one repair ({reason}). "
                    f"Diagnostics: {diagnostics_dir}") from parse_error

        repair_instruction = repair_guidance or (
            "Preserve all substantive candidates, decisions, scores, rationales, "
            "IDs and criteria already present. This is shape repair, not a chance "
            "to reconsider the research.")
        repair_prompt = (
            "RECOVERY TASK: The previous response was complete but could not be "
            f"consumed because: {reason}.\n\n"
            "Return JSON only and obey the supplied JSON schema exactly. "
            f"{repair_instruction} Do not add commentary or Markdown.\n\n"
            "ORIGINAL REQUEST:\n\n" + job.prompt +
            "\n\nPREVIOUS RESPONSE:\n\n" + result.text)
        current = dataclasses.replace(current, prompt=repair_prompt)
        repairing = True
        if isinstance(recovery_error, segmentation.ConsolidationContractError):
            print(f"  ! {job.id} returned an unusable structured response; "
                  "running one bounded contract repair")
        else:
            print(f"  ! {job.id} returned malformed/schema-invalid JSON; "
                  "running one structured shape repair")


def _report_stage04_attempt(job, result, repairing=False):
    """Print the Stage 04 budget split for every inference attempt."""
    phase = "repair" if repairing else "validation"
    stop = result.stop_reason or "unknown"
    usage = result.budget_note or "usage metadata unavailable"
    print(f"  Stage 04 {phase} attempt at {_token_tier_label(job.max_tokens)}: "
          f"finish_reason={stop} · {usage}")


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
    if kind == "string":
        if len(value) < schema.get("minLength", 0):
            return f"{path} is shorter than minLength"
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(pattern, value) is None:
            return f"{path} does not match the required pattern"
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
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            return f"{path} has fewer than minItems"
        if schema.get("uniqueItems"):
            markers = [json.dumps(item, sort_keys=True, ensure_ascii=False)
                       for item in value]
            if len(markers) != len(set(markers)):
                return f"{path} must contain unique items"
        if "items" in schema:
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


def _decode_job_rows(job, raw, key, row_validator=None, row_cleaner=None):
    payload = _json_object(raw)
    try:
        rows = payload[key]
    except KeyError as e:
        raise ValueError(f"missing top-level {key!r}") from e
    if not isinstance(rows, list):
        raise ValueError(f"{key!r} is not a list")
    # A stage may have a tiny, provably safe normalization that must precede
    # schema validation (03A's per-chunk enum is the motivating case). It may
    # change only its documented invariant; all remaining schema checks follow.
    if row_cleaner is not None:
        row_cleaner(job, rows)
    issue = _schema_issue(payload, job.schema)
    if issue:
        raise ValueError(issue)
    if job.expected_ids is not None:
        issue = _coverage_issue(rows, job.expected_ids)
        if issue:
            raise ValueError(f"incomplete record coverage ({issue})")
    if row_validator is not None:
        row_validator(job, rows)
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
MAX_BATCH_REPAIR_ATTEMPTS = 1


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
    if result.provider_error:
        error = result.provider_error
        message = error.get("message") or "no provider message"
        location = ""
        if error.get("custom_id") or error.get("batch_id"):
            location = (f", custom_id {error.get('custom_id')!r}, "
                        f"batch {error.get('batch_id')!r}")
        return (f"provider batch error {error.get('type', 'unknown')!r}: "
                f"{message}{location}")
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
    provider_error = (json.dumps(
        result.provider_error, ensure_ascii=False, sort_keys=True)
        if result.provider_error else "none")
    with open(os.path.join(diagnostics_dir, f"{job.id}.{suffix}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(f"WHY SAVED: {reason}\n"
                 f"STOP REASON: {result.stop_reason or 'unknown'}\n"
                 f"MAX TOKENS: {job.max_tokens}\n"
                 f"REASONING ASKED: {job.reasoning_max_tokens or 'uncapped'}"
                 f" (effort {job.effort or 'client default'})\n"
                 f"BUDGET SPENT: {result.budget_note or 'not reported'}\n"
                 f"RESPONSE CHARS: {len(result.text)}\n"
                 f"PROVIDER ERROR: {provider_error}\n\n"
                 f"{result.text}")


def _rerun_batch(client_, corpus, preamble, failures, factor):
    """Re-issue the ORIGINAL requests with a larger output budget.

    The right recovery when a provider never finished writing: there is no prior
    output worth repairing, and the repair prompt is strictly LONGER than the
    request that already exhausted the budget — so repairing a budget failure at
    the same budget is close to guaranteed to fail the same way.

    A re-run is the SAME request with more room, so it is derived from the job
    rather than rebuilt from its parts. Listing the fields by hand is what broke
    this: the rebuild named prompt, budget, schema and ids but not `effort` or
    `reasoning_max_tokens`, so every recovery request fell back to the client
    default and a stage pinned to a 2,000-token reasoning ceiling re-ran at
    `reasoning: {"effort": "high"}` with no ceiling — escalating reasoning on
    requests that had failed because of reasoning, and spending the wider budget
    on thinking rather than answering. `replace` carries every field the caller
    does not explicitly change, including ones added later.
    """
    jobs = []
    for job, result, _reason in failures:
        # Prefer the observed spend over a multiple of a budget already known to
        # be wrong: a reply that stopped at its ceiling proves only that the
        # ceiling was too low, never by how much.
        budget = max(job.max_tokens * factor, result.completion_tokens * factor)
        jobs.append(dataclasses.replace(job, max_tokens=int(budget)))
    return client_.batch(corpus, preamble, jobs)


def _repair_batch(client_, corpus, preamble, failures, key, guidance=None,
                  preserve_guidance=None):
    """Recover invalid model outputs in one structured recovery batch.

    Most callers need shape-only repair.  A stage-specific semantic validator
    may instead supply concise regeneration guidance so a vacuous but valid JSON
    judgement is reconsidered rather than faithfully preserved.
    """
    from llm import Job

    jobs = []
    for job, raw, reason in failures:
        raw = _raw_text(raw)
        prior = raw if raw.strip() else "[No response was returned. Re-run the request.]"
        if guidance:
            instruction = (
                "The JSON shape may be valid, but the substantive result violated "
                "this stage's semantic contract. Re-evaluate the ORIGINAL REQUEST "
                "and regenerate the affected judgement; do not preserve a generic, "
                "unsupported, or structurally invalid candidate merely because it "
                "appeared in the previous response.\n\n" + guidance)
        else:
            instruction = (
                "Preserve every decision, reason, group, score, cue, and assignment "
                "already present in the previous response; this is data-shape "
                "repair, not a chance to reconsider those judgments. Use the "
                "original request only to restore an omitted input row or to re-run "
                "the job when no response was returned.")
            if preserve_guidance:
                instruction += "\n\n" + preserve_guidance
        prompt = (
            "RECOVERY TASK: The previous model response could not be consumed by "
            f"the pipeline because: {reason}.\n\n"
            "Return JSON only and obey the supplied JSON schema exactly. "
            + instruction + " The final top-level array must be named "
            f"{key!r}. Do not add commentary or Markdown.\n\n"
            "ORIGINAL REQUEST:\n\n" + job.prompt +
            "\n\nPREVIOUS RESPONSE:\n\n" + prior)
        # Derived from the original for the same reason as the re-run: a repair
        # that silently reasons at the client default is a different request
        # from the one being repaired, and a more expensive one.
        jobs.append(dataclasses.replace(job, prompt=prompt))
    return client_.batch(corpus, preamble, jobs)


def _decode_into(decoded, job, result, key, row_validator=None, row_cleaner=None):
    """Decode one reply into `decoded`, or return the reason it could not be.

    Returns None on success. The reason is derived from the provider's stop
    reason first and the parser's complaint only second, so an unfinished reply
    is never reported as a JSON syntax error.
    """
    try:
        decoded[job.id] = _decode_job_rows(
            job, result.text, key, row_validator=row_validator,
            row_cleaner=row_cleaner)
        return None
    except (ValueError, TypeError) as e:
        return _failure_reason(result, str(e))


def _failure_mode(result):
    """The shortest true name for how one reply failed.

    "Truncated mid-output" and "nothing written at all" are the same
    `finish_reason` and different problems: the first says the answer did not
    fit beside the reasoning, the second says the reasoning consumed the budget
    outright. Only the first was ever announced, and only in the empty case — so
    a run where 76 replies were cut off mid-JSON printed nothing until the totals
    arrived, and then reported all of them as one undifferentiated number.
    """
    if result.stop_reason in NO_RETRY_STOP_REASONS:
        return "blocked by the provider"
    if result.provider_error:
        return ("provider batch error: " +
                str(result.provider_error.get("type") or "unknown"))
    if (result.stop_reason or "").startswith("request_error"):
        return "the request never completed"
    if result.out_of_budget:
        return ("hit the output budget before writing anything"
                if not (result.text or "").strip()
                else "hit the output budget partway through the answer")
    if not (result.text or "").strip():
        return "returned an empty reply"
    return "returned the wrong shape"


def _report_failure_modes(failures, total, key):
    """Name the first-pass failure modes, with counts and what they cost."""
    modes = Counter(_failure_mode(r) for _j, r, _x in failures)
    print(f"  ! {len(failures)}/{total} {key} response(s) failed on the first pass:")
    for mode, n in modes.most_common():
        spent = [r for _j, r, _x in failures
                 if _failure_mode(r) == mode and r.completion_tokens]
        detail = ""
        if spent:
            # The median, not the mean: one outlier should not decide what the
            # operator thinks the budget is being spent on.
            reasoning = statistics.median(r.reasoning_tokens for r in spent)
            asked = [j.reasoning_max_tokens for j, r, _x in failures
                     if _failure_mode(r) == mode and j.reasoning_max_tokens]
            over = (" — OVER the ceiling these requests asked for"
                    if asked and reasoning > min(asked) else "")
            detail = f" · median {reasoning:,.0f} reasoning tokens{over}"
        print(f"      {n:>4}  {mode}{detail}")


def _batch_rows(results, jobs, key, diagnostics_dir, repair=None, rerun=None,
                rerun_factor=BUDGET_RETRY_FACTOR, adaptive_stats=None,
                row_validator=None, row_cleaner=None):
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
                  else _decode_into(
                      decoded, job, result, key, row_validator=row_validator,
                      row_cleaner=row_cleaner))
        if reason:
            first_result[jid] = result
            failures.append((job, result, reason))

    unexpected = sorted(set(results) - set(by_id))
    for job, result, reason in failures:
        _save_failure(diagnostics_dir, job, result, reason, "original")
    if failures:
        _report_failure_modes(failures, len(jobs), key)

    # ---- unfinished replies: re-run the original request with more room ------
    if failures and rerun is not None:
        starved = [f for f in failures if f[1].retryable]
        if starved:
            retry_budget = ("the same token budget" if rerun_factor == 1 else
                            f"{rerun_factor}x the token budget")
            print(f"  ! {len(starved)}/{len(jobs)} {key} response(s) never finished "
                  f"(output budget, empty or failed request) — re-running those "
                  f"requests at {retry_budget}")
            replies = rerun(starved, rerun_factor)
            recovered, still_bad = set(), []
            for job, result, original_reason in starved:
                retried = _as_result(replies.get(job.id, ""))
                _save_failure(diagnostics_dir, job, retried,
                              f"re-run of: {original_reason}", "rerun")
                if job.id not in replies:
                    still_bad.append((job, result,
                                      f"{original_reason}; re-run returned no response"))
                    continue
                reason = _decode_into(
                    decoded, job, retried, key, row_validator=row_validator,
                    row_cleaner=row_cleaner)
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
    repair_attempts = 0
    if failures and repair is not None and MAX_BATCH_REPAIR_ATTEMPTS:
        # Only replies that actually contain text. An empty one has no shape to
        # repair — that was the original failure, and sending "" to the repair
        # prompt just pays for a second helping of it.
        fixable = [f for f in failures if f[1].repairable]
        if fixable:
            repair_attempts += 1
            if adaptive_stats is not None:
                adaptive_stats.json_repairs += len(fixable)
            chunk_ids = ", ".join(job.id for job, _result, _reason in fixable)
            print(f"  ! repairing {len(fixable)} malformed {key} response(s) "
                  f"in one additional structured batch (attempt 1/"
                  f"{MAX_BATCH_REPAIR_ATTEMPTS}; chunks: {chunk_ids})")
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
                reason = _decode_into(
                    decoded, job, fixed, key, row_validator=row_validator,
                    row_cleaner=row_cleaner)
                if reason:
                    still_bad.append((job, result,
                                      f"original: {original_reason}; repair: {reason}"))
            failures = [f for f in failures if not f[1].repairable] + still_bad

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
            "blocked by the provider" if first.stop_reason in NO_RETRY_STOP_REASONS else
            "empty response" if not first.text.strip() else "malformed JSON"
            for first in (first_result[job.id] for job, _r, _reason in failures))
        if unexpected:
            modes["unexpected response id"] += len(unexpected)
        breakdown = ", ".join(f"{n} {name}" for name, n in modes.most_common())
        raise BatchOutputError(
            f"{len(descriptions)}/{len(jobs)} batch response(s) could not be used "
            f"after recovery (bounded repair attempts: {repair_attempts}/"
            f"{MAX_BATCH_REPAIR_ATTEMPTS}) — {breakdown} ({sample}). Original, "
            "re-run and repaired "
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
# Substring match against a whole item, so every pattern here must be one that
# cannot appear inside something a customer wrote. `permalinkembedsave` and
# `sharesavehidereport` used to be on this list and are not, deliberately: they
# are the separators *between* old.reddit comments, not page furniture, so
# matching them condemned an entire thread rather than a line of chrome. They
# deleted 11.8M of 16.2M characters — whole subreddits at 100% — before
# split_old_reddit existed to cut them out properly. Anything the splitter
# leaves behind is one item's worth of junk, which skill 01 rejects as
# `interface_chrome` at a cost of one record instead of a thread.
BOILER = re.compile(
    r"welcome to reddit|become a redditor|create an account|sign up|log in|"
    r"this is an archived post|i am a bot|automoderator|"
    r"privacy policy|user agreement|content policy|"
    r"submission guidelines|weekly thread|link to wiki", re.I)


def reddit_source_fields(url):
    """Return (subreddit, thread_id) for a standard Reddit comments URL."""
    if not isinstance(url, str) or not url.strip():
        return None, None
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None, None
    if host != "reddit.com" and not host.endswith(".reddit.com"):
        return None, None
    parts = [part for part in parsed.path.split("/") if part]
    if (len(parts) < 4 or parts[0].lower() != "r"
            or parts[2].lower() != "comments"):
        return None, None
    subreddit, thread_id = parts[1].strip(), parts[3].strip()
    return (subreddit or None), (thread_id or None)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_jsonl_atomic(path, rows):
    """Write stable JSONL without leaving a half-written canonical export."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def refine_voc(cfg, input_path=None, groups_path=None, announce=True):
    """Deterministically split deduplicated VOC into production and audit files."""
    voc = paths.voc(cfg["_dir"])
    input_path = input_path or os.path.join(voc, "deduplicated_voc.jsonl")
    groups_path = groups_path or os.path.join(voc, "duplicate_groups.jsonl")
    production_path = os.path.join(voc, PRODUCTION_VOC_FILE)
    audit_path = os.path.join(voc, AUDIT_VOC_FILE)
    if not os.path.exists(input_path):
        raise SystemExit(
            f"No completed deduplicated VOC at {input_path}. Run ingest first.")

    source_rows = _read_jsonl(input_path)
    groups = _read_jsonl(groups_path) if os.path.exists(groups_path) else []
    groups_by_canonical = defaultdict(list)
    for group in groups:
        canonical_id = group.get("canonical_id")
        if canonical_id is not None:
            groups_by_canonical[canonical_id].append(group)

    production, audit, seen_ids = [], [], set()
    subreddit_count = thread_count = 0
    held_back = Counter()
    for position, source in enumerate(source_rows, 1):
        if not isinstance(source, dict):
            raise ValueError(f"VOC record {position} is not an object")
        if "id" not in source or "text" not in source:
            raise ValueError(f"VOC record {position} is missing id or text")
        if source["id"] in seen_ids:
            raise ValueError(f"VOC record {position} repeats id={source['id']!r}")
        seen_ids.add(source["id"])
        evidence_id = source.get("evidence_id")
        if evidence_id is not None and evidence_id != source["id"]:
            raise ValueError(
                f"VOC record {position} has conflicting id={source['id']!r} "
                f"and evidence_id={evidence_id!r}")
        subreddit, thread_id = reddit_source_fields(source.get("url"))
        subreddit = subreddit or source.get("subreddit") or None
        thread_id = thread_id or source.get("thread_id") or None
        tier = segmentation.evidence_tier(source)
        # Both corpus policies are applied here rather than at ingest, because
        # this stage is deterministic and free: changing a project's scope or
        # re-tuning the corroboration bar is a re-run of refine-voc, not another
        # pass over 5,000 records at model prices. Held-back rows keep their
        # place in the audit file with the rule that held them, so the decision
        # is explainable and every one of them is recoverable.
        held = None
        if tier is not None:
            if corpus_policy.subreddit_excluded(subreddit, cfg):
                held = corpus_policy.OUT_OF_SCOPE_SUBREDDIT
            else:
                held = corpus_policy.corroboration_cut(source)
        if tier is not None and held is None:
            subreddit_count += int(subreddit is not None)
            thread_count += int(thread_id is not None)
            production.append({
                "id": source["id"],
                "text": source["text"],
                "thread_id": thread_id,
                "subreddit": subreddit,
                "tier": tier,
            })
        elif held is not None:
            held_back[held] += 1

        audit_row = {
            "id": source["id"],
            "text": source["text"],
            "url": source.get("url") or None,
            "title": source.get("title") or None,
            "thread_id": thread_id,
            "subreddit": subreddit,
            "tier": tier,
        }
        # Keep every upstream audit/decision field except the redundant model
        # response identifier. Production and audit both use canonical `id`.
        reserved = {"id", "evidence_id", "text", "url", "title",
                    "subreddit", "thread_id", "tier"}
        for key, value in source.items():
            if key not in reserved:
                audit_row[key] = value
        if held is not None:
            audit_row["production_excluded"] = held
        if groups_by_canonical.get(source["id"]):
            # Preserve the exact Stage 02 group objects and field names. The
            # envelope only associates them with their surviving canonical row.
            audit_row["dedup_groups"] = groups_by_canonical[source["id"]]
        audit.append(audit_row)

    _write_jsonl_atomic(production_path, production)
    _write_jsonl_atomic(audit_path, audit)
    missing = len(production) - min(subreddit_count, thread_count)
    tiers = segmentation.tier_counts(production)
    summary = {
        "input_path": input_path,
        "input": len(source_rows),
        "production": len(production),
        "audit": len(audit),
        "subreddits": subreddit_count,
        "thread_ids": thread_count,
        "missing_or_non_reddit": missing,
        "production_path": production_path,
        "audit_path": audit_path,
        "tiers": tiers,
        "held_back": dict(held_back),
        "audience_scope": corpus_policy.scope(cfg),
    }
    prompt_metrics = voc_prompt_view_metrics(production)
    summary.update(prompt_metrics)
    if announce:
        print("\n  refine-voc (deterministic local export)")
        print(f"  Source:     {input_path}")
        print(f"  Input:      {summary['input']:,} deduplicated evidence items")
        if held_back:
            total_held = sum(held_back.values())
            print(f"  Held back:  {total_held:,} record(s) "
                  f"({total_held / max(len(source_rows), 1) * 100:.1f}%) — "
                  f"kept in the audit file, withheld from research:")
            for line in corpus_policy.summarise_exclusions(held_back):
                print(line)
        print(f"  Production: {summary['production']:,} records")
        print(f"    -> {production_path}")
        print("  Evidence tiers:")
        print(f"    Core:       {tiers['core']:,}")
        print(f"    Supporting: {tiers['supporting']:,}")
        print(f"    Context:    {tiers['context']:,}")
        print(f"  Audit:      {summary['audit']:,} records")
        print(f"    -> {audit_path}")
        print(f"  Subreddit parsed: {subreddit_count:,}")
        print(f"  Thread IDs parsed: {thread_count:,}")
        print(f"  Missing/non-Reddit URLs: {missing:,}")
        print("  Segmentation input views (planning estimate, ~4 chars/token):")
        print(f"    Old monolithic Stage 03 corpus: ~"
              f"{prompt_metrics['stage03_before_est_tokens']:,} tokens")
        print(f"    Stage 03A Core evidence total: ~{prompt_metrics['stage03_est_tokens']:,} "
              f"tokens ({prompt_metrics['stage03_reduction_pct']:.1f}% reduction)")
        print(f"    Stage 04 raw corpus avoided: ~"
              f"{prompt_metrics['stage04_raw_corpus_est_tokens_avoided']:,} tokens")
        print("    Stage 04's compact packet is measured after discovery.")
        print("  No model calls made.")
    return summary


def cmd_refine_voc(cfg, args):
    try:
        refine_voc(cfg, input_path=getattr(args, "source", None))
    except ValueError as error:
        sys.exit(f"refine-voc failed: {error}")


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
    # Out-of-scope communities are dropped here rather than left for skill 01 to
    # judge. Membership of a diagnosis subreddit is a fact about the URL, not a
    # reading of the comment, and paying a model to re-derive it costs tokens on
    # every record the project has already decided it does not want.
    pre, seen, dropped = [], {}, Counter()
    excluded_communities = Counter()
    for item in blocks:
        text = re.sub(r"\s+", " ", item["text"]).strip()
        if len(text.split()) < cfg["filter"].get("min_words", 8):
            dropped["too_short"] += 1; continue
        if BOILER.search(text):
            dropped["interface_chrome"] += 1; continue
        subreddit, _thread = reddit_source_fields(item.get("url"))
        if corpus_policy.subreddit_excluded(subreddit, cfg):
            dropped[corpus_policy.OUT_OF_SCOPE_SUBREDDIT] += 1
            excluded_communities[f"r/{subreddit}"] += 1
            continue
        h = hashlib.sha256(text.lower().encode()).hexdigest()
        if h in seen:
            dropped["exact_duplicate"] += 1; continue
        seen[h] = True
        pre.append({"id": len(pre) + 1, "text": text,
                    "url": item.get("url", ""), "title": item.get("title", "")})

    print(f"  parsed {len(blocks):,} records")
    if excluded_communities:
        print(f"  audience scope '{corpus_policy.scope(cfg)}' excluded "
              f"{sum(excluded_communities.values()):,} record(s) from "
              f"{len(excluded_communities)} community/ies:")
        for line in corpus_policy.summarise_exclusions(excluded_communities):
            print(line)
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
    scope_rule = corpus_policy.scope_prompt(cfg)
    if scope_rule:
        ctx += "\n" + scope_rule + "\n"

    # ---- skill 01 -----------------------------------------------------------
    # Judgement sections only: the record shape, the duplicate pass and the
    # self-audit are stated by the schema, done in code, and not asked for.
    s01 = skill_sections(1, FILTER_SKILL_SECTIONS, include_intro=False)
    CHUNK = filter_chunk(cfg, args)
    chunks = [pre[i:i + CHUNK] for i in range(0, len(pre), CHUNK)]
    jobs = [Job(id=f"f{n:04d}",
                # The contract lives in the preamble and the schema. Repeating
                # it here in different words is what the model was reconciling.
                prompt=("Classify each record below: retain or reject, with "
                        "reason codes.\n\nRECORDS:\n\n"
                        + "\n\n".join(f"[{r['id']}] {r['text']}" for r in ch)),
                # Stage 01 starts generously and learns a persistent floor from
                # live route behaviour. Only max_tokens changes on promotion.
                max_tokens=ADAPTIVE_TOKEN_TIERS[0], schema=filter_schema(cfg),
                expected_ids=tuple(r["id"] for r in ch),
                # Skill 01 is the bouncer at the door: retain or reject, with
                # reasons drawn from a closed list. It is judgement, but it is
                # not deep reasoning, and paying synthesis-depth reasoning 83
                # times over is what left no budget to answer with.
                effort=filter_effort(cfg, args),
                reasoning_max_tokens=REASONING_RESERVE)
            for n, ch in enumerate(chunks)]

    prefix = f"{s01}\n\n---\n\n{ctx}"
    print(f"\n  01 filter: {len(pre):,} records in {len(jobs)} batched chunks "
          f"of {CHUNK} ({jobs[0].max_tokens:,} output tokens each, "
          f"effort {jobs[0].effort})")
    if not confirm(c.estimate(prefix, FILTER_PREAMBLE, jobs, batched=True), args.yes):
        return
    c.prewarm(prefix, FILTER_PREAMBLE)
    filter_failures = os.path.join(voc, "_model_failures", "01_filter")
    filter_stats = AdaptiveStageStats(stage="01", total=len(jobs))
    filter_results, jobs = _run_stage01_adaptive(
        c, prefix, FILTER_PREAMBLE, jobs, filter_failures,
        wave_size=_adaptive_wave_size(c), stats=filter_stats,
        debug=getattr(args, "stage01_debug", False))
    records = _batch_rows(
        filter_results, jobs, "records",
        filter_failures,
        repair=lambda failed: _repair_batch(
            c, prefix, FILTER_PREAMBLE, failed, "records"),
        rerun=lambda failed, factor: _rerun_batch(
            c, prefix, FILTER_PREAMBLE, failed, factor),
        # The adaptive executor has already resolved every output-budget stop.
        # Empty/provider failures retain the existing recovery path but must not
        # promote or widen the ceiling.
        rerun_factor=1, adaptive_stats=filter_stats)
    _require_exact_ids(records, {r["id"] for r in pre}, "skill 01 filter")
    _print_adaptive_summary(filter_stats)
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
    stage02_effort = dedup_effort(cfg, args)
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
                 # Stage 02 has its own adaptive floor. Its reasoning policy is
                 # intentionally independent from Stage 01 and remains under
                 # the client/project/CLI effort setting below.
                 max_tokens=ADAPTIVE_TOKEN_TIERS[0], schema=DEDUP_SCHEMA,
                 effort=stage02_effort)
             for n, ch in enumerate(dchunks)]

    dprefix = f"{s02}\n\n---\n\n{ctx}"
    print(f"\n  02 deduplicate: {len(retained):,} records in {len(djobs)} chunks "
          f"({ADAPTIVE_TOKEN_TIERS[0]:,} output tokens initially, "
          f"effort {stage02_effort or 'provider default'})")
    if not confirm(c.estimate(dprefix, PREAMBLE, djobs, batched=True), args.yes):
        return
    c.prewarm(dprefix, PREAMBLE)
    dedup_failures = os.path.join(voc, "_model_failures", "02_deduplicate")
    dedup_stats = AdaptiveStageStats(stage="02", total=len(djobs))
    dedup_results, djobs = _run_stage02_adaptive(
        c, dprefix, PREAMBLE, djobs, dedup_failures,
        wave_size=_adaptive_wave_size(c), stats=dedup_stats,
        debug=getattr(args, "stage02_debug", False))
    drop = set()
    groups = _batch_rows(
        dedup_results, djobs, "groups", dedup_failures,
        repair=lambda failed: _repair_batch(c, dprefix, PREAMBLE, failed, "groups"),
        rerun=lambda failed, factor: _rerun_batch(c, dprefix, PREAMBLE, failed, factor),
        # Budget stops have already been retried through the tier state machine.
        # Other retryable failures keep the existing recovery without widening.
        rerun_factor=1, adaptive_stats=dedup_stats)
    _print_adaptive_summary(dedup_stats)
    for g in groups:
        drop.update(i for i in g["duplicate_ids"] if i != g["canonical_id"])

    deduped = [r for r in retained if r["id"] not in drop]
    _write_jsonl(os.path.join(voc, "deduplicated_voc.jsonl"), deduped)
    # The segment stage reads filtered_voc.jsonl — keep that name as the contract.
    _write_jsonl(os.path.join(voc, "filtered_voc.jsonl"), deduped)
    with open(os.path.join(voc, "duplicate_groups.jsonl"), "w", encoding="utf-8") as fh:
        for g in groups:
            fh.write(json.dumps(g) + "\n")

    # One implementation serves both normal ingest and the manual refine-voc
    # command. This is local schema projection and URL parsing only.
    refine_voc(cfg)

    types = Counter(g["duplicate_type"] for g in groups)
    print(f"  02 deduplicate: {len(drop):,} merged away "
          + ("(" + " · ".join(f"{k} {v}" for k, v in types.most_common()) + ")"
             if types else ""))
    print(f"\n  {len(deduped):,} deduplicated evidence items -> {voc}/deduplicated_voc.jsonl")
    print("  (legacy rich copy retained as filtered_voc.jsonl)")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ------------------------------------------------------------------ segmentation 03-09

VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "segment_id": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["validated", "merged", "split_required",
                                    "reclassified_as_facet",
                                    "needs_more_research", "rejected"]},
                "rationale": {"type": "string"},
                "merged_into": {"type": "string"},
                "split_into": {"type": "array", "items": {"type": "string"}},
                # Populated only for `reclassified_as_facet`; empty otherwise.
                "facet_key": {"type": "string"},
                "facet_type": {"type": "string",
                               "enum": ["", "attribute", "journey_state"]},
            },
            "required": ["segment_id", "status", "rationale", "merged_into",
                         "split_into", "facet_key", "facet_type"],
            "additionalProperties": False}},
        "facet_vocabulary": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "facet_key": {"type": "string", "minLength": 1},
                "name": {"type": "string", "minLength": 1},
                "definition": {"type": "string"},
                "facet_type": {"type": "string",
                               "enum": ["attribute", "journey_state"]},
                "source_facet_ids": {"type": "array", "uniqueItems": True,
                                     "items": {"type": "string"}},
            },
            "required": ["facet_key", "name", "definition", "facet_type",
                         "source_facet_ids"],
            "additionalProperties": False}},
        "segment_edges": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "from_segment_id": {"type": "string"},
                "to_segment_id": {"type": "string"},
                "edge_type": {"type": "string",
                              "enum": list(segmentation.EDGE_TYPES)},
                # Strength is a judgement the model can actually make. Turning
                # it into a retrieval weight is config's job, not the model's.
                "strength": {"type": "string",
                             "enum": list(segmentation.EDGE_STRENGTHS)},
                "rationale": {"type": "string"},
            },
            "required": ["from_segment_id", "to_segment_id", "edge_type",
                         "strength", "rationale"],
            "additionalProperties": False}},
    },
    "required": ["decisions", "facet_vocabulary", "segment_edges"],
    "additionalProperties": False,
}


def validation_schema(segment_ids, provisional_facet_ids=()):
    schema = copy.deepcopy(VALIDATION_SCHEMA)
    ids = list(segment_ids)
    schema["properties"]["decisions"]["items"]["properties"][
        "segment_id"]["enum"] = ids
    edge = schema["properties"]["segment_edges"]["items"]["properties"]
    edge["from_segment_id"]["enum"] = ids
    edge["to_segment_id"]["enum"] = ids
    facet_ids = list(provisional_facet_ids)
    if facet_ids:
        schema["properties"]["facet_vocabulary"]["items"]["properties"][
            "source_facet_ids"]["items"]["enum"] = facet_ids
    return schema

# Mirrored from skills/05_assign_primary_segment.md § How to score an assignment.
# The skill lists "the score/margin thresholds were ignored" as a run-failure
# condition, but the thresholds lived only in prose: on the montisella run 118
# rows were assigned below them and nothing noticed, because `score` and
# `winning_margin` were read solely as sort keys. A declared contract that no
# code checks is a suggestion.
ASSIGN_MIN_SCORE = 6
ASSIGN_MIN_MARGIN = 2
# Above this share of assignments, the model is not slipping on edge cases — it
# is working to a different rule than the one it was given, and quietly
# repairing that many rows would hide a broken stage behind a clean output.
ASSIGN_VIOLATION_LIMIT = 0.05

ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {"assignments": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer"},
            "primary_segment_id": {"type": "string"},
            "score": {"type": "integer", "minimum": 0},
            "winning_margin": {"type": "integer", "minimum": 0},
            "cue_types": {"type": "array", "items": {"type": "string"}},
            "primary_cues": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
            "assignment_status": {"type": "string",
                                  "enum": ["assigned", "unassigned_ambiguous",
                                           "unassigned_insufficient_evidence",
                                           "unassigned_no_matching_segment"]},
            # The segment that scored second. Nothing is ever placed here: it is
            # recorded because a comment where two segments scored closely is
            # direct evidence that those two audiences overlap, and `winning_margin`
            # alone throws away which segment the margin was against.
            "runner_up_segment_id": {"type": "string"},
            # Closed vocabulary from Stage 04. Replaces the old free-text
            # `secondary_attributes`: an open list cannot be counted, joined or
            # audited, so it could only ever be read by a human one row at a time.
            "facet_ids": {"type": "array", "uniqueItems": True,
                          "items": {"type": "string"}},
        },
        "required": ["evidence_id", "primary_segment_id", "score", "winning_margin",
                     "cue_types", "primary_cues", "rationale", "assignment_status",
                     "runner_up_segment_id", "facet_ids"],
        "additionalProperties": False}}},
    "required": ["assignments"], "additionalProperties": False,
}


def assignment_schema(evidence_ids, segment_ids, facet_ids=()):
    schema = copy.deepcopy(ASSIGN_SCHEMA)
    row = schema["properties"]["assignments"]["items"]["properties"]
    row["evidence_id"]["enum"] = list(evidence_ids)
    row["primary_segment_id"]["enum"] = [""] + list(segment_ids)
    row["runner_up_segment_id"]["enum"] = [""] + list(segment_ids)
    facets = list(facet_ids)
    if facets:
        row["facet_ids"]["items"]["enum"] = facets
    else:
        # No vocabulary means no legal tag, and an unconstrained array would let
        # the free-text field back in through the schema's own gap.
        row["facet_ids"]["maxItems"] = 0
    return schema


def _normalize_assignment_contract(rows):
    """Fill fields added after an assignment artifact was written.

    Stage 05 is the most expensive stage in the pipeline, so a contract addition
    must not invalidate a completed run: an older row is loadable, it simply has
    no runner-up and no facets. What it must never do is look like a row that was
    asked the question and answered `none` — `runner_up_recorded` marks the
    difference so the co-occurrence graph is built only from rows that were.
    """
    for row in rows:
        if not isinstance(row, dict):
            continue
        row["runner_up_recorded"] = "runner_up_segment_id" in row
        row.setdefault("runner_up_segment_id", "")
        if "facet_ids" not in row:
            row["facet_ids"] = []
            # Preserve the pre-vocabulary free text rather than dropping it; it
            # is not a closed tag and never becomes one, but it is the only
            # record of what that run observed.
            legacy = row.pop("secondary_attributes", None)
            if legacy:
                row["legacy_secondary_attributes"] = legacy
        row.pop("secondary_attributes", None)
    return rows


def _demote(decisions, segment_id, failure):
    """Record a floor demotion on the persisted Stage 04 decision."""
    for decision in decisions:
        if decision.get("segment_id") != segment_id:
            continue
        decision["status"] = "needs_more_research"
        decision["rationale"] = (
            f"{decision.get('rationale', '').rstrip()} "
            f"[demoted by evidence floor: {failure}]").strip()
        return


def _root_order(segment_ids, graph):
    """Segment IDs ordered parents-before-children.

    Floors are hierarchical, so a child can only be judged as a child once its
    parent's fate is known. A child whose parent was demoted is not a child any
    more — it is a root, and it gets judged as one.
    """
    parents = segmentation.parent_map(graph)
    ordered, placed = [], set()
    remaining = list(segment_ids)
    while remaining:
        ready = [segment_id for segment_id in remaining
                 if parents.get(segment_id) not in set(remaining) - {segment_id}]
        if not ready:  # Unreachable given the graph is acyclic; stay total anyway.
            ready = sorted(remaining)
        for segment_id in ready:
            ordered.append(segment_id)
            placed.add(segment_id)
        remaining = [segment_id for segment_id in remaining
                     if segment_id not in placed]
    return ordered


def _apply_segment_floors(validated, decisions, cfg, verbose=True, graph=None):
    """Demote validated candidates too thin to be audiences, before assignment.

    Skill 04 already rules that a single thread is a conversation rather than an
    audience, but a rule stated only in prose is one the model can weigh against
    everything else and decide it liked the candidate anyway.

    These are *discovery* counts — how much of the corpus the candidate was
    drawn from — so they are an upper bound on the segment's real support and
    this gate only catches candidates that were hopeless from the start. The
    floor that does the work runs after assignment, below.

    A child clears the lower child volume bar instead, because its parent has
    already carried the burden of proving the audience exists. The distinctness
    half of the child floor is not applied here: it needs assigned evidence to
    compare, and discovery evidence is not that.

    Demotion is to `needs_more_research`, not `rejected`: a thin candidate is
    usually a real audience the corpus under-samples, and it should come back if
    a later scrape finds it.
    """
    min_threads, min_evidence = corpus_policy.segment_floors(cfg)
    child = corpus_policy.child_floors(cfg)
    parents = segmentation.parent_map(graph)
    by_id = {row["segment_id"]: row for row in validated}
    order = [row["segment_id"] for row in validated]
    surviving = set(by_id)
    kept_ids, demoted = [], []
    for segment_id in _root_order(order, graph):
        candidate = by_id[segment_id]
        is_child = parents.get(segment_id) in surviving
        if is_child:
            failure = corpus_policy.child_floor_failure(
                int(candidate.get("unique_thread_count") or 0),
                int(candidate.get("unique_evidence_count") or 0),
                [], [], {**child, "min_distinctive_terms": 0})
        else:
            failure = corpus_policy.floor_failure(
                candidate, min_threads, min_evidence)
        if failure is None:
            kept_ids.append(segment_id)
            continue
        surviving.discard(segment_id)
        demoted.append((candidate, failure))
        _demote(decisions, candidate["segment_id"], failure)
    kept = [by_id[segment_id] for segment_id in sorted(kept_ids, key=order.index)]
    if demoted and verbose:
        print(f"  04 floors: {len(demoted)} validated candidate(s) below "
              f"{min_threads} threads / {min_evidence} items "
              f"({child['min_threads']}/{child['min_evidence']} for children) "
              "-> needs_more_research")
        for candidate, failure in demoted:
            print(f"    {candidate['slug']:44} {failure}")
    return kept


def _apply_assigned_floors(validated, decisions, rows, by_id, cfg, verbose=True,
                           graph=None):
    """Demote segments whose *assigned* evidence is too thin, after Stage 05.

    This is the floor that matters, and applying it earlier cannot work. A
    Stage 03 candidate card counts the threads that contributed to discovering
    the candidate, which is systematically larger than the threads Stage 05
    ultimately assigns to it: on the montisella run `fitness_overuse_rotator_cuff`
    carried 16 threads on its card and two in the finished segment. Gating on the
    card caught 1 of 70 segments; gating here catches the 19 that actually rest
    on three conversations or fewer.

    Children are judged differently, and the difference cuts both ways: a lower
    volume bar, because the parent already proved the audience exists, plus a
    distinctness bar the parent never has to clear. A child that only repeats its
    parent's vocabulary is a relabelling, however much evidence it carries.

    Where a demoted root's evidence returns to the unassigned pool, a demoted
    child's evidence moves to its parent. That is not a rounding-up of counts:
    `specialises` means every member of the child is a member of the parent, so
    the parent is where those comments belonged all along, and the parent's card
    records which child it absorbed.
    """
    min_threads, min_evidence = corpus_policy.segment_floors(cfg)
    child_rules = corpus_policy.child_floors(cfg)
    parents = segmentation.parent_map(graph)
    assigned = defaultdict(list)
    for row in rows:
        if row.get("assignment_status") == "assigned":
            assigned[row.get("primary_segment_id")].append(row)

    def texts(segment_id):
        return [by_id.get(row["evidence_id"], {}).get("text", "")
                for row in assigned.get(segment_id, [])]

    by_id_segment = {row["segment_id"]: row for row in validated}
    surviving = set(by_id_segment)
    kept_ids, demoted, absorbed = [], [], defaultdict(list)
    for segment_id in _root_order([row["segment_id"] for row in validated], graph):
        segment = by_id_segment[segment_id]
        segment_rows = assigned.get(segment_id, [])
        threads = {(by_id.get(row["evidence_id"], {}).get("thread_id")
                    or by_id.get(row["evidence_id"], {}).get("url"))
                   for row in segment_rows}
        threads.discard(None)
        parent_id = parents.get(segment_id)
        parent_id = parent_id if parent_id in surviving else None
        if parent_id:
            failure = corpus_policy.child_floor_failure(
                len(threads), len(segment_rows), texts(segment_id),
                texts(parent_id), child_rules)
        else:
            failure = corpus_policy.floor_failure(
                {"unique_thread_count": len(threads),
                 "unique_evidence_count": len(segment_rows)},
                min_threads, min_evidence)
        if failure is None:
            kept_ids.append(segment_id)
            continue
        surviving.discard(segment_id)
        demoted.append((segment, failure, len(segment_rows), parent_id))
        _demote(decisions, segment_id, failure)
        for row in segment_rows:
            if parent_id:
                row["primary_segment_id"] = parent_id
                row["absorbed_from_segment_id"] = segment_id
                assigned[parent_id].append(row)
            else:
                row["assignment_status"] = "unassigned_insufficient_evidence"
                row["primary_segment_id"] = ""
        if parent_id:
            absorbed[parent_id].append(
                {"segment_id": segment_id, "slug": segment["slug"],
                 "name": segment.get("name") or segment["slug"],
                 "evidence_count": len(segment_rows), "reason": failure})

    order = [row["segment_id"] for row in validated]
    kept = [by_id_segment[segment_id]
            for segment_id in sorted(kept_ids, key=order.index)]
    for segment in kept:
        if absorbed.get(segment["segment_id"]):
            segment["absorbed_children"] = absorbed[segment["segment_id"]]
    if demoted and verbose:
        released = sum(count for _s, _f, count, parent in demoted if not parent)
        folded = sum(count for _s, _f, count, parent in demoted if parent)
        print(f"  05 floors: {len(demoted)} segment(s) below {min_threads} "
              f"threads / {min_evidence} assigned items "
              f"({child_rules['min_threads']}/{child_rules['min_evidence']} plus "
              "distinctness for children) -> needs_more_research "
              f"({released:,} item(s) returned to unassigned, "
              f"{folded:,} folded into a parent)")
        for segment, failure, _count, parent_id in demoted:
            destination = (f" -> {by_id_segment[parent_id]['slug']}"
                           if parent_id else "")
            print(f"    {segment['slug']:44} {failure}{destination}")
    return kept


def _enforce_assignment_thresholds(rows, diagnostics_dir, verbose=True):
    """Demote assignments that did not clear skill 05's own bar.

    JSON Schema can bound one field at a time but cannot express "assigned
    implies score >= 6 and margin >= 2", so the contract is enforced here
    instead. Demotion rather than deletion: the model's score, cues and
    rationale are all preserved, and the row simply rejoins the unassigned
    audit set where a below-threshold judgement belongs.

    Returns the violations, so the caller can report them beside the tally
    rather than discovering them in a file nobody opens.
    """
    violations = []
    for row in rows:
        if row.get("assignment_status") != "assigned":
            continue
        score = row.get("score") or 0
        margin = row.get("winning_margin") or 0
        if score >= ASSIGN_MIN_SCORE and margin >= ASSIGN_MIN_MARGIN:
            continue
        violations.append({
            "evidence_id": row.get("evidence_id"),
            "primary_segment_id": row.get("primary_segment_id"),
            "score": score, "winning_margin": margin,
            "rationale": row.get("rationale"),
            "required": {"score": ASSIGN_MIN_SCORE, "margin": ASSIGN_MIN_MARGIN},
        })
        row["assignment_status"] = "unassigned_insufficient_evidence"
        row["primary_segment_id"] = ""
    if not violations:
        return violations

    os.makedirs(diagnostics_dir, exist_ok=True)
    path = os.path.join(diagnostics_dir, "threshold_violations.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for violation in violations:
            fh.write(json.dumps(violation, ensure_ascii=False) + "\n")
    share = len(violations) / max(len(rows), 1)
    if verbose:
        print(f"  ! {len(violations):,} assignment(s) fell below skill 05's "
              f"score>={ASSIGN_MIN_SCORE}/margin>={ASSIGN_MIN_MARGIN} bar and "
              f"were demoted to unassigned -> {path}")
    if share > ASSIGN_VIOLATION_LIMIT:
        sys.exit(
            f"  Stage 05 broke its own assignment threshold on "
            f"{len(violations):,} of {len(rows):,} rows ({share * 100:.1f}%, "
            f"limit {ASSIGN_VIOLATION_LIMIT * 100:.0f}%).\n"
            f"  Audited to {path}. The stage is not applying the rule it was "
            f"given; re-run it with --reassign rather than building on this.")
    return violations


def _stage03_corpus_text(items, limit=None):
    """Stage 03 view: exactly the stable ID and untouched customer text."""
    rows = items if limit is None else items[:limit]
    return "\n\n".join(f"[{i['id']}] {i['text']}" for i in rows)


def _corpus_text(items, limit=None):
    """Compatibility name for the Stage 03 ID/text formatter."""
    return _stage03_corpus_text(items, limit)


def _validation_corpus_text(items):
    """Stage 04's source-diversity view, without leaking audit-only fields."""
    blocks = []
    for item in items:
        subreddit = item.get("subreddit")
        thread_id = item.get("thread_id")
        if subreddit is None and thread_id is None:
            subreddit, thread_id = reddit_source_fields(item.get("url"))
        blocks.append(
            f"[{item['id']}] [t:{thread_id or '-'}] [r:{subreddit or '-'}] "
            f"{item['text']}")
    return "\n\n".join(blocks)


def _stage05_evidence_text(items):
    """Stage 05 view: ID and text only, independent of production metadata."""
    return _stage03_corpus_text(items)


def voc_prompt_view_metrics(items):
    """Measure deterministic raw views available before discovery runs."""
    monolithic = _stage03_corpus_text(items)
    core = segmentation.evidence_text(
        segmentation.initial_discovery_records(items), include_tier=True)
    stage04_raw = _validation_corpus_text(items)
    monolithic_tokens = max(len(monolithic) // 4, 1)
    core_tokens = max(len(core) // 4, 1) if core else 0
    stage04_raw_tokens = max(len(stage04_raw) // 4, 1)
    return {
        "stage03_before_chars": len(monolithic),
        "stage03_chars": len(core),
        "stage03_before_est_tokens": monolithic_tokens,
        "stage03_est_tokens": core_tokens,
        "stage03_reduction_pct": round(
            (1 - (len(core) / len(monolithic))) * 100, 2) if monolithic else 0.0,
        # Stage 04's compact packet depends on candidates that do not exist yet.
        # Report only the raw corpus the new architecture avoids sending.
        "stage04_raw_corpus_chars_avoided": len(stage04_raw),
        "stage04_raw_corpus_est_tokens_avoided": stage04_raw_tokens,
    }


def _join_production_audit(production, audit, require_complete=True):
    """Resolve Stage 06 source records without adding audit data to prompts."""
    audit_by_id = {item["id"]: item for item in audit}
    joined, missing, changed = {}, [], []
    for item in production:
        rich = audit_by_id.get(item["id"])
        if rich is None:
            if require_complete:
                missing.append(item["id"])
            joined[item["id"]] = item
            continue
        if rich.get("text") != item.get("text"):
            changed.append(item["id"])
        joined[item["id"]] = rich
    if missing:
        raise ValueError(
            f"audit VOC is missing {len(missing)} production ID(s)")
    if changed:
        raise ValueError(
            f"production/audit VOC text differs for {len(changed)} ID(s)")
    return joined


SEGMENT_PREAMBLE = (
    "You are executing one bounded structured step in a customer-segmentation "
    "research pipeline. Evidence IDs and verbatim text are authoritative. Never "
    "invent an ID, quote, count, source, or audience. Return only the requested "
    "structured data; deterministic code handles counting, joining and ordering."
)
SEGMENT_STEP_ORDER = ("03a", "03b", "03c", "04", "05", "06", "07", "08", "09")


def _segment_setting(cfg, key, default):
    return cfg.get("segmentation", {}).get(key, default)


def _segment_force(args, step):
    start = getattr(args, "from_stage", None)
    if getattr(args, "rediscover", False):
        start = "03a"
    return bool(start and SEGMENT_STEP_ORDER.index(step) >=
                SEGMENT_STEP_ORDER.index(start))


def _json_atomic(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(temporary, path)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _value_fingerprint(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _meta_matches(path, fingerprint):
    try:
        return _load_json(path).get("input_fingerprint") == fingerprint
    except (OSError, ValueError, AttributeError):
        return False


def _model_meter(client_):
    usage = {key: int(value or 0) for key, value in
             dict(getattr(client_, "spent", {}) or {}).items()}
    cost = 0.0
    actual = getattr(client_, "actual_usd", None)
    if callable(actual):
        cost = float(actual())
    return {"usage": usage, "cost_usd": cost}


def _meter_delta(before, after):
    keys = set(before["usage"]) | set(after["usage"])
    return {
        "usage": {key: after["usage"].get(key, 0) - before["usage"].get(key, 0)
                  for key in sorted(keys)},
        "cost_usd": round(after["cost_usd"] - before["cost_usd"], 6),
    }


def _legacy_stage03_benchmark(cfg, candidate_path):
    """Best-effort comparison data from the preserved output and audit logs."""
    benchmark = {"available": os.path.isfile(candidate_path)}
    if os.path.isfile(candidate_path):
        candidates = _load_json(candidate_path)
        empty = sum(not row.get("representative_evidence_ids") for row in candidates)
        placeholder = re.compile(r"placeholder|tbd|lorem|unknown audience", re.I)
        degraded = sum(bool(placeholder.search(json.dumps(row))) for row in candidates)
        benchmark.update({
            "candidate_count": len(candidates),
            "empty_representative_id_count": empty,
            "empty_representative_id_rate": round(empty / len(candidates), 4)
            if candidates else 0.0,
            "obvious_placeholder_candidate_count": degraded,
        })
    log_root = os.path.join(cfg["_dir"], "logs", "model")
    newest = None
    if os.path.isdir(log_root):
        for base, _dirs, files in os.walk(log_root):
            if "request.json" not in files or "response.json" not in files:
                continue
            try:
                request = _load_json(os.path.join(base, "request.json"))
                if request.get("job_id") != "03_discover":
                    continue
                stamp = os.path.getmtime(os.path.join(base, "response.json"))
                if newest is None or stamp > newest[0]:
                    newest = (stamp, base, _load_json(os.path.join(base, "response.json")))
            except (OSError, ValueError):
                continue
    if newest:
        response = newest[2]
        provider = response.get("provider_response") or {}
        usage = ((response.get("metadata") or {}).get("usage")
                 or provider.get("usage") or {})
        detail = usage.get("completion_tokens_details") or {}
        benchmark.update({
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "completion_tokens": usage.get("completion_tokens",
                                            usage.get("output_tokens")),
            "reasoning_tokens": detail.get("reasoning_tokens",
                                            usage.get("reasoning_tokens")),
            "audit_log": os.path.relpath(newest[1], cfg["_dir"]),
        })
    return benchmark


def _job_fingerprint(job, evidence_ids, corpus="", contract_version=""):
    payload = json.dumps({"prompt": job.prompt, "schema": job.schema,
                          "ids": list(evidence_ids), "corpus": corpus,
                          "contract_version": contract_version}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _legacy_job_fingerprint_v1(job, evidence_ids, corpus=""):
    """Fingerprint format used before semantic contract versions existed."""
    payload = json.dumps({"prompt": job.prompt, "schema": job.schema,
                          "ids": list(evidence_ids), "corpus": corpus}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _stage03a_compatible_fingerprints(jobs, chunks):
    """Fingerprints of every earlier 03A contract whose results still migrate.

    Each generation is authenticated by the exact schema, skill text and contract
    version it ran under, so a finished chunk is recognised as completed work
    rather than silently re-harvested. Registers are taught entirely in the skill
    text, which is why the register-less generation shares this generation's
    prompt and differs only by schema, snapshot and version.
    """
    compatible = {}
    for job, chunk in zip(jobs, chunks):
        evidence_ids = chunk["evidence_ids"]
        v1 = dataclasses.replace(
            job, prompt=segmentation.legacy_harvest_prompt_v1(chunk["records"]),
            schema=segmentation.legacy_harvest_schema_v1())
        v2 = dataclasses.replace(
            job, schema=segmentation.legacy_harvest_schema_v2(evidence_ids))
        v3 = dataclasses.replace(
            job, schema=segmentation.legacy_harvest_schema_v3(evidence_ids))
        compatible[job.id] = {
            _legacy_job_fingerprint_v1(
                v1, evidence_ids, segmentation.LEGACY_HARVEST_SKILL_V1),
            _job_fingerprint(
                v2, evidence_ids, segmentation.LEGACY_HARVEST_SKILL_V2,
                segmentation.LEGACY_HARVEST_CONTRACT_V2),
            _job_fingerprint(
                v3, evidence_ids, segmentation.LEGACY_HARVEST_SKILL_V3,
                segmentation.LEGACY_HARVEST_CONTRACT_V2),
        }
    return compatible


def _print_03a_contract_error(error, fallback_chunk=None):
    """Render a model-contract failure as an actionable pipeline event."""
    if getattr(error, "reported", False):
        return
    chunk = getattr(error, "chunk_id", None) or fallback_chunk or "unknown"
    candidate = getattr(error, "candidate", None) or "unknown"
    invalid = getattr(error, "invalid_evidence_ids", None)
    if isinstance(error, segmentation.HarvestProvenanceFailure):
        print("\n  03A PROVENANCE FAILURE")
        print(f"  chunk: {chunk}")
        print(f"  candidate: {candidate}")
        print("  all cited evidence IDs were outside the chunk")
        if invalid:
            print(f"  impossible evidence IDs: {invalid}")
        return
    print("\n  03A CONTRACT ERROR")
    print(f"  chunk: {chunk}")
    print(f"  candidate: {candidate}")
    if invalid:
        print(f"  invalid evidence IDs: {invalid}")
    else:
        print(f"  violation: {error}")


def _prewarm_segment_stage(client_, corpus, preamble, stage):
    """Warm where useful; let native Anthropic Batch waves seed themselves."""
    if (stage in ("03E", "05")
            and getattr(client_, "batch_cache_self_seeds", False)):
        unit = "request" if stage == "03E" else "wave"
        print(f"  Anthropic Batch cache: first {stage} {unit} seeds the shared "
              "prefix; later {unit}s read it")
        return
    client_.prewarm(corpus, preamble)


def _run_persisted_segment_jobs(c, corpus, jobs, chunks, key, artifact_dir,
                                diagnostics_dir, args, stage, force=False,
                                row_validator=None, row_cleaner=None,
                                repair_guidance=None,
                                contract_version="", compatible_jobs=None,
                                compatible_fingerprints=None,
                                cached_migrator=None,
                                local_cleanup_key="provenance_cleanup"):
    """Run missing chunk jobs with adaptive budgets and persist each result."""
    from llm import confirm

    os.makedirs(artifact_dir, exist_ok=True)
    compatible_by_id = {job.id: job for job in (compatible_jobs or [])}
    complete, missing_jobs, missing_chunks, cached_repairs = {}, [], [], []
    for job, chunk in zip(jobs, chunks):
        path = os.path.join(artifact_dir, f"{job.id}.json")
        fingerprint = _job_fingerprint(
            job, chunk["evidence_ids"], corpus, contract_version)
        if os.path.isfile(path) and not force:
            try:
                with open(path, encoding="utf-8") as fh:
                    saved = json.load(fh)
                acceptable = {fingerprint}
                acceptable.update(
                    (compatible_fingerprints or {}).get(job.id, ()))
                compatible = compatible_by_id.get(job.id)
                if compatible is not None:
                    acceptable.add(_job_fingerprint(
                        compatible, chunk["evidence_ids"], corpus,
                        contract_version))
                if saved.get("fingerprint") in acceptable:
                    locally_migrated = False
                    if cached_migrator is not None:
                        migrated_rows = cached_migrator(
                            job, saved.get(key, []), saved.get("fingerprint"))
                        locally_migrated = migrated_rows != saved.get(key, [])
                        saved[key] = migrated_rows
                    locally_cleaned = []
                    if row_cleaner is not None:
                        try:
                            locally_cleaned = row_cleaner(job, saved.get(key, [])) or []
                        except segmentation.HarvestContractError as error:
                            if repair_guidance:
                                cached_repairs.append((job, chunk, saved, error))
                                continue
                            raise
                    if row_validator is not None:
                        try:
                            row_validator(job, saved.get(key, []))
                        except segmentation.HarvestContractError as error:
                            if repair_guidance:
                                cached_repairs.append((job, chunk, saved, error))
                                continue
                            raise
                    if (saved.get("fingerprint") != fingerprint or locally_cleaned
                            or locally_migrated):
                        if locally_cleaned:
                            saved[local_cleanup_key] = locally_cleaned
                        if saved.get("fingerprint") != fingerprint:
                            saved["migrated_from_fingerprint"] = saved.get("fingerprint")
                        saved["fingerprint"] = fingerprint
                        _json_atomic(path, saved)
                    complete[job.id] = saved
                    continue
            except (OSError, ValueError, TypeError):
                pass
        missing_jobs.append(job)
        missing_chunks.append(chunk)

    work_jobs = missing_jobs + [job for job, _chunk, _saved, _error
                                in cached_repairs]
    if work_jobs:
        detail = f"{len(missing_jobs)} new/stale"
        if cached_repairs:
            detail += f" · {len(cached_repairs)} cached contract repair"
        print(f"  {stage}: {detail}; {len(complete)} reusable")
        if not confirm(c.estimate(corpus, SEGMENT_PREAMBLE, work_jobs,
                                  batched=True), getattr(args, "yes", False)):
            return None
        _prewarm_segment_stage(c, corpus, SEGMENT_PREAMBLE, stage)

        if missing_jobs:
            stage_stats = AdaptiveStageStats(stage=stage, total=len(missing_jobs))
            results, final_jobs = _run_adaptive_stage(
                c, corpus, SEGMENT_PREAMBLE, missing_jobs, diagnostics_dir,
                stage=stage, key=key, wave_size=_adaptive_wave_size(c),
                stats=stage_stats, debug=getattr(args, "segment_debug", False),
                row_validator=row_validator, row_cleaner=row_cleaner)
            if stage_stats.provider_routes:
                routes = " · ".join(
                    f"{name}: {count}" for name, count in
                    stage_stats.provider_routes.most_common())
                print(f"  {stage} provider routes (adaptive attempts): {routes}")
            final_by_id = {job.id: job for job in final_jobs}
            chunks_by_id = {job.id: chunk for job, chunk in
                            zip(missing_jobs, missing_chunks)}
            for original in missing_jobs:
                job = final_by_id[original.id]
                rows = _batch_rows(
                    {job.id: results[job.id]}, [job], key, diagnostics_dir,
                    repair=lambda failed: _repair_batch(
                        c, corpus, SEGMENT_PREAMBLE, failed, key,
                        guidance=repair_guidance),
                    rerun=lambda failed, factor: _rerun_batch(
                        c, corpus, SEGMENT_PREAMBLE, failed, factor),
                    row_validator=row_validator, row_cleaner=row_cleaner)
                if cached_migrator is not None:
                    rows = cached_migrator(original, rows, None)
                chunk = chunks_by_id[job.id]
                saved = {
                    "chunk_id": job.id,
                    "evidence_ids": chunk["evidence_ids"],
                    "estimated_input_tokens": chunk["estimated_tokens"],
                    "fingerprint": _job_fingerprint(
                        original, chunk["evidence_ids"], corpus,
                        contract_version),
                    "first_response_telemetry": {
                        "provider": results[job.id].provider,
                        "stop_reason": results[job.id].stop_reason,
                        "completion_tokens": results[job.id].completion_tokens,
                        "reasoning_tokens": results[job.id].reasoning_tokens,
                    },
                    key: rows,
                }
                _json_atomic(os.path.join(artifact_dir, f"{job.id}.json"), saved)
                complete[job.id] = saved

        for job, chunk, saved, contract_error in cached_repairs:
            _print_03a_contract_error(contract_error, job.id)
            print("  Repairing this chunk while preserving candidates and valid IDs...")
            prior = BatchResult(
                json.dumps({key: saved.get(key, [])}, ensure_ascii=False), "stop",
                int((saved.get("first_response_telemetry") or {}).get(
                    "reasoning_tokens") or 0),
                int((saved.get("first_response_telemetry") or {}).get(
                    "completion_tokens") or 0),
                (saved.get("first_response_telemetry") or {}).get("provider"))
            try:
                rows = _batch_rows(
                    {job.id: prior}, [job], key, diagnostics_dir,
                    repair=lambda failed: _repair_batch(
                        c, corpus, SEGMENT_PREAMBLE, failed, key,
                        guidance=repair_guidance), row_validator=row_validator,
                    row_cleaner=row_cleaner)
            except BatchOutputError as error:
                project = os.environ.get("ADPIPE_PROJECT", "PROJECT")
                raise BatchOutputError(
                    f"Stage {stage} contract repair failed for {job.id}. "
                    f"Diagnostics: {diagnostics_dir}. Resume with: "
                    f"./adpipe -p {project} segment") from error
            repaired = dict(saved)
            repaired["migrated_from_fingerprint"] = saved.get("fingerprint")
            repaired["fingerprint"] = _job_fingerprint(
                job, chunk["evidence_ids"], corpus, contract_version)
            repaired["contract_repaired"] = True
            repaired[key] = (cached_migrator(job, rows, None)
                             if cached_migrator is not None else rows)
            _json_atomic(os.path.join(artifact_dir, f"{job.id}.json"), repaired)
            complete[job.id] = repaired
    else:
        print(f"  {stage}: reusing all {len(jobs)} completed chunk(s)")
    return [complete[job.id] for job in jobs]


def _finalize_stage04_facets(vocabulary, provisional_facets, decisions, candidates):
    """Build the closed facet vocabulary from 04's decisions and 03A's discoveries.

    A reclassified candidate hands its discovery evidence to the facet it became.
    That evidence is not an audience count and never becomes one -- it is kept so
    the facet can be traced back to the comments that produced it, which is the
    only thing the old free-text `secondary_attributes` field could never do.
    """
    by_segment_id = {row["segment_id"]: row for row in candidates}
    reclassified = []
    for decision in decisions:
        if decision.get("status") != "reclassified_as_facet":
            continue
        candidate = by_segment_id.get(decision["segment_id"])
        key = str(decision.get("facet_key") or "").strip()
        if not key:
            # A reclassification with no destination would silently delete the
            # candidate, so it falls back to its own slug rather than vanishing.
            key = (candidate or {}).get("slug") or decision["segment_id"]
            decision["facet_key"] = key
        reclassified.append({
            "facet_key": key,
            "facet_type": decision.get("facet_type") or "attribute",
            "name": (candidate or {}).get("name") or key,
            "definition": (candidate or {}).get("definition") or "",
            "segment_id": decision["segment_id"],
            "evidence_ids": (candidate or {}).get("core_evidence_ids") or [],
        })
    facets, unknown = segmentation.finalize_facets(
        vocabulary, provisional_facets, reclassified)
    if unknown:
        # The vocabulary is a tag set, not a count, so an unresolvable citation is
        # dropped and reported rather than failing a stage that has already done
        # its real work.
        for row in unknown:
            print(f"  ! 04 facet {row['facet_key']!r} cited "
                  f"{len(row['unknown_ids'])} unknown provisional facet ID(s); "
                  "dropped from its lineage")
    return facets


def _finalize_stage04_graph(edges, decisions, verbose=True):
    """Validate declared relationships against the segments that survived 04.

    Endpoints are restricted to candidates Stage 04 actually validated. An edge
    onto something it rejected, merged or reclassified is not a relationship —
    it is a relationship to a segment that no longer exists — so it is dropped
    and reported rather than left to break the floors that read this graph.
    """
    eligible = [row["segment_id"] for row in decisions
                if row.get("status") == "validated"]
    graph, dropped = segmentation.finalize_segment_graph(edges, eligible)
    if verbose and (graph["specialises"] or graph["adjacent"]):
        print(f"  04 graph: {len(graph['specialises'])} specialises · "
              f"{len(graph['adjacent'])} adjacent edge(s)")
    if verbose and dropped:
        reasons = Counter(row["reason"] for row in dropped)
        print("  ! 04 graph: dropped " + " · ".join(
            f"{count} {reason}" for reason, count in sorted(reasons.items())))
    return graph


def _validate_decision_coverage(decisions, candidates):
    expected = [row["segment_id"] for row in candidates]
    got = [row.get("segment_id") for row in decisions]
    if Counter(expected) != Counter(got):
        raise BatchOutputError(
            "Stage 04 did not return exactly one decision per discovered candidate")


def _remap_expansion_matches(matches, initial, final):
    """Carry 03E matches through the one allowed novelty consolidation cycle."""
    initial_sources = {row["segment_id"]: set(row["source_candidate_ids"])
                       for row in initial}
    final_sources = {row["segment_id"]: set(row["source_candidate_ids"])
                     for row in final}
    output = []
    for row in matches:
        mapped = set()
        for old_id in row["segment_ids"]:
            for new_id, source_ids in final_sources.items():
                if source_ids.intersection(initial_sources.get(old_id, set())):
                    mapped.add(new_id)
        item = dict(row)
        item["segment_ids"] = sorted(mapped)
        if not mapped:
            item["match_strength"] = "none"
        output.append(item)
    return output


def _print_03b_contract_error(error, *, repairing=False, diagnostics_dir=None):
    title = "03B CONTRACT REPAIR FAILED" if repairing else "03B CONTRACT ERROR"
    print(f"\n  {title}")
    for violation in error.invalid_references:
        print(f"  canonical slug: {violation.get('slug') or 'unknown'}")
        print("  invalid source candidate IDs: " +
              json.dumps(violation["invalid_ids"], ensure_ascii=False))
    print("  Source lineage must use exact machine candidate_id values from Stage 03A.")
    if diagnostics_dir:
        print(f"  Diagnostics saved to: {diagnostics_dir}")
    if not repairing:
        print("  Repairing the completed 03B response; Stage 03A will not rerun...")


def _run_03b_consolidation(client_, corpus, preamble, job, catalogue,
                           diagnostics_dir, initial_result=None):
    """Run and finalize 03B with exact, repairable source-lineage contracts."""
    def validate(payload):
        rows = payload.get("candidates") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            segmentation.validate_consolidated_lineage(rows, catalogue)

    payload = _run_single_structured(
        client_, corpus, preamble, job, diagnostics_dir,
        token_tiers=STAGE03B_TOKEN_TIERS,
        response_validator=validate,
        repair_guidance=(
            "Act as a bounded reconciliation reviewer. Return the COMPLETE original "
            "candidate collection in the same order and with the same row count. "
            "Correct only invalid source_candidate_ids by selecting exact machine "
            "candidate_id values from the supplied 03A catalogue. Preserve every "
            "slug, name, definition, criterion, status, alias, and already-valid "
            "source ID exactly. A partial collection is invalid."),
        validation_error_handler=lambda error, **context:
            _print_03b_contract_error(error, **context)
            if isinstance(error, segmentation.ConsolidationContractError) else None,
        initial_result=initial_result,
        repair_merger=segmentation.merge_consolidation_repair)
    try:
        return segmentation.finalize_consolidated(payload["candidates"], catalogue)
    except segmentation.ConsolidationContractError as error:
        # Defence in depth if a provider/schema implementation mutates or skips
        # the pre-return validator. Never leak a model-contract ValueError.
        _print_03b_contract_error(
            error, repairing=True, diagnostics_dir=diagnostics_dir)
        raise BatchOutputError(
            "Stage 03B lineage validation failed after structured recovery. "
            f"Diagnostics: {diagnostics_dir}") from error


def _03b_audit_recovery(project_dir, job_id, catalogue):
    """Find a completed audited 03B response for this exact input catalogue.

    Early 03B versions audited the provider response but crashed before writing
    the resumable discovery artifact. Matching the embedded catalogue prevents a
    response from another run/input being reused. Only a completed `stop`
    response is eligible; truncated or failed calls remain diagnostics only.
    """
    log_root = os.path.join(project_dir, "logs", "model")
    if not os.path.isdir(log_root):
        return None
    candidates = []
    for root, _dirs, files in os.walk(log_root):
        if "request.json" in files and "response.json" in files:
            candidates.append(root)
    expected = _value_fingerprint(catalogue)
    for root in sorted(candidates, key=os.path.getmtime, reverse=True):
        try:
            request = _load_json(os.path.join(root, "request.json"))
            response = _load_json(os.path.join(root, "response.json"))
            if (request.get("job_id") != job_id
                    or request.get("operation") != "single_structured"
                    or response.get("metadata", {}).get("finish_reason") != "stop"):
                continue
            messages = request.get("request", {}).get("messages") or []
            prompt = messages[-1].get("content") if messages else None
            if not isinstance(prompt, str) or "\nCATALOGUE:\n" not in prompt:
                continue
            prior_catalogue = json.loads(prompt.split("\nCATALOGUE:\n", 1)[1])
            if _value_fingerprint(prior_catalogue) != expected:
                continue
            text = response.get("text") or ""
            _json_object(text)
            usage = response.get("metadata", {}).get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            provider = (response.get("provider_response") or {}).get("provider")
            return (BatchResult(
                text=text, stop_reason="stop",
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                provider=provider), root)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


@dataclasses.dataclass
class Stage05ExecutionStats:
    """Stage-05-only recovery counters and first-pass provenance."""
    reusable: int = 0
    grammar_rate_limit_retries: int = 0
    grammar_retried_ids: set[str] = dataclasses.field(default_factory=set)
    budget_retried_ids: set[str] = dataclasses.field(default_factory=set)
    successful_ids: set[str] = dataclasses.field(default_factory=set)
    repaired_ids: set[str] = dataclasses.field(default_factory=set)
    hard_failure_ids: set[str] = dataclasses.field(default_factory=set)


def _stage05_grammar_rate_limited(result):
    """True only for Anthropic's transient structured-grammar capacity limit."""
    error = result.provider_error or {}
    message = str(error.get("message") or "").lower()
    return (error.get("type") == "invalid_request_error"
            and "grammar compilation rate limit exceeded" in message)


def _stage05_assignment_rows(value):
    """Find one unambiguous assignment-row array inside wrapper-only drift.

    This deliberately does not edit a row.  It only recognizes arrays whose
    objects already look like Stage 05 assignment rows, then lets the normal
    schema and exact-coverage validators decide whether their contents are
    usable.  Multiple different arrays are ambiguous and therefore not fixed.
    """
    found = []

    def visit(node, path):
        if isinstance(node, list):
            looks_like_rows = bool(node) and all(
                isinstance(row, dict) for row in node) and any(
                    "evidence_id" in row for row in node)
            if looks_like_rows:
                found.append((node, path))
                return
            for index, child in enumerate(node):
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}[{index}]")
        elif isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}.{key}")

    visit(value, "$")
    unique = []
    for rows, path in found:
        if not any(rows == prior for prior, _prior_path in unique):
            unique.append((rows, path))
    return unique


def _normalize_stage05_assignment_wrapper(raw):
    """Canonicalize only objectively structural Stage 05 wrapper mistakes.

    Accepted drift includes a bare row array, a differently named wrapper, an
    extra object/list layer, duplicate equivalent wrappers, and harmless
    metadata beside the one assignment array.  Assignment decisions are never
    added, removed, merged, or reinterpreted here.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I)
        text = re.sub(r"\s*```$", "", text, count=1)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        try:
            payload = presets.extract_json(raw)
        except presets.PresetError:
            return raw, None

    if (isinstance(payload, dict) and set(payload) == {"assignments"}
            and isinstance(payload["assignments"], list)):
        return raw, None

    candidates = _stage05_assignment_rows(payload)
    if len(candidates) != 1:
        return raw, None
    rows, path = candidates[0]
    normalized = json.dumps({"assignments": rows}, ensure_ascii=False)
    return normalized, {
        "kind": "wrapper_only",
        "source_path": path,
        "assignment_count": len(rows),
    }


def _normalize_stage05_result(job, value, events=None):
    """Return a BatchResult with safe wrapper drift normalized, if present."""
    result = _as_result(value)
    normalized, event = _normalize_stage05_assignment_wrapper(result.text)
    if event is None:
        return result
    event = {"chunk_id": job.id, **event}
    if events is not None:
        events[job.id] = event
    print("\n  STAGE 05 STRUCTURAL NORMALIZATION")
    print(f"  chunk: {job.id}")
    print(f"  assignment array recovered from: {event['source_path']}")
    print(f"  rows preserved unchanged: {event['assignment_count']}")
    print("  model calls made: 0")
    return dataclasses.replace(result, text=normalized)


def _merge_stage05_incomplete_coverage(job, original_raw, reviewed_rows):
    """Preserve valid prior rows and import only IDs omitted by the model.

    A complete reviewer artifact is still required and validated first.  This
    merge is allowed only when the original payload's sole defect is missing
    coverage: its wrapper/schema are valid and it has no unknown or duplicate
    IDs.  No existing assignment decision can therefore be overwritten by a
    contract repair that was asked only to restore omitted rows.
    """
    try:
        payload = _json_object(original_raw)
        original_rows = payload.get("assignments")
        if not isinstance(original_rows, list):
            return reviewed_rows, None
        if _schema_issue(payload, job.schema):
            return reviewed_rows, None
    except (ValueError, TypeError):
        return reviewed_rows, None

    expected = list(job.expected_ids or ())
    original_ids = [row.get("evidence_id") for row in original_rows]
    counts = Counter(original_ids)
    missing = [evidence_id for evidence_id in expected
               if evidence_id not in counts]
    extra = [evidence_id for evidence_id in original_ids
             if evidence_id not in set(expected)]
    duplicate = [evidence_id for evidence_id, count in counts.items()
                 if count > 1]
    if not missing or extra or duplicate:
        return reviewed_rows, None

    reviewed_by_id = {row["evidence_id"]: row for row in reviewed_rows}
    original_by_id = {row["evidence_id"]: row for row in original_rows}
    if any(evidence_id not in reviewed_by_id for evidence_id in missing):
        return reviewed_rows, None
    merged = [original_by_id.get(evidence_id, reviewed_by_id[evidence_id])
              for evidence_id in expected]
    _decode_job_rows(
        job, json.dumps({"assignments": merged}, ensure_ascii=False),
        "assignments")
    return merged, {
        "kind": "incomplete_coverage",
        "preserved_existing_rows": len(original_rows),
        "imported_evidence_ids": missing,
        "overwritten_existing_rows": 0,
    }


class _Stage05BatchRunner:
    """Paced native-Anthropic Message Batch transport for Stage 05 only."""

    def __init__(self, client_, stats, *,
                 wave_size=STAGE05_ANTHROPIC_WAVE_SIZE,
                 interval_seconds=STAGE05_ANTHROPIC_BATCH_INTERVAL_SECONDS,
                 grammar_retry_attempts=STAGE05_GRAMMAR_RETRY_ATTEMPTS,
                 sleep_fn=time.sleep, clock_fn=time.monotonic,
                 success_callback=None):
        self.client = client_
        self.stats = stats
        self.native_anthropic = bool(getattr(
            client_, "native_anthropic_batches", False))
        self.wave_size = wave_size
        self.interval_seconds = interval_seconds
        self.grammar_retry_attempts = grammar_retry_attempts
        self.sleep_fn = sleep_fn
        self.clock_fn = clock_fn
        self.success_callback = success_callback
        self.last_submission_at = None
        self.normalization_events = {}

    def _pace(self):
        if self.last_submission_at is None:
            return
        remaining = (self.last_submission_at + self.interval_seconds
                     - self.clock_fn())
        if remaining > 0:
            print(f"  Stage 05 Anthropic capacity backoff: waiting "
                  f"{remaining:.0f}s before the next Message Batch")
            self.sleep_fn(remaining)

    def _submit(self, corpus, preamble, jobs):
        self._pace()
        self.last_submission_at = self.clock_fn()
        return self.client.batch(corpus, preamble, jobs)

    def _persist_completed(self, jobs, replies):
        """Checkpoint valid rows before any capacity backoff can be interrupted."""
        if self.success_callback is None:
            return
        for job in jobs:
            result = _as_result(replies.get(job.id, ""))
            if result.out_of_budget or result.provider_error:
                continue
            try:
                _decode_job_rows(job, result.text, "assignments")
            except (ValueError, TypeError):
                continue
            self.success_callback(job, result)

    def _wave(self, corpus, preamble, jobs):
        raw_replies = self._submit(corpus, preamble, jobs)
        replies = {
            job.id: _normalize_stage05_result(
                job, raw_replies.get(job.id, ""), self.normalization_events)
            for job in jobs
        }
        self._persist_completed(jobs, replies)
        by_id = {job.id: job for job in jobs}
        limited = [job for job in jobs if _stage05_grammar_rate_limited(
            _as_result(replies.get(job.id, "")))]
        attempt = 0
        while limited and attempt < self.grammar_retry_attempts:
            attempt += 1
            self.stats.grammar_rate_limit_retries += len(limited)
            self.stats.grammar_retried_ids.update(job.id for job in limited)
            print("\n  STAGE 05 GRAMMAR CAPACITY RETRY")
            print(f"  {len(limited)} request(s) hit Anthropic's grammar "
                  "compilation rate limit")
            print(f"  Retry attempt: {attempt}/{self.grammar_retry_attempts}")
            print("  Retrying only those requests in a later paced Message Batch")
            raw_retried = self._submit(corpus, preamble, limited)
            retried = {
                job.id: _normalize_stage05_result(
                    job, raw_retried.get(job.id, ""), self.normalization_events)
                for job in limited
            }
            self._persist_completed(limited, retried)
            for job in limited:
                replies[job.id] = _as_result(retried.get(job.id, ""))
            limited = [by_id[job.id] for job in limited
                       if _stage05_grammar_rate_limited(
                           _as_result(replies.get(job.id, "")))]
        return replies

    def __call__(self, corpus, preamble, jobs):
        if not self.native_anthropic:
            raw_replies = self.client.batch(corpus, preamble, jobs)
            return {
                job.id: _normalize_stage05_result(
                    job, raw_replies.get(job.id, ""), self.normalization_events)
                for job in jobs
            }
        replies = {}
        for start in range(0, len(jobs), self.wave_size):
            wave = jobs[start:start + self.wave_size]
            replies.update(self._wave(corpus, preamble, wave))
        return replies


class _Stage05BatchProxy:
    """Give existing bounded repair helpers the paced Stage 05 transport."""

    def __init__(self, runner):
        self.runner = runner

    def batch(self, corpus, preamble, jobs):
        return self.runner(corpus, preamble, jobs)


def _stage05_audit_recoveries(project_dir, jobs, corpus, preamble):
    """Recover paid, exact-contract Stage 05 successes from durable audit logs.

    The interrupted legacy path audited every provider response but had no
    per-chunk artifacts. Exact prompt, cached system prefix, schema and ID
    coverage checks make this a local migration, not a fuzzy cache lookup.
    """
    wanted = {job.id: job for job in jobs}
    if not wanted:
        return {}, {}
    log_root = os.path.join(project_dir, "logs", "model")
    if not os.path.isdir(log_root):
        return {}, {}
    roots = []
    for root, _dirs, files in os.walk(log_root):
        if ("request.json" in files and "response.json" in files
                and "pipeline_batch_job_05_" in os.path.basename(root)):
            roots.append(root)
    expected_system = [text for text in (preamble, corpus)
                       if isinstance(text, str) and text.strip()]
    recovered = {}
    reviewable = {}
    for root in sorted(roots, key=os.path.getmtime, reverse=True):
        try:
            request = _load_json(os.path.join(root, "request.json"))
            job = wanted.get(request.get("job_id"))
            if job is None or job.id in recovered:
                continue
            body = request.get("request") or {}
            messages = body.get("messages") or []
            prompt = messages[-1].get("content") if messages else None
            schema = (((body.get("output_config") or {}).get("format") or {})
                      .get("schema"))
            system = body.get("system") or []
            system_text = ([block.get("text") for block in system
                            if isinstance(block, dict)
                            and block.get("type") == "text"]
                           if isinstance(system, list) else [system])
            if (prompt != job.prompt or schema != job.schema
                    or system_text != expected_system):
                continue
            response = _load_json(os.path.join(root, "response.json"))
            metadata = response.get("metadata") or {}
            stop = metadata.get("stop_reason") or metadata.get("finish_reason")
            if stop not in ("end_turn", "stop"):
                continue
            text = response.get("text") or ""
            normalized, normalization = _normalize_stage05_assignment_wrapper(text)
            usage = metadata.get("usage") or {}
            output_details = (usage.get("output_tokens_details") or
                              usage.get("completion_tokens_details") or {})
            result = BatchResult(
                text=text, stop_reason=stop,
                reasoning_tokens=int(
                    output_details.get("thinking_tokens") or
                    output_details.get("reasoning_tokens") or 0),
                completion_tokens=int(
                    usage.get("output_tokens") or
                    usage.get("completion_tokens") or 0),
                provider=request.get("provider"))
            result = dataclasses.replace(result, text=normalized)
            audited_job = dataclasses.replace(
                job, max_tokens=int(body.get("max_tokens") or job.max_tokens))
            source = os.path.relpath(root, project_dir)
            try:
                rows = _decode_job_rows(job, normalized, "assignments")
            except (ValueError, TypeError) as error:
                reviewable.setdefault(job.id, (
                    audited_job, result, str(error), source, normalization))
                continue
            recovered[job.id] = (
                result, rows, source, normalization)
            reviewable.pop(job.id, None)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            continue
    return recovered, reviewable


def _stage05_artifact(original, chunk, corpus, rows, result, *,
                      audit_source=None, repaired=False,
                      deterministic_normalization=None,
                      incomplete_coverage_merge=None):
    saved = {
        "chunk_id": original.id,
        "evidence_ids": chunk["evidence_ids"],
        "estimated_input_tokens": chunk["estimated_tokens"],
        "fingerprint": _job_fingerprint(
            original, chunk["evidence_ids"], corpus, "stage05_assign_v1"),
        "response_telemetry": {
            "provider": result.provider,
            "stop_reason": result.stop_reason,
            "completion_tokens": result.completion_tokens,
            "reasoning_tokens": result.reasoning_tokens,
        },
        "assignments": rows,
    }
    if audit_source:
        saved["migrated_from_model_audit"] = audit_source
    if repaired:
        saved["structured_repair"] = True
    if deterministic_normalization:
        saved["deterministic_normalization"] = deterministic_normalization
    if incomplete_coverage_merge:
        saved["incomplete_coverage_merge"] = incomplete_coverage_merge
    return saved


def _print_stage05_execution_summary(stats, adaptive_stats, total):
    first_pass = len(
        stats.successful_ids - stats.grammar_retried_ids
        - stats.budget_retried_ids - stats.repaired_ids)
    print("\n  Stage 05 execution summary")
    print(f"  Chunks complete/reusable:       "
          f"{stats.reusable + len(stats.successful_ids)}/{total}")
    print(f"  First-pass successes:           {first_pass}")
    print(f"  Grammar-rate-limit retries:     "
          f"{stats.grammar_rate_limit_retries}")
    print(f"  Output-budget retries:          {adaptive_stats.budget_retries}")
    print(f"  Malformed-response repairs:     {adaptive_stats.json_repairs}")
    print(f"  Hard failures:                  {len(stats.hard_failure_ids)}")


def _run_stage05_assignments(c, corpus, jobs, chunks, artifact_dir,
                             diagnostics_dir, args, project_dir, force=False):
    """Run Stage 05 with resumable artifacts and Anthropic-safe Batch waves."""
    from llm import confirm

    os.makedirs(artifact_dir, exist_ok=True)
    originals = {job.id: job for job in jobs}
    chunks_by_id = {job.id: chunk for job, chunk in zip(jobs, chunks)}
    complete = {}
    stats = Stage05ExecutionStats()
    for job in jobs:
        path = os.path.join(artifact_dir, f"{job.id}.json")
        expected_fingerprint = _job_fingerprint(
            job, chunks_by_id[job.id]["evidence_ids"], corpus,
            "stage05_assign_v1")
        if os.path.isfile(path) and not force:
            try:
                saved = _load_json(path)
                if saved.get("fingerprint") != expected_fingerprint:
                    continue
                _decode_job_rows(
                    job, json.dumps({"assignments": saved.get("assignments")}),
                    "assignments")
                complete[job.id] = saved
            except (OSError, ValueError, TypeError):
                continue

    missing = [job for job in jobs if job.id not in complete]
    if missing and not force:
        audited, reviewable_audits = _stage05_audit_recoveries(
            project_dir, missing, corpus, SEGMENT_PREAMBLE)
        for job in missing:
            if job.id not in audited:
                continue
            result, rows, source, normalization = audited[job.id]
            saved = _stage05_artifact(
                job, chunks_by_id[job.id], corpus, rows, result,
                audit_source=source,
                deterministic_normalization=normalization)
            _json_atomic(os.path.join(artifact_dir, f"{job.id}.json"), saved)
            complete[job.id] = saved
        if audited:
            print(f"  Stage 05 audit migration: preserved {len(audited)} "
                  "completed paid response(s); 0 model calls made")
    else:
        reviewable_audits = {}

    stats.reusable = len(complete)
    missing = [job for job in jobs if job.id not in complete]
    if not missing:
        print(f"  05 assign: reusing all {len(jobs)} completed chunk artifact(s)")
        return [complete[job.id] for job in jobs]

    reviewable_audits = {
        job_id: recovery for job_id, recovery in reviewable_audits.items()
        if job_id in {job.id for job in missing}
    }
    fresh_missing = [job for job in missing if job.id not in reviewable_audits]
    native_anthropic = bool(getattr(c, "native_anthropic_batches", False))
    wave_size = (STAGE05_ANTHROPIC_WAVE_SIZE if native_anthropic
                 else max(len(fresh_missing), 1))
    print(f"  05 assign: {len(missing)} unfinished; {len(complete)} reusable")
    if reviewable_audits:
        chunk_ids = ", ".join(sorted(reviewable_audits))
        print(f"  Stage 05 audit recovery: {len(reviewable_audits)} completed "
              f"contract-invalid response(s) will go directly to one bounded "
              f"reviewer each ({chunk_ids})")
        print("  Original assignment calls replayed for these chunks: 0")
    if native_anthropic:
        print(f"  Stage 05 Anthropic waves: up to {wave_size} requests · "
              f"{STAGE05_ANTHROPIC_BATCH_INTERVAL_SECONDS}s minimum interval")
        print(f"  Grammar-capacity retries: at most "
              f"{STAGE05_GRAMMAR_RETRY_ATTEMPTS} per affected request")
    print("  Stage 05 output ladder: " + " → ".join(
        _token_tier_label(tier) for tier in STAGE05_TOKEN_TIERS))
    if not confirm(c.estimate(corpus, SEGMENT_PREAMBLE, missing, batched=True),
                   getattr(args, "yes", False)):
        return None
    _prewarm_segment_stage(c, corpus, SEGMENT_PREAMBLE, "05")

    runner = _Stage05BatchRunner(c, stats)
    proxy = _Stage05BatchProxy(runner)
    adaptive_stats = AdaptiveStageStats(stage="05", total=len(fresh_missing))

    def persist_success(final_job, result):
        original = originals[final_job.id]
        rows = _decode_job_rows(original, result.text, "assignments")
        saved = _stage05_artifact(
            original, chunks_by_id[original.id], corpus, rows, result,
            deterministic_normalization=runner.normalization_events.get(
                original.id))
        _json_atomic(os.path.join(artifact_dir, f"{original.id}.json"), saved)
        complete[original.id] = saved
        stats.successful_ids.add(original.id)
        if final_job.max_tokens != STAGE05_TOKEN_TIERS[0]:
            stats.budget_retried_ids.add(original.id)

    # Native Batch capacity retries can wait for a minute. Checkpoint every
    # valid response as soon as its Batch result is available, before waiting,
    # so an interrupted backoff never discards completed paid work.
    runner.success_callback = persist_success

    results, final_jobs = {}, []
    if fresh_missing:
        try:
            results, final_jobs = _run_adaptive_stage(
                c, corpus, SEGMENT_PREAMBLE, fresh_missing, diagnostics_dir,
                stage="05", key="assignments", wave_size=wave_size,
                stats=adaptive_stats,
                debug=getattr(args, "segment_debug", False),
                token_tiers=STAGE05_TOKEN_TIERS, batch_runner=runner,
                success_callback=persist_success,
                first_pass_exclusions=stats.grammar_retried_ids)
        except BatchOutputError:
            stats.hard_failure_ids.update(adaptive_stats.terminal_failure_ids)
            _print_stage05_execution_summary(stats, adaptive_stats, len(jobs))
            raise

    for job_id in sorted(reviewable_audits):
        audited_job, result, reason, source, normalization = (
            reviewable_audits[job_id])
        results[job_id] = result
        final_jobs.append(audited_job)
        runner.normalization_events.update(
            {job_id: {"chunk_id": job_id, **normalization}}
            if normalization else {})
        print("\n  STAGE 05 CONTRACT REVIEW")
        print(f"  chunk: {job_id}")
        print(f"  completed response reused from: {source}")
        print(f"  contract violation: {reason}")
        print("  original assignment call replayed: no")

    final_by_id = {job.id: job for job in final_jobs}
    errors = []
    for original in missing:
        if original.id in complete:
            continue
        final_job = final_by_id[original.id]
        result = results[original.id]
        repairs_before = adaptive_stats.json_repairs
        try:
            result = _normalize_stage05_result(
                final_job, result, runner.normalization_events)
            rows = _batch_rows(
                {original.id: result}, [final_job], "assignments",
                diagnostics_dir,
                repair=lambda failed: _repair_batch(
                    proxy, corpus, SEGMENT_PREAMBLE, failed, "assignments",
                    preserve_guidance=(
                        "Return the COMPLETE corrected assignment artifact for "
                        "this chunk, including every valid row from the previous "
                        "response plus one row for every omitted input evidence "
                        "ID. The validated segment catalogue is in the system "
                        "context. Do not return only the repaired or missing rows.")),
                rerun=lambda failed, factor: _rerun_batch(
                    proxy, corpus, SEGMENT_PREAMBLE, failed, factor),
                rerun_factor=1, adaptive_stats=adaptive_stats)
            repaired = adaptive_stats.json_repairs > repairs_before
            rows, coverage_merge = _merge_stage05_incomplete_coverage(
                original, result.text, rows)
            if coverage_merge:
                imported = ", ".join(
                    str(value) for value in
                    coverage_merge["imported_evidence_ids"])
                print("\n  STAGE 05 INCOMPLETE-COVERAGE MERGE")
                print(f"  chunk: {original.id}")
                print(f"  existing rows preserved exactly: "
                      f"{coverage_merge['preserved_existing_rows']}")
                print(f"  reviewer rows imported only for missing IDs: {imported}")
                print("  existing assignment decisions overwritten: 0")
            saved = _stage05_artifact(
                original, chunks_by_id[original.id], corpus, rows, result,
                repaired=repaired,
                deterministic_normalization=runner.normalization_events.get(
                    original.id), incomplete_coverage_merge=coverage_merge)
            _json_atomic(os.path.join(
                artifact_dir, f"{original.id}.json"), saved)
            complete[original.id] = saved
            stats.successful_ids.add(original.id)
            if repaired:
                stats.repaired_ids.add(original.id)
        except BatchOutputError as error:
            stats.hard_failure_ids.add(original.id)
            errors.append((original.id, error))

    _print_stage05_execution_summary(stats, adaptive_stats, len(jobs))
    if errors:
        sample = "; ".join(f"{job_id}: {error}" for job_id, error in errors[:3])
        raise BatchOutputError(
            f"Stage 05 could not complete {len(errors)} chunk(s). Successful "
            f"chunk artifacts were preserved for resume. {sample}")
    return [complete[job.id] for job in jobs]


def _completed_segmentation_inputs(cfg, voc):
    """Load authenticated Stage 04/05 outputs, and the graph, for a resume."""
    discovery = paths.research(cfg["_dir"], "segments", "discovery")
    candidates_path = os.path.join(discovery, "discovered_segments.json")
    decisions_path = os.path.join(voc, "validated_segments.json")
    assignments_path = os.path.join(voc, "segment_assignments.jsonl")
    missing = [path for path in (candidates_path, decisions_path, assignments_path)
               if not os.path.isfile(path)]
    if missing:
        sys.exit("Cannot start the commercial synthesis layer: missing completed "
                 "Stage 03--05 artifact(s):\n  " + "\n  ".join(missing))
    candidates = _load_json(candidates_path)
    decisions = _load_json(decisions_path)
    by_segment_id = {row["segment_id"]: row for row in candidates}
    validated = [by_segment_id[row["segment_id"]] for row in decisions
                 if (row.get("status") == "validated"
                     and row.get("segment_id") in by_segment_id)]
    assignments = _normalize_assignment_contract(_read_jsonl(assignments_path))
    # The resume path must see the same taxonomy the full run would, or Stage 07
    # coalesces a segment set that Stage 05 never assigned against. That includes
    # the graph: without it every child is re-judged as a root and the resume
    # quietly drops segments the full run kept.
    graph_path = os.path.join(voc, "segment_graph.json")
    graph = (_load_json(graph_path) if os.path.isfile(graph_path)
             else {"specialises": [], "adjacent": []})
    validated = _apply_segment_floors(validated, decisions, cfg, graph=graph)
    if not validated:
        sys.exit("Cannot start Stage 07: no validated Stage-04 segments found.")
    return validated, decisions, assignments, graph


def _stage08_card(canonical):
    return {key: canonical[key] for key in (
        "canonical_segment_id", "slug", "name", "definition",
        "commercial_distinction", "subsegments", "attributes")}


def _stage08_chunk_prompt(canonical, records):
    return (
        "Extract VOC signals from this bounded portion of the complete evidence "
        "union for the canonical audience below. Do not reconsider the audience."
        "\n\nCANONICAL AUDIENCE:\n" +
        json.dumps(_stage08_card(canonical), ensure_ascii=False, indent=2) +
        "\n\nEVIDENCE CHUNK:\n" +
        segmentation.evidence_text(records, include_tier=True))


# Which extraction skill owns each commercial signal dimension. This mapping is
# the whole point of sourcing synthesis from extractions: a pain point in the
# operator's pack becomes the same object skill 07 defined over 980 lines of
# classification boundaries, rather than a second, thinner notion of a pain point
# re-derived from raw text by a stage that had never read those rules.
SIGNAL_DIMENSION_SKILLS = {
    "pain_points": (7,),
    "desired_outcomes": (9,),
    "failed_solutions": (14,),
    "triggers": (8,),
    "beliefs": (12,),
    "emotional_states": (10,),
    "objections": (18,),
    "buying_triggers": (16,),
    "representative_language": (24, 25),
}


def _extraction_documents(cfg, slugs):
    """Every existing extraction that feeds a commercial signal dimension."""
    wanted = {}
    for dimension, numbers in SIGNAL_DIMENSION_SKILLS.items():
        for number in numbers:
            wanted.setdefault(number, []).append(dimension)
    documents = []
    for slug in slugs:
        directory = paths.extractions(cfg["_dir"], slug)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".md"):
                continue
            try:
                number = int(name[:2])
            except ValueError:
                continue
            if number not in wanted:
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    text = handle.read().strip()
            except OSError:
                continue
            if text:
                documents.append({
                    "segment_slug": slug, "skill": number,
                    "dimensions": wanted[number], "name": name, "text": text,
                })
    return documents


def _stage08_extraction_chunks(canonical, documents, target_tokens):
    """Chunk extraction documents, keeping each document whole where possible."""
    chunks, current, size = [], [], 0
    for document in documents:
        cost = max(len(document["text"]) // 4, 1)
        if current and size + cost > target_tokens:
            chunks.append(current)
            current, size = [], 0
        current.append(document)
        size += cost
    if current:
        chunks.append(current)
    canonical_id = canonical["canonical_segment_id"]
    return [{
        "chunk_id": f"08a_{canonical_id}_x{index:04d}",
        "evidence_ids": list(canonical["primary_evidence_ids"]),
        "estimated_tokens": max(
            sum(len(document["text"]) for document in group) // 4, 1),
        "documents": group,
    } for index, group in enumerate(chunks)]


def _stage08_extraction_prompt(canonical, documents):
    body = "\n\n".join(
        f"--- {document['segment_slug']} · {document['name']} "
        f"(dimensions: {', '.join(document['dimensions'])}) ---\n"
        f"{document['text']}" for document in documents)
    return (
        "Convert the completed extraction documents below into structured VOC "
        "signals for this canonical audience. These are finished analyses of "
        "this audience's own evidence, produced by the extraction skills — do "
        "not re-analyse the raw text, re-open the audience, or introduce "
        "findings the documents do not contain. Carry each document's findings "
        "across, preserving its labels and wording where they are already "
        "good, and cite the evidence IDs it cites."
        "\n\nCANONICAL AUDIENCE:\n" +
        json.dumps(_stage08_card(canonical), ensure_ascii=False, indent=2) +
        "\n\nEXTRACTION DOCUMENTS:\n" + body)


def _run_commercial_layers(c, cfg, args, items, by_id, validated, decisions,
                           assignments):
    """Run the resumable semantic Stages 07/08 and deterministic Stage 09."""
    from llm import Job, confirm

    commercial_dir = paths.research(cfg["_dir"], "segments", "commercial")
    synthesis_dir = os.path.join(commercial_dir, "synthesis")
    diagnostics = paths.voc(cfg["_dir"], "_model_failures", "commercial")
    os.makedirs(commercial_dir, exist_ok=True)
    os.makedirs(synthesis_dir, exist_ok=True)
    stage_start = _model_meter(c) if c is not None else None

    # --------------------------------------------------- 07 commercial grouping
    s07 = skill_named("commercial_07_coalesce.md")
    cards = commercial.build_stage07_cards(
        validated, decisions, assignments, by_id)
    schema07 = commercial.stage07_schema(row["segment_id"] for row in validated)
    fingerprint07 = _value_fingerprint({
        "cards": cards, "skill": s07, "schema": schema07,
        "contract": commercial.COMMERCIAL_CONTRACT_VERSION,
    })
    catalogue_path = os.path.join(commercial_dir, "07_canonical_segments.json")
    mapping_path = os.path.join(commercial_dir, "07_commercial_mapping.json")
    model07_path = os.path.join(commercial_dir, "07_model_response.json")
    meta07_path = os.path.join(commercial_dir, "07.meta.json")
    catalogue = mapping = None
    if (not _segment_force(args, "07") and _meta_matches(
            meta07_path, fingerprint07)
            and os.path.isfile(catalogue_path) and os.path.isfile(mapping_path)):
        try:
            catalogue = _load_json(catalogue_path)
            mapping = _load_json(mapping_path)
            commercial.validate_stage07_artifacts(
                catalogue, mapping, validated, assignments)
        except (OSError, TypeError, ValueError) as error:
            print(f"  07 cached artifact failed validation: {error}")
            catalogue = mapping = None
        else:
            print(f"  07 commercial coalescing: reusing "
                  f"{catalogue['canonical_segment_count']} canonical audiences")
    if catalogue is None:
        if getattr(args, "from_stage", None) in ("08", "09"):
            sys.exit("Stage 07 artifacts are missing or stale. Resume with: "
                     f"./adpipe -p {cfg['name']} segment --from 07")
        if c is None:
            sys.exit("Stage 07 requires a model provider.")
        prompt07 = (
            "Coalesce this complete compact catalogue of validated research "
            "segments. Metrics are deterministic; representative evidence is a "
            "bounded sample. Map every supplied segment_id exactly once.\n\n"
            "RESEARCH SEGMENT CARDS:\n" +
            json.dumps(cards, ensure_ascii=False, indent=2))
        job07 = Job(
            id="07_commercial_coalesce", prompt=prompt07,
            max_tokens=STAGE07_TOKEN_TIERS[0], schema=schema07,
            effort=_segment_setting(cfg, "07_effort", None))
        print("  Stage 07 retry ladder: " + " → ".join(
            _token_tier_label(tier) for tier in STAGE07_TOKEN_TIERS))
        if not confirm(c.estimate(s07, SEGMENT_PREAMBLE, [job07]),
                       getattr(args, "yes", False)):
            return False

        def validate07(payload):
            commercial.finalize_stage07(
                payload, validated, assignments, by_id)

        raw07 = _run_single_structured(
            c, s07, SEGMENT_PREAMBLE, job07,
            os.path.join(diagnostics, "07"), token_tiers=STAGE07_TOKEN_TIERS,
            response_validator=validate07,
            repair_guidance=(
                "Return the COMPLETE catalogue and COMPLETE mapping. Correct only "
                "the stated immutable-ID, exact-coverage, or shape violation. "
                "Never return only the rows being repaired and do not make new "
                "evidence assignments."))
        catalogue, mapping = commercial.finalize_stage07(
            raw07, validated, assignments, by_id)
        _json_atomic(model07_path, raw07)
        _json_atomic(catalogue_path, catalogue)
        _json_atomic(mapping_path, mapping)
        _json_atomic(meta07_path, {"input_fingerprint": fingerprint07})
        print(f"  07 commercial coalescing: {len(validated)} research segments → "
              f"{catalogue['canonical_segment_count']} canonical audiences")

    # ------------------------------------------------------ 08 VOC synthesis
    s08a = skill_named("commercial_08a_extract_signals.md")
    s08b = skill_named("commercial_08b_coalesce_signals.md")
    canonical_rows = catalogue["canonical_segments"]
    synthesis_by_id = {}
    pending = []
    fingerprints08 = {}
    slug_by_segment_id = {row["segment_id"]: row["slug"] for row in validated}
    source_slugs = {
        row["canonical_segment_id"]: [
            slug_by_segment_id[segment_id]
            for segment_id in row["source_segment_ids"]
            if segment_id in slug_by_segment_id]
        for row in canonical_rows}
    for canonical in canonical_rows:
        canonical_id = canonical["canonical_segment_id"]
        evidence_input = [{
            "id": evidence_id,
            "tier": by_id[evidence_id].get("tier", "context"),
            "text": by_id[evidence_id]["text"],
        } for evidence_id in canonical["primary_evidence_ids"]]
        fingerprint = _value_fingerprint({
            "canonical_mapping": canonical,
            "assigned_evidence": evidence_input,
            # Included so that running `extract` and re-running the commercial
            # layer actually re-synthesises. Without it, a pack built before the
            # extractions existed would be served from cache forever, and the
            # switch to extraction-sourced synthesis would silently never happen.
            "extraction_documents": _extraction_documents(
                cfg, source_slugs[canonical_id]),
            "skills": {"08a": s08a, "08b": s08b},
            "contract": commercial.COMMERCIAL_CONTRACT_VERSION,
        })
        fingerprints08[canonical_id] = fingerprint
        output_path = os.path.join(synthesis_dir, canonical_id + ".json")
        meta_path = output_path + ".meta.json"
        if (not _segment_force(args, "08")
                and _meta_matches(meta_path, fingerprint)
                and os.path.isfile(output_path)):
            try:
                synthesis = _load_json(output_path)
                commercial.validate_synthesis(synthesis, canonical)
            except (OSError, TypeError, ValueError) as error:
                print(f"  08 {canonical_id} cache failed validation: {error}")
            else:
                synthesis_by_id[canonical_id] = synthesis
                continue
        pending.append(canonical)

    if pending and getattr(args, "from_stage", None) == "09":
        sys.exit("Stage 08 artifacts are missing or stale. Resume with: "
                 f"./adpipe -p {cfg['name']} segment --from 08")
    if pending and c is None:
        sys.exit("Stage 08 requires a model provider.")

    chunks08a = []
    jobs08a = []
    canonical_for_job = {}
    evidence_for_job = {}
    target08 = int(_segment_setting(cfg, "08a_chunk_tokens", 8000))
    for canonical in pending:
        canonical_id = canonical["canonical_segment_id"]
        # Prefer the completed extraction documents. Where they exist, the
        # operator pack's pain points are skill 07's pain points rather than a
        # second definition derived here; where they do not, this falls back to
        # the raw evidence so the pipeline still runs end to end in one pass.
        documents = _extraction_documents(cfg, source_slugs[canonical_id])
        if documents:
            chunks = _stage08_extraction_chunks(canonical, documents, target08)
            print(f"  08A {canonical['slug']}: sourcing from "
                  f"{len(documents)} extraction document(s)")
        else:
            records = [by_id[evidence_id]
                       for evidence_id in canonical["primary_evidence_ids"]]
            chunks = segmentation.chunk_by_tokens(
                records, target08, f"08a_{canonical_id}")
            segmentation.assert_exact_chunk_coverage(
                chunks, canonical["primary_evidence_ids"])
            print(f"  ! 08A {canonical['slug']}: no extractions found, "
                  "re-deriving signals from raw evidence. Run `extract` on its "
                  "research segments first for pack-sourced synthesis.")
        for chunk in chunks:
            prompt = (_stage08_extraction_prompt(canonical, chunk["documents"])
                      if "documents" in chunk
                      else _stage08_chunk_prompt(canonical, chunk["records"]))
            job = Job(
                id=chunk["chunk_id"], prompt=prompt,
                max_tokens=STAGE08A_TOKEN_TIERS[0],
                schema=commercial.stage08a_schema(chunk["evidence_ids"]),
                effort=_segment_setting(cfg, "08a_effort", None))
            chunks08a.append(chunk)
            jobs08a.append(job)
            canonical_for_job[job.id] = canonical_id
            evidence_for_job[job.id] = {
                evidence_id: by_id[evidence_id]
                for evidence_id in chunk["evidence_ids"]}

    if jobs08a:
        def validate08a(job, rows):
            commercial.validate_stage08a(
                {"signals": rows}, evidence_for_job[job.id])

        results08a = _run_persisted_segment_jobs(
            c, s08a, jobs08a, chunks08a, "signals",
            os.path.join(synthesis_dir, "_08a_chunks"),
            os.path.join(diagnostics, "08a"), args, "08A",
            force=_segment_force(args, "08"), row_validator=validate08a,
            repair_guidance=(
                "Return the complete signals array for this one evidence chunk. "
                "Correct only structural/provenance violations, preserve valid "
                "signals and verbatim quotes, and cite only supplied IDs."),
            contract_version=commercial.COMMERCIAL_CONTRACT_VERSION)
        if results08a is None:
            return False
    else:
        results08a = []

    results_by_canonical = defaultdict(list)
    for result in results08a:
        results_by_canonical[canonical_for_job[result["chunk_id"]]].append(result)

    jobs08b = []
    job08b_context = {}
    for canonical in pending:
        canonical_id = canonical["canonical_segment_id"]
        signal_catalogue = commercial.catalogue_stage08a(
            results_by_canonical[canonical_id])
        if not signal_catalogue:
            empty = {dimension: [] for dimension in commercial.SIGNAL_DIMENSIONS}
            synthesis = {
                "canonical_segment_id": canonical_id,
                "slug": canonical["slug"], "name": canonical["name"],
                "primary_evidence_count": canonical["primary_evidence_count"],
                **empty,
            }
            synthesis_by_id[canonical_id] = synthesis
            _json_atomic(os.path.join(synthesis_dir, canonical_id + ".json"), synthesis)
            _json_atomic(os.path.join(
                synthesis_dir, canonical_id + ".json.meta.json"),
                {"input_fingerprint": fingerprints08[canonical_id]})
            continue
        schema08b = commercial.stage08b_schema(
            [row["signal_id"] for row in signal_catalogue],
            canonical["primary_evidence_ids"])
        prompt08b = (
            "Coalesce the complete extracted signal catalogue for this one "
            "canonical audience. Every signal_id must be mapped exactly once."
            "\n\nCANONICAL AUDIENCE:\n" +
            json.dumps({key: canonical[key] for key in (
                "canonical_segment_id", "slug", "name", "definition",
                "commercial_distinction")}, ensure_ascii=False, indent=2) +
            "\n\nSIGNAL CATALOGUE:\n" +
            json.dumps(signal_catalogue, ensure_ascii=False, indent=2))
        job = Job(
            id=f"08b_{canonical_id}", prompt=prompt08b,
            max_tokens=STAGE08B_TOKEN_TIERS[0], schema=schema08b,
            effort=_segment_setting(cfg, "08b_effort", None))
        jobs08b.append(job)
        job08b_context[job.id] = (canonical, signal_catalogue)

    if jobs08b:
        print(f"  08B: {len(jobs08b)} canonical segment synthesis request(s)")
        print("  Stage 08B retry ladder: " + " → ".join(
            _token_tier_label(tier) for tier in STAGE08B_TOKEN_TIERS))
        if not confirm(c.estimate(s08b, SEGMENT_PREAMBLE, jobs08b),
                       getattr(args, "yes", False)):
            return False
    for job in jobs08b:
        canonical, signal_catalogue = job08b_context[job.id]

        def validate08b(payload, canonical=canonical,
                        signal_catalogue=signal_catalogue):
            commercial.finalize_stage08(
                canonical, signal_catalogue, payload, by_id)

        raw08b = _run_single_structured(
            c, s08b, SEGMENT_PREAMBLE, job,
            os.path.join(diagnostics, "08b", job.id),
            token_tiers=STAGE08B_TOKEN_TIERS,
            response_validator=validate08b,
            repair_guidance=(
                "Return the COMPLETE themes collection. Correct only the stated "
                "signal-lineage or shape violation; preserve every valid theme and "
                "map every original signal_id exactly once."))
        synthesis = commercial.finalize_stage08(
            canonical, signal_catalogue, raw08b, by_id)
        canonical_id = canonical["canonical_segment_id"]
        output_path = os.path.join(synthesis_dir, canonical_id + ".json")
        _json_atomic(os.path.join(synthesis_dir, canonical_id + ".08b.json"), raw08b)
        _json_atomic(output_path, synthesis)
        _json_atomic(output_path + ".meta.json", {
            "input_fingerprint": fingerprints08[canonical_id]})
        synthesis_by_id[canonical_id] = synthesis
        print(f"  08 {canonical_id}: {canonical['primary_evidence_count']:,} "
              "evidence items synthesized")
    if not pending:
        print(f"  08 VOC synthesis: reusing all {len(canonical_rows)} segment(s)")

    for canonical in canonical_rows:
        canonical_id = canonical["canonical_segment_id"]
        if canonical_id not in synthesis_by_id:
            synthesis_by_id[canonical_id] = _load_json(
                os.path.join(synthesis_dir, canonical_id + ".json"))
        commercial.validate_synthesis(synthesis_by_id[canonical_id], canonical)

    # ------------------------------------------------- 09 deterministic pack
    assigned_count = len({row["evidence_id"]
                          for row in commercial.assigned_rows(assignments)})
    stage09_fingerprint = _value_fingerprint({
        "catalogue": catalogue, "mapping": mapping,
        "synthesis": synthesis_by_id,
        "refined_count": len(items), "assigned_count": assigned_count,
        "contract": commercial.COMMERCIAL_CONTRACT_VERSION,
    })
    final_dir = paths.research(cfg["_dir"], "segments", "final")
    meta09 = os.path.join(final_dir, "09.meta.json")
    index_path = os.path.join(final_dir, "00_segment_index.txt")
    if (_segment_force(args, "09") or not _meta_matches(
            meta09, stage09_fingerprint) or not os.path.isfile(index_path)):
        index, _filenames = commercial.render_research_pack(
            final_dir, cfg["name"], len(items), assigned_count,
            len(items) - assigned_count, validated, catalogue, mapping,
            synthesis_by_id, by_id)
        _json_atomic(meta09, {"input_fingerprint": stage09_fingerprint})
        print(f"  09 human research pack: {len(canonical_rows)} segment files -> "
              f"{final_dir}")
    else:
        print(f"  09 human research pack: reusing {index_path}")

    if c is not None:
        stage_end = _model_meter(c)
        summary = {
            "research_segment_count": len(validated),
            "canonical_segment_count": len(canonical_rows),
            "assigned_evidence_count": assigned_count,
            "preserved_evidence_count": mapping["preserved_evidence_count"],
            "model_usage": _meter_delta(stage_start, stage_end),
            "output_paths": {
                "canonical_segments": catalogue_path,
                "commercial_mapping": mapping_path,
                "synthesis": synthesis_dir,
                "research_pack": final_dir,
            },
        }
        _json_atomic(os.path.join(commercial_dir, "07_09_run_summary.json"), summary)
        usage = summary["model_usage"]["usage"]
        print(f"  Stages 07--09 model usage: {usage.get('in', 0):,} input · "
              f"{usage.get('out', 0):,} output · "
              f"{usage.get('reasoning', 0):,} reasoning · "
              f"${summary['model_usage']['cost_usd']:.4f}")
    return True


def cmd_segment(cfg, args):
    """Resumable 03A -> 03B -> 03C -> 04 -> 05 -> 06 -> 07 -> 08 -> 09."""
    from llm import Job, confirm

    requested_source = getattr(args, "source", None)
    src = requested_source or paths.voc(cfg["_dir"], PRODUCTION_VOC_FILE)
    if not os.path.exists(src):
        sys.exit(f"No production VOC at {src}. Run: adpipe -p {cfg['name']} "
                 "refine-voc (or run ingest first).")
    items = _read_jsonl(src)
    for row in items:
        row["tier"] = row.get("tier") or "context"
        if row["tier"] not in segmentation.EVIDENCE_TIERS:
            sys.exit(f"Evidence {row.get('id')} has invalid tier {row['tier']!r}; "
                     "run refine-voc again.")
    voc = paths.voc(cfg["_dir"]); os.makedirs(voc, exist_ok=True)
    audit_path = os.path.join(voc, AUDIT_VOC_FILE)
    if not requested_source and not os.path.exists(audit_path):
        sys.exit(f"No audit VOC at {audit_path}. Run: adpipe -p {cfg['name']} "
                 "refine-voc before segmenting.")
    audit_items = _read_jsonl(audit_path) if os.path.exists(audit_path) else []
    try:
        by_id = _join_production_audit(
            items, audit_items, require_complete=not bool(requested_source))
    except ValueError as error:
        sys.exit(f"{error}; run refine-voc again.")

    # Starting at the synthesis layer is a deliberate migration/resume path.
    # It loads the completed immutable Stage 04/05 contracts directly and never
    # executes Stages 03--06 (including deterministic Stage 06 rewrites).
    if getattr(args, "from_stage", None) in ("07", "08", "09"):
        validated, decisions, rows, graph = _completed_segmentation_inputs(cfg, voc)
        expected = {row["id"] for row in items}
        _require_exact_ids(rows, expected, "persisted Stage 05 assignment")
        # The resume path must reach Stage 07 with the taxonomy the full run
        # would have produced, floors and thresholds included, or it coalesces
        # a segment set that no evidence file was ever built for.
        _enforce_assignment_thresholds(
            rows, paths.voc(cfg["_dir"], "_model_failures", "05_assign"))
        validated = _apply_assigned_floors(validated, decisions, rows, by_id, cfg,
                                           graph=graph)
        if not validated:
            sys.exit("  No segment cleared the evidence floor after assignment.")
        measured = _measure_segment_graph(rows, validated, graph, voc)
        build_research_packs(cfg, validated, rows, by_id, graph, measured, voc)
        stage_client = (None if args.from_stage == "09" else client(cfg, args))
        _run_commercial_layers(
            stage_client, cfg, args, items, by_id, validated, decisions, rows)
        return

    c = client(cfg, args)
    stage03_started = time.monotonic()
    meter_start = _model_meter(c)
    discovery = paths.research(cfg["_dir"], "segments", "discovery")
    os.makedirs(discovery, exist_ok=True)
    diagnostics = os.path.join(voc, "_model_failures")
    counts = segmentation.tier_counts(items)
    print(f"  {len(items):,} deduplicated evidence items · "
          f"Core {counts['core']:,} · Supporting {counts['supporting']:,} · "
          f"Context {counts['context']:,}")

    # Preserve the monolithic result before the first multi-pass run.
    cand_p = os.path.join(voc, "candidate_segments.json")
    legacy_p = os.path.join(discovery, "03_monolithic_candidate_segments.json")
    if os.path.isfile(cand_p) and not os.path.exists(legacy_p):
        try:
            prior_candidates = _load_json(cand_p)
        except (OSError, ValueError):
            prior_candidates = []
        if any("scores" in row or "confidence" in row
               for row in prior_candidates if isinstance(row, dict)):
            shutil.copy2(cand_p, legacy_p)

    # -------------------------------------------------------------- 03A harvest
    core_items = [row for row in items if row["tier"] == "core"]
    discovery_items = segmentation.initial_discovery_records(
        items, include_supporting=bool(
            _segment_setting(cfg, "03a_include_supporting", False)))
    if not core_items:
        sys.exit("Stage 03A requires Core evidence; run refine-voc and inspect tiers.")
    chunks03a = segmentation.chunk_by_tokens(
        discovery_items, int(_segment_setting(cfg, "03a_chunk_tokens", 12000)), "03a")
    segmentation.assert_exact_chunk_coverage(
        chunks03a, [row["id"] for row in discovery_items])
    manifest03a = [{k: chunk[k] for k in
                    ("chunk_id", "estimated_tokens", "evidence_ids")}
                   for chunk in chunks03a]
    _json_atomic(os.path.join(discovery, "03a_chunk_manifest.json"), manifest03a)
    s03a = skill_named("03a_discover_segment_candidates.md")
    chunk_ids03a = {chunk["chunk_id"]: tuple(chunk["evidence_ids"])
                    for chunk in chunks03a}
    cleanup_events03a = {}
    provenance_failures03a = set()

    def clean03a(job, rows):
        try:
            events = segmentation.clean_harvest_provenance(
                rows, chunk_ids03a[job.id], chunk_id=job.id)
        except segmentation.HarvestProvenanceFailure as error:
            signature = (job.id, error.candidate,
                         tuple(error.invalid_evidence_ids))
            if signature not in provenance_failures03a:
                provenance_failures03a.add(signature)
                _print_03a_contract_error(error, job.id)
            error.reported = True
            raise
        for event in events:
            signature = (
                event["chunk_id"], event["candidate"],
                tuple(event["removed_evidence_ids"]),
                event["remaining_evidence_count"],
            )
            if signature in cleanup_events03a:
                continue
            cleanup_events03a[signature] = event
            print("\n  03A PROVENANCE CLEANUP")
            print(f"  chunk: {event['chunk_id']}")
            print(f"  candidate: {event['candidate']}")
            print(f"  removed impossible IDs: {event['removed_evidence_ids']}")
            print("  remaining valid evidence IDs: "
                  f"{event['remaining_evidence_count']}")
        return events

    def validate03a(job, rows):
        segmentation.validate_harvest_rows(
            rows, chunk_ids03a[job.id], chunk_id=job.id)

    jobs03a = [Job(
        id=chunk["chunk_id"],
        prompt=segmentation.harvest_prompt(chunk["records"]),
        max_tokens=int(_segment_setting(cfg, "03a_max_tokens", 12000)),
        schema=segmentation.harvest_schema(chunk["evidence_ids"]),
        effort=_segment_setting(cfg, "03a_effort", None)) for chunk in chunks03a]
    # Compatibility is intentionally narrow: these are the exact same jobs with
    # the prior static schema. A cached result from that contract is locally
    # revalidated and migrated; impossible provenance is cleaned deterministically,
    # while genuinely structural/semantic failures alone enter model repair.
    legacy_jobs03a = [dataclasses.replace(
        job, schema=segmentation.pre_register_harvest_schema()) for job in jobs03a]
    compatible_03a_fingerprints = _stage03a_compatible_fingerprints(
        jobs03a, chunks03a)
    repair03a = (
        "Preserve every discovered candidate and every valid supporting evidence "
        "ID from the previous response. Correct only contract violations: "
        "deduplicate repeated IDs and repair invalid enum values or empty required "
        "fields. If a candidate has no valid supporting ID, cite "
        "only supplied IDs that genuinely support it, or omit that candidate when "
        "none do. Do not merge, add, or reinterpret candidates. Input evidence may "
        "remain unassigned.")

    def run03a():
        return _run_persisted_segment_jobs(
            c, s03a, jobs03a, chunks03a, "candidates",
            os.path.join(discovery, "03a_chunk_candidates"),
            os.path.join(diagnostics, "03a"), args, "03A",
            force=_segment_force(args, "03a"), row_validator=validate03a,
            row_cleaner=clean03a,
            repair_guidance=repair03a,
            contract_version=segmentation.HARVEST_CONTRACT_VERSION,
            compatible_jobs=legacy_jobs03a,
            compatible_fingerprints=compatible_03a_fingerprints,
            cached_migrator=lambda job, rows, _fingerprint:
                segmentation.assign_harvest_candidate_ids(
                    segmentation.migrate_legacy_harvest_rows(rows), job.id))

    results03a = run03a()
    if results03a is None:
        return
    if cleanup_events03a:
        events = list(cleanup_events03a.values())
        print("\n  03A provenance cleanup:")
        print(f"    {len({event['chunk_id'] for event in events})} chunks affected")
        print(f"    {sum(len(event['removed_evidence_ids']) for event in events)} "
              "impossible evidence IDs removed")
        print("    0 candidates dropped")
        print("    0 model calls made for cleanup")
    meter_03a = _model_meter(c)
    full_claims = segmentation.harvest_full_chunk_claims(results03a)
    if full_claims:
        sample = ", ".join(
            f"{row['chunk_id']}→{row['candidate_key']} ({row['evidence_count']} IDs)"
            for row in full_claims[:4])
        print(f"  ! 03A review signal: {len(full_claims)}/{len(results03a)} chunk(s) "
              f"returned one candidate claiming every input ID: {sample}")
        print("    This is allowed for a genuinely homogeneous chunk, but repeated "
              "instances should be audited before trusting 03B.")
    try:
        catalogue = segmentation.aggregate_harvest(results03a)
    except segmentation.HarvestContractError as error:
        # Ordinarily persisted-result validation catches this earlier. Keep the
        # aggregate boundary guarded as defence against a manually edited or
        # concurrently changed artifact, and send it through the same repair path.
        _print_03a_contract_error(error)
        print("  Repairing this chunk before aggregation...")
        results03a = run03a()
        if results03a is None:
            return
        try:
            catalogue = segmentation.aggregate_harvest(results03a)
        except segmentation.HarvestContractError as final_error:
            _print_03a_contract_error(final_error)
            project = cfg.get("name", "PROJECT")
            raise BatchOutputError(
                "Stage 03A contract repair did not produce a valid artifact. "
                f"Diagnostics: {os.path.join(diagnostics, '03a')}. Resume with: "
                f"./adpipe -p {project} segment") from final_error
    catalogue_p = os.path.join(discovery, "03a_candidate_catalogue.json")
    legacy_catalogue = (_load_json(catalogue_p)
                        if os.path.isfile(catalogue_p) else None)
    _json_atomic(catalogue_p, catalogue)
    print(f"  03A harvest: {len(chunks03a)} chunks · {len(catalogue)} provisional "
          f"candidates -> {catalogue_p}")
    if not catalogue:
        sys.exit("Stage 03A found no recurring candidate with at least two Core IDs.")

    # Only the `segment` register continues into consolidation. Attributes and
    # journey states become the facet vocabulary comments are tagged with;
    # symptoms leave the taxonomy for skills 07/08. Every partition is written
    # out, so nothing discovered here is lost — it is filed.
    segment_rows, facet_rows, symptom_rows = segmentation.partition_by_register(
        catalogue)
    provisional_facets = segmentation.facet_catalogue(facet_rows)
    # 03B is handed the register-free cards. The field is constant across every
    # row that reaches it, so carrying it would add nothing to the prompt while
    # changing the input fingerprint of every completed consolidation -- and the
    # full catalogue above, plus the partition artifact below, already hold it.
    catalogue = [{key: value for key, value in row.items() if key != "register"}
                 for row in segment_rows]
    registers_p = os.path.join(discovery, "03a_register_partition.json")
    _json_atomic(registers_p, {
        "segment_candidates": len(catalogue),
        "facet_candidates": len(facet_rows),
        "symptom_candidates": len(symptom_rows),
        "provisional_facets": provisional_facets,
        "symptoms": symptom_rows,
    })
    if facet_rows or symptom_rows:
        print(f"  03A registers: {len(catalogue)} segment · "
              f"{len(facet_rows)} facet ({len(provisional_facets)} after grouping) · "
              f"{len(symptom_rows)} symptom -> {registers_p}")
    if not catalogue:
        sys.exit("Stage 03A found no candidate in the `segment` register. Every "
                 f"discovery was an attribute, journey state or symptom.\n"
                 f"  Review {registers_p}.")

    # ---------------------------------------------------------- 03B consolidate
    s03b = skill_named("03b_consolidate_segment_candidates.md")
    initial_p = os.path.join(discovery, "03b_initial_consolidated_candidates.json")
    initial_meta = initial_p + ".meta.json"
    catalogue_fingerprint = _value_fingerprint(catalogue)
    initial = None
    force03b = _segment_force(args, "03b")
    if (os.path.isfile(initial_p) and not force03b
            and _meta_matches(initial_meta, catalogue_fingerprint)):
        initial = _load_json(initial_p)
        print(f"  03B consolidate: reusing {len(initial)} candidates")
    elif os.path.isfile(initial_p) and legacy_catalogue and not force03b:
        try:
            initial = segmentation.migrate_legacy_consolidated(
                _load_json(initial_p), legacy_catalogue, catalogue)
        except (KeyError, TypeError, ValueError) as migration_error:
            print("  03B machine-lineage migration could not prove every old join: "
                  f"{migration_error}")
            print("  Only Stage 03B will be rerun; completed Stage 03A work is safe.")
        else:
            _json_atomic(initial_p, initial)
            _json_atomic(initial_meta, {"input_fingerprint": catalogue_fingerprint})
            print(f"  03B lineage migration: recovered all {len(initial)} canonical "
                  "segments from exact saved 03A provenance")
            print("  0 model calls made; Stage 03A and 03B semantic work preserved")
    if initial is None:
        schema03b = segmentation.consolidate_schema(
            row["candidate_id"] for row in catalogue)
        job03b = Job(
            id="03b_consolidate",
            prompt=("Consolidate this complete provisional catalogue globally. "
                    "Rename, merge, and split semantic concepts freely. Return "
                    "source_candidate_ids by selecting exact machine candidate_id "
                    "values from this catalogue. Do not create a canonical machine "
                    "ID; code assigns segment_id after parsing. Do not output a "
                    "validation status."
                    "\n\nCATALOGUE:\n" +
                    json.dumps(catalogue, ensure_ascii=False, indent=2)),
            max_tokens=int(_segment_setting(cfg, "03b_max_tokens", 64000)),
            schema=schema03b,
            effort=_segment_setting(cfg, "03b_effort", None))
        audit_recovery = (None if force03b else
                          _03b_audit_recovery(
                              cfg["_dir"], job03b.id, catalogue))
        if audit_recovery:
            print("  03B recovery: found a completed audited response for the "
                  "current 03A catalogue")
            print(f"  Audit source: {audit_recovery[1]}")
            print("  It will be validated/repaired without rerunning full "
                  "consolidation")
        if not confirm(c.estimate(s03b, SEGMENT_PREAMBLE, [job03b]),
                       getattr(args, "yes", False)):
            return
        initial = _run_03b_consolidation(
            c, s03b, SEGMENT_PREAMBLE, job03b, catalogue,
            os.path.join(diagnostics, "03b"),
            initial_result=audit_recovery[0] if audit_recovery else None)
        _json_atomic(initial_p, initial)
        _json_atomic(initial_meta, {"input_fingerprint": catalogue_fingerprint})
        print(f"  03B consolidate: {len(initial)} canonical candidates -> {initial_p}")
    meter_03b = _model_meter(c)

    # ------------------------------------------------------- evidence expansion
    defs = [{k: row[k] for k in
             ("segment_id", "slug", "name", "definition",
              "commercial_distinction", "inclusion_criteria", "exclusion_criteria")}
            for row in initial]
    skill03e = skill_named("03e_expand_segment_evidence.md")
    catalogue03e = json.dumps(defs, ensure_ascii=False, indent=2)
    s03e = skill03e + STAGE03E_CATALOGUE_MARKER + catalogue03e
    chunks03e = segmentation.chunk_by_tokens(
        items, int(_segment_setting(cfg, "03e_chunk_tokens", 8000)), "03e")
    segmentation.assert_exact_chunk_coverage(chunks03e, [row["id"] for row in items])
    evidence03e = [segmentation.evidence_text(chunk["records"], include_tier=True)
                   for chunk in chunks03e]
    jobs03e = [Job(
        id=chunk["chunk_id"],
        prompt=STAGE03E_EVIDENCE_PREFIX + evidence_payload,
        max_tokens=int(_segment_setting(cfg, "03e_max_tokens", 12000)),
        schema=segmentation.expansion_schema(
            [row["segment_id"] for row in initial]),
        expected_ids=tuple(chunk["evidence_ids"]),
        effort=_segment_setting(cfg, "03e_effort", None))
        for chunk, evidence_payload in zip(chunks03e, evidence03e)]
    normalization_events03e = {}

    def clean03e(job, rows):
        events = segmentation.normalize_expansion_matches(rows, chunk_id=job.id)
        for event in events:
            signature = (event["chunk_id"], event["evidence_id"],
                         tuple(event["removed_segment_ids"]))
            normalization_events03e[signature] = event
        return events

    _print_stage03e_prompt_composition(_stage03e_prompt_composition(
        skill03e, catalogue03e, jobs03e, evidence03e))
    results03e = _run_persisted_segment_jobs(
        c, s03e, jobs03e, chunks03e, "matches",
        os.path.join(discovery, "03e_chunk_matches"),
        os.path.join(diagnostics, "03e"), args, "03E",
        force=_segment_force(args, "03b"), row_cleaner=clean03e,
        local_cleanup_key="deterministic_normalization")
    if results03e is None:
        return
    if normalization_events03e:
        events = list(normalization_events03e.values())
        print("\n  03E deterministic normalization:")
        print(f"    {len(events)} rows had match_strength=\"none\" with "
              "segment_ids present")
        print("    segment_ids cleared")
        print(f"    {len({event['chunk_id'] for event in events})} batch "
              "artifact(s) normalized")
        print("    0 model calls made")
    matches = [row for result in results03e for row in result["matches"]]
    _require_exact_ids(matches, {row["id"] for row in items}, "03E evidence expansion")
    segmentation.validate_match_rows(matches, [row["segment_id"] for row in initial])
    meter_03e = _model_meter(c)

    # -------------------------------------------------------- 03C novelty audit
    strongly_covered = {row["evidence_id"] for row in matches
                        if row["match_strength"] == "strong" and row["segment_ids"]}
    novelty_items = [row for row in core_items if row["id"] not in strongly_covered]
    if getattr(args, "novelty_supporting", False):
        novelty_items += [row for row in items if row["tier"] == "supporting"]
    chunks03c = segmentation.chunk_by_tokens(
        novelty_items, int(_segment_setting(cfg, "03c_chunk_tokens", 8000)), "03c")
    if chunks03c:
        segmentation.assert_exact_chunk_coverage(
            chunks03c, [row["id"] for row in novelty_items])
    s03c = (skill_named("03c_audit_segment_coverage.md") +
            "\n\nCURRENT CANDIDATE CATALOGUE:\n" +
            json.dumps(defs, ensure_ascii=False, indent=2))
    jobs03c = [Job(
        id=chunk["chunk_id"],
        prompt=("Audit every evidence item exactly once.\n\nEVIDENCE:\n" +
                segmentation.evidence_text(chunk["records"], include_tier=True)),
        max_tokens=int(_segment_setting(cfg, "03c_max_tokens", 12000)),
        schema=segmentation.NOVELTY_SCHEMA,
        expected_ids=tuple(chunk["evidence_ids"]),
        effort=_segment_setting(cfg, "03c_effort", None)) for chunk in chunks03c]
    results03c = _run_persisted_segment_jobs(
        c, s03c, jobs03c, chunks03c, "audits",
        os.path.join(discovery, "03c_chunk_results"),
        os.path.join(diagnostics, "03c"), args, "03C",
        force=_segment_force(args, "03c")) if jobs03c else []
    if results03c is None:
        return
    audits = []
    for result in results03c:
        for row in result["audits"]:
            audits.append({**row, "origin_chunk": result["chunk_id"]})
    novelty = segmentation.novelty_catalogue(audits)
    novelty_p = os.path.join(discovery, "03c_novelty_results.json")
    _json_atomic(novelty_p, {"audits": audits, "recurring_proposals": novelty})
    meter_03c = _model_meter(c)

    # One fixed novelty cycle: recurrent proposals return through 03B once.
    final03b_p = os.path.join(discovery, "03b_consolidated_candidates.json")
    final03b_meta = final03b_p + ".meta.json"
    final03b_fingerprint = _value_fingerprint({"initial": initial, "novelty": novelty})
    if (os.path.isfile(final03b_p) and not _segment_force(args, "03c")
            and _meta_matches(final03b_meta, final03b_fingerprint)):
        final = _load_json(final03b_p)
    elif novelty:
        combined = catalogue + novelty
        schema03b = segmentation.consolidate_schema(
            row["candidate_id"] for row in combined)
        job03b2 = Job(
            id="03b_novelty_consolidate",
            prompt=("Run one final consolidation cycle. Preserve existing canonical "
                    "audiences where possible and incorporate only defensible recurring "
                    "novelty proposals. Rename semantic slugs and names freely. "
                    "Select every source_candidate_ids value exactly from the "
                    "machine candidate_id values in this catalogue; code will "
                    "assign canonical segment IDs.\n\nCATALOGUE:\n" +
                    json.dumps(combined, ensure_ascii=False, indent=2)),
            max_tokens=int(_segment_setting(cfg, "03b_max_tokens", 64000)),
            schema=schema03b,
            effort=_segment_setting(cfg, "03b_effort", None))
        audit_recovery = (None if _segment_force(args, "03c") else
                          _03b_audit_recovery(
                              cfg["_dir"], job03b2.id, combined))
        if audit_recovery:
            print("  03B recovery: found a completed audited novelty response "
                  "for the current catalogue")
            print(f"  Audit source: {audit_recovery[1]}")
            print("  It will be validated/repaired without rerunning full "
                  "consolidation")
        if not confirm(c.estimate(s03b, SEGMENT_PREAMBLE, [job03b2]),
                       getattr(args, "yes", False)):
            return
        final = _run_03b_consolidation(
            c, s03b, SEGMENT_PREAMBLE, job03b2, combined,
            os.path.join(diagnostics, "03b_novelty"),
            initial_result=audit_recovery[0] if audit_recovery else None)
        _json_atomic(final03b_p, final)
        _json_atomic(final03b_meta, {"input_fingerprint": final03b_fingerprint})
    else:
        final = initial
        _json_atomic(final03b_p, final)
        _json_atomic(final03b_meta, {"input_fingerprint": final03b_fingerprint})
    meter_final = _model_meter(c)

    remapped_matches = _remap_expansion_matches(matches, initial, final)
    candidates = segmentation.assemble_candidate_evidence(final, remapped_matches, items)
    evidence_p = os.path.join(discovery, "03_candidate_evidence.json")
    discovered_p = os.path.join(discovery, "discovered_segments.json")
    _json_atomic(evidence_p, candidates)
    _json_atomic(discovered_p, candidates)
    _json_atomic(cand_p, candidates)  # compatibility contract for Studio/older tools
    raw_03a_candidates = sum(len(result["candidates"]) for result in results03a)
    summary03 = {
        "architecture": "03A harvest -> 03B consolidate -> evidence expansion -> "
                        "03C novelty audit -> optional one-time 03B consolidation",
        "evidence_tiers": counts,
        "03a_chunks": len(chunks03a),
        "03a_raw_candidates": raw_03a_candidates,
        "03a_recurring_catalogue_candidates": len(catalogue),
        "03b_initial_candidates": len(initial),
        "03e_evidence_items_classified": len(matches),
        "03c_evidence_items_audited": len(audits),
        "03c_recurring_novelty_proposals": len(novelty),
        "final_discovered_candidates": len(candidates),
        "representative_evidence_coverage": {
            "with_representatives": sum(bool(row["representative_evidence_ids"])
                                        for row in candidates),
            "total": len(candidates),
        },
        "model_usage": {
            "03a": _meter_delta(meter_start, meter_03a),
            "03b_initial": _meter_delta(meter_03a, meter_03b),
            "03e_expansion": _meter_delta(meter_03b, meter_03e),
            "03c_novelty": _meter_delta(meter_03e, meter_03c),
            "03b_novelty_cycle": _meter_delta(meter_03c, meter_final),
            "stage03_total": _meter_delta(meter_start, meter_final),
        },
        "wall_clock_seconds": round(time.monotonic() - stage03_started, 3),
        "old_monolithic_stage03": _legacy_stage03_benchmark(cfg, legacy_p),
    }
    summary03_p = os.path.join(discovery, "03_run_summary.json")
    _json_atomic(summary03_p, summary03)
    reports_dir = paths.research(cfg["_dir"], "segments", "reports")
    stagereport.write(reports_dir, "03_discovery.md", stagereport.discovery_report(
        cfg.get("name", "project"), _load_json(registers_p), catalogue,
        candidates, by_id))
    print(f"  Stage 03 report -> {os.path.join(reports_dir, '03_discovery.md')}")
    print(f"  Stage 03 complete: {len(candidates)} discovered candidates · "
          f"{len(novelty)} recurring novelty proposal(s) -> {discovered_p}")
    usage = summary03["model_usage"]["stage03_total"]["usage"]
    print(f"  Stage 03 model usage: {usage.get('in', 0):,} input · "
          f"{usage.get('out', 0):,} output · "
          f"{usage.get('reasoning', 0):,} reasoning tokens · "
          f"${summary03['model_usage']['stage03_total']['cost_usd']:.4f} -> "
          f"{summary03_p}")

    # -------------------------------------------------------------- skill 04
    val_p = os.path.join(voc, "validated_segments.json")
    val_meta = val_p + ".meta.json"
    facets_p = os.path.join(voc, "facet_vocabulary.json")
    graph_p = os.path.join(voc, "segment_graph.json")
    candidate_fingerprint = _value_fingerprint(
        {"candidates": candidates, "provisional_facets": provisional_facets})
    if (os.path.exists(val_p) and not _segment_force(args, "04")
            and _meta_matches(val_meta, candidate_fingerprint)):
        decisions = _load_json(val_p)
        facets = _load_json(facets_p) if os.path.exists(facets_p) else []
        graph = (_load_json(graph_p) if os.path.exists(graph_p)
                 else {"specialises": [], "adjacent": []})
        print("  04 validate: reusing decisions")
    else:
        _, s04 = skill(4)
        packet = segmentation.stage04_packet(candidates, items)
        min_threads, min_evidence = corpus_policy.segment_floors(cfg)
        floor_rule = (
            f"FLOORS — a candidate resting on fewer than {min_threads} unique "
            f"threads, or carrying fewer than {min_evidence} unique evidence "
            "items, cannot be validated. Those counts are on every card and are "
            "enforced in code after you decide; validating below them only "
            "moves the decision, it does not make the segment.\n\n")
        scope_rule = corpus_policy.scope_prompt(cfg)
        child_rules = corpus_policy.child_floors(cfg)
        edge_rule = (
            "RELATIONSHIPS — declare how the validated segments relate in "
            "`segment_edges`. Use `specialises` (from child to parent) only "
            "when every member of the child is also a member of the parent, and "
            "`adjacent` when two audiences overlap or are discussed together "
            "with neither containing the other. Not every segment needs an "
            "edge, and an empty array is correct when none genuinely relate.\n"
            f"A child is held to a lower volume floor than a root "
            f"({child_rules['min_threads']} threads / "
            f"{child_rules['min_evidence']} items rather than "
            f"{min_threads}/{min_evidence}) and must contribute language its "
            "parent does not already use. So declaring a parent is how a "
            "narrow but genuine audience survives — and declaring one for a "
            "segment that merely relabels its parent is how it gets folded "
            "back in.\n\n")
        register_rule = (
            "REGISTERS — a candidate that is a response to the problem or the "
            "market (treatment preference, price stance, scepticism) or a point "
            "in the treatment journey is not an audience. Give it "
            "`reclassified_as_facet` with a `facet_key` and `facet_type` rather "
            "than `rejected`: it keeps its language and becomes a tag every "
            "comment can carry. Nothing has been assigned yet, so this costs "
            "nothing now and cannot be done losslessly later.\n\n")
        facet_packet = (
            "PROVISIONAL FACET VOCABULARY — discovered at 03A and not yet "
            "consolidated. Merge near-duplicates into one closed set in "
            "`facet_vocabulary`, citing the provisional_facet_id values each "
            "entry absorbs, and include every facet_key you reclassify a "
            "candidate into.\n\n"
            + json.dumps(provisional_facets, ensure_ascii=False, indent=2)
            + "\n\n") if provisional_facets else (
            "PROVISIONAL FACET VOCABULARY — 03A discovered none. Build "
            "`facet_vocabulary` from the candidates you reclassify, or return "
            "it empty.\n\n")
        validation_job = Job(
            id="04_validate",
            prompt=("Apply skill 04 to every compact candidate card below. The "
                    "metrics were computed in code and the evidence excerpts are a "
                    "bounded representative sample, not the whole corpus. Give every "
                    "candidate exactly one decision.\n\n"
                    + floor_rule
                    + register_rule
                    + edge_rule
                    + ((scope_rule + "\nA candidate whose defining trait is a "
                        "diagnosis is out of scope: reject it, however coherent "
                        "its evidence.\n\n") if scope_rule else "")
                    + facet_packet
                    + packet),
            max_tokens=STAGE04_TOKEN_TIERS[0],
            schema=validation_schema(
                [row["segment_id"] for row in candidates],
                [row["provisional_facet_id"] for row in provisional_facets]),
            effort=_segment_setting(cfg, "04_effort", None))
        print(f"  Stage 04 starting ceiling: "
              f"{_token_tier_label(STAGE04_TOKEN_TIERS[0])}")
        print("  Stage 04 retry ladder: " + " → ".join(
            _token_tier_label(tier) for tier in STAGE04_TOKEN_TIERS))
        if not confirm(c.estimate(s04, SEGMENT_PREAMBLE, [validation_job]),
                       getattr(args, "yes", False)):
            return
        validation = _run_single_structured(
            c, s04, SEGMENT_PREAMBLE, validation_job,
            os.path.join(diagnostics, "04_validate"),
            token_tiers=STAGE04_TOKEN_TIERS,
            attempt_reporter=_report_stage04_attempt)
        decisions = validation["decisions"]
        _validate_decision_coverage(decisions, candidates)
        facets = _finalize_stage04_facets(
            validation.get("facet_vocabulary"), provisional_facets,
            decisions, candidates)
        graph = _finalize_stage04_graph(validation.get("segment_edges"), decisions)
        _json_atomic(val_p, decisions)
        _json_atomic(facets_p, facets)
        _json_atomic(graph_p, graph)
        _json_atomic(val_meta, {"input_fingerprint": candidate_fingerprint})
        tally = Counter(d["status"] for d in decisions)
        print("  04 validate: " + " · ".join(
            f"{key} {value}" for key, value in tally.most_common()) + f" -> {val_p}")
        if facets:
            types = Counter(row["facet_type"] for row in facets)
            print(f"  04 facets: {len(facets)} in the closed vocabulary ("
                  + " · ".join(f"{key} {value}"
                               for key, value in sorted(types.items()))
                  + f") -> {facets_p}")

    by_segment_id = {candidate["segment_id"]: candidate
                     for candidate in candidates}
    validated = [by_segment_id[row["segment_id"]] for row in decisions
                 if (row["status"] == "validated"
                     and row["segment_id"] in by_segment_id)]
    validated = _apply_segment_floors(validated, decisions, cfg, graph=graph)
    if not validated:
        sys.exit("  No segment survived validation. Nothing to build.\n"
                 f"  Review {val_p} — every candidate has a written rationale.")
    min_threads, min_evidence = corpus_policy.segment_floors(cfg)
    child_rules = corpus_policy.child_floors(cfg)
    stagereport.write(
        paths.research(cfg["_dir"], "segments", "reports"), "04_validation.md",
        stagereport.validation_report(
            cfg.get("name", "project"), decisions, validated, facets, graph,
            (min_threads, min_evidence,
             child_rules["min_threads"], child_rules["min_evidence"])))
    print(f"  {len(validated)} validated segment(s): " +
          ", ".join(row["slug"] for row in validated))

    # -------------------------------------------------------------- skill 05
    asg_p = os.path.join(voc, "segment_assignments.jsonl")
    asg_meta = asg_p + ".meta.json"
    assignment_fingerprint = _value_fingerprint({
        "validated": validated,
        "evidence": [{"id": row["id"], "text": row["text"]} for row in items],
    })
    force_assign = (getattr(args, "reassign", False)
                    or _segment_force(args, "05"))
    redo_assign = (force_assign
                   or not _meta_matches(asg_meta, assignment_fingerprint))
    if os.path.exists(asg_p) and not redo_assign:
        rows = _normalize_assignment_contract(_read_jsonl(asg_p))
        print(f"  05 assign: reusing {len(rows):,} assignments (--reassign to redo)")
        if not any(row.get("runner_up_recorded") for row in rows):
            print("  ! these assignments predate runner-up and facet recording; "
                  "--reassign to collect them")
    else:
        _, s05 = skill(5)
        seg_defs = json.dumps(
            [{key: row[key] for key in ("segment_id", "slug", "name", "definition",
                                        "inclusion_criteria", "exclusion_criteria")}
             for row in validated], ensure_ascii=False, indent=2)
        facet_defs = json.dumps(
            [{key: row[key] for key in ("facet_id", "facet_key", "name",
                                        "definition", "facet_type")}
             for row in facets], ensure_ascii=False, indent=2)
        prefix = (f"{s05}\n\n---\n\nTHE VALIDATED SEGMENTS:\n\n{seg_defs}\n"
                  f"\n---\n\nTHE FACET VOCABULARY — the closed set `facet_ids` "
                  f"draws from. A facet tags a comment inside its segment; it is "
                  f"never a second membership and carries no counts.\n\n"
                  f"{facet_defs}\n")
        chunks05 = segmentation.chunk_by_tokens(
            items, int(_segment_setting(cfg, "05_chunk_tokens", 8000)), "05")
        jobs05 = [Job(
            id=chunk["chunk_id"],
            prompt=("Assign each evidence item by cue type. Assign only when the "
                    "winning score is >= 6 and the margin is >= 2; otherwise use the "
                    "correct unassigned status. Record the segment that scored "
                    "second in runner_up_segment_id, and tag facets from the "
                    "supplied vocabulary only. Tier is evidence strength, not "
                    "membership. Return exactly one row per ID.\n\nEVIDENCE:\n" +
                    _stage05_evidence_text(chunk["records"])),
            max_tokens=STAGE05_TOKEN_TIERS[0], schema=assignment_schema(
                chunk["evidence_ids"],
                [row["segment_id"] for row in validated],
                [row["facet_id"] for row in facets]),
            expected_ids=tuple(chunk["evidence_ids"]),
            effort=_segment_setting(cfg, "05_effort", None)) for chunk in chunks05]
        print(f"  05 assign: {len(items):,} items in {len(jobs05)} batched chunks")
        artifact_dir = paths.research(
            cfg["_dir"], "segments", "assignments", "05_chunk_assignments")
        artifacts05 = _run_stage05_assignments(
            c, prefix, jobs05, chunks05, artifact_dir,
            os.path.join(diagnostics, "05_assign"), args, cfg["_dir"],
            force=force_assign)
        if artifacts05 is None:
            return
        rows = _normalize_assignment_contract(
            [row for artifact in artifacts05 for row in artifact["assignments"]])
        _require_exact_ids(rows, {row["id"] for row in items}, "skill 05 assignment")
        # Enforced before the rows are persisted, so the saved contract and the
        # reused-from-cache path can never disagree about what was assigned.
        _enforce_assignment_thresholds(
            rows, os.path.join(diagnostics, "05_assign"))
        _write_jsonl_atomic(asg_p, rows)
        _json_atomic(asg_meta, {"input_fingerprint": assignment_fingerprint})
        tally = Counter(row["assignment_status"] for row in rows)
        print("  05 assign: " + " · ".join(
            f"{key} {value}" for key, value in tally.most_common()))

    validated = _apply_assigned_floors(validated, decisions, rows, by_id, cfg,
                                       graph=graph)
    if not validated:
        sys.exit("  No segment cleared the evidence floor after assignment.\n"
                 f"  Review {asg_p} — every item kept its score and rationale.")
    measured = _measure_segment_graph(rows, validated, graph, voc)
    reports_dir = paths.research(cfg["_dir"], "segments", "reports")
    stagereport.write(reports_dir, "05_assignment.md", stagereport.assignment_report(
        cfg.get("name", "project"), rows, validated,
        ASSIGN_MIN_SCORE, ASSIGN_MIN_MARGIN))
    build_evidence_files(cfg, validated, rows, by_id, voc, facets,
                         reports_dir=reports_dir, total_evidence=len(items))
    build_research_packs(cfg, validated, rows, by_id, graph, measured, voc)
    _run_commercial_layers(
        c, cfg, args, items, by_id, validated, decisions, rows)


def _measure_segment_graph(rows, validated, graph, voc):
    """Stage 06B — derive co-occurrence edges and audit them against Stage 04.

    Measurement, not declaration: these edges come from what Stage 05 actually
    did, so nothing here asks a model whether two audiences are related. The
    audit is the useful part. Where a declared adjacency has no measured support,
    or a strong measured overlap was never declared, an operator wants to know —
    and neither is an error, so neither changes the declared graph.
    """
    known = {row["segment_id"]: row["slug"] for row in validated}
    pairs, sizes, measured_from = segmentation.measure_cooccurrence(rows, known)
    edges = segmentation.measured_edges(pairs, sizes)
    audit = segmentation.audit_graph(graph, edges)
    path = os.path.join(voc, "segment_cooccurrence.json")
    _json_atomic(path, {
        "measured_from_assignments": measured_from,
        "note": ("First-and-second counts from Stage 05. `rate` is against the "
                 "smaller segment, because that is the one an overlap would "
                 "change the message for."),
        "raw_pairs": [{"segment_ids": list(pair),
                       "slugs": [known[pair[0]], known[pair[1]]],
                       "count": count}
                      for pair, count in sorted(pairs.items(),
                                                key=lambda kv: (-kv[1], kv[0]))],
        "co_occurs_edges": [
            {**edge, "slugs": [known[edge["segment_ids"][0]],
                               known[edge["segment_ids"][1]]]}
            for edge in edges],
        "graph_audit": {
            key: [[known.get(left, left), known.get(right, right)]
                  for left, right in value]
            for key, value in audit.items()},
    })
    if measured_from:
        print(f"  06B graph: {len(edges)} co-occurrence edge(s) from "
              f"{measured_from:,} runner-up(s) -> {path}")
        if audit["measured_not_declared"]:
            print(f"    {len(audit['measured_not_declared'])} measured overlap(s) "
                  "Stage 04 did not declare: " + ", ".join(
                      f"{known.get(a, a)}↔{known.get(b, b)}"
                      for a, b in audit["measured_not_declared"][:3]))
        if audit["declared_not_measured"]:
            print(f"    {len(audit['declared_not_measured'])} declared "
                  "adjacency(ies) the evidence does not support")
    return edges


def build_research_packs(cfg, validated, rows, by_id, graph, measured, voc):
    """Stage 06C — Layer 2, one weighted research pack per segment."""
    settings = researchpack.pack_settings(cfg)
    rows_by_segment = defaultdict(list)
    for row in rows:
        if row.get("assignment_status") == "assigned":
            rows_by_segment[row["primary_segment_id"]].append(row)
    directory = paths.research(cfg["_dir"], "segments", "packs")
    manifest = researchpack.write_packs(
        directory, validated, rows_by_segment, by_id, graph, measured, settings)
    _json_atomic(os.path.join(voc, "research_pack_manifest.json"), {
        "context_budget_tokens": settings["context_budget_tokens"],
        "relation_weights": settings["relation_weights"],
        "strength_factors": settings["strength_factors"],
        "packs": manifest,
    })
    borrowed = sum(row["borrowed_item_count"] for row in manifest)
    print(f"  06C packs: {len(manifest)} research pack(s), {borrowed:,} borrowed "
          f"context item(s) -> {directory}/")
    return manifest


def build_evidence_files(cfg, validated, rows, by_id, voc, facets=(),
                        reports_dir=None, total_evidence=None):
    """Skill 06 — pure assembly, deterministic, plus the audit set.

    The skill file is loaded and written beside the output as the build contract:
    06 is the one stage whose rules are wholly mechanical (join, group, sort, emit,
    audit), so it is implemented in code rather than sent to the model — but the
    spec it implements travels with the artefacts it produced.

    One evidence item -> exactly one primary segment, or unassigned. Never two
    files. An item carrying more than one active assignment fails the build.
    """
    ev = paths.evidence(cfg["_dir"]); os.makedirs(ev, exist_ok=True)
    facets_by_id = {row["facet_id"]: row for row in facets or ()}
    _, s06 = skill(6)
    with open(os.path.join(voc, "06_build_contract.md"), "w",
              encoding="utf-8") as fh:
        fh.write(s06)
    seen, conflicts, missing = {}, [], []
    grouped = defaultdict(list)
    unassigned = []
    valid_segment_ids = {s["segment_id"] for s in validated}

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
        if r["primary_segment_id"] not in valid_segment_ids:
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
        rs = grouped.get(s["segment_id"], [])
        if not rs:
            print(f"  ! {s['slug']}: zero assigned evidence (validated but empty)")
            report.append((s["segment_id"], s["slug"], 0, 0,
                           {tier: 0 for tier in segmentation.EVIDENCE_TIERS}))
            continue
        # Deterministic: score desc, then margin desc, then evidence id asc.
        rs.sort(key=lambda r: (-r["score"], -r["winning_margin"], r["evidence_id"]))
        threads = {
            (by_id[r["evidence_id"]].get("thread_id")
             or by_id[r["evidence_id"]].get("url"))
            for r in rs
            if (by_id[r["evidence_id"]].get("thread_id")
                or by_id[r["evidence_id"]].get("url"))
        }
        tiers = Counter(
            by_id[r["evidence_id"]].get("tier", "context") for r in rs)
        facet_tally = Counter(facet_id for r in rs
                              for facet_id in r.get("facet_ids") or [])
        p = os.path.join(ev, f"{s['slug']}.txt")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"{s['name'].upper()}\n{'=' * 72}\n\n")
            fh.write(f"Segment ID: {s['segment_id']}\nSegment slug: {s['slug']}\n")
            fh.write("Validation status: validated\n")
            fh.write(f"Evidence items: {len(rs)}\nUnique threads: {len(threads)}\n\n")
            fh.write(f"SEGMENT DEFINITION\n{'-' * 72}\n{s['definition']}\n\n")
            fh.write("INCLUSION\n" + "".join(f"  - {x}\n" for x in s["inclusion_criteria"]))
            fh.write("EXCLUSION\n" + "".join(f"  - {x}\n" for x in s["exclusion_criteria"]))
            fh.write("\nEach item appears in this segment only once.\n\n")
            if s.get("absorbed_children"):
                # A child that could not stand on its own is still a named
                # sub-context inside its parent. Extractors get to see it,
                # which is the whole reason folding beats deleting.
                fh.write(f"ABSORBED SUB-CONTEXTS\n{'-' * 72}\n")
                for child in s["absorbed_children"]:
                    fh.write(f"  - {child['name']}: {child['evidence_count']} "
                             f"item(s) — {child['reason']}\n")
                fh.write("\n")
            if facet_tally:
                # Facets describe the people already in this segment. The counts
                # are shares of this file, never audience sizes, and they are
                # printed here so an extractor can see the split without leaving
                # the evidence file it is required to read.
                fh.write(f"FACETS PRESENT\n{'-' * 72}\n")
                for facet_id, count in sorted(
                        facet_tally.items(), key=lambda kv: (-kv[1], kv[0])):
                    facet = facets_by_id.get(facet_id, {})
                    label = facet.get("name") or facet_id
                    kind = facet.get("facet_type") or "attribute"
                    fh.write(f"  - {label} [{kind}]: {count} of {len(rs)} items\n")
                fh.write("\n")
            fh.write(f"EVIDENCE ITEMS\n{'-' * 72}\n")
            for r in rs:
                it = by_id[r["evidence_id"]]
                fh.write(f"[{r['evidence_id']}] TYPE: comment\n")
                fh.write(f"TITLE: {it.get('title') or ''}\n"
                         f"URL: {it.get('url') or ''}\n")
                fh.write(f"EVIDENCE TIER: {it.get('tier', 'context')}\n")
                fh.write(f"ASSIGNMENT SCORE: {r['score']}\n")
                fh.write(f"WINNING MARGIN: {r['winning_margin']}\n")
                fh.write(f"PRIMARY CUES: {', '.join(r['primary_cues'])}\n")
                fh.write(f"CUE TYPES: {', '.join(r['cue_types'])}\n")
                if r.get("facet_ids"):
                    fh.write("FACETS: " + ", ".join(
                        facets_by_id.get(facet_id, {}).get("name") or facet_id
                        for facet_id in r["facet_ids"]) + "\n")
                fh.write(f"RATIONALE: {r['rationale']}\n")
                fh.write(f"TEXT: {it['text']}\n\n")
        print(f"  {s['slug']:38} {len(rs):>6,} items -> {p}")
        record_provenance(cfg, s["slug"], "pipeline",
                          f"skills 01-06, {len(rs)} assigned items")
        report.append((s["segment_id"], s["slug"], len(rs), len(threads), {
            tier: tiers[tier] for tier in segmentation.EVIDENCE_TIERS}))

    # ------------------------------------------------------------- audit set
    with open(os.path.join(voc, "unassigned_evidence.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Unassigned evidence — {len(unassigned)} item(s)\n\n")
        fh.write("Unassigned is a valid outcome. Forcing an ambiguous comment into a "
                 "segment is worse than leaving it out.\n\n")
        for r in unassigned:
            it = by_id.get(r["evidence_id"], {})
            fh.write(f"## [{r['evidence_id']}] {r['assignment_status']}\n")
            fh.write(f"- Rationale: {r['rationale']}\n"
                     f"- URL: {it.get('url') or ''}\n\n")
            fh.write(f"{it.get('text', '')}\n\n---\n\n")

    if missing:
        with open(os.path.join(voc, "missing_evidence.jsonl"), "w", encoding="utf-8") as fh:
            for x in missing:
                fh.write(json.dumps(x) + "\n")

    with open(os.path.join(voc, "segment_evidence_manifest.yaml"), "w",
              encoding="utf-8") as fh:
        fh.write("segments:\n")
        for segment_id, slug, n, t, tier_counts in report:
            fh.write(f"  - segment_id: {segment_id}\n    slug: {slug}\n"
                     f"    evidence_file: evidence/{slug}.txt\n")
            fh.write(f"    evidence_count: {n}\n    thread_count: {t}\n")
            fh.write(f"    core_count: {tier_counts['core']}\n")
            fh.write(f"    supporting_count: {tier_counts['supporting']}\n")
            fh.write(f"    context_count: {tier_counts['context']}\n")
        fh.write(f"unassigned_count: {len(unassigned)}\n")
        fh.write(f"missing_count: {len(missing)}\n")

    if reports_dir:
        stagereport.write(reports_dir, "06_build.md", stagereport.build_report(
            cfg.get("name", "project"), report, len(unassigned), len(missing),
            total_evidence if total_evidence is not None
            else sum(n for _, _, n, _, _ in report) + len(unassigned)))
    total = sum(n for _, _, n, _, _ in report)
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


def research_pack_path(cfg, segment, required=True):
    directory = paths.research(cfg["_dir"], "segments", "packs")
    path = os.path.join(directory, f"{segment}.txt")
    if os.path.exists(path):
        return path
    if not required:
        return None
    available = ([name[:-4] for name in sorted(os.listdir(directory))
                  if name.endswith(".txt")] if os.path.isdir(directory) else [])
    sys.exit(f"No research pack for {segment!r}. Run `segment` to build "
             f"Layer 2, or pass --evidence to read Layer 1 instead.\n"
             f"  Available packs: {', '.join(available) or 'none'}")


def extraction_source(cfg, segment, args):
    """Which layer skills 07-26 read, and why.

    Layer 2 is the default because it is what the extractors were starved for:
    a narrow segment's own evidence is often too thin to characterise, while the
    context that would characterise it sits one edge away. Layer 1 stays one
    flag away, and a project that predates packs falls back to it silently
    rather than failing.
    """
    if getattr(args, "evidence", False):
        return evidence_path(cfg, segment), "evidence"
    pack = research_pack_path(cfg, segment, required=False)
    if pack:
        return pack, "pack"
    return evidence_path(cfg, segment), "evidence"


# Appended to every extraction prompt reading Layer 2. The extractors were
# written against a file where every item belonged to the segment; a pack breaks
# that assumption, so the assumption has to be replaced explicitly rather than
# left for each of twenty skills to rediscover.
PACK_READING_RULE = (
    "\n\nTHIS IS A LAYER-2 RESEARCH PACK. It contains this segment's PRIMARY "
    "EVIDENCE followed by a BORROWED CONTEXT section drawn from related "
    "segments, each borrowed item labelled with its origin.\n"
    "- Every count you report is a count of PRIMARY evidence only.\n"
    "- Borrowed context tells you how these people talk, and may inform "
    "labels, wording and interpretation.\n"
    "- Where a finding rests on borrowed context, say so and give its primary "
    "count separately — never add the two together.\n"
    "- A finding that appears ONLY in borrowed context is not this segment's "
    "finding. Report it as context, clearly marked, or leave it out."
)


def cmd_extract(cfg, args):
    """Skills 07-26 against one segment file. The corpus is identical across all
    20, so it goes in a cached system block and only the skill varies — then the
    whole set fans out as one batch at 50%."""
    from llm import Job, confirm
    warn_if_imported(cfg, args.segment)
    c = client(cfg, args)
    source, layer = extraction_source(cfg, args.segment, args)
    with open(source, encoding="utf-8") as fh:
        corpus = fh.read()
    label = ("Layer 2 (research pack)" if layer == "pack"
             else "Layer 1 (evidence file)")
    print(f"  reading {label}: {source}")
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
            f"{body}\n\n---\n\nApply this skill to the segment file. Output only the "
            f"skill's specified artefact in Markdown — no preamble, no meta-commentary."
            + (PACK_READING_RULE if layer == "pack" else "")
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
    from llm import Job, confirm
    c = client(cfg, args)
    corpus = corpus if corpus is not None else open(
        evidence_path(cfg, args.segment), encoding="utf-8").read()
    if os.path.exists(dest) and not args.force:
        print(f"  {os.path.basename(dest)} exists (--force to redo)")
        return open(dest, encoding="utf-8").read()
    job = Job(id=stage, prompt=prompt, max_tokens=max_tokens, schema=schema)
    if not confirm(c.estimate(corpus, PREAMBLE, [job]),
                   args.yes):
        sys.exit(1)
    if schema:
        decoded = _run_single_structured(
            c, corpus, PREAMBLE, job,
            os.path.join(os.path.dirname(dest), "_model_failures", stage))
        text = json.dumps(decoded, indent=2)
    else:
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
    s.add_argument("--stage01-debug", action="store_true",
                   help="show Stage 01 scheduler ordering, stop reasons, token "
                        "splits, and diagnostic paths")
    s.add_argument("--dedup-effort",
                   choices=["none", "low", "medium", "high", "xhigh", "max"],
                   help="reasoning effort for Stage 02 only (default: provider setting)")
    s.add_argument("--stage02-debug", action="store_true",
                   help="show Stage 02 scheduler ordering, stop reasons, token "
                        "splits, and diagnostic paths")
    s.set_defaults(fn=cmd_ingest)
    s = common(sub.add_parser(
        "refine-voc",
        help="regenerate production/audit VOC locally from completed deduplication"))
    s.add_argument("--source", help="VOC JSONL to refine (default: "
                                     "voc/deduplicated_voc.jsonl)")
    s.set_defaults(fn=cmd_refine_voc)
    s = common(sub.add_parser("segment")); s.add_argument("--rediscover", action="store_true")
    s.add_argument("--reassign", action="store_true")
    s.add_argument("--from", dest="from_stage", choices=SEGMENT_STEP_ORDER,
                   help="rerun this segmentation substep and every downstream step; "
                        "earlier completed artifacts are reused")
    s.add_argument("--segment-debug", action="store_true",
                   help="show adaptive chunk scheduler and token diagnostics")
    s.add_argument("--novelty-supporting", action="store_true",
                   help="also audit Supporting evidence for novelty (Core-only by default)")
    s.add_argument("--source", help="VOC file to segment (default: voc/production_voc.jsonl)")
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
            s.add_argument("--evidence", action="store_true",
                           help="read Layer 1 (the segment evidence file) instead "
                                "of the default Layer-2 research pack")
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
