#!/usr/bin/env python3
"""Import a segmentation that was produced somewhere else.

Stages 01--06 turn a raw VOC dump into one evidence file per audience. That work
is expensive and it is not always done here: the same skills run perfectly well
pasted into a chat window, and someone who has already spent a week doing that
should not have to spend it again to get at stages 07 onwards.

So this reads the artefact that chat produces -- one markdown file per audience,
each holding the audience's definition, its facet tally and every assigned
comment -- and writes the evidence file stage 06 would have written. From there
`extract`, `picc`, `concepts` and `brief` cannot tell the difference, because
nothing downstream parses an evidence file: it is a corpus the skills read.

Which is exactly why the import is recorded. `_provenance.json` already had an
`imported` origin and no way to produce one; every extraction now prints where
its evidence came from, so an unknown lineage cannot pass itself off as a run.

Two things are deliberately not invented. The source carries no evidence tier
and no rationale prose, so those come out as `unspecified` and are omitted --
filling them with something plausible would put words the research never said in
front of twenty skills that are about to believe them.

Standard library only.
"""

from __future__ import annotations

import datetime
import io
import os
import re
import zipfile

import paths
import store

# The fenced block holding one comment's original text. Comment text can itself
# begin a line with '#' -- three do in the corpus this was written against
# ("#12: Not so humble brag...") -- so every structural test below runs only
# outside these fences. A line-shape test alone silently splits one comment in
# two and loses the half it does not recognise.
FENCE_OPEN = "```text"
FENCE_CLOSE = "```"
TEXT_MARKER = "**Original text**"

BULLET = re.compile(r"^- \*\*(?P<key>[^:*]+):\*\*\s*(?P<value>.*)$")
COMMENT_HEAD = re.compile(r"^### Comment\b")
DOC_TITLE = re.compile(r"^# (?P<name>.+?)\s*$")
FACET_HEAD = re.compile(r"^## Attribute / journey-state tally\s*$")
COMMENTS_HEAD = re.compile(r"^## Comments\s*$")
TABLE_RULE = re.compile(r"^\|[\s:-]+\|[\s|:-]*$")

# Not audiences. `unassigned` is a valid outcome stage 06 writes separately, and
# a leading underscore is this format's own marker for a report about the run.
NOT_AN_AUDIENCE = {"unassigned"}

SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class ImportProblem(Exception):
    """A file that cannot be read as an audience file, named and explained."""


# ---------------------------------------------------------------- parsing

