#!/usr/bin/env python3
"""
Turn a segment's extraction Markdown into selectable levers.

`adpipe extract` writes one Markdown file per dimension (skills 07-26) into
projects/<project>/extractions/<segment>/. Those files are written by a model
following a skill spec, so they are prose-shaped: a list of named items, each
with a statement, quotes, a frequency and an observed/inferred basis.

The Remix tab needs those same items as discrete things you can pick from a
dropdown — "pain point: can't sleep on the injured side" — so this module reads
the Markdown back into structured levers.

It parses tolerantly on purpose. The skills pin a canonical item block, but the
files are model-written and drift: headings come numbered or not, fields arrive
as `- **Statement:** x`, `- **Statement** — x`, or as a bold line followed by a
paragraph. Anything that looks like an item block is accepted; anything that
looks like front matter (metadata, summaries, decision logs, checklists) is
skipped. A file that yields nothing yields an empty list rather than a guess —
a wrong lever list is worse than an absent one, so callers can say "0 parsed"
and show the raw file.

    python3 pipeline/levers.py montisella side_sleepers_night_pain
    python3 pipeline/levers.py montisella side_sleepers_night_pain --json

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The dimensions offered as levers, in the order they appear in the UI.
#
# `required` marks the three that decide what the ad is actually about — who it
# is for, what hurts, what they want instead. Everything else sharpens a brief
# but no brief is incoherent without it, so the rest stay optional. Segment is
# not here: it is the folder these files live in, chosen before any of them.
#
# `multi` marks dimensions where picking several at once is normal — an ad can
# answer two objections, or borrow four bits of the segment's vocabulary — as
# opposed to the spine levers, where a second pick means a second ad.
DIMENSIONS = [
    # skill, key,                    label,                  required, multi
    (7,  "pain_point",           "Pain point",              True,  False),
    (9,  "desired_outcome",      "Desired outcome",         True,  False),
    (8,  "pain_moment",          "Pain moment",             False, False),
    (10, "emotional_state",      "Emotional state",         False, False),
    (11, "psychological_driver", "Psychological driver",    False, False),
    (12, "belief",               "Belief",                  False, True),
    (13, "limiting_belief",      "Limiting belief",         False, True),
    (14, "failed_solution",      "Failed solution",         False, True),
    (15, "assumed_solution",     "Assumed solution",        False, True),
    (16, "buying_trigger",       "Buying trigger",          False, False),
    (17, "buying_criterion",     "Buying criterion",        False, True),
    (18, "objection",            "Objection",               False, True),
    (19, "mechanism",            "Mechanism",               False, False),
    (20, "desired_proof",        "Desired proof",           False, True),
    (21, "product_mention",      "Product mention",         False, True),
    (22, "competitor_mention",   "Competitor mention",      False, True),
    (23, "offer",                "Offer",                   False, False),
    (24, "representative_voc",   "Representative quote",    False, True),
    (25, "terminology",          "Terminology",             False, True),
    (26, "slang",                "Slang",                   False, True),
]

REQUIRED_KEYS = [k for _, k, _, r, _ in DIMENSIONS if r]

# Headings that are structure, not items. Matched as a substring of the lowered
# heading text, so "## Extraction Metadata" and "## Metadata" both go.
SKIP_HEADINGS = (
    "metadata", "summary", "contents", "overview", "how to read", "method",
    "methodology", "decision", "audit", "checklist", "validation", "exclusion",
    "quick reference", "appendix", "notes", "caveat", "coverage", "glossary",
    "ranking", "scoring", "confidence key", "legend", "definition of",
)

# Field labels that carry the one-line meaning of an item, best first.
STATEMENT_LABELS = (
    "canonical statement", "statement", "plain meaning", "meaning",
    "definition", "description", "what it is", "canonical form",
)

# Labels worth keeping for the brief prompt beyond the statement.
KEEP_LABELS = {
    "basis", "frequency", "confidence", "priority score", "score", "horizon",
    "type", "register", "variants", "head term", "related pain point",
    "why it matters", "boundary note", "aliases", "segment specificity",
}

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_NUMBERING = re.compile(r"^(?:\d+[.)]|[-*])\s+")
_BULLET_FIELD = re.compile(
    r"^\s*[-*]\s+\*\*(?P<label>[^*]+?)\*\*\s*(?::|[—–-])?\s*(?P<value>.*)$")
_PARA_FIELD = re.compile(r"^\s*\*\*(?P<label>[^*]+?)\*\*\s*(?::|[—–-])?\s*(?P<value>.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_EV_ID = re.compile(r"\b(ev_[0-9a-zA-Z_]+)\b")
_TRAILING_ID = re.compile(r"\s*[—–-]\s*`?ev_[0-9a-zA-Z_]+`?\s*$")
# A blockquote line carrying only an attribution, not quoted speech.
_ATTRIBUTION = re.compile(r"^(?:[—–-]\s*)?(?:ev_[0-9a-zA-Z_]+|[—–-]+)$", re.I)


class LeverError(Exception):
    """Raised for bad project/segment paths. Callers decide how to surface it."""


def _clean(text: str) -> str:
    """Strip Markdown emphasis, code ticks and numbering from a heading or value.

    Underscores are only removed when they are paired around a word — stripping
    every `_` would turn `ev_000123` into `ev000123` and break the evidence IDs
    the quotes are traceable by.
    """
    text = text.strip()
    text = _NUMBERING.sub("", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"[*`]", "", text)
    return text.strip(" .:—–-\t")


def _is_skippable(title: str) -> bool:
    low = title.lower()
    return any(s in low for s in SKIP_HEADINGS)


def _blocks(lines):
    """Yield (level, title, body_lines, ancestors) for every heading.

    `ancestors` is the list of enclosing heading titles, so a subsection of a
    skipped section can be skipped too — skill 07's decision log puts `###
    Merges` at the same depth as its pain points, and only its parent says it
    is not an item.
    """
    cur, stack = None, []
    for raw in lines:
        m = _HEADING.match(raw)
        if m:
            if cur:
                yield cur
            level, title = len(m.group(1)), _clean(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            cur = (level, title, [], [t for _, t in stack])
            stack.append((level, title))
        elif cur:
            cur[2].append(raw)
    if cur:
        yield cur


def _buried(block) -> bool:
    """True if this heading, or anything it sits under, is structure not items."""
    _, title, _, ancestors = block
    return _is_skippable(title) or any(_is_skippable(a) for a in ancestors)


def _item_level(blocks):
    """Decide which heading level holds the items.

    Files nest items under a section ("## Pain Points" → "### 1. …"), but a
    shorter file may put them straight at `##`. Pick the deepest level that has
    at least two candidate headings; two is what distinguishes a list of items
    from a single section that happens to be nested.
    """
    counts = {}
    for b in blocks:
        if not _buried(b):
            counts[b[0]] = counts.get(b[0], 0) + 1
    for level in sorted(counts, reverse=True):
        if counts[level] >= 2:
            return level
    return max(counts) if counts else None


def _parse_fields(body):
    """Pull labelled fields and quotes out of one item's body.

    Handles bullet fields (`- **Statement:** x`), paragraph fields (a bold line
    then the value on following lines) and blockquotes.
    """
    fields, quotes, loose = {}, [], []
    pending = None            # label awaiting a value on later lines
    for raw in body:
        line = raw.rstrip()
        q = _QUOTE.match(line)
        if q:
            text = _TRAILING_ID.sub("", _clean(q.group(1))).strip().strip('"“”')
            # An attribution on its own line ("> — `ev_000123`") is not a quote.
            if text and not _ATTRIBUTION.match(text):
                quotes.append(text)
            continue
        if not line.strip():
            continue
        m = _BULLET_FIELD.match(line) or _PARA_FIELD.match(line)
        if m:
            label = _clean(m.group("label")).lower()
            value = _clean(m.group("value"))
            if value:
                fields.setdefault(label, value)
                pending = None
            else:
                pending = label       # value is on the following line(s)
            continue
        if pending:
            fields.setdefault(pending, _clean(line))
            pending = None
        elif not line.lstrip().startswith(("|", "```", "---")):
            loose.append(_clean(line))
    return fields, quotes, loose


def _statement(fields, loose):
    for label in STATEMENT_LABELS:
        if label in fields:
            return fields[label]
    return loose[0] if loose else ""


def parse_markdown(text: str) -> list:
    """Parse one extraction file into items. Never raises on odd input."""
    blocks = [b for b in _blocks(text.splitlines())]
    if not blocks:
        return []
    level = _item_level(blocks)
    if level is None:
        return []

    items = []
    for block in blocks:
        lvl, title, body, _ = block
        if lvl != level or _buried(block) or not title:
            continue
        fields, quotes, loose = _parse_fields(body)
        name = title
        # Skill 25/26 name the item in a "Head term" field when the heading is
        # the term itself; prefer the heading, it is what a person recognises.
        statement = _statement(fields, loose)
        ev = list(dict.fromkeys(_EV_ID.findall("\n".join(body))))
        items.append({
            "name": name,
            "statement": statement,
            "quotes": quotes[:3],
            "evidence_ids": ev[:12],
            "basis": fields.get("basis", ""),
            "frequency": fields.get("frequency", ""),
            "confidence": fields.get("confidence", ""),
            "detail": {k: v for k, v in fields.items() if k in KEEP_LABELS},
        })
    return items


def extraction_dir(project: str, segment: str) -> str:
    if not project or not segment:
        raise LeverError("project and segment are both required")
    for part in (project, segment):
        if os.path.sep in part or part in ("..", ".") or part.startswith("."):
            raise LeverError(f"bad path component: {part!r}")
    import paths
    return paths.extractions(project, segment)


def _file_for(directory: str, skill_n: int):
    if not os.path.isdir(directory):
        return None
    for f in store.names_in(directory):
        if f.startswith(f"{skill_n:02d}_") and f.endswith(".md"):
            return os.path.join(directory, f)
    return None


def load(project: str, segment: str) -> dict:
    """Read every available dimension for one segment.

    Returns a dict with a `dimensions` list — one entry per lever, present or
    not, so the UI can show what is missing and point at the stage that fills
    it, rather than silently offering a shorter menu.
    """
    directory = extraction_dir(project, segment)
    out, found = [], 0
    for skill_n, key, label, required, multi in DIMENSIONS:
        path = _file_for(directory, skill_n)
        entry = {"key": key, "label": label, "skill": skill_n,
                 "required": required, "multi": multi, "items": [],
                 "present": bool(path), "file": None}
        if path:
            found += 1
            entry["file"] = os.path.relpath(path, ROOT)
            try:
                text = store.read_text(path) or ""
            except OSError as e:
                entry["error"] = str(e)
            else:
                entry["items"] = parse_markdown(text)
        out.append(entry)
    return {
        "project": project,
        "segment": segment,
        "extracted": found,
        "total": len(DIMENSIONS),
        "dimensions": out,
    }


def selection_text(data: dict, chosen: dict) -> str:
    """Render the picked levers as the block that goes into a brief prompt.

    `chosen` maps a dimension key to a list of item names. Names are matched
    against the parsed items so a stale or hand-edited selection cannot smuggle
    text that is not in the extractions into the prompt.
    """
    by_key = {d["key"]: d for d in data["dimensions"]}
    parts = []
    for _, key, label, _, _ in DIMENSIONS:
        names = chosen.get(key) or []
        if isinstance(names, str):
            names = [names]
        dim = by_key.get(key)
        if not dim or not names:
            continue
        picked = [i for i in dim["items"] if i["name"] in set(names)]
        if not picked:
            continue
        lines = [f"### {label}"]
        for item in picked:
            lines.append(f"- **{item['name']}**"
                         + (f" — {item['statement']}" if item["statement"] else ""))
            for q in item["quotes"][:2]:
                lines.append(f"  > “{q}”")
            if item["basis"]:
                lines.append(f"  ({item['basis']})")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def missing_required(chosen: dict) -> list:
    """Which mandatory levers have no selection yet."""
    labels = {k: l for _, k, l, _, _ in DIMENSIONS}
    return [labels[k] for k in REQUIRED_KEYS if not (chosen or {}).get(k)]


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: levers.py <project> <segment> [--json]")
    data = load(sys.argv[1], sys.argv[2])
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2)); return
    print(f"  {data['project']}/{data['segment']}: "
          f"{data['extracted']}/{data['total']} dimensions extracted\n")
    for d in data["dimensions"]:
        if not d["present"]:
            print(f"  {d['label']:<24} —  not extracted")
            continue
        mark = "!" if not d["items"] else " "
        print(f" {mark}{d['label']:<24} {len(d['items'])} items"
              + ("   <- file present but nothing parsed" if not d["items"] else ""))
        for item in d["items"][:3]:
            print(f"      · {item['name'][:70]}")


if __name__ == "__main__":
    main()