def slugify(name):
    """A filename stem or audience name reduced to an evidence-file slug."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return s[:64]


def parse_audience_file(text):
    """One markdown audience file -> its name, scope, facet tally and comments.

    Returns the fields the source actually carries. Absent fields stay absent
    rather than becoming defaults, so the renderer can tell "empty" from
    "never supplied" and say so.
    """
    doc = {"name": "", "fields": {}, "facets": [], "comments": []}
    comment = None
    section = "header"
    fenced = awaiting_text = False

    for raw in str(text or "").split("\n"):
        line = raw.rstrip()

        if fenced:
            if line == FENCE_CLOSE:
                fenced = False
            else:
                comment["text"].append(raw)
            continue

        if awaiting_text and line == FENCE_OPEN:
            fenced, awaiting_text = True, False
            continue

        if COMMENT_HEAD.match(line):
            comment = {"fields": {}, "text": []}
            doc["comments"].append(comment)
            section = "comments"
            awaiting_text = False
            continue

        if line == TEXT_MARKER and comment is not None:
            awaiting_text = True
            continue

        if FACET_HEAD.match(line):
            section = "facets"
            continue
        if COMMENTS_HEAD.match(line):
            section = "comments"
            continue

        if not doc["name"]:
            title = DOC_TITLE.match(line)
            if title:
                doc["name"] = title.group("name").strip()
                continue

        bullet = BULLET.match(line)
        if bullet:
            key = bullet.group("key").strip()
            value = bullet.group("value").strip()
            target = comment["fields"] if section == "comments" and comment else doc["fields"]
            target[key] = value
            continue

        if section == "facets" and line.startswith("|") and not TABLE_RULE.match(line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[1].replace(",", "").isdigit():
                doc["facets"].append((cells[0], int(cells[1].replace(",", ""))))

    for c in doc["comments"]:
        c["text"] = "\n".join(c["text"]).strip("\n")
    return doc


def looks_like_audience_file(text):
    """Cheap enough to run on every member of an uploaded zip.

    Tests the first non-empty line rather than searching, because DOC_TITLE is
    anchored for matching one line at a time; searching a multi-line head with
    it never matches, which is silent -- an upload simply reports finding
    nothing.
    """
    body = str(text or "")
    if "### Comment" not in body:
        return False
    for line in body[:4000].split("\n"):
        if line.strip():
            return bool(DOC_TITLE.match(line.rstrip()))
    return False


# ---------------------------------------------------------------- rendering

RULE = "-" * 72

# Source key -> evidence-file label, in the order stage 06 writes them.
ITEM_FIELDS = [
    ("Source type", "TYPE"),
    ("Title", "TITLE"),
    ("URL", "URL"),
    ("Thread", "THREAD"),
    ("Score", "ASSIGNMENT SCORE"),
    ("Margin", "WINNING MARGIN"),
    ("Runner-up", "RUNNER-UP"),
    ("Cues fired", "PRIMARY CUES"),
    ("Tags", "FACETS"),
]

EMPTY_TAG = {"(none)", "none", "-", "—", ""}


def render_evidence(doc, slug, segment_id):
    """The parsed audience file as the evidence file stage 06 would have written.

    Deliberately the same shape, because twenty skills were written against that
    shape and read it as prose. The provenance banner is the one addition: an
    extractor that is told the corpus was imported can say so, and a person
    reading the file knows before the first comment.
    """
    fields = doc["fields"]
    name = doc["name"] or slug.replace("_", " ").title()
    threads = {c["fields"].get("Thread", "") for c in doc["comments"]}
    threads.discard("")

    out = [f"{name.upper()}", "=" * 72, "",
           f"Segment ID: {segment_id}",
           f"Segment slug: {slug}",
           "Validation status: imported",
           f"Evidence items: {len(doc['comments'])}",
           f"Unique threads: {len(threads)}",
           "Origin: imported — stages 01-06 were run outside this project.",
           ""]

    definition = fields.get("Definition", "").strip()
    out += ["SEGMENT DEFINITION", RULE,
            definition or "(no definition was supplied with the import)", ""]

    for key, label in (("Who's in", "INCLUSION"), ("Who's out", "EXCLUSION")):
        value = fields.get(key, "").strip()
        out.append(label)
        out.append(f"  - {value}" if value else "  - (not supplied)")
    out += ["", "Each item appears in this segment only once.", ""]

    if doc["facets"]:
        total = len(doc["comments"])
        out += ["FACETS PRESENT", RULE]
        for label, count in doc["facets"]:
            out.append(f"  - {label} [attribute]: {count} of {total} items")
        out.append("")

    out += ["EVIDENCE ITEMS", RULE]
    for c in doc["comments"]:
        cf = c["fields"]
        out.append(f"[{cf.get('ID', '?')}] TYPE: {cf.get('Source type', 'comment')}")
        for key, label in ITEM_FIELDS:
            if key == "Source type":
                continue
            value = cf.get(key, "").strip()
            if not value or (label == "FACETS" and value.lower() in EMPTY_TAG):
                continue
            out.append(f"{label}: {value}")
        # The source assigns no tier. Saying so beats defaulting to one of the
        # three real tiers, which would read as a judgement nobody made.
        out.append("EVIDENCE TIER: unspecified")
        out.append(f"TEXT: {c['text']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- provenance

def provenance_path(project_dir):
    return paths.evidence(project_dir, "_provenance.json")


def read_provenance(project_dir):
    p = provenance_path(project_dir)
    return store.read_json(p, {}) if store.exists(p) else {}


def record_provenance(project_dir, segment, origin, detail, now=None):
    """Every evidence file records where it came from.

    Without this an imported file is indistinguishable from one the pipeline
    produced, and downstream output silently inherits an unknown lineage.
    """
    prov = read_provenance(project_dir)
    stamp = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    prov[segment] = {"origin": origin, "detail": detail, "recorded_at": stamp}
    store.write_json(provenance_path(project_dir), prov)
    return prov


# ---------------------------------------------------------------- importing

def audience_members(blob):
    """(name, text) for every plausible audience file in an uploaded zip.

    Accepts a zip of markdown files or a zip containing such a zip, because
    that is what comes out of a chat: an export whose own export is nested.
    """
    found = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            base = os.path.basename(name)
            if base.startswith((".", "._")) or "__MACOSX" in name:
                continue
            if name.lower().endswith(".zip"):
                try:
                    found += audience_members(zf.read(info))
                except zipfile.BadZipFile:
                    continue
                continue
            if not base.lower().endswith((".md", ".txt", ".markdown")):
                continue
            try:
                text = zf.read(info).decode("utf-8", errors="replace")
            except OSError:
                continue
            if looks_like_audience_file(text):
                found.append((base, text))
    # A nested zip and its unpacked twin both appear in a chat export. Keep one.
    unique = {}
    for base, text in found:
        unique.setdefault(base, text)
    return sorted(unique.items())


def plan(members):
    """What an import would write, decided before anything is written.

    Slugs and segment ids are assigned here, over the whole set at once, so they
    are stable: importing the same export twice produces the same ids, and a
    caller can show the plan and be believed.
    """
    rows = []
    for base, text in members:
        stem = os.path.splitext(base)[0]
        # A leading underscore is this format's own marker for a report about
        # the run rather than an audience in it. Stripping it instead of
        # honouring it turns _stage06_summary.md into a 38th audience whose
        # "comments" are the summary's own examples.
        if stem.startswith("_"):
            continue
        slug = slugify(stem)
        if not slug or not SLUG_OK.match(slug) or slug in NOT_AN_AUDIENCE:
            continue
        doc = parse_audience_file(text)
        if not doc["comments"]:
            continue
        rows.append({"slug": slug, "name": doc["name"] or stem,
                     "items": len(doc["comments"]), "doc": doc})
    rows.sort(key=lambda r: r["slug"])
    for ordinal, row in enumerate(rows, start=1):
        row["segment_id"] = f"seg_{ordinal:03d}"
    return rows


def write(project_dir, rows, detail, now=None):
    """Write the planned evidence files and record each one's origin."""
    written = []
    for row in rows:
        text = render_evidence(row["doc"], row["slug"], row["segment_id"])
        dest = paths.evidence(project_dir, f"{row['slug']}.txt")
        store.write_text(dest, text)
        record_provenance(project_dir, row["slug"], "imported",
                          f"{detail}, {row['items']} assigned items", now=now)
        written.append({"slug": row["slug"], "name": row["name"],
                        "items": row["items"], "path": dest,
                        "bytes": len(text.encode("utf-8"))})
    return written
