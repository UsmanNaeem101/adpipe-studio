#!/usr/bin/env python3
"""
adpipe studio — the whole pipeline in a browser. No terminal needed.

Three tabs:
  Remix    upload a product photo, write a brief, pick reference layouts, generate
  Pipeline run any stage (ingest -> ... -> render) and watch the output live
  Settings paste your API keys

Keys saved in Settings go to AdPipe's private user-level credential store, outside
every project and repository. The browser never receives a saved key; older
localStorage credentials are migrated once and removed after a successful save.

Start it by double-clicking `Ad Studio.command`, or:
    ./adpipe studio

Standard library only.
"""

from __future__ import annotations

import base64
import datetime
import http.server
import io
import json
import mimetypes
import os
import re
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import importer  # noqa: E402
import remix  # noqa: E402
import exifstrip  # noqa: E402
import presets  # noqa: E402
import levers  # noqa: E402
import briefs  # noqa: E402
import products  # noqa: E402
import paths
import settings  # noqa: E402
import enrich  # noqa: E402
import synth  # noqa: E402
import auditlog  # noqa: E402
import credentials  # noqa: E402
import store

REFS = os.path.join(ROOT, "references")
PORT = int(os.environ.get("STUDIO_PORT") or os.environ.get("PORT") or "8765")
# Loopback by default: on a laptop this app is for the person sitting at it, and
# binding wider would put an app with no login of its own on the local network.
# A container has to bind 0.0.0.0 to be reachable at all, and there it sits on a
# private network behind Topic Atlas, which is what checks the session.
HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
SIZES = {"4:5 portrait": "1024x1536", "1:1 square": "1024x1024",
         "1.91:1 landscape": "1536x1024"}

# The deterministic refinement export is the lean ingest contract consumed by
# skills 03-09. Rich audit and legacy files are visible as diagnostics only.
SEGMENT_VOC_FILES = ("production_voc.jsonl",)
INGEST_ADDITIONAL_FILES = (
    ("audit_voc.jsonl", "audit_voc.jsonl · provenance and Stage 01/02 audit"),
    ("filtered_voc.jsonl", "filtered_voc.jsonl · legacy rich corpus"),
    ("retained_voc.jsonl", "retained_voc.jsonl · 01 retained records"),
    ("rejected_voc.jsonl", "rejected_voc.jsonl · 01 rejected records"),
    ("deduplicated_voc.jsonl", "deduplicated_voc.jsonl · 02 deduplicated copy"),
    ("duplicate_groups.jsonl", "duplicate_groups.jsonl · 02 duplicate audit"),
)

_lock = threading.Lock()


def _credential_snapshot():
    """Resolve current credentials without exposing them to browser responses."""
    with _lock:
        return credentials.resolve_all()


STAGES = [
    ("import",   "Adopt audience files run outside this project", False, False),
    ("ingest",   "Skills 01-02: filter + deduplicate",         True,  True),
    ("refine-voc", "Deterministic VOC refinement + export",    False, False),
    ("segment",  "Stages 03-09: research segments to commercial pack", True, False),
    ("extract",  "Skills 07-26: 20 dimensions, batched",       True,  False),
    ("picc",     "Skill 27 + PICC card + 5 angles",            True,  False),
    ("concepts", "10 concepts + hooks + layouts",             True,  False),
    ("brief",    "Production briefs",                         True,  False),
    ("qa",       "Compliance gate",                           False, False),
    ("render",   "Composite copy into layouts -> PNG",        False, False),
    ("run",      "Everything: extract -> ... -> render",      True,  False),
]


_migrated = False


def projects():
    global _migrated
    if not _migrated:
        _migrated = True
        for p, moves in paths.migrate_all().items():
            print(f"  [{p}] layout updated: " + ", ".join(moves))
    d = os.path.join(ROOT, "projects")
    if not store.exists(d):
        return []
    return [p for p in store.dirs_in(d) if not p.startswith(("_", "."))]


def segments(project):
    """Segments this project has, by either of the two things that make one real.

    An evidence file is what the pipeline stages run against; an extractions
    folder is what the Remix tab's levers come from. Listing only the first
    hides any segment whose evidence file was moved or cleaned up after
    extraction, which is exactly when you still want to build ads from it.
    """
    base = os.path.join(ROOT, "projects", project)
    out = set()
    ev = paths.evidence(base)
    if store.exists(ev):
        out |= {f[:-4] for f in store.names_in(ev) if f.endswith(".txt")}
    ex = paths.extractions(base)
    if store.exists(ex):
        out |= set(store.dirs_in(ex))
    return sorted(out)


SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


TEMPLATE_PROJECT = {
    "_comment": "Per-project config. Everything product-specific lives in this "
                "folder: project.json, product.json, product_sheet.md, facts.json.",
    "brand": "pipeline/brand.json",
    "filter": {
        "_comment": "Skill-01 filtering. TOPIC decides whether an item is about this "
                    "market at all; the rest score relevance. Regexes are "
                    "case-insensitive. These are EMPTY — write them for this niche "
                    "before ingesting, or nothing will match.",
        "topic": "", "pain": "", "first_person": "", "solution": "",
        "emotion": "", "product": "", "min_words": 12, "min_score": 2,
    },
    "audience": {
        "_comment": "Who counts as this market. 'all' researches everyone who "
                    "mentions the topic. 'mainstream' excludes communities "
                    "organised around a diagnosis and tells skills 01 and 04 to "
                    "treat a named condition as out of scope — narrower voices, "
                    "broader market. Nothing is deleted either way: excluded "
                    "records stay in audit_voc.jsonl, so changing this setting "
                    "and re-running refine-voc is free.",
        "population_scope": "all",
        "excluded_subreddits": [],
        "included_subreddits": [],
    },
    "segmentation": {
        "_comment": "Floors a validated segment must clear. Below either one it "
                    "is demoted to needs_more_research: too few conversations is "
                    "a thread, not an audience.",
        "min_segment_threads": 4, "min_segment_evidence": 8,
    },
    "compliance": {
        "_comment": "Which qa.py ruleset applies. Use 'health_adjacent' for anything "
                    "wellness-adjacent; it is the strict Meta set.",
        "profile": "health_adjacent", "platform": "meta", "notes": "",
    },
    "creative": {"awareness": "problem-aware", "traffic": "cold", "formats": ["4x5"],
                 "concepts_per_run": 10, "briefs_per_run": 4},
    "model": {"id": "claude-opus-5", "effort": "high"},
}

BLANK_FACTS = {
    "_comment": "The ONLY numbers and product claims allowed to appear in an ad. "
                "qa.py fails any stat not listed here or found verbatim in the "
                "segment evidence file. Move a fact out of "
                "candidate_facts_pending_confirmation and into approved_numbers "
                "only once it is confirmed with the supplier or store.",
    "product": "",
    "approved_numbers": [],
    "candidate_facts_pending_confirmation": {},
    "safe_mechanism_language": {"allowed": [], "banned": []},
    "safe_outcome_language": {"allowed": [], "banned": []},
}


def ensure_project(name, product="", market=""):
    """The project scaffolding, created only if it is not already there.

    A product lives inside a project, and the project that holds the research is
    usually older than the product — you segment a market first and decide what
    to sell into it afterwards. Refusing to touch an existing project meant those
    projects could never hold a product at all, which put their research out of
    reach of the thing that needed it.

    Returns (cfg, created).
    """
    if not SAFE_NAME.match(name or ""):
        raise ValueError("name must be lowercase letters, digits, - or _ (2-41 chars)")
    dest = os.path.join(ROOT, "projects", name)
    existing = os.path.join(dest, "project.json")
    if store.exists(existing):
        return json.load(open(existing, encoding="utf-8")), False

    cfg = json.loads(json.dumps(TEMPLATE_PROJECT))
    cfg = {"_comment": cfg.pop("_comment"), "name": name,
           "product": product or name, "market": market or "", **cfg}
    paths.scaffold(name)
    json.dump(cfg, open(existing, "w", encoding="utf-8"), indent=2)

    facts = json.loads(json.dumps(BLANK_FACTS))
    facts["product"] = product or name
    json.dump(facts, open(os.path.join(dest, "facts.json"), "w", encoding="utf-8"),
              indent=2)
    return cfg, True


def add_product(project, doc):
    """Put a product into a project, creating the project only if it is missing."""
    name = products.value_of((doc.get("identity") or {}).get("name")) or ""
    if not name.strip():
        raise ValueError("the product needs a name")
    slug = products.slugify(name)[:40]
    ensure_project(project, name)
    if slug in products.list_products(project):
        raise ValueError(
            f"project {project!r} already has a product {slug!r} — rename this one "
            f"or open the existing product")
    products.save(project, slug, doc)
    return {"project": project, "product": slug}


def move_product(project, product, to):
    """Relink an existing product to another project.

    A product folder (products/<slug>/) is moved wholesale; its segments and
    segment sheets move with it. The one thing that must not change is which
    project's research a segment reads: segments with an explicit
    `evidence_project` keep it (the product can legally point at research in
    another project), but a segment with an EMPTY one defaulted to "this
    product's own project" — so when the product moves, that segment's default
    would silently switch to the destination project's research. Those segments
    are pinned to the source project before the move so the meaning is preserved,
    and the count is returned so the UI can say so.
    """
    if not SAFE_NAME.match(to or ""):
        raise ValueError("destination project name is invalid")
    if to not in projects():
        raise ValueError(f"no project {to!r} — create it first")
    if to == project:
        raise ValueError("product already lives in that project")
    have = products.list_products(project)
    if product not in have:
        raise ValueError(f"no product {product!r} in project {project!r}")
    if product in products.list_products(to):
        raise ValueError(
            f"project {to!r} already has a product {product!r} — open the existing "
            f"one or rename before moving")

    src = products.product_dir(project, product)
    dst = products.product_dir(to, product)

    # Pin any segment that relied on the "own project" default so a move cannot
    # silently re-point its research at the destination project.
    pinned = 0
    seg_path = os.path.join(src, "segments.json")
    if store.exists(seg_path):
        try:
            rows = json.load(open(seg_path, encoding="utf-8"))
        except (ValueError, OSError):
            rows = []
        changed = False
        for r in rows:
            c = ((r.get("doc") or {}).get("identity", {})
                 .get("evidence_project", {}).get("value"))
            if not c or not str(c).strip():
                seg = r.setdefault("doc", {}).setdefault("identity", {})
                seg["evidence_project"] = products.cell(
                    project, "user_approved", "migration",
                    f"pinned to source project before product moved from {project} "
                    f"to {to}", "")
                changed = True
                pinned += 1
        if changed:
            json.dump(rows, open(seg_path, "w", encoding="utf-8"), indent=2)

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    return {"project": to, "product": product, "pinned": pinned}


def create_project(name, product, market):
    """A project is a folder, a project.json and a blank facts.json. No product.

    Research comes first: you segment a market, then decide what to sell into it.
    Creating a product sheet alongside the project inverted that — every new
    project arrived with a product named after the project, whose identity and
    market hypothesis were guesses made before a single record had been read,
    and which then had to be renamed or deleted once the research said otherwise.
    Products are added on demand from the Product tab (add_product) once there is
    something to say about them, and a project with none is a normal state that
    products.resolve_product already reports.

    `product` and `market` are still recorded on the project: they are the
    research subject, the MARKET CONTEXT line the VOC stages are given. That is
    a different thing from a product sheet, and only the sheet is deferred.

    Nothing is copied from another project. Cloning montisella used to carry its
    filter regexes, compliance notes and — via the shared pipeline/facts.json —
    its approved claims into every new product, which is exactly the kind of
    inheritance that puts one product's claims in another product's ads. A new
    project starts blank and says so.
    """
    if not SAFE_NAME.match(name or ""):
        raise ValueError("name must be lowercase letters, digits, - or _ (2-41 chars)")
    dest = os.path.join(ROOT, "projects", name)
    if store.exists(dest):
        raise ValueError(f"project {name!r} already exists")

    cfg = json.loads(json.dumps(TEMPLATE_PROJECT))   # deep copy
    cfg = {"_comment": cfg.pop("_comment"), "name": name,
           "product": product or name, "market": market or "", **cfg}
    paths.scaffold(name)
    json.dump(cfg, open(os.path.join(dest, "project.json"), "w", encoding="utf-8"),
              indent=2)

    facts = json.loads(json.dumps(BLANK_FACTS))
    facts["product"] = product or name
    json.dump(facts, open(os.path.join(dest, "facts.json"), "w", encoding="utf-8"),
              indent=2)

    return cfg


def project_summary(name):
    """What a project actually contains — so a delete confirmation can state the
    cost of the mistake rather than asking 'are you sure?' about an unknown."""
    root = os.path.join(ROOT, "projects", name)
    if not os.path.isdir(root):
        return None
    counts = {"evidence": 0, "extractions": 0, "renders": 0, "voc_files": 0,
              "bytes": 0, "segments": []}
    for base, _dirs, files in os.walk(root):
        for f in files:
            fp = os.path.join(base, f)
            try:
                counts["bytes"] += os.path.getsize(fp)
            except OSError:
                pass
            rel = os.path.relpath(fp, root)
            # Only .txt directly in evidence/ is a segment — subfolders hold other
            # artefacts, and counting them made pain_points files look like segments.
            if rel == os.path.join("evidence", f) and f.endswith(".txt"):
                counts["evidence"] += 1
                counts["segments"].append(f[:-4])
            elif rel.startswith("extractions" + os.sep) and f.endswith(".md"):
                counts["extractions"] += 1
            elif f.lower().endswith(IMG_EXT) and "renders" in rel:
                counts["renders"] += 1
            elif rel.startswith("voc" + os.sep):
                counts["voc_files"] += 1
    return counts


def delete_project(name):
    """Archive, don't erase. A project can hold hours of paid model output;
    moving it to projects/_deleted/ makes a mis-click recoverable with a mv."""
    if name not in projects():
        raise ValueError(f"no project {name!r}")
    src = os.path.join(ROOT, "projects", name)
    trash = os.path.join(ROOT, "projects", "_deleted")
    os.makedirs(trash, exist_ok=True)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(trash, f"{name}_{stamp}")
    os.rename(src, dest)
    return os.path.relpath(dest, ROOT)


def project_outputs(project):
    """Everything every stage has produced, so the UI can show real artefacts
    rather than just a log tail."""
    root = os.path.join(ROOT, "projects", project)
    if not store.exists(root):
        return {}
    prov = {}
    pp = paths.evidence(root, "_provenance.json")
    if store.exists(pp):
        try:
            prov = json.load(open(pp, encoding="utf-8"))
        except Exception:
            prov = {}
    out = {"stages": [], "provenance": prov}

    def entry(stage, label, path, kind="text", role=None):
        if store.exists(path):
            stat = os.stat(path)
            rel = os.path.relpath(path, ROOT)
            out["stages"].append({"stage": stage, "label": label, "path": rel,
                                  "kind": kind,
                                  "role": role,
                                  "size": stat.st_size,
                                  "mtime": stat.st_mtime,
                                  "modified_at": datetime.datetime.fromtimestamp(
                                      stat.st_mtime).astimezone().isoformat(
                                          timespec="seconds")})

    voc = paths.voc(root)
    entry("ingest", "production_voc.jsonl · segment-ready corpus",
          os.path.join(voc, "production_voc.jsonl"), role="final")
    for f, lbl in INGEST_ADDITIONAL_FILES:
        entry("ingest", lbl, os.path.join(voc, f), role="additional")

    for f, lbl in (("candidate_segments.json", "03 candidates"),
                   ("validated_segments.json", "04 decisions"),
                   ("facet_vocabulary.json", "04 facet vocabulary"),
                   ("segment_graph.json", "04 segment graph"),
                   ("segment_assignments.jsonl", "05 assignments"),
                   ("segment_cooccurrence.json", "06B co-occurrence"),
                   ("research_pack_manifest.json", "06C pack manifest"),
                   ("unassigned_evidence.md", "06 unassigned"),
                   ("segment_evidence_manifest.yaml", "06 manifest"),
                   ("assignment_conflicts.jsonl", "06 conflicts"),
                   ("missing_evidence.jsonl", "06 missing")):
        entry("segment", lbl, os.path.join(voc, f))

    discovery = paths.research(root, "segments", "discovery")
    if os.path.isdir(discovery):
        for base, _dirs, files in os.walk(discovery):
            for f in sorted(files):
                if f.endswith((".json", ".md")):
                    path = os.path.join(base, f)
                    rel = os.path.relpath(path, discovery)
                    entry("segment", f"03 discovery · {rel}", path)

    for folder, prefix in (("commercial", "07-08 commercial"),
                           ("final", "09 research pack")):
        stage_dir = paths.research(root, "segments", folder)
        if os.path.isdir(stage_dir):
            for base, _dirs, files in os.walk(stage_dir):
                for f in sorted(files):
                    if f.startswith(".") or f.endswith(".meta.json"):
                        continue
                    path = os.path.join(base, f)
                    rel = os.path.relpath(path, stage_dir)
                    entry("segment", f"{prefix} · {rel}", path,
                          role="final" if folder == "final" else None)

    ed = paths.evidence(root)
    if store.exists(ed):
        for f in store.names_in(ed):
            if f.endswith(".txt"):
                entry("segment", f"06 evidence · {f[:-4]}", os.path.join(ed, f))

    xd = paths.extractions(root)
    if store.exists(xd):
        for seg in store.dirs_in(xd):
            d = os.path.join(xd, seg)
            if store.exists(d):
                for f in store.names_in(d):
                    entry("extract", f"{seg} · {f[:-3]}", os.path.join(d, f))

    od = paths.assets(root)
    if store.exists(od):
        for seg in store.dirs_in(od):
            d = os.path.join(od, seg)
            if not store.exists(d):
                continue
            for f, st in (("01_picc_card.md", "picc"), ("02_concepts.md", "concepts"),
                          ("concepts.json", "concepts"),
                          ("03_production_brief.md", "brief"),
                          ("plates.json", "brief"), ("remix.json", "brief")):
                entry(st, f"{seg} · {f}", os.path.join(d, f))
            rd = os.path.join(d, "renders")
            if store.exists(rd):
                for f in store.names_in(rd):
                    if f.lower().endswith(IMG_EXT):
                        entry("render", f"{seg} · {f}", os.path.join(rd, f), "image")

    ld = os.path.join(root, "logs", "model")
    if os.path.isdir(ld):
        for base, _dirs, files in os.walk(ld):
            for f in sorted(files):
                if f.endswith((".json", ".jsonl", ".png")):
                    path = os.path.join(base, f)
                    label = os.path.relpath(path, ld)
                    entry("logs", label, path,
                          "image" if f.endswith(".png") else "text")
    return out


# The audit log's own stage label. auditlog writes "context" before "metadata"
# and "request", so it is in the first few hundred bytes — worth reading a prefix
# rather than parsing ~30KB of prompt for each of the several hundred runs one
# ingest leaves behind.
_STAGE_RE = re.compile(r'"stage"\s*:\s*"([^"]*)"')


def _run_stage(request_json):
    """This request's stage, read cheaply but not carelessly.

    The prefix scan is bounded to the "context" block. Searching the whole prefix
    for a "stage" key would happily match one inside the prompt or the schema and
    file the log under a stage that never ran it — a mislabelled log is worse
    than a slow one, because it is missing from the export you go looking in.
    """
    try:
        with store.open_key(request_json, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    start = head.find('"context"')
    if start >= 0:
        block = head[start:]
        # Stop at the next top-level key so the scan cannot run past the context.
        for following in ('"metadata"', '"request"'):
            cut = block.find(following)
            if cut >= 0:
                block = block[:cut]
        found = _STAGE_RE.search(block)
        if found:
            return found.group(1) or None
        # A complete context block with no stage in it is a real answer:
        # unattributed, no need to re-read the file to confirm it.
        if len(head) < 4096 or '"metadata"' in head or '"request"' in head:
            return None
    try:
        with store.open_key(request_json, encoding="utf-8", errors="replace") as fh:
            return ((json.load(fh).get("context") or {}).get("stage")) or None
    except (ValueError, OSError):
        return None


def log_runs(project):
    """Every audited model call in a project, with the stage that made it.

    One request is one directory (request.json, response.json, events.jsonl),
    so a stage is a set of directories rather than a file — which is why picking
    them out of the Outputs list by hand does not scale: an 83-chunk ingest with
    a recovery pass leaves several hundred rows there, in the order they happen
    to sort, and the interesting ones are the failures scattered among them.
    """
    root = os.path.join(ROOT, "projects", project, "logs", "model")
    runs = []
    if not os.path.isdir(root):
        return runs
    for base, _dirs, files in os.walk(root):
        if "request.json" not in files:
            continue
        size = 0
        for f in files:
            try:
                size += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
        runs.append({
            "dir": base,
            "rel": os.path.relpath(base, root),
            "stage": _run_stage(os.path.join(base, "request.json")) or "unattributed",
            "files": sorted(files),
            "bytes": size,
            "mtime": os.path.getmtime(base),
        })
    runs.sort(key=lambda r: r["rel"])
    return runs


def log_stages(project):
    """What is available to export, so the button can say what it will hand over."""
    stages = {}
    for run in log_runs(project):
        s = stages.setdefault(run["stage"],
                              {"stage": run["stage"], "runs": 0, "files": 0,
                               "bytes": 0, "mtime": 0})
        s["runs"] += 1
        s["files"] += len(run["files"])
        s["bytes"] += run["bytes"]
        s["mtime"] = max(s["mtime"], run["mtime"])
    out = sorted(stages.values(), key=lambda s: -s["mtime"])
    for s in out:
        s["modified_at"] = datetime.datetime.fromtimestamp(
            s["mtime"]).astimezone().isoformat(timespec="seconds")
    return out


def export_logs(project, stage=""):
    """Zip every audited request for one stage, plus a manifest describing them.

    Returns (filename, bytes). An empty stage exports every stage — asking for
    "everything that happened" is a legitimate request, and making the caller
    enumerate stages to get it is the same busywork this replaces.

    The manifest goes in first so the archive is readable without unpacking it:
    it lists every run with its stage, operation, job id and model, which is
    enough to find the one request worth opening among several hundred.
    """
    if project not in projects():
        raise ValueError(f"unknown project {project!r}")
    runs = [r for r in log_runs(project) if not stage or r["stage"] == stage]
    if not runs:
        raise ValueError(
            f"no model logs for stage {stage!r} in {project!r}"
            if stage else f"no model logs in {project!r} yet")

    manifest = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for run in runs:
            row = {"path": run["rel"], "stage": run["stage"],
                   "bytes": run["bytes"], "files": run["files"]}
            try:
                with store.open_key(os.path.join(run["dir"], "request.json"),
                          encoding="utf-8", errors="replace") as fh:
                    req = json.load(fh)
                row.update({k: req.get(k) for k in
                            ("started_at", "provider", "model", "operation",
                             "job_id")})
            except (ValueError, OSError):
                row["unreadable"] = True
            manifest.append(row)
            for name in run["files"]:
                src = os.path.join(run["dir"], name)
                try:
                    zf.write(src, os.path.join(run["rel"], name))
                except OSError:
                    # One unreadable file must not cost the export. Say so in
                    # the manifest rather than dropping it silently.
                    row.setdefault("skipped", []).append(name)
        zf.writestr("manifest.json", json.dumps({
            "project": project,
            "stage": stage or "(all stages)",
            "exported_at": datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"),
            "runs": len(runs),
            "note": "One directory per model request: request.json is what was "
                    "sent, response.json is what came back (including usage and "
                    "finish_reason), events.jsonl holds retries and errors.",
            "requests": manifest,
        }, indent=2))

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{project}-{re.sub(r'[^a-z0-9]+', '-', (stage or 'all').lower())}-logs-{stamp}.zip"
    return name, buf.getvalue()


def segment_voc_files(project):
    """Only completed ingest contracts that segmentation Stages 03-09 consume."""
    if project not in projects():
        return []
    voc_dir = paths.voc(os.path.join(ROOT, "projects", project))
    files = []
    for name in SEGMENT_VOC_FILES:
        path = os.path.join(voc_dir, name)
        if store.exists(path):
            stat = os.stat(path)
            files.append({
                "name": name,
                "label": f"Final · {name}",
                "path": path,
                "mtime": stat.st_mtime,
                "modified_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            })
    return files


def refine_voc_files(project):
    """Project-owned JSONL inputs whose records have the refinement contract."""
    if project not in projects():
        return []
    voc_dir = paths.voc(os.path.join(ROOT, "projects", project))
    if not os.path.isdir(voc_dir):
        return []
    files = []
    for base, _dirs, names in os.walk(voc_dir):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(base, name)
            try:
                with store.open_key(path, encoding="utf-8") as fh:
                    first = next((json.loads(line) for line in fh if line.strip()), None)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(first, dict) or not {"id", "text"} <= set(first):
                continue
            tierable = (first.get("decision") == "reject"
                        or bool(first.get("retention_reasons"))
                        or first.get("tier") in ("core", "supporting", "context"))
            if not tierable:
                continue
            stat = os.stat(path)
            rel = os.path.relpath(path, voc_dir)
            recommended = rel == "deduplicated_voc.jsonl"
            files.append({
                "name": rel,
                "label": ("Recommended" if recommended else "Available") + f" · {rel}",
                "path": path,
                "mtime": stat.st_mtime,
                "modified_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                "recommended": recommended,
            })
    return sorted(files, key=lambda row: (not row["recommended"], row["name"]))


DIMCACHE = os.path.join(ROOT, ".cache", "dims.json")
_dims_mem = None


def _dims_cache():
    global _dims_mem
    if _dims_mem is None:
        try:
            _dims_mem = json.load(open(DIMCACHE, encoding="utf-8"))
        except Exception:
            _dims_mem = {}
    return _dims_mem


def _dims_save():
    try:
        os.makedirs(os.path.dirname(DIMCACHE), exist_ok=True)
        json.dump(_dims_mem, open(DIMCACHE, "w", encoding="utf-8"))
    except Exception:
        pass


def real_format(path):
    """What the file ACTUALLY is, by magic bytes — not what the extension claims.
    A lot of saved ad creative is AVIF or WebP wearing a .jpg extension, which the
    image APIs reject even though macOS and Chrome open it happily."""
    try:
        with store.open_key(path, "rb") as f:
            h = f.read(16)
    except Exception:
        return "unknown"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if h[:2] == b"\xff\xd8":
        return "jpeg"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[4:8] == b"ftyp":
        brand = h[8:12].decode("latin1", "replace")
        if "avif" in brand:
            return "avif"
        if "heic" in brand or "heif" in brand or "mif1" in brand:
            return "heic"
        return "iso-" + brand.strip()
    return "unknown"


def sips_dims(path):
    """macOS reads AVIF/HEIC natively; the header parsers here do not. Only called
    for formats the fast path can't do, and the result is cached."""
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
            capture_output=True, text=True, timeout=15).stdout
        w = h = 0
        for line in out.splitlines():
            if "pixelWidth" in line:
                w = int(line.split(":")[1])
            if "pixelHeight" in line:
                h = int(line.split(":")[1])
        return w, h
    except Exception:
        return 0, 0


def image_dims(path):
    """Width/height from the file header — no Pillow, no full decode.
    Returns (w, h) or (0, 0) if the format isn't recognised."""
    try:
        with store.open_key(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w = int.from_bytes(head[16:20], "big")
                h = int.from_bytes(head[20:24], "big")
                return w, h
            if head[:2] == b"\xff\xd8":                      # JPEG: walk to a SOF
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        break
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if marker in (b"\xc0", b"\xc1", b"\xc2", b"\xc3",
                                  b"\xc5", b"\xc6", b"\xc7", b"\xc9",
                                  b"\xca", b"\xcb", b"\xcd", b"\xce", b"\xcf"):
                        f.read(3)
                        h = int.from_bytes(f.read(2), "big")
                        w = int.from_bytes(f.read(2), "big")
                        return w, h
                    seg = int.from_bytes(f.read(2), "big")
                    if seg < 2:
                        break
                    f.seek(seg - 2, 1)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                f.seek(0); d = f.read(30)
                if d[12:16] == b"VP8X":
                    w = int.from_bytes(d[24:27], "little") + 1
                    h = int.from_bytes(d[27:30], "little") + 1
                    return w, h
    except Exception:
        pass
    return 0, 0


def library():
    """Every reference ad with the detail needed to curate it — size, dimensions,
    and a content hash so exact duplicates are findable rather than eyeballed."""
    import hashlib
    out, hashes, dirty = [], {}, False
    if not os.path.isdir(REFS):
        return {"items": [], "duplicates": []}
    for cat in sorted(os.listdir(REFS)):
        d = os.path.join(REFS, cat)
        if not os.path.isdir(d) or not is_ref_category(cat):
            continue
        for f in sorted(os.listdir(d)):
            if not f.lower().endswith(IMG_EXT):
                continue
            full = os.path.join(d, f)
            rel = os.path.join(cat, f)
            w, h = image_dims(full)
            fmt = real_format(full)
            if not w:
                cache = _dims_cache()
                key = f"{rel}:{os.path.getmtime(full):.0f}"
                if key in cache:
                    w, h = cache[key]
                else:
                    w, h = sips_dims(full)
                    cache[key] = [w, h]
                    dirty = True
            digest = hashlib.sha256(open(full, "rb").read()).hexdigest()
            hashes.setdefault(digest, []).append(rel)
            claimed = os.path.splitext(f)[1].lower().lstrip(".")
            claimed = "jpeg" if claimed == "jpg" else claimed
            out.append({"rel": rel, "category": cat, "name": f,
                        "bytes": os.path.getsize(full), "w": w, "h": h,
                        "format": fmt,
                        "mislabelled": fmt != claimed and fmt != "unknown",
                        # png<->jpeg mislabels are harmless: every API and browser
                        # accepts both. AVIF/HEIC is the one that gets rejected by
                        # image endpoints even though macOS opens it fine.
                        "risky": fmt in ("avif", "heic") or fmt.startswith("iso-"),
                        "hash": digest[:12]})
    if dirty:
        _dims_save()
    dupes = [v for v in hashes.values() if len(v) > 1]
    return {"items": out, "duplicates": dupes,
            "mislabelled": [i["rel"] for i in out if i["mislabelled"]],
            "risky": [i["rel"] for i in out if i["risky"]],
            "categories": sorted({i["category"] for i in out})}


def delete_reference(rel):
    """Delete one reference image. Path is validated against the library root, and
    the file is moved to references/_deleted/ rather than unlinked — a mis-click
    on a swipe file you cannot re-download is not worth the convenience."""
    full = safe_ref_path(rel)
    trash = os.path.join(REFS, "_deleted")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, rel.replace(os.sep, "__"))
    n = 1
    while store.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base}_{n}{ext}"; n += 1
    os.rename(full, dest)
    return os.path.relpath(dest, ROOT)


def convert_reference(rel, quality=90):
    """Rewrite an AVIF/HEIC reference as a real JPEG, in place.

    The files already carry a .jpg name — only the bytes are wrong — so the name
    stays and nothing that points at it breaks. The original is copied to
    references/_originals/ first: sips is reliable but this is swipe material that
    may not be re-downloadable, and AVIF->JPEG is lossy and one-way.

    Expect the file to get roughly 5-10x bigger. AVIF is simply a much more
    efficient codec; that size jump is the cost of a format the image APIs accept.
    """
    full = safe_ref_path(rel)
    fmt = real_format(full)
    if fmt in ("jpeg", "png"):
        return {"rel": rel, "skipped": f"already {fmt}"}
    originals = os.path.join(REFS, "_originals")
    os.makedirs(originals, exist_ok=True)
    backup = os.path.join(originals, rel.replace(os.sep, "__"))
    if not store.exists(backup):
        with open(full, "rb") as a, open(backup, "wb") as b:
            b.write(a.read())
    before = os.path.getsize(full)
    tmp = full + ".converting.jpg"
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
             full, "--out", tmp],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not store.exists(tmp):
            raise RuntimeError((r.stderr or "sips failed").strip()[:200])
        if real_format(tmp) != "jpeg":
            raise RuntimeError("sips produced a non-JPEG")
        os.replace(tmp, full)
    except Exception as e:
        if store.exists(tmp):
            os.remove(tmp)
        return {"rel": rel, "error": str(e)}
    return {"rel": rel, "from": fmt, "before": before,
            "after": os.path.getsize(full)}


def safe_project_file(rel):
    """Only ever serve files from inside projects/."""
    base = os.path.realpath(os.path.join(ROOT, "projects"))
    full = os.path.realpath(os.path.join(ROOT, rel))
    if not full.startswith(base + os.sep) or not store.exists(full):
        raise remix.RemixError("path outside projects/")
    return full


def compliance_notes(project="montisella"):
    try:
        cfg = json.load(open(os.path.join(ROOT, "projects", project, "project.json"),
                             encoding="utf-8"))
        return cfg.get("compliance", {}).get("notes", "")
    except Exception:
        return ""


def picc_cards(project):
    """Every PICC card in a project, newest first.

    The concepts stage builds on exactly one card, so the operator picks it
    rather than having the segment's own card assumed. Any Markdown under
    output/ whose name mentions "picc" counts, which means a card you saved
    aside as a variant shows up next to the generated ones.
    """
    base = paths.assets(os.path.join(ROOT, "projects", project))
    if not store.exists(base):
        return []
    out = []
    for seg in store.dirs_in(base):
        d = os.path.join(base, seg)
        if not store.exists(d):
            continue
        for f in store.names_in(d):
            if f.endswith(".md") and "picc" in f.lower():
                full = os.path.join(d, f)
                out.append({
                    "rel": os.path.relpath(full, ROOT),
                    "segment": seg,
                    "name": f,
                    "label": f"{seg} · {f}",
                    "bytes": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                })
    out.sort(key=lambda r: -r["mtime"])
    return out


def build_image_prompt(req):
    """The exact prompt the image model receives, from one request payload.

    Shared by /prompt and /generate so the text shown for review is assembled by
    the same code that would send it. Returns (prompt, preset_or_None, mode).
    """
    # The preset (if any) decides how strictly the reference layout is held.
    # Without one, the reference is the only direction there is, so it's fixed.
    mode = "preset" if req.get("conflict_mode") == "preset" else "reference"
    chosen = presets.by_id(req["preset"]) if req.get("preset") else None

    if chosen and mode == "preset":
        head = ("Use this reference ad as the starting point for layout and "
                "composition, and the item in the second image as the product. "
                "Replace all copy with the brief below. You may adapt the "
                "reference's structure where the execution preset requires it. "
                "Render every word spelled exactly as written; put no text "
                "anywhere the brief doesn't specify.")
    else:
        head = ("Recreate this reference ad's exact layout, composition and text "
                "placement, but replace the product with the item shown in the "
                "second image and replace all copy with the brief below. Keep the "
                "reference's structure and proportions. Render every word spelled "
                "exactly as written; put no text anywhere the brief doesn't specify.")

    prompt = head + "\n\nBRIEF:\n" + (req.get("brief") or "").strip()
    if chosen:
        prompt += "\n\n" + presets.prompt_block(chosen, mode)
    # Explicitly-set levers go last so they win: a preset fills all 49 by
    # implication, but only these were actually chosen by a person.
    block = presets.custom_block(req.get("levers") or {})
    if block:
        prompt += "\n\n" + block
    return prompt, chosen, mode


def is_ref_category(name):
    """Real category folders only. `_originals` and `_deleted` live inside
    references/ so backups stay next to what they back up — but they must never
    be scanned as layouts, or a deleted or pre-conversion file reappears in the
    picker and inflates every count."""
    return not name.startswith("_") and not name.startswith(".")


def list_references():
    out = {}
    if not os.path.isdir(REFS):
        return out
    for cat in sorted(os.listdir(REFS)):
        d = os.path.join(REFS, cat)
        if os.path.isdir(d) and is_ref_category(cat):
            files = sorted(f for f in os.listdir(d) if f.lower().endswith(IMG_EXT))
            if files:
                out[cat] = files
    return out


def safe_ref_path(rel):
    """Never trust a path from the browser — confirm it stays inside references/."""
    full = os.path.realpath(os.path.join(REFS, rel))
    if not full.startswith(os.path.realpath(REFS) + os.sep):
        raise remix.RemixError("reference path escapes the library")
    if not store.exists(full):
        raise remix.RemixError(f"reference not found: {rel}")
    return full


THUMBS = os.path.join(ROOT, ".cache", "thumbs")


def thumbnail(full_path, rel):
    """The reference ads are full-size creatives (~1MB each); serving 221 of them
    raw makes the picker crawl. Downscale once with macOS's built-in `sips` and
    cache. Falls back to the original if sips isn't available."""
    os.makedirs(THUMBS, exist_ok=True)
    key = rel.replace(os.sep, "__").rsplit(".", 1)[0] + ".jpg"
    dest = os.path.join(THUMBS, key)
    if store.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(full_path):
        return dest, "image/jpeg"
    try:
        subprocess.run(
            ["sips", "-Z", "300", "-s", "format", "jpeg", full_path, "--out", dest],
            check=True, capture_output=True, timeout=25)
        return dest, "image/jpeg"
    except Exception:
        return full_path, mimetypes.guess_type(full_path)[0] or "image/png"


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>adpipe studio</title>
<style>
 :root{--ink:#14161A;--soft:#4A5058;--paper:#fff;--surface:#F4F2EE;--accent:#1F6F5C;
   --accent-soft:#DCEBE5;--signal:#C4553B;--line:#DFDAD2}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
   color:var(--ink);background:var(--surface)}
 header{background:var(--ink);color:#fff;padding:16px 28px 0}
 .htop{display:flex;align-items:baseline;gap:14px;margin-bottom:14px}
 .htop b{font-size:19px;letter-spacing:.04em}.htop span{color:#98a2a0;font-size:13px}
 .tabs{display:flex;gap:4px}
 .tab{padding:11px 20px;border-radius:10px 10px 0 0;background:#252a30;color:#c8cfcd;
   cursor:pointer;font-weight:600;font-size:14px;border:0}
 .tab.on{background:var(--surface);color:var(--ink)}
 .wrap{max-width:1200px;margin:0 auto;padding:26px 28px 90px}
 .card{background:var(--paper);border:1px solid var(--line);border-radius:14px;
   padding:20px;margin-bottom:20px}
 h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 14px}
 label{display:block;font-weight:600;margin:0 0 6px;font-size:14px}
 textarea,select,input{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);
   border-radius:9px;background:var(--paper);color:var(--ink)}
 textarea{min-height:140px;resize:vertical}
 .btn{background:var(--accent);color:#fff;border:0;border-radius:999px;padding:13px 26px;
   font:inherit;font-weight:700;cursor:pointer}
 .btn.wide{width:100%}.btn:disabled{background:#c2c9c7;cursor:not-allowed}
 .btn.ghost{background:transparent;color:var(--accent);border:1.5px solid var(--accent)}
 .hint{color:var(--soft);font-size:13px;margin:8px 0 0}
 .grid{display:grid;grid-template-columns:340px 1fr;gap:26px;align-items:start}
 .drop{border:2px dashed var(--line);border-radius:11px;padding:26px;text-align:center;
   cursor:pointer;color:var(--soft)}
 .drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
 /* The import plan: long enough to scroll, short enough not to push Run off
    the page — the point is to read it before pressing that button. */
 .planlist{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px;
   max-height:260px;display:block;overflow-y:auto}
 .planlist td{padding:3px 8px;border-bottom:1px solid var(--line)}
 .planlist code{opacity:.6}
 .drop img{max-width:100%;max-height:210px;border-radius:8px}
 .cats{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:11px;
   padding:6px;background:var(--surface)}
 .cat h3{font-size:13px;margin:12px 8px 6px;color:var(--soft);position:sticky;top:0;
   background:var(--surface);padding:4px 0}
 .outstage{background:var(--paper);border:1px solid var(--line);border-radius:10px;
   margin-bottom:7px;overflow:hidden}
 .outstage>summary{cursor:pointer;list-style:none;padding:10px 11px;display:grid;
   grid-template-columns:14px minmax(0,1fr);gap:8px;align-items:start}
 .outstage>summary::-webkit-details-marker{display:none}
 .outstage>summary::before{content:"›";font-size:20px;line-height:18px;color:var(--accent);
   transform-origin:center;transition:transform .12s ease}
 .outstage[open]>summary::before{transform:rotate(90deg)}
 .outstage[open]>summary{border-bottom:1px solid var(--line);background:var(--accent-soft)}
 .osline{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
 .osname{font-size:13px;text-transform:capitalize;font-weight:700}
 .oscount{font-size:11px;color:var(--soft);font-weight:500}
 .osdate{font-size:11px;color:var(--soft);margin-top:2px}
 .outbody{padding:7px}
 .outgroup-title{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
   color:var(--soft);font-weight:800;padding:7px 7px 3px}
 .orow{padding:7px 8px;border-radius:7px;cursor:pointer;font-size:12.5px;line-height:1.35}
 .orow:hover,.orow.on{background:var(--accent-soft)}
 .ormeta{color:var(--soft);font-size:10.5px;margin-top:2px}
 .thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:8px}
 .thumb{position:relative;border:3px solid transparent;border-radius:8px;overflow:hidden;
   cursor:pointer;aspect-ratio:4/5;background:#eee}
 .thumb img{width:100%;height:100%;object-fit:cover;display:block}
 .thumb.sel{border-color:var(--accent)}
 .thumb.sel::after{content:"✓";position:absolute;top:2px;right:5px;color:#fff;
   background:var(--accent);border-radius:50%;width:20px;height:20px;text-align:center;
   line-height:20px;font-size:12px}
 .results{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:18px}
 .res{background:var(--paper);border:1px solid var(--line);border-radius:12px;overflow:hidden}
 .res img{width:100%;display:block}
 .res .meta{padding:10px 12px;font-size:13px;display:flex;justify-content:space-between;gap:8px}
 .res a{color:var(--accent);font-weight:600;text-decoration:none}
 .res .spin,.res .err{padding:34px 12px;text-align:center;color:var(--soft);font-size:13px}
 .res .err{color:var(--signal)}
 .comply{background:#FFF6F3;border:1px solid var(--signal);border-radius:12px;padding:15px 18px;
   margin-bottom:20px;font-size:14px}
 .comply b{color:var(--signal)}
 pre.log{background:#14161A;color:#d6e2df;padding:16px;border-radius:11px;max-height:460px;
   overflow:auto;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;margin:0}
 .row{display:flex;gap:14px;align-items:end;flex-wrap:wrap}
 .row>div{flex:1;min-width:170px}
 .keyrow{display:flex;gap:10px;align-items:center;margin-bottom:6px}
 .keyrow input{flex:1}
 .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700}
 .ok{background:var(--accent-soft);color:var(--accent)}
 .no{background:#f3dcd6;color:var(--signal)}
 .stagelist{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px}
 .stage{border:1.5px solid var(--line);border-radius:11px;padding:12px 14px;cursor:pointer;background:var(--paper)}
 .stage:hover{border-color:var(--accent)}
 .stage.on{border-color:var(--accent);background:var(--accent-soft)}
 .stage b{display:block;font-size:14px}.stage small{color:var(--soft);font-size:12.5px}
 .stage .costs{color:var(--signal);font-size:11px;font-weight:700;letter-spacing:.05em}
 /* which skills a stage actually runs — collapsed until asked for, so the
    stage list stays scannable but a sub-skill is never invisible again */
 /* project settings — every knob that changes what a stage does, in the place
    where you choose the stage */
 .settingsbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}
 .ghost{background:transparent;color:var(--accent);border:1.5px solid var(--line);
   border-radius:9px;padding:6px 12px;font-size:12.5px;cursor:pointer}
 .ghost:hover{border-color:var(--accent)}
 #settingsbox{border:1.5px solid var(--line);border-radius:11px;padding:14px;
   margin-bottom:14px;background:var(--paper)}
 .setgroup{margin-bottom:16px}
 .setgroup>h4{margin:0 0 8px;font-size:12px;letter-spacing:.06em;
   text-transform:uppercase;color:var(--soft)}
 .setfield{margin-bottom:11px}
 .setfield label{display:block;font-size:12.5px;font-weight:600;margin-bottom:3px}
 .setfield input,.setfield select,.setfield textarea{width:100%;box-sizing:border-box}
 .setfield textarea{min-height:62px;font-family:inherit;font-size:12.5px}
 .setfield .sethelp{color:var(--soft);font-size:11.5px;margin-top:3px}
 .setfield.changed label{color:var(--accent)}
 .settingsactions{display:flex;align-items:center;gap:12px;
   border-top:1px solid var(--line);padding-top:12px}
 .stageskills{margin-top:7px}
 .skilltoggle{font-size:11.5px;color:var(--soft);border-bottom:1px dotted var(--line);
   cursor:pointer;user-select:none}
 .skilltoggle:hover{color:var(--accent);border-color:var(--accent)}
 .stageskills .skillrows{display:none;margin-top:7px}
 .stageskills.open .skillrows{display:block}
 .skillrow{display:grid;grid-template-columns:1fr auto;gap:2px 10px;
   padding:5px 0;border-top:1px solid var(--line);font-size:11.5px}
 .skillrow code{color:var(--ink);font-size:11px;word-break:break-all}
 .skillrow em{color:var(--soft);font-style:normal;text-align:right;white-space:nowrap}
 .skillrow span{color:var(--accent);font-weight:600}
 .skillrow small{grid-column:1/-1;color:var(--soft);font-size:11px}
 .skillrow.bad code,.skillrow.bad em{color:var(--signal);font-weight:700}
 .bad{color:var(--signal)}
 .hide{display:none}
 /* lever pickers */
 .lev{margin-bottom:11px}
 .lev label{display:block;font-size:12.5px;font-weight:600;margin-bottom:4px}
 .lev .req{color:var(--signal)}
 .lev select,.lev input{width:100%}
 .lev small{color:var(--soft);font-size:11.5px;display:block;margin-top:3px}
 .lev.set select,.lev.set input{border-color:var(--accent);background:var(--accent-soft)}
 details.lvg{border:1.5px solid var(--line);border-radius:11px;margin-bottom:9px;padding:0}
 details.lvg>summary{cursor:pointer;padding:11px 13px;font-weight:600;font-size:13.5px;
   list-style:none;display:flex;justify-content:space-between;align-items:center}
 details.lvg>summary::-webkit-details-marker{display:none}
 details.lvg[open]>summary{border-bottom:1.5px solid var(--line)}
 details.lvg .body{padding:12px 13px}
 .lvn{font-size:11.5px;color:var(--accent);font-weight:700}
 /* briefs */
 .bf{border:1.5px solid var(--line);border-radius:11px;padding:12px 14px;margin-bottom:10px}
 .bf.on{border-color:var(--accent);background:var(--accent-soft)}
 .bf h4{margin:0 0 6px;font-size:14px;display:flex;gap:8px;align-items:flex-start}
 .bf h4 input{width:auto;margin-top:2px}
 .bf p{margin:4px 0;font-size:13px;line-height:1.45}
 .bf .vis{color:var(--soft);font-size:12.5px;margin-top:7px;font-style:italic}
 /* move-product modal — same overlay pattern as the prompt confirm */
 #mvwrap{position:fixed;inset:0;background:rgba(20,20,20,.5);z-index:60;
   display:flex;align-items:center;justify-content:center;padding:24px}
 #mvwrap.hide{display:none}
 #mvbox{background:var(--paper);border-radius:14px;width:100%;max-width:460px;
   padding:20px 22px}
 #mvsel{background:var(--surface);border:1.5px solid var(--line);border-radius:9px;
   padding:9px 10px;min-height:42px}
 /* prompt confirm */
 #pmwrap{position:fixed;inset:0;background:rgba(20,20,20,.5);z-index:60;
   display:flex;align-items:center;justify-content:center;padding:24px}
 /* Must out-specify #pmwrap's display:flex, or the hidden overlay still covers
    the page and eats every click. */
 #pmwrap.hide{display:none}
 #pmbox{background:var(--paper);border-radius:14px;width:100%;max-width:780px;
   max-height:88vh;display:flex;flex-direction:column;padding:20px 22px}
 /* pre-wrap, not pre: a prompt you have to scroll sideways to read is a prompt
    you will accept without reading. */
 #pmtext{flex:1;min-height:340px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
   font-size:12.5px;line-height:1.5;white-space:pre-wrap;overflow:auto}
</style></head><body>
<header>
  <div class=htop><b>adpipe studio</b><span>voice-of-customer → finished ads</span></div>
  <div class=tabs>
    <button class="tab on" data-t=remix>Remix</button>
    <button class=tab data-t=product>Product</button>
    <button class=tab data-t=pipeline>Pipeline</button>
    <button class=tab data-t=library>Library</button>
    <button class=tab data-t=outputs>Outputs</button>
    <button class=tab data-t=settings>Settings</button>
  </div>
</header>
<div class=wrap>

<!-- ================= REMIX ================= -->
<section id=t-remix>
  <div class=comply><b>Compliance is manual on this tab.</b> The image model writes text
    as pixels, so the automatic checker can't read it. Read every result before shipping.
    Never allowed: __COMPLIANCE__</div>
  <div class=grid>
    <div>
      <div class=card>
        <h2>1 · Project &amp; segment</h2>
        <div class=row>
          <div><label>Project</label><select id=rx_proj></select></div>
          <div><label>Segment</label><select id=rx_seg></select></div>
        </div>
        <p class=hint id=rx_levstate>Pick a project to load its extracted levers.</p>
      </div>
      <div class=card>
        <h2>2 · Your product</h2>
        <div class=drop id=drop>
          <div id=dropmsg>Click or drop a product photo</div>
          <img id=preview hidden>
        </div>
        <input type=file id=file accept="image/*" class=hide>
        <p class=hint>A clean shot of the real product. This gets placed into each layout.</p>
      </div>
      <div class=card>
        <h2>3 · Levers</h2>
        <p class=hint style="margin-top:0">What the ad is about, taken from this
          segment's extractions. <b>Pain point</b> and <b>desired outcome</b> are
          required; everything else sharpens the brief.</p>
        <div id=levbox><p class=hint>No project loaded yet.</p></div>
      </div>
      <div class=card>
        <h2>4 · Creative brief</h2>
        <label>What each ad should say and feel like</label>
        <textarea id=brief placeholder="Headline: You didn't buy bad pillows, you bought the wrong height.
Subtext: It's set by your shoulder, not by how it feels in the shop.
CTA: Which height are you?
Calm premium bedding brand, deep green accent. Spell 'Montisella' exactly."></textarea>
        <div style="margin-top:12px">
          <button class="btn ghost" id=writebriefs style="white-space:nowrap">Write 4 briefs from levers</button>
          <p class=hint id=bfhint style="margin:7px 0 0">Uses the levers above instead
            of the box — you pick which results to render. Anything in the box is
            passed along as extra direction.</p>
        </div>
        <label style="margin-top:14px">Output shape</label>
        <select id=size>__SIZES__</select>
      </div>
      <div class=card id=briefcard hidden>
        <h2>Briefs</h2>
        <p class=hint style="margin-top:0" id=bfnote></p>
        <div id=brieflist></div>
      </div>
      <div class=card>
        <label style="font-weight:500;font-size:14px;display:flex;gap:8px;
        align-items:flex-start;margin:0 0 12px">
        <input type=checkbox id=stripexif checked style="width:auto;margin-top:3px">
        <span>Strip EXIF metadata from each result
          <span class=hint style="display:block;margin:2px 0 0">Removes camera tags,
          timestamps, software/producer fields and local paths. Does <b>not</b> remove
          C2PA content credentials and does not defeat AI detection — platforms
          classify from pixels, not tags.</span></span>
      </label>
      <button class="btn wide" id=go disabled>Generate</button>
        <p class=hint id=gohint>Add a product photo and pick at least one template.</p>
      </div>
    </div>
    <div>
      <div class=card>
        <h2>5 · Execution preset</h2>
        <div style="display:flex;gap:9px;align-items:center">
          <select id=preset_sel style=flex:1><option value="">No preset — the image
            model decides</option></select>
          <button class="btn ghost" id=presetauto
            style="padding:11px 16px;white-space:nowrap">AI picks</button>
        </div>
        <p class=hint id=presetwhy>60 canonical presets. Each one sets how the ad
          executes — visual direction, voice, proof, urgency — without touching what
          your brief says. Leave it off and the image model decides for itself.</p>
        <div id=conflictbox hidden style="background:#FFF8F1;border:1px solid #E0A87A;
          border-radius:11px;padding:14px 16px;margin-top:12px">
          <b style=font-size:13.5px id=cfhead></b>
          <div id=cflist style="font-size:13px;margin:9px 0 12px;line-height:1.5"></div>
          <label style="font-weight:500;font-size:13.5px;display:flex;gap:8px;
            align-items:flex-start;margin:0 0 7px">
            <input type=radio name=cfmode value=reference checked style="width:auto;margin-top:3px">
            <span><b>Keep the reference layout.</b> Composition, panels and text
              placement stay; the preset drives colour, mood, expression, density and
              copy inside them.</span></label>
          <label style="font-weight:500;font-size:13.5px;display:flex;gap:8px;
            align-items:flex-start;margin:0">
            <input type=radio name=cfmode value=preset style="width:auto;margin-top:3px">
            <span><b>Follow the preset.</b> It may restructure the reference to get its
              visual direction — expect the result to drift from the layout you picked.</span></label>
        </div>
      </div>
      <div class=card>
        <h2>6 · Execution levers</h2>
        <p class=hint style="margin-top:0">All 49, all optional. Anything you leave
          alone is left to the model to decide from the brief and whatever else is
          set. Anything you set is injected into the prompt verbatim.
          <span id=lvcount style="color:var(--accent);font-weight:700"></span></p>
        <div style="display:flex;gap:9px;align-items:center;margin-bottom:10px">
          <a href=# id=lvclear style="color:var(--soft);font-size:13px">clear all</a>
          <a href=# id=lvfill style="color:var(--soft);font-size:13px">fill from preset</a>
        </div>
        <div id=lvbox>loading…</div>
      </div>
      <div class=card>
        <h2>7 · Pick reference layouts</h2>
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
          <b id=selcount style="font-size:14px">0 selected</b>
          <a href=# id=clearsel style="color:var(--soft);font-size:13px">clear</a>
        </div>
        <div class=cats id=cats>loading…</div>
      </div>
      <div class=card id=resultscard hidden>
        <h2>Results</h2><div class=results id=results></div>
      </div>
    </div>
  </div>
</section>

<!-- ================= PRODUCT ================= -->
<section id=t-product class=hide>
  <div class=card>
    <h2>Products</h2>
    <p class=hint style="margin-top:0">A project can hold several products, and each
      product several customer segments. Product truth is what is true whatever you
      sell it for; who it is for, what it solves and how to position it belong to
      each segment, and come from research rather than from guessing here. The picc,
      concepts and brief stages read what you approve.</p>
    <div id=prodlist>loading…</div>
    <div style="margin-top:18px;border-top:1.5px solid var(--line);padding-top:16px">
      <h3 style="font-size:14px;margin:0 0 4px">New product</h3>
      <p class=hint style="margin:0 0 12px">Only what you already know before any
        research — what the thing is, what it is made of, what it does not do, what
        it costs and what you may claim. Who it is for and how to position it are
        discovered later, per segment. You can fill any of this in now or after
        creating.</p>
      <label>Project</label>
      <div style="display:flex;gap:9px;align-items:center">
        <select id=np_proj style=flex:1></select>
        <input id=np_key placeholder="new-project-key" class=hide style=flex:1>
      </div>
      <p class=hint id=np_projwhy style="margin:5px 0 0">Put this product in a
        project that already holds research and its segments become importable
        straight away.</p>
      <div id=np_form style="margin-top:12px"></div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
        <button class=btn id=np_go>Create product</button>
        <span class=hint id=np_msg style="margin:0">Nothing is copied from another
          product — no filters, facts or claims.</span>
      </div>
    </div>
  </div>

  <div class=card id=sheetcard hidden>
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px">
      <div>
        <h2 style="margin:0" id=sheettitle>Product</h2>
        <p class=hint style="margin:4px 0 0" id=sheetstate></p>
      </div>
      <div style="display:flex;gap:9px;white-space:nowrap">
        <button class="btn ghost" id=sheetclose>Close</button>
        <button class=btn id=sheetsave>Save</button>
      </div>
    </div>

    <div class=tabs style="margin:14px 0 0;gap:6px">
      <button class="tab on" data-pt=truth>Product truth</button>
      <button class=tab data-pt=segments>Segments</button>
      <button class=tab data-pt=ready>Readiness</button>
    </div>

    <div id=pt-truth>
      <p class=hint>Layer 1 — true regardless of who it is sold to. Nothing
        customer-related belongs here; that lives on each segment.</p>
      <div id=sheetmissing></div>
      <div id=sheetform style="margin-top:12px"></div>
    </div>

    <div id=pt-segments class=hide>
      <p class=hint>Layers 2 and 3 — each segment carries its own research and its
        own strategy. A product legitimately has several, with different problems
        and different positioning.</p>
      <div style="display:flex;gap:9px;align-items:center;margin:10px 0">
        <input id=segnew placeholder="new segment name" style="flex:1">
        <button class="btn ghost" id=segadd style=white-space:nowrap>Add segment</button>
        <button class="btn ghost" id=segimport style=white-space:nowrap>Import from pipeline</button>
      </div>
      <p class=hint id=segmsg style="margin:0 0 10px"></p>
      <div id=segpick hidden style="border:1.5px solid var(--accent);border-radius:11px;
        padding:13px 15px;margin-bottom:12px"></div>

      <div style="border:1.5px solid var(--line);border-radius:11px;padding:13px 15px;
        margin-bottom:14px">
        <b style=font-size:13.5px>Enrich from research</b>
        <p class=hint style="margin:5px 0 10px">Reads this segment's extraction
          outputs and proposes values for its research fields. Evidence only — it
          records what customers said, never what to do about it. Every suggestion
          arrives as <i>ai&nbsp;suggested</i> for you to accept, edit or reject, and
          fields you have already approved are left alone.</p>
        <div class=row style="margin-bottom:9px">
          <div><label>Segment</label><select id=en_seg></select></div>
          <div><label>&nbsp;</label>
            <div style="display:flex;gap:8px">
              <button class="btn ghost" id=en_preview style="flex:1;white-space:nowrap">Preview prompt</button>
              <button class=btn id=en_go style="flex:1;white-space:nowrap">Enrich</button>
            </div></div>
        </div>
        <label>Sections to populate</label>
        <div id=en_sections style="display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 8px"></div>
        <a href=# id=en_all style="font-size:13px;color:var(--accent)">select all recommended</a>
        <p class=hint id=en_msg style="margin:9px 0 0"></p>
        <div id=en_review style="margin-top:12px"></div>
      </div>

      <div style="border:1.5px solid var(--line);border-radius:11px;padding:13px 15px;
        margin-bottom:14px">
        <b style=font-size:13.5px>Product × segment strategy</b>
        <p class=hint style="margin:5px 0 10px">Turns product truth and this
          segment's <b>approved</b> research into positioning, primary benefit,
          reason to buy, reason to believe, differentiation, creative territories
          and landing page strategy. Fields still sitting as unreviewed suggestions
          are excluded and listed — strategy built on unreviewed evidence is what
          running §6 first exists to prevent.</p>
        <div class=row style="margin-bottom:9px">
          <div><label>Segment</label><select id=sy_seg></select></div>
          <div><label>&nbsp;</label>
            <div style="display:flex;gap:8px">
              <button class="btn ghost" id=sy_preview style="flex:1;white-space:nowrap">Preview prompt</button>
              <button class=btn id=sy_go style="flex:1;white-space:nowrap">Synthesise</button>
            </div></div>
        </div>
        <label>Sections to produce</label>
        <div id=sy_sections style="display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 8px"></div>
        <a href=# id=sy_all style="font-size:13px;color:var(--accent)">select all</a>
        <p class=hint id=sy_msg style="margin:9px 0 0"></p>
        <div id=sy_review style="margin-top:12px"></div>
      </div>

      <div id=seglist></div>
    </div>

    <div id=pt-ready class=hide>
      <p class=hint>What each stage can actually run on. Readiness differs per
        segment on purpose — a barely-researched segment must not inherit a
        well-researched one's green light.</p>
      <div id=readybox></div>
    </div>

    <div style="margin-top:16px;display:flex;gap:10px;align-items:center">
      <button class=btn id=sheetsave2>Save</button>
      <span class=hint id=sheetmsg style="margin:0"></span>
    </div>
  </div>
</section>

<!-- ================= PIPELINE ================= -->
<section id=t-pipeline class=hide>
  <div class=card>
    <h2>Run a stage</h2>
    <div class=row style="margin-bottom:16px">
      <div><label>Project</label><select id=proj></select></div>
      <div><label>Segment</label><select id=seg></select></div>
      <div id=prodwrap class="hide"><label>Product</label><select id=prod></select></div>
    </div>
    <div class=settingsbar>
      <button class=ghost id=settingstoggle type=button>Project settings</button>
      <span class=hint id=settingspath></span>
    </div>
    <div id=settingsbox class=hide>
      <div id=settingsfields></div>
      <div class=settingsactions>
        <button id=settingssave type=button>Save settings</button>
        <span id=settingsmsg class=hint></span>
      </div>
    </div>
    <div class=stagelist id=stages></div>
    <div id=ingestbox class="hide" style="margin-top:16px">
      <label>Raw VOC file</label>
      <div class=drop id=vocdrop>
        <input type=file id=vocfile accept=".txt,.jsonl,.json,.csv,.md" hidden>
        <div id=vocmsg>Drop a VOC dump here, or click to choose</div>
      </div>
      <p class=hint id=vocstate></p>
      <label style="margin-top:12px">…or a path already on disk</label>
      <input type=text id=ingestpath placeholder="/full/path/to/raw_voc.txt">
      <label style="font-weight:500;font-size:14px;display:flex;gap:8px;align-items:center;margin:10px 0 0">
        <input type=checkbox id=rulesonly style="width:auto"> Rules only — skip skills 01/02 (free, no judgement)
      </label>
      <p class=hint>Full path to the exported Reddit dump.</p>
    </div>
    <div id=importbox class="hide" style="margin-top:16px">
      <label>Audience files from a run done elsewhere</label>
      <div class=drop id=impdrop>
        <input type=file id=impfile accept=".zip,.md" hidden>
        <div id=impmsg>Drop the stage&nbsp;06 audience-file zip here, or click to choose</div>
      </div>
      <p class=hint id=impstate></p>
      <div id=impplan class=hide></div>
      <p class=hint>Stages 01&ndash;06 run perfectly well pasted into a chat. This adopts
        what came back &mdash; one markdown file per audience &mdash; as the evidence files
        stage&nbsp;06 would have written, so <code>extract</code> onwards can read them.
        Every one is recorded as <b>imported</b>, and every later stage says so before it runs.</p>
    </div>
    <div id=segvocbox class="hide" style="margin-top:16px">
      <label>VOC source for segmentation</label>
      <select id=segvoc style="width:100%">
        <option value="">Loading final ingest file…</option>
      </select>
      <p class=hint>Only completed, segment-ready ingest files are shown. Audit and
        intermediate files remain available on the Outputs tab.</p>
    </div>
    <div id=refinevocbox class="hide" style="margin-top:16px">
      <label>VOC file to refine</label>
      <select id=refinevoc style="width:100%">
        <option value="">Loading refinable VOC files…</option>
      </select>
      <p class=hint>Choose a project JSONL containing <code>id</code>,
        <code>text</code>, and Stage 01 reasons or an existing tier. The recommended
        input is the Stage 02 deduplicated file;
        refinement rewrites <code>production_voc.jsonl</code> and
        <code>audit_voc.jsonl</code>.</p>
    </div>
    <div id=opt_model style="margin-top:16px">
      <div class=row>
        <div><label>Provider</label><select id=provider>
          <option value=openrouter selected>OpenRouter</option>
          <option value=anthropic>Anthropic</option></select></div>
        <div><label>Model</label><input id=modelid value="deepseek/deepseek-v4-flash"></div>
      </div>
      <p class=hint id=modelhint></p>
    </div>
    <div id=opt_extract class=opts style="margin-top:14px" hidden>
      <label>Research depth</label>
      <select id=preset>
        <option value=fast selected>Fast Test — 10 core dimensions</option>
        <option value=standard>Standard — 18 dimensions</option>
        <option value=deep>Deep Research — all 20 extraction skills (07–26)</option>
      </select>
      <p class=hint id=extracthint></p>
    </div>
    <div id=opt_extract_single class=opts style="margin-top:14px" hidden>
      <label>Individual skill rerun</label>
      <select id=extractskill>
        <option value="">Use the research depth preset above</option>
      </select>
      <p class=hint>Selecting one skill runs only that extractor and overwrites its
        existing output. Empty responses retry immediately three times and are never
        written as 0-byte files.</p>
    </div>
    <div id=opt_concepts class=opts style="margin-top:14px" hidden>
      <div class=row>
        <div><label>Concepts</label><input type=number id=nconcepts value=10 min=1 max=30></div>
        <div><label>Hooks each</label><input type=number id=nhooks value=3 min=1 max=6></div>
      </div>
      <label style="margin-top:12px">PICC card to build on</label>
      <select id=piccsel></select>
      <p class=hint id=piccwhy>The concepts stage builds on exactly one card. Pick
        it rather than letting the segment's own card be assumed — the card decides
        the strategy every concept inherits.</p>
    </div>
    <div id=opt_brief class=opts style="margin-top:14px" hidden>
      <label>Production briefs</label><input type=number id=nbriefs value=4 min=1 max=10>
    </div>
    <div style="margin-top:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <button class=btn id=runbtn disabled>Run stage</button>
      <label style="font-weight:500;font-size:14px;display:flex;gap:8px;align-items:center;margin:0">
        <input type=checkbox id=approve style="width:auto"> I approve any API spend for this run
      </label>
      <label style="font-weight:500;font-size:14px;display:flex;gap:8px;align-items:center;margin:0">
        <input type=checkbox id=force style="width:auto"> Force redo existing outputs
      </label>
    </div>
    <p class=hint style="margin-top:8px">Force redo replaces outputs that would otherwise
      be skipped because their files already exist.</p>
    <p class=hint id=runhint>Pick a stage. Stages marked COSTS call the Claude API.</p>
  </div>
  <div class=card>
    <h2>Output</h2><pre class=log id=log>Nothing run yet.</pre>
  </div>
</section>

<!-- ================= LIBRARY ================= -->
<section id=t-library class=hide>
  <div class=card>
    <h2>Reference layouts</h2>
    <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:12px">
      <div style=flex:1;min-width:170px><label>Category</label><select id=lcat></select></div>
      <div><label>Import into</label><select id=limportcat></select></div>
      <button class="btn ghost" id=limportbtn>Import…</button>
      <input type=file id=limportfile accept="image/*" multiple hidden>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <button class="btn ghost" id=ldupes>Show duplicates</button>
      <button class="btn ghost" id=lselall>Select all shown</button>
      <button class="btn ghost" id=lselnone>Clear selection</button>
      <button class="btn ghost" id=lconvert>Convert AVIF/HEIC → JPEG</button>
      <button class=btn id=ldelete disabled style="background:var(--signal)">Delete selected</button>
    </div>
    <p class=hint id=lstate></p>

    <div class=grid style="grid-template-columns:1fr 330px;align-items:start">
      <div class=thumbs id=lgrid style="max-height:640px;overflow:auto"></div>

      <div class=card id=lside style="margin:0;position:sticky;top:12px">
        <div id=lpreview><p class=hint>Click an image to inspect it.</p></div>
      </div>
    </div>
  </div>
</section>

<!-- ================= OUTPUTS ================= -->
<section id=t-outputs class=hide>
  <div class=card>
    <h2>Every artefact this project has produced</h2>
    <div class=row style="margin-bottom:14px">
      <div><label>Project</label><select id=oproj></select></div>
      <div style="display:flex;align-items:flex-end"><button class="btn ghost" id=orefresh>Refresh</button></div>
    </div>
    <div class=row style="margin-bottom:14px;align-items:flex-end">
      <div><label>Model logs</label><select id=logstage></select></div>
      <div style="display:flex;align-items:flex-end">
        <button class="btn ghost" id=logexport>Export logs</button></div>
    </div>
    <p class=hint id=logmsg style="margin:-6px 0 14px">One folder per model
      request — what was sent, what came back (usage and finish reason included),
      and any retries — plus a manifest listing them. This is what to attach when
      a stage fails.</p>
    <div id=provwarn></div>
    <div class=grid style="grid-template-columns:340px 1fr">
      <div class=cats id=olist style=max-height:600px></div>
      <div id=oview class=card style="margin:0;min-height:420px">
        <p class=hint>Pick an artefact on the left.</p></div>
    </div>
  </div>
</section>

<!-- ================= SETTINGS ================= -->
<section id=t-settings class=hide>
  <div class=card style="max-width:760px">
    <h2>New project</h2>
    <div class=row>
      <div><label>Name</label><input id=npname placeholder=lumbar_cushion></div>
      <div><label>Research subject <span class=costs>optional</span></label>
        <input id=npproduct placeholder="lumbar support cushions"></div>
    </div>
    <label style=margin-top:10px>Market</label>
    <input id=npmarket placeholder="desk workers with lower back pain, UK/US">
    <button class=btn id=npbtn style=margin-top:14px>Create project</button>
    <p class=hint id=npmsg>Nothing is copied from another project — it starts blank, so
      <b>set the filter regexes and compliance profile</b> in
      <code>projects/&lt;name&gt;/project.json</code> before ingesting. Subject and market are the
      context the VOC stages are given; <b>no product is created</b> — run the research first,
      then add products in the Product tab whenever you are ready.</p>

    <h2 style="margin-top:26px">Existing projects</h2>
    <div id=plist></div>
    <p class=hint>Deleting archives the folder to <code>projects/_deleted/</code> —
      nothing is erased, and you can restore it by moving it back.</p>
  </div>
  <div class=card style="max-width:760px">
    <h2>API keys</h2>
    <p class=hint style="margin:0 0 16px">Saved in AdPipe's private user-level credential
      store, outside projects and Git. Environment variables override saved values. Saved
      keys are never returned to the browser.</p>

    <label>OpenAI — image generation (Remix tab)</label>
    <div class=keyrow>
      <input type=password id=k_openai placeholder="sk-...">
      <span class="pill no" id=p_openai>not set</span>
    </div>
    <p class=hint style="margin:0 0 18px">platform.openai.com/api-keys · needs pay-as-you-go
      credit (ChatGPT Plus does not include API access).</p>

    <label>Anthropic — the writing stages (Pipeline tab)</label>
    <div class=keyrow>
      <input type=password id=k_anthropic placeholder="sk-ant-...">
      <span class="pill no" id=p_anthropic>not set</span>
    </div>
    <p class=hint style="margin:0 0 18px">console.anthropic.com · only needed for extract /
      picc / concepts / brief. ingest, qa and render are free.</p>

    <label>OpenRouter — alternative for the writing stages</label>
    <div class=keyrow>
      <input type=password id=k_openrouter placeholder="sk-or-...">
      <span class="pill no" id=p_openrouter>not set</span>
    </div>
    <p class=hint style="margin:0 0 18px">openrouter.ai/keys · one key, many models
      (DeepSeek, Llama, Gemini…). Far cheaper per token, but <b>no prompt caching and no
      batch discount</b> — the corpus is re-sent on every request, so cost scales with
      requests x corpus rather than corpus + requests.</p>

    <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <button class=btn id=savekeys>Save keys</button>
      <button class="btn ghost" id=clearkeys>Clear</button>
    </div>
    <p class=hint id=keymsg></p>
  </div>
</section>

</div>

<!-- Nothing is sent until this is accepted. The text in the box IS what goes to
     the image model — edit it here and the edit is what gets sent. -->
<div id=mvwrap class=hide>
  <div id=mvbox>
    <h2 style="margin-top:0" id=mvtitle>Move product</h2>
    <p class=hint id=mvdesc style="margin-top:0"></p>
    <select id=mvsel style="width:100%;margin:6px 0 4px"></select>
    <p class=hint id=mvnote style="margin:6px 0 0"></p>
    <div style="display:flex;gap:10px;margin-top:16px;align-items:center">
      <button class=btn id=mvgo>Move</button>
      <button class="btn ghost" id=mvcancel>Cancel</button>
      <span class=hint id=mvmsg style="margin:0"></span>
    </div>
  </div>
</div>
<div id=pmwrap class=hide>
  <div id=pmbox>
    <h2 style="margin-top:0">Confirm the prompt</h2>
    <p class=hint id=pmsub style="margin-top:0"></p>
    <textarea id=pmtext spellcheck=false></textarea>
    <div style="display:flex;gap:10px;margin-top:14px;align-items:center">
      <button class=btn id=pmgo>Send it</button>
      <button class="btn ghost" id=pmcancel>Cancel</button>
      <span class=hint id=pmcost style="margin:0"></span>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
var LIB,LSEL,LDUPE,SKILLS,SKILLMAP={};

// ---------- tabs ----------
$$('.tab').forEach(b=>b.onclick=()=>{
  $$('.tab').forEach(x=>x.classList.toggle('on',x===b));
  // Derive the panel list from the buttons — a hardcoded array silently stops
  // switching the moment a tab is added.
  $$('.tab').forEach(x=>{
    const sec=$('#t-'+x.dataset.t);
    if(sec) sec.classList.toggle('hide', x!==b);
  });
  // Lazy-load, guarded: these helpers are defined later in the file.
  if(b.dataset.t==='library'  && typeof loadLibrary==='function') loadLibrary();
  if(b.dataset.t==='outputs'  && typeof loadOutputs==='function'){
    loadOutputs(); if(typeof loadLogStages==='function') loadLogStages(); }
  if(b.dataset.t==='settings' && typeof renderProjectList==='function') renderProjectList();
  if(b.dataset.t==='product' && typeof loadProducts==='function'){ loadProducts(); renderNewProduct(); loadNewProjectPicker(); }
});

// ---------- settings ----------
function setPill(which,on){ const p=$('#p_'+which);
  p.textContent=on?'set':'not set'; p.className='pill '+(on?'ok':'no'); }
async function pushKeys(){
  const body={openai:$('#k_openai').value.trim(),anthropic:$('#k_anthropic').value.trim(),
      openrouter:$('#k_openrouter').value.trim()};
  const r=await fetch('/keys',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const j=await r.json();
  if(!r.ok||j.error){ $('#keymsg').textContent='Could not save keys: '+(j.error||r.status); return false; }
  setPill('openai',j.openai); setPill('anthropic',j.anthropic);
  setPill('openrouter',j.openrouter);
  $('#k_openai').value=''; $('#k_anthropic').value=''; $('#k_openrouter').value='';
  $('#keymsg').textContent='Saved privately for Studio and CLI · '+new Date().toLocaleTimeString();
  return true;
}
$('#savekeys').onclick=pushKeys;
$('#clearkeys').onclick=async()=>{ $('#k_openai').value=''; $('#k_anthropic').value='';
  $('#k_openrouter').value='';
  const r=await fetch('/keys',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({clear:['openai','anthropic','openrouter']})});
  const j=await r.json();
  if(!r.ok||j.error){ $('#keymsg').textContent='Could not clear keys: '+(j.error||r.status); return; }
  localStorage.removeItem('adpipe_keys');
  setPill('openai',j.openai);setPill('anthropic',j.anthropic);setPill('openrouter',j.openrouter);
  $('#keymsg').textContent='Saved AdPipe keys cleared. Environment variables, if set, still override.'; };
(async function(){
  // Older Studio versions kept opt-in credentials in localStorage. Move them
  // to the backend once, and retain the old copy if persistence fails so an
  // existing user is never silently logged out.
  const legacy=localStorage.getItem('adpipe_keys');
  if(legacy){
    try{
      const k=JSON.parse(legacy);
      const r=await fetch('/keys',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({openai:k.openai||'',anthropic:k.anthropic||'',
          openrouter:k.openrouter||''})});
      const j=await r.json();
      if(!r.ok||j.error) throw new Error(j.error||String(r.status));
      localStorage.removeItem('adpipe_keys');
      $('#keymsg').textContent='Migrated your browser-saved keys to the private AdPipe store.';
    }catch(e){
      $('#keymsg').textContent='Could not migrate browser-saved keys. They remain in this browser; click Save keys to retry.';
    }
  }
  fetch('/keys').then(r=>r.json()).then(j=>{
    if(j.error){ $('#keymsg').textContent='Could not read saved keys: '+j.error; return; }
    setPill('openai',j.openai);setPill('anthropic',j.anthropic);
    setPill('openrouter',j.openrouter);});
})();

// ---------- remix ----------
/* Declared up here, not next to their own section, because refreshRemix() reads
   them and is called from handlers wired further up the file. */
let product=null, selected=new Map();
let LEVERS=null, BRIEFS=[], briefPick=new Set();
const drop=$('#drop'), file=$('#file'), preview=$('#preview');
drop.onclick=()=>file.click();
file.onchange=e=>loadFile(e.target.files[0]);
['dragover','dragenter'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>loadFile(e.dataTransfer.files[0]));
function loadFile(f){ if(!f) return; const r=new FileReader();
  r.onload=()=>{product=r.result;preview.src=r.result;preview.hidden=false;
    $('#dropmsg').classList.add('hide');refreshRemix();}; r.readAsDataURL(f); }

fetch('/references').then(r=>r.json()).then(refs=>{
  const c=$('#cats'); c.innerHTML='';
  for(const [cat,files] of Object.entries(refs)){
    const nice=cat.replace(/^\d+_/,'').replace(/_/g,' ');
    const box=document.createElement('div'); box.className='cat';
    box.innerHTML=`<h3>${nice} · ${files.length}</h3>`;
    const g=document.createElement('div'); g.className='thumbs';
    files.forEach(fn=>{ const rel=cat+'/'+fn;
      const t=document.createElement('div'); t.className='thumb';
      t.innerHTML=`<img loading=lazy src="/ref?path=${encodeURIComponent(rel)}&thumb=1">`;
      t.onclick=()=>{ if(selected.has(rel)){selected.delete(rel);t.classList.remove('sel');}
        else{selected.set(rel,nice);t.classList.add('sel');} refreshRemix(); };
      g.appendChild(t);});
    box.appendChild(g); c.appendChild(box);
  }
});
$('#clearsel').onclick=e=>{e.preventDefault();selected.clear();
  $$('.thumb.sel').forEach(t=>t.classList.remove('sel'));refreshRemix();};
function refreshRemix(){ $('#selcount').textContent=selected.size+' selected';
  const ok=product&&selected.size>0; $('#go').disabled=!ok;
  const nb=briefPick.size;
  const jobs=selected.size*(nb||1);
  $('#gohint').textContent=!ok
    ? 'Add a product photo and pick at least one template.'
    : nb ? `Will generate ${jobs} ad(s) — ${nb} brief(s) × ${selected.size} layout(s). You confirm the prompt first.`
         : `Will generate ${jobs} ad(s) from the brief box. You confirm the prompt first.`;
  refreshConflicts(); }

/* ---------- execution presets ---------- */
let PRESETS={}, cfTimer=null;
const cfMode=()=>{const r=document.querySelector('input[name=cfmode]:checked');
  return r?r.value:'reference';};

fetch('/presets').then(r=>r.json()).then(j=>{
  const sel=$('#preset_sel');
  if(j.error){ $('#presetwhy').textContent='⚠ '+j.error; return; }
  (j.groups||[]).forEach(g=>{
    const og=document.createElement('optgroup'); og.label=g.label;
    g.items.forEach(p=>{ PRESETS[p.id]=p;
      const o=document.createElement('option'); o.value=p.id;
      o.textContent=`${p.id} · ${p.name}`; og.appendChild(o); });
    sel.appendChild(og);
  });
});
$('#preset_sel').onchange=()=>{ describePreset(); refreshConflicts(); };
function describePreset(note){
  const p=PRESETS[$('#preset_sel').value], w=$('#presetwhy');
  if(!p){ w.textContent='No preset — the image model decides execution from your '+
    'brief and the reference alone.'; return; }
  w.innerHTML=(note?`<b style=color:var(--accent)>${note}</b><br>`:'')+
    `<b>${p.name}</b> — ${p.purpose}`+
    (p.reaction?`<br><i>Reader should think: “${p.reaction}”</i>`:'')+
    `<br><span style=color:var(--soft)>${p.visual['Visual Style']} · hero:
      ${p.visual['Hero Image Type']} · people: ${p.visual['Human Presence']} ·
      product: ${p.visual['Product Visibility']} · density:
      ${p.visual['Information Density']}</span>`;
}
/* Debounced: selecting layouts fires this on every click. */
function refreshConflicts(){ clearTimeout(cfTimer); cfTimer=setTimeout(doConflicts,180); }
async function doConflicts(){
  const box=$('#conflictbox'), id=$('#preset_sel').value;
  if(!id||!selected.size){ box.hidden=true; return; }
  const j=await (await fetch('/presets/conflicts',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({preset:id,references:[...selected.keys()]})})).json();
  const rows=Object.entries(j.conflicts||{});
  if(j.error||!rows.length){ box.hidden=true; return; }
  box.hidden=false;
  $('#cfhead').textContent=`⚠ Preset ${j.preset.id} ${j.preset.name} fights `+
    `${rows.length} of your ${selected.size} layout(s)`;
  /* Group by the clash, not by the file. The same two levers usually fight every
     layout in a category, and listing them once per file buries the point. */
  const agg=new Map();
  rows.forEach(([rel,cs])=>cs.forEach(c=>{
    const k=c.lever+'|'+c.note;
    if(!agg.has(k)) agg.set(k,{...c,files:[]});
    agg.get(k).files.push(rel.split('/').pop());
  }));
  $('#cflist').innerHTML=[...agg.values()].map(a=>
    `<div style=margin-bottom:7px>· <b>${a.lever}</b> (${a.value}) — ${a.note}
      <span style=color:var(--soft)>[${a.files.length} layout${a.files.length>1?'s':''}:
      ${a.files.slice(0,2).join(', ')}${a.files.length>2?', …':''}]</span></div>`).join('');
}
$('#presetauto').onclick=async()=>{
  const b=$('#presetauto'); b.disabled=true; const was=b.textContent;
  b.textContent='thinking…';
  try{
    const j=await (await fetch('/presets/pick',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:$('#rx_proj').value,segment:$('#rx_seg').value,
        brief:$('#brief').value.trim(),
        reference:selected.size?[...selected.keys()][0]:''})})).json();
    if(j.error){ $('#presetwhy').innerHTML=`<b style=color:var(--signal)>⚠ ${j.error}</b>`; }
    else{ $('#preset_sel').value=j.id;
      describePreset(`AI picked ${j.id} ${j.name} (${j.model}) — ${j.why}`);
      refreshConflicts(); }
  }catch(e){ $('#presetwhy').innerHTML=`<b style=color:var(--signal)>⚠ ${e}</b>`; }
  b.disabled=false; b.textContent=was;
};

/* ---------- levers from the pipeline ---------- */
fetch('/projects').then(r=>r.json()).then(j=>{
  const s=$('#rx_proj'); s.innerHTML='<option value="">—</option>';
  (j.projects||[]).forEach(p=>{const o=document.createElement('option');
    o.value=o.textContent=p; s.appendChild(o);});
  if((j.projects||[]).length===1){ s.value=j.projects[0]; s.onchange(); }
});
$('#rx_proj').onchange=async()=>{
  const p=$('#rx_proj').value, s=$('#rx_seg'); s.innerHTML='';
  LEVERS=null; renderLevers();
  if(!p){ $('#rx_levstate').textContent='Pick a project to load its extracted levers.'; return; }
  const j=await (await fetch('/segments?project='+encodeURIComponent(p))).json();
  const segs=j.segments||[];
  if(!segs.length){ $('#rx_levstate').textContent=
    'No segments in this project yet — run the segment stage on the Pipeline tab.'; return; }
  s.innerHTML='<option value="">—</option>';
  segs.forEach(x=>{const o=document.createElement('option');o.value=o.textContent=x;s.appendChild(o);});
  if(segs.length===1){ s.value=segs[0]; }
  s.onchange();
};
$('#rx_seg').onchange=async()=>{
  const p=$('#rx_proj').value, sg=$('#rx_seg').value;
  if(!p||!sg){ LEVERS=null; renderLevers(); return; }
  $('#rx_levstate').textContent='loading levers…';
  const j=await (await fetch(`/levers?project=${encodeURIComponent(p)}&segment=${encodeURIComponent(sg)}`)).json();
  if(j.error){ LEVERS=null; renderLevers(); $('#rx_levstate').textContent='⚠ '+j.error; return; }
  LEVERS=j; renderLevers();
  const parsed=j.dimensions.filter(d=>d.items.length).length;
  $('#rx_levstate').innerHTML=`${j.extracted}/${j.total} dimensions extracted · `+
    `<b>${parsed}</b> with selectable items`+
    (j.extracted?'':' — run <b>extract</b> on the Pipeline tab to fill these');
};

function renderLevers(){
  const box=$('#levbox');
  if(!LEVERS){ box.innerHTML='<p class=hint>No project loaded yet.</p>'; refreshRemix(); return; }
  box.innerHTML='';
  LEVERS.dimensions.forEach(d=>{
    const wrap=document.createElement('div'); wrap.className='lev';
    const req=d.required?'<span class=req>*</span> ':'';
    if(!d.present){
      wrap.innerHTML=`<label>${req}${d.label}</label>
        <small>not extracted — skill ${String(d.skill).padStart(2,'0')} hasn't run</small>`;
      box.appendChild(wrap); return;
    }
    if(!d.items.length){
      wrap.innerHTML=`<label>${req}${d.label}</label>
        <small>⚠ file present but no items could be read —
        <a href="/file?path=${encodeURIComponent(d.file)}" target=_blank>open it</a></small>`;
      box.appendChild(wrap); return;
    }
    const opts=d.items.map(i=>`<option value="${esc(i.name)}">${esc(i.name)}</option>`).join('');
    wrap.innerHTML=`<label>${req}${d.label} <span style="color:var(--soft);font-weight:400">
      (${d.items.length})</span></label>`+
      (d.multi?`<select multiple size=${Math.min(4,d.items.length)} data-dim="${d.key}">${opts}</select>`
             :`<select data-dim="${d.key}"><option value="">—</option>${opts}</select>`);
    const sel=wrap.querySelector('select');
    sel.onchange=()=>{ wrap.classList.toggle('set',!!picked(sel).length); refreshRemix(); };
    box.appendChild(wrap);
  });
  refreshRemix();
}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const picked=sel=>[...sel.selectedOptions].map(o=>o.value).filter(Boolean);
function chosenLevers(){
  const out={};
  $$('#levbox select[data-dim]').forEach(s=>{const v=picked(s); if(v.length) out[s.dataset.dim]=v;});
  return out;
}
function missingRequired(){
  if(!LEVERS) return [];
  const c=chosenLevers();
  return LEVERS.dimensions.filter(d=>d.required&&!(c[d.key]||[]).length).map(d=>d.label);
}

/* ---------- the 49 execution levers ---------- */
let LVSCHEMA=[];
fetch('/leverschema').then(r=>r.json()).then(j=>{
  LVSCHEMA=j.groups||[];
  const box=$('#lvbox');
  if(j.error){ box.innerHTML=`<p class=hint>⚠ ${j.error}</p>`; return; }
  box.innerHTML='';
  LVSCHEMA.forEach(g=>{
    const d=document.createElement('details'); d.className='lvg';
    d.innerHTML=`<summary>${g.label}<span class=lvn data-g="${esc(g.label)}"></span></summary>`;
    const body=document.createElement('div'); body.className='body';
    g.levers.forEach(l=>{
      const row=document.createElement('div'); row.className='lev';
      const id='lv_'+l.name.replace(/\W+/g,'_');
      if(l.kind==='choice'){
        row.innerHTML=`<label>${esc(l.name)}</label>
          <select id="${id}" data-lever="${esc(l.name)}"><option value="">— model decides</option>`+
          l.options.map(o=>`<option value="${esc(o)}">${esc(o)}</option>`).join('')+`</select>`;
      }else{
        row.innerHTML=`<label>${esc(l.name)}</label>
          <input id="${id}" data-lever="${esc(l.name)}" list="${id}_dl"
            placeholder="model decides"><datalist id="${id}_dl">`+
          l.options.slice(0,40).map(o=>`<option value="${esc(o)}">`).join('')+`</datalist>`;
      }
      const f=row.querySelector('[data-lever]');
      f.oninput=f.onchange=()=>{ row.classList.toggle('set',!!f.value.trim()); lvCount(); };
      body.appendChild(row);
    });
    d.appendChild(body); box.appendChild(d);
  });
  lvCount();
});
function customLevers(){
  const out={};
  $$('#lvbox [data-lever]').forEach(f=>{ const v=f.value.trim();
    if(v) out[f.dataset.lever]=v; });
  return out;
}
function lvCount(){
  const n=Object.keys(customLevers()).length;
  $('#lvcount').textContent=n?`${n} set`:'';
  LVSCHEMA.forEach(g=>{
    const c=g.levers.filter(l=>{const f=document.querySelector(`[data-lever="${CSS.escape(l.name)}"]`);
      return f&&f.value.trim();}).length;
    const badge=document.querySelector(`.lvn[data-g="${CSS.escape(g.label)}"]`);
    if(badge) badge.textContent=c?`${c} set`:'';
  });
}
$('#lvclear').onclick=e=>{e.preventDefault();
  $$('#lvbox [data-lever]').forEach(f=>{f.value='';f.closest('.lev').classList.remove('set');});
  lvCount();};
$('#lvfill').onclick=async e=>{e.preventDefault();
  const id=$('#preset_sel').value;
  if(!id){ alert('Pick an execution preset first — this copies its 49 values in so you can edit them.'); return; }
  const j=await (await fetch('/presets/levers',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({preset:id})})).json();
  if(j.error){ alert(j.error); return; }
  Object.entries(j.levers||{}).forEach(([k,v])=>{
    const f=document.querySelector(`[data-lever="${CSS.escape(k)}"]`);
    if(!f) return;
    if(f.tagName==='SELECT'&&![...f.options].some(o=>o.value===v)){
      const o=document.createElement('option'); o.value=o.textContent=v; f.appendChild(o);
    }
    f.value=v; f.closest('.lev').classList.add('set');
  });
  lvCount();};

/* ---------- write briefs from the levers ---------- */
$('#writebriefs').onclick=async()=>{
  const miss=missingRequired();
  if(!LEVERS){ $('#bfhint').textContent='Load a project and segment first.'; return; }
  if(miss.length){ $('#bfhint').textContent='Pick a '+miss.join(' and a ').toLowerCase()+' first.'; return; }
  const b=$('#writebriefs'); b.disabled=true; const was=b.textContent; b.textContent='writing…';
  try{
    const j=await (await fetch('/briefs',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:$('#rx_proj').value,segment:$('#rx_seg').value,
        chosen:chosenLevers(), preset:$('#preset_sel').value,
        levers:customLevers(), extra:$('#brief').value.trim()})})).json();
    if(j.error){ $('#bfhint').textContent='⚠ '+j.error; }
    else{ BRIEFS=j.briefs; briefPick=new Set(BRIEFS.map(x=>x.id)); renderBriefs(j);
      $('#bfhint').textContent=`${j.briefs.length} briefs from ${j.model}.`; }
  }catch(e){ $('#bfhint').textContent='⚠ '+e; }
  b.disabled=false; b.textContent=was;
};
function renderBriefs(meta){
  const card=$('#briefcard'), list=$('#brieflist');
  card.hidden=!BRIEFS.length; list.innerHTML='';
  $('#bfnote').textContent=BRIEFS.length
    ? 'Tick the ones to render. Each ticked brief is generated against each layout you picked.'
    : '';
  BRIEFS.forEach(b=>{
    const el=document.createElement('div'); el.className='bf on';
    el.innerHTML=`<h4><input type=checkbox checked data-bf="${esc(b.id)}">
      <span>${esc(b.id)} · ${esc(b.angle||'')}</span></h4>
      <p><b>${esc(b.headline||'')}</b></p>
      <p>${esc(b.subtext||'')}</p>
      <p style="color:var(--soft)">CTA: ${esc(b.cta||'')}</p>
      ${b.why?`<p class=vis>${esc(b.why)}</p>`:''}
      ${b.visual_brief?`<p class=vis>Plate: ${esc(b.visual_brief)}</p>`:''}`;
    const cb=el.querySelector('input');
    cb.onchange=()=>{ cb.checked?briefPick.add(b.id):briefPick.delete(b.id);
      el.classList.toggle('on',cb.checked); refreshRemix(); };
    list.appendChild(el);
  });
  refreshRemix();
}

/* ---------- generate, with a confirm step ---------- */
/* One job per (brief × layout). With no briefs written, the brief box is the
   single brief, so this is one job per layout exactly as before. */
function buildJobs(){
  const layouts=[...selected.entries()];
  const chosen=BRIEFS.filter(b=>briefPick.has(b.id));
  if(!chosen.length){
    const t=$('#brief').value.trim();
    return layouts.map(([rel,nice])=>({rel,nice,brief:t,label:nice}));
  }
  const out=[];
  chosen.forEach(b=>layouts.forEach(([rel,nice])=>
    out.push({rel,nice,brief:b.image_brief,label:`${b.id} · ${nice}`})));
  return out;
}

const SEP='\n\n════════ JOB %n% · %label% ════════\n\n';
const sepRe=/\n*════════ JOB \d+ · .*? ════════\n*/;

let pending=null;
$('#go').onclick=async()=>{
  const jobs=buildJobs();
  if(!jobs.length) return;
  const payload=j=>({project:$('#rx_proj').value,segment:$('#rx_seg').value,
    product,reference:j.rel,brief:j.brief,size:$('#size').value,
    preset:$('#preset_sel').value, conflict_mode:cfMode(), levers:customLevers(),
    strip_exif:$('#stripexif')?$('#stripexif').checked:false});
  $('#go').disabled=true;
  try{
    const texts=[];
    for(const j of jobs){
      const r=await (await fetch('/prompt',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(payload(j))})).json();
      if(r.error){ $('#gohint').textContent='⚠ '+r.error; $('#go').disabled=false; return; }
      texts.push(r.prompt);
    }
    pending={jobs,payload};
    $('#pmtext').value=texts.map((t,i)=>
      jobs.length>1?SEP.replace('%n%',i+1).replace('%label%',jobs[i].label).trimStart()+t:t
    ).join('\n');
    const nset=Object.keys(customLevers()).length;
    $('#pmsub').textContent=`${jobs.length} image${jobs.length>1?'s':''} · `+
      ($('#preset_sel').value?`preset ${$('#preset_sel').value} · `:'no preset · ')+
      `${nset} lever${nset===1?'':'s'} set explicitly. This is the exact text that will be sent — edit it here if you want.`;
    $('#pmcost').textContent='Nothing has been sent yet.';
    $('#pmwrap').classList.remove('hide');
  }catch(e){ $('#gohint').textContent='⚠ '+e; $('#go').disabled=false; }
};
$('#pmcancel').onclick=()=>{ $('#pmwrap').classList.add('hide'); pending=null; $('#go').disabled=false; };
$('#pmwrap').onclick=e=>{ if(e.target===$('#pmwrap')) $('#pmcancel').click(); };
$('#pmgo').onclick=async()=>{
  if(!pending) return;
  const {jobs,payload}=pending;
  /* Split the reviewed text back into per-job prompts, so what was on screen is
     what each job sends — including any edit just made in the box. */
  const raw=$('#pmtext').value;
  const parts=jobs.length>1?raw.split(sepRe).filter(s=>s.trim()):[raw];
  if(parts.length!==jobs.length){
    $('#pmcost').textContent=`⚠ the job separators were edited — expected ${jobs.length} sections, found ${parts.length}. Cancel and try again.`;
    return;
  }
  $('#pmwrap').classList.add('hide'); pending=null;
  $('#resultscard').hidden=false; const res=$('#results'); res.innerHTML='';
  const cards=jobs.map(j=>{ const el=document.createElement('div');
    el.className='res'; el.innerHTML=`<div class=spin>generating…<br><small>${esc(j.label)}</small></div>`;
    res.appendChild(el); return el;});
  for(let i=0;i<jobs.length;i++){
    const job=jobs[i], rel=job.rel, nice=job.label;
    try{
      const body={...payload(job), prompt:parts[i].trim()};
      const r=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)});
      const j=await r.json();
      if(j.error) cards[i].innerHTML=`<div class=err>✕ ${j.error}</div>`;
      else{ const nm=rel.split('/').pop().replace(/\.[^.]+$/,'');
        const badge = j.meta
          ? `<div style="font-size:11px;padding:5px 8px;border-radius:6px;margin-top:6px;
               background:${j.meta.stripped?'var(--accent-soft)':'var(--surface)'};
               color:${j.meta.stripped?'var(--accent)':'var(--soft)'}"
               title="Publishing hygiene only — does not remove C2PA credentials and does not defeat AI detection.">
               ${j.meta.stripped?'✓':'—'} ${j.meta.detail}</div>`
          : '';
        const pres = j.preset
          ? `<div style="font-size:11px;padding:5px 8px;border-radius:6px;margin-top:6px;
               background:var(--accent-soft);color:var(--accent)">▣ ${j.preset}</div>`
          : '';
        cards[i].innerHTML=`<img src="${j.image}"><div class=meta><span>${esc(nice)}</span>`+
          `<a download="remix_${nm}.png" href="${j.image}">download</a></div>${pres}${badge}`;}
    }catch(e){ cards[i].innerHTML=`<div class=err>✕ ${e}</div>`; }
  }
  $('#go').disabled=false;
};

// ---------- product ----------
/* Three layers, three editors: Product Truth is global to the product, each
   Segment carries its own customer truth and strategy, and readiness is derived
   from both. Field values are cells {value,state,source,...} so provenance
   survives a round-trip through the form. */
let PSCHEMA=null, PDOC=null, PSEGS=[], PPROJ='', PPRODUCT='', PPIPESEGS=[];

fetch('/product/schema').then(r=>r.json()).then(j=>{PSCHEMA=j; renderNewProduct();});
loadNewProjectPicker();

const cv=c=>(c&&typeof c==='object'&&'value' in c)?c.value:(c===undefined?'':c);
function setcv(c,v){ if(c&&typeof c==='object'&&'value' in c){ c.value=v;
  if(c.state==='empty'||!c.state) c.state='user_approved'; } return c; }

async function loadProducts(){
  const box=$('#prodlist');
  try{
    const j=await (await fetch('/products')).json();
    const rows=j.products||[];
    if(!rows.length){ box.innerHTML='<p class=hint>No products yet — projects start '+
      'without one. Run the research, then add a product below whenever you are ready.</p>';
      return; }
    box.innerHTML='<p class=hint style="margin:0 0 9px">Click a product to open '+
      'its sheet, segments and readiness.</p>';
    rows.forEach(p=>{
      const el=document.createElement('div'); el.className='stage';
      const pct=p.total?Math.round(100*p.answered/p.total):0;
      const verdict=p.verdict?`<span class=costs>${esc(p.verdict)}</span>`:'';
      const score=(p.score!==null&&p.score!==undefined)?` · score ${p.score}/10`:'';
      el.innerHTML=`<b>${esc(p.name||p.product)} ${verdict}</b>
        <small>${esc(p.project)} / ${esc(p.product)} · truth ${p.answered}/${p.total} (${pct}%)${score}
        · <b>${p.segments}</b> segment(s), ${p.active_segments} active
        ${p.missing_required?` · <b style=color:var(--signal)>${p.missing_required} required blank</b>`:''}</small>
        <button class="btn ghost" data-move style="margin-top:8px;padding:4px 10px;font-size:12px;float:right">Move to project…</button>
        ${p.definition?`<small style="display:block;margin-top:4px">${esc(p.definition)}</small>`:''}`;
      el.title='Open this product sheet';
      el.onclick=()=>openSheet(p.project,p.product);
      el.querySelector('[data-move]').onclick=async ev=>{
        ev.stopPropagation();
        const projs=(await (await fetch('/projects')).json()).projects||[];
        const avail=projs.filter(x=>x!==p.project);
        if(!avail.length){ alert('No other projects to move it into.'); return; }
        openMoveModal(p, avail);
      };
      box.appendChild(el);
    });
  }catch(e){ box.innerHTML=`<p class=hint>⚠ ${e}</p>`; }
}

/* ---- move-product modal ---- */
let MVMOVE=null;   // {project, product, name, avail[]} current move target
function openMoveModal(p, avail){
  MVMOVE=null;
  $('#mvtitle').textContent=`Move "${p.name||p.product}" to a project`;
  $('#mvdesc').textContent=`Currently in ${p.project}. Its segments keep their research links.`;
  const sel=$('#mvsel'); sel.innerHTML=avail.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');
  $('#mvnote').textContent='Segments pointing at research here stay pointed at it.';
  $('#mvmsg').textContent='';
  MVMOVE={project:p.project, product:p.product, name:p.name||p.product};
  $('#mvwrap').classList.remove('hide');
}
$('#mvcancel').onclick=()=>{ $('#mvwrap').classList.add('hide'); MVMOVE=null; };
$('#mvwrap').onclick=e=>{ if(e.target===$('#mvwrap')) $('#mvcancel').click(); };
$('#mvgo').onclick=async()=>{
  if(!MVMOVE) return;
  const to=$('#mvsel').value; if(!to){ $('#mvmsg').textContent='Pick a destination.'; return; }
  $('#mvgo').disabled=true; $('#mvmsg').textContent='moving…';
  try{
    const r=await (await fetch('/product/move',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:MVMOVE.project, product:MVMOVE.product, to})})).json();
    if(r.error){ $('#mvmsg').textContent='⚠ '+r.error; $('#mvgo').disabled=false; return; }
    $('#mvwrap').classList.add('hide'); MVMOVE=null;
    loadProducts();
  }catch(e){ $('#mvmsg').textContent='⚠ '+e; }
  $('#mvgo').disabled=false;
};
async function openSheet(proj,prod){
  const j=await (await fetch(`/product?project=${encodeURIComponent(proj)}&product=${encodeURIComponent(prod||'')}`)).json();
  if(j.error){ alert(j.error); return; }
  PPROJ=proj; PPRODUCT=j.product; PDOC=j.doc; PSEGS=j.segments||[];
  PPIPESEGS=j.pipeline_segments||[];
  $('#sheetcard').hidden=false;
  $('#sheettitle').textContent=cv(PDOC.identity.name)||j.product;
  renderTruth(); renderSegments(); renderReady(j.readiness);
  updateSheetState(j.answered,j.total,j.missing_required);
  $('#sheetcard').scrollIntoView({behavior:'smooth',block:'start'});
}
$('#sheetclose').onclick=()=>{ $('#sheetcard').hidden=true; PDOC=null; PSEGS=[]; };

$$('[data-pt]').forEach(b=>b.onclick=()=>{
  $$('[data-pt]').forEach(x=>x.classList.toggle('on',x===b));
  ['truth','segments','ready'].forEach(k=>
    $('#pt-'+k).classList.toggle('hide',k!==b.dataset.pt));
});

function updateSheetState(a,t,missing){
  $('#sheetstate').textContent=`${PPROJ} / ${PPRODUCT} · product truth ${a}/${t} · ${PSEGS.length} segment(s)`;
  const m=$('#sheetmissing');
  if(!missing||!missing.length){ m.innerHTML=''; return; }
  m.innerHTML=`<div style="background:#FFF8F1;border:1px solid #E0A87A;border-radius:11px;
    padding:12px 14px"><b style=font-size:13.5px>${missing.length} required product fact(s) still blank</b>
    <div style="font-size:13px;margin-top:6px;line-height:1.6">${missing.map(esc).join('<br>')}</div>
    <div class=hint style="margin:8px 0 0">These are Product Truth. Until they are
      answered the pipeline treats them as unknown — and an unanswered fact cannot
      license a claim.</div></div>`;
}

function renderTruth(){
  const box=$('#sheetform'); box.innerHTML='';
  (PSCHEMA.product||[]).forEach(sec=>{
    box.appendChild(sectionEl(sec, PDOC[sec.key], sec.stage==='create'));
  });
}

/* One collapsible section, generic over the schema. */
function sectionEl(sec, vals, openByDefault){
  const d=document.createElement('details'); d.className='lvg';
  const done=sec.fields.filter(f=>{const v=cv(vals[f.key]);
    return Array.isArray(v)?v.length:String(v||'').trim();}).length;
  d.innerHTML=`<summary>${esc(sec.title)}
    <span class=lvn>${done}/${sec.fields.length}</span></summary>`;
  const body=document.createElement('div'); body.className='body';
  if(sec.help) body.innerHTML=`<p class=hint style="margin:0 0 12px">${esc(sec.help)}</p>`;
  sec.fields.forEach(f=>body.appendChild(cellRow(vals,f)));
  d.appendChild(body);
  if(openByDefault&&done<sec.fields.length) d.open=true;
  return d;
}

function cellRow(vals,f){
  const row=document.createElement('div'); row.className='lev';
  const c=vals[f.key], val=cv(c);
  const req=f.required?' <span class=req>*</span>':'';
  const state=(c&&c.state)||'empty';
  const chip=state!=='empty'&&state!=='user_approved'
    ? ` <span class=lvn>${esc(state.replace(/_/g,' '))}</span>`:'';
  row.innerHTML=`<label>${esc(f.label)}${req}${chip}</label>`;
  let ctl;
  if(f.kind==='table'){
    const t=document.createElement('div'); const cols=f.columns;
    if(!Array.isArray(cv(c))||!cv(c).length)
      setcv(c,(f.rows_hint||[]).map(h=>[h,...cols.slice(1).map(()=>'')]));
    const draw=()=>{
      const rows=cv(c)||[];
      t.innerHTML=`<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr>${cols.map(x=>`<th style="text-align:left;padding:4px 6px;border-bottom:1.5px solid var(--line)">${esc(x)}</th>`).join('')}<th></th></tr></thead>
        <tbody>${rows.map((r,i)=>`<tr>${cols.map((x,ci)=>
          `<td style="padding:3px 4px"><input data-tr="${i}" data-tc="${ci}" value="${esc(r[ci]||'')}"></td>`).join('')}
          <td><a href=# data-del="${i}" style="color:var(--soft)">×</a></td></tr>`).join('')}</tbody></table></div>
        <a href=# data-add style="font-size:13px;color:var(--accent)">+ add row</a>`;
      t.querySelectorAll('input[data-tr]').forEach(inp=>inp.oninput=()=>{
        cv(c)[+inp.dataset.tr][+inp.dataset.tc]=inp.value; if(c.state==='empty')c.state='user_approved';});
      t.querySelectorAll('[data-del]').forEach(a=>a.onclick=e=>{e.preventDefault();
        cv(c).splice(+a.dataset.del,1); draw();});
      t.querySelector('[data-add]').onclick=e=>{e.preventDefault();
        cv(c).push(cols.map(()=>'')); draw();};
    };
    draw(); row.appendChild(t); return row;
  }
  if(f.kind==='choice'){
    ctl=`<select><option value="">—</option>`+
      f.options.map(o=>`<option${val===o?' selected':''}>${esc(o)}</option>`).join('')+`</select>`;
  }else if(f.kind==='score'){
    ctl=`<input type=number min=0 max=10 value="${esc(val||'')}">`;
  }else if(f.kind==='list'){
    ctl=`<textarea data-list=1 rows=3 placeholder="${esc(f.placeholder||'one per line')}">${esc((val||[]).join('\n'))}</textarea>`;
  }else if(f.kind==='long'){
    ctl=`<textarea rows=3 placeholder="${esc(f.placeholder||'')}">${esc(val||'')}</textarea>`;
  }else{
    ctl=`<input value="${esc(val||'')}" placeholder="${esc(f.placeholder||'')}">`;
  }
  row.insertAdjacentHTML('beforeend',ctl);
  const el=row.querySelector('input,textarea,select');
  el.oninput=el.onchange=()=>{
    setcv(c, el.dataset.list?el.value.split('\n').map(x=>x.trim()).filter(Boolean):el.value);
    row.classList.toggle('set',!!(el.dataset.list?cv(c).length:el.value.trim()));
  };
  row.classList.toggle('set',!!(Array.isArray(val)?val.length:String(val||'').trim()));
  return row;
}

/* ---- segments ---- */
function blankSegDoc(){
  const d={};
  (PSCHEMA.segment||[]).forEach(sec=>{ d[sec.key]={};
    sec.fields.forEach(f=>d[sec.key][f.key]=
      {value:(f.kind==='list'||f.kind==='table')?[]:'',state:'empty',source:'',ref:'',confidence:''});});
  return d;
}
function renderSegments(){
  renderEnrichPanel(); renderSynthPanel();
  const box=$('#seglist'); box.innerHTML='';
  if(!PSEGS.length){ box.innerHTML='<p class=hint>No segments yet. Research discovers '+
    'these — add one, or import a validated segment from the pipeline.</p>'; return; }
  PSEGS.forEach((seg,i)=>{
    const name=cv(seg.doc.identity.name)||seg.slug;
    const status=cv(seg.doc.identity.status)||'Discovered';
    const compat=cv(seg.doc.compatibility.status)||'';
    const d=document.createElement('details'); d.className='lvg';
    d.innerHTML=`<summary>${esc(name)}
      <span class=lvn>${esc(status)}${compat?' · '+esc(compat):''}</span></summary>`;
    const body=document.createElement('div'); body.className='body';
    if(compat==='Incompatible') body.innerHTML=
      `<p class=hint style="color:var(--signal);margin:0 0 10px"><b>Marked incompatible</b>
       — PICC and concepts are blocked for this pair.</p>`;
    (PSCHEMA.segment||[]).forEach(sec=>
      body.appendChild(sectionEl(sec, seg.doc[sec.key], sec.key==='identity')));
    const rm=document.createElement('a'); rm.href='#'; rm.textContent='remove this segment';
    rm.style.cssText='color:var(--soft);font-size:13px';
    rm.onclick=e=>{e.preventDefault();
      if(confirm(`Remove segment "${name}"? Its research files are not deleted.`)){
        PSEGS.splice(i,1); renderSegments(); }};
    body.appendChild(rm);
    d.appendChild(body); box.appendChild(d);
  });
}
$('#segadd').onclick=()=>{
  const n=$('#segnew').value.trim();
  if(!n){ $('#segmsg').textContent='Give the segment a name.'; return; }
  const doc=blankSegDoc();
  doc.identity.name={value:n,state:'user_approved',source:'user',ref:'',confidence:''};
  doc.identity.status={value:'Discovered',state:'user_approved',source:'user',ref:'',confidence:''};
  PSEGS.push({slug:'',doc}); $('#segnew').value='';
  $('#segmsg').textContent=`added "${n}" — save to persist`; renderSegments();
};
/* The pipeline already discovers segments; importing one links the Product
   segment to the evidence and extractions that already exist for it — including
   research done under a different project, which is normal when a product is
   created after the research rather than before it. */
$('#segimport').onclick=()=>{
  const have=new Set(PSEGS.map(s=>
    (cv(s.doc.identity.evidence_project)||PPROJ)+'/'+cv(s.doc.identity.evidence_slug))
    .filter(x=>!x.endsWith('/')));
  const avail=(PPIPESEGS||[]).filter(s=>!have.has(s.project+'/'+s.slug));
  if(!avail.length){ $('#segmsg').textContent=
    (PPIPESEGS||[]).length?'Every pipeline segment is already linked.'
      :'No pipeline segments found in any project — run ingest and the segment '+
       'stage first.'; return; }
  const box=$('#segpick');
  box.hidden=false;
  const byProj={};
  avail.forEach(s=>(byProj[s.project]=byProj[s.project]||[]).push(s.slug));
  box.innerHTML='<b style=font-size:13.5px>Import a researched segment</b>'+
    '<p class=hint style="margin:4px 0 9px">Segments found across every project. '+
    'Importing keeps the research where it is and points this product at it.</p>'+
    Object.entries(byProj).map(([pr,slugs])=>
      `<div style="margin-bottom:8px"><div style="font-size:12.5px;color:var(--soft);
        font-weight:600">${esc(pr)}${pr===PPROJ?' (this product\'s project)':''}</div>`+
      slugs.map(sl=>`<label style="font-weight:500;font-size:13px;display:flex;gap:7px;
        align-items:center;margin:3px 0 0">
        <input type=checkbox data-imp="${esc(pr)}|${esc(sl)}" style="width:auto">
        ${esc(sl)}</label>`).join('')+'</div>').join('')+
    '<div style="display:flex;gap:9px;margin-top:10px">'+
    '<button class=btn id=impgo style="padding:7px 15px;font-size:13px">Import selected</button>'+
    '<button class="btn ghost" id=impcancel style="padding:7px 15px;font-size:13px">Cancel</button></div>';
  $('#impcancel').onclick=()=>{ box.hidden=true; };
  $('#impgo').onclick=()=>{
    const picked=$$('#segpick [data-imp]:checked').map(c=>c.dataset.imp);
    if(!picked.length){ $('#segmsg').textContent='Tick at least one.'; return; }
    picked.forEach(k=>{
      const [pr,sl]=k.split('|');
      const doc=blankSegDoc();
      const rd=v=>({value:v,state:'research_derived',source:'research',ref:'',confidence:''});
      doc.identity.name=rd(sl.replace(/[-_]/g,' '));
      doc.identity.evidence_slug=rd(sl);
      doc.identity.evidence_project=rd(pr);
      doc.identity.status=rd('Discovered');
      PSEGS.push({slug:'',doc});
    });
    box.hidden=true;
    $('#segmsg').textContent=`imported ${picked.length} segment(s) — review and save`;
    renderSegments();
  };
};

/* ---- §6 enrich from research ---- */
let ENSUG=null;

function renderEnrichPanel(){
  const sel=$('#en_seg'); if(!sel) return;
  const was=sel.value;
  sel.innerHTML=PSEGS.map((s,i)=>{
    const n=cv(s.doc.identity.name)||s.slug;
    return `<option value="${esc(s.slug||('#'+i))}">${esc(n)}${s.slug?'':' (unsaved)'}</option>`;
  }).join('')||'<option value="">— no segments —</option>';
  /* Only restore a selection that still exists: after a save assigns real slugs
     the placeholder value no longer matches any option, and setting it blanks
     the select — which then reads as "no segment chosen" to enrich/synthesise. */
  if(was&&[...sel.options].some(o=>o.value===was)) sel.value=was;
  const box=$('#en_sections');
  if(!box.children.length&&PSCHEMA&&PSCHEMA.enrich_sections){
    box.innerHTML=PSCHEMA.enrich_sections.map(s=>
      `<label style="font-weight:500;font-size:13px;display:flex;gap:6px;align-items:center;margin:0">
        <input type=checkbox checked data-en="${esc(s.key)}" style="width:auto">
        ${esc(s.title)} <span style=color:var(--soft)>(${s.fields})</span></label>`).join('');
  }
}
$('#en_all').onclick=e=>{e.preventDefault();
  $$('#en_sections [data-en]').forEach(c=>c.checked=true);};

function enSections(){ return $$('#en_sections [data-en]:checked').map(c=>c.dataset.en); }

async function runEnrich(dry){
  const slug=$('#en_seg').value;
  if(!slug||slug.startsWith('#')){ $('#en_msg').textContent=
    'Save the segment first — enrichment reads its saved research link.'; return; }
  const secs=enSections();
  if(!secs.length){ $('#en_msg').textContent='Pick at least one section.'; return; }
  const btn=dry?$('#en_preview'):$('#en_go'); btn.disabled=true;
  $('#en_msg').textContent=dry?'building prompt…':'reading research…';
  try{
    const j=await (await fetch('/product/enrich',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:PPROJ,product:PPRODUCT,segment:slug,
                           sections:secs,dry_run:!!dry})})).json();
    if(j.error){ $('#en_msg').textContent='⚠ '+j.error; btn.disabled=false; return; }
    if(dry){
      $('#pmtext').value=j.prompt;
      $('#pmsub').textContent=`${j.asked} field(s) would be proposed`+
        (j.skipped.length?`; ${j.skipped.length} already approved and left alone.`:'.');
      $('#pmcost').textContent='Preview only — nothing sent.';
      pending=null; $('#pmgo').disabled=true; $('#pmwrap').classList.remove('hide');
      $('#en_msg').textContent='';
    }else{
      ENSUG={slug,data:j};
      /* Apply immediately as ai_suggested. The spec stores proposals in that
         state, so a review you have not finished survives a save instead of
         being silently dropped; accept promotes, reject clears. */
      const seg0=segBySlug(slug);
      if(seg0) Object.entries(j.suggestions||{}).forEach(([sk,fs])=>
        Object.entries(fs).forEach(([fk,cell])=>{ seg0.doc[sk][fk]={...cell}; }));
      renderSegments();
      $('#en_msg').textContent=`${j.model} proposed values for `+
        `${Object.values(j.suggestions).reduce((n,o)=>n+Object.keys(o).length,0)} field(s)`+
        (j.skipped.length?`; ${j.skipped.length} left alone (already approved).`:'.');
      renderSuggestions();
    }
  }catch(e){ $('#en_msg').textContent='⚠ '+e; }
  btn.disabled=false;
}
$('#en_preview').onclick=()=>runEnrich(true);
$('#en_go').onclick=()=>runEnrich(false);

function segBySlug(slug){ return PSEGS.find(s=>s.slug===slug); }
function fieldMeta(seckey,fkey){
  const sec=(PSCHEMA.segment||[]).find(s=>s.key===seckey);
  return [sec, sec&&sec.fields.find(f=>f.key===fkey)];
}

/* Shared by §6 and §7 — the review contract is identical: proposals arrive as
   ai_suggested, and Accept / Edit / Reject is the only way they become truth. */
function renderReview(store, boxId){
  const box=$(boxId); box.innerHTML='';
  if(!store) return;
  const {slug,data}=store, seg=segBySlug(slug);
  if(!seg){ box.innerHTML='<p class=hint>segment no longer loaded</p>'; return; }
  const wrap=document.createElement('div');
  wrap.style.cssText='border:1.5px solid var(--accent);border-radius:11px;padding:13px 15px';
  wrap.innerHTML=`<b style=font-size:13.5px>Review suggestions</b>
    <p class=hint style="margin:4px 0 10px">Accepting marks the field
      <i>user approved</i> and it will not be re-proposed. Rejecting leaves it empty.
      Nothing is written until you save.</p>`;
  let n=0;
  Object.entries(data.suggestions).forEach(([sk,fields])=>{
    if(sk==='_notes') return;
    Object.entries(fields).forEach(([fk,cell])=>{
      const [sec,f]=fieldMeta(sk,fk); if(!f) return;
      n++;
      const cur=seg.doc[sk][fk], applied=cur.state==='ai_suggested'||cur.state==='user_approved';
      const el=document.createElement('div'); el.className='bf';
      const val=Array.isArray(cell.value)?cell.value:[String(cell.value)];
      el.innerHTML=`<h4 style="display:block"><span>${esc(sec.title)} · ${esc(f.label)}</span>
        <span class=lvn style="float:right">${esc(cell.confidence||'')}${
          cell.ref?' · '+esc(cell.ref):''}</span></h4>
        <div style="font-size:13px;line-height:1.55">${
          val.map(v=>`• ${esc(v)}`).join('<br>')}</div>
        <div style="display:flex;gap:8px;margin-top:9px">
          <button class="btn ghost" data-act=accept style="padding:6px 13px;font-size:13px">Accept</button>
          <button class="btn ghost" data-act=edit style="padding:6px 13px;font-size:13px">Edit</button>
          <button class="btn ghost" data-act=reject style="padding:6px 13px;font-size:13px">Reject</button>
          <span class=hint data-state style="margin:0;align-self:center"></span>
        </div>`;
      const mark=t=>el.querySelector('[data-state]').textContent=t;
      el.querySelector('[data-act=accept]').onclick=()=>{
        seg.doc[sk][fk]={...cell,state:'user_approved'};
        el.classList.add('on'); mark('accepted — save to persist'); renderSegments();};
      el.querySelector('[data-act=reject]').onclick=()=>{
        seg.doc[sk][fk]={value:(f.kind==='list'||f.kind==='table')?[]:'',
          state:'rejected',source:'research',ref:cell.ref||'',confidence:''};
        el.classList.remove('on'); mark('rejected'); renderSegments();};
      el.querySelector('[data-act=edit]').onclick=()=>{
        const isList=f.kind==='list';
        const cur=prompt(`${f.label}`+(isList?'\n(one per line)':''),
          isList?val.join('\n'):val[0]||'');
        if(cur===null) return;
        seg.doc[sk][fk]={...cell,
          value:isList?cur.split('\n').map(x=>x.trim()).filter(Boolean):cur.trim(),
          state:'user_approved'};
        el.classList.add('on'); mark('edited and accepted — save to persist');
        renderSegments();};
      if(applied) el.classList.add('on');
      wrap.appendChild(el);
    });
  });
  if(!n){ box.innerHTML='<p class=hint>Nothing proposed.</p>'; return; }
  const all=document.createElement('button'); all.className='btn';
  all.style.cssText='margin-top:6px'; all.textContent='Accept all';
  all.onclick=()=>{ wrap.querySelectorAll('[data-act=accept]').forEach(b=>b.click()); };
  wrap.appendChild(all);
  const held=data.skipped||data.unreviewed;
  if(held&&held.length){
    const sk=document.createElement('p'); sk.className='hint';
    sk.style.marginTop='10px';
    sk.innerHTML=(data.skipped?'<b>Left alone (already approved):</b> '
                              :'<b>Excluded (still unreviewed):</b> ')+
      held.map(x=>esc(x.label||x)).join(' · ');
    wrap.appendChild(sk);
  }
  box.appendChild(wrap);
}
function renderSuggestions(){ renderReview(ENSUG,'#en_review'); }

/* ---- §7 product x segment strategy ---- */
let SYSUG=null;

function renderSynthPanel(){
  const sel=$('#sy_seg'); if(!sel) return;
  const was=sel.value;
  sel.innerHTML=PSEGS.map((s,i)=>{
    const n=cv(s.doc.identity.name)||s.slug;
    return `<option value="${esc(s.slug||('#'+i))}">${esc(n)}${s.slug?'':' (unsaved)'}</option>`;
  }).join('')||'<option value="">— no segments —</option>';
  /* Only restore a selection that still exists: after a save assigns real slugs
     the placeholder value no longer matches any option, and setting it blanks
     the select — which then reads as "no segment chosen" to enrich/synthesise. */
  if(was&&[...sel.options].some(o=>o.value===was)) sel.value=was;
  const box=$('#sy_sections');
  if(!box.children.length&&PSCHEMA&&PSCHEMA.synth_sections){
    box.innerHTML=PSCHEMA.synth_sections.map(s=>
      `<label style="font-weight:500;font-size:13px;display:flex;gap:6px;align-items:center;margin:0">
        <input type=checkbox checked data-sy="${esc(s.key)}" style="width:auto">
        ${esc(s.title)} <span style=color:var(--soft)>(${s.fields})</span></label>`).join('');
  }
}
$('#sy_all').onclick=e=>{e.preventDefault();
  $$('#sy_sections [data-sy]').forEach(c=>c.checked=true);};

async function runSynth(dry){
  const slug=$('#sy_seg').value;
  if(!slug||slug.startsWith('#')){ $('#sy_msg').textContent=
    'Save the segment first.'; return; }
  const secs=$$('#sy_sections [data-sy]:checked').map(c=>c.dataset.sy);
  if(!secs.length){ $('#sy_msg').textContent='Pick at least one section.'; return; }
  const btn=dry?$('#sy_preview'):$('#sy_go'); btn.disabled=true;
  $('#sy_msg').textContent=dry?'building prompt…':'synthesising…';
  try{
    const j=await (await fetch('/product/synth',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:PPROJ,product:PPRODUCT,segment:slug,
                           sections:secs,dry_run:!!dry})})).json();
    if(j.error){ $('#sy_msg').textContent='⚠ '+j.error; btn.disabled=false; return; }
    if(dry){
      $('#pmtext').value=j.prompt;
      $('#pmsub').textContent=`${j.asked} strategy field(s) would be produced`+
        (j.unreviewed.length?`; ${j.unreviewed.length} unreviewed research field(s) excluded.`:'.');
      $('#pmcost').textContent='Preview only — nothing sent.';
      pending=null; $('#pmgo').disabled=true; $('#pmwrap').classList.remove('hide');
      $('#sy_msg').textContent='';
    }else{
      SYSUG={slug,data:j};
      const seg0=segBySlug(slug);
      if(seg0) Object.entries(j.suggestions||{}).forEach(([sk,fs])=>{
        if(sk==='_notes') return;
        Object.entries(fs).forEach(([fk,cell])=>{ if(seg0.doc[sk]) seg0.doc[sk][fk]={...cell}; });
      });
      renderSegments();
      const n=Object.entries(j.suggestions||{}).filter(([k])=>k!=='_notes')
        .reduce((a,[,o])=>a+Object.keys(o).length,0);
      const drop=(j.suggestions._notes||{}).dropped_unlicensed;
      $('#sy_msg').innerHTML=`${esc(j.model)} produced ${n} strategy field(s)`+
        (j.unreviewed.length?`; ${j.unreviewed.length} unreviewed research field(s) excluded.`:'.')+
        (drop&&drop.value&&drop.value.length
          ? `<br><b style=color:var(--signal)>Dropped ${drop.value.length} claim(s) with no licensing product fact:</b> ${
              drop.value.map(esc).join(' · ')}`:'');
      renderReview(SYSUG,'#sy_review');
    }
  }catch(e){ $('#sy_msg').textContent='⚠ '+e; }
  btn.disabled=false;
}
$('#sy_preview').onclick=()=>runSynth(true);
$('#sy_go').onclick=()=>runSynth(false);

function renderReady(r){
  const box=$('#readybox');
  if(!r){ box.innerHTML=''; return; }
  const bar=v=>`<div style="background:var(--surface);border-radius:5px;height:7px;width:110px;
     display:inline-block;vertical-align:middle;overflow:hidden">
     <div style="background:${v>=80?'var(--accent)':v>=40?'#E0A87A':'var(--signal)'};
     height:100%;width:${v}%"></div></div> ${v}%`;
  let h=`<p style="font-size:13.5px;margin:0 0 10px"><b>Product facts</b> ${bar(r.product_facts)}</p>`;
  if(!r.segments.length){ h+='<p class=hint>No segments — nothing to be ready for yet.</p>'; }
  else{
    h+=`<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr>${['Segment','Status','Compatibility','Research','Strategy','PICC','Concepts','Briefs']
        .map(c=>`<th style="text-align:left;padding:5px 7px;border-bottom:1.5px solid var(--line)">${c}</th>`).join('')}</tr></thead><tbody>`;
    r.segments.forEach(s=>{
      h+=`<tr${s.blocked?' style="opacity:.55"':''}>
        <td style="padding:5px 7px">${esc(s.name||s.slug)}</td>
        <td style="padding:5px 7px">${esc(s.status||'—')}</td>
        <td style="padding:5px 7px">${esc(s.compatibility||'—')}</td>
        <td style="padding:5px 7px">${bar(s.research)}</td>
        <td style="padding:5px 7px">${bar(s.strategy)}</td>
        <td style="padding:5px 7px">${s.blocked?'blocked':bar(s.picc)}</td>
        <td style="padding:5px 7px">${s.blocked?'blocked':bar(s.concepts)}</td>
        <td style="padding:5px 7px">${s.blocked?'blocked':bar(s.briefs)}</td></tr>`;
    });
    h+='</tbody></table></div>';
  }
  box.innerHTML=h;
}

async function saveSheet(){
  if(!PDOC) return;
  $('#sheetmsg').textContent='saving…';
  try{
    const j=await (await fetch('/product/save',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:PPROJ,product:PPRODUCT,doc:PDOC,segments:PSEGS})})).json();
    if(j.error){ $('#sheetmsg').textContent='⚠ '+j.error; return; }
    $('#sheetmsg').textContent=`saved → ${j.sheet}`;
    updateSheetState(j.answered,j.total,j.missing_required);
    renderReady(j.readiness); loadProducts();
    /* Slugs are assigned server-side on save. Without pulling them back, a
       freshly imported segment stays marked "(unsaved)" in the enrich and
       strategy pickers and both refuse to run on it. */
    try{
      const fresh=await (await fetch(
        `/product?project=${encodeURIComponent(PPROJ)}&product=${encodeURIComponent(PPRODUCT)}`)).json();
      if(!fresh.error&&fresh.segments){ PSEGS=fresh.segments; renderSegments(); }
    }catch(e){}
  }catch(e){ $('#sheetmsg').textContent='⚠ '+e; }
}
$('#sheetsave').onclick=saveSheet; $('#sheetsave2').onclick=saveSheet;

/* The create screen renders the schema's create-stage sections, so a merchant
   enters what they already know in one pass instead of creating a shell and
   hunting for where the rest goes. Post-research fields are deliberately absent:
   the spec is explicit that nobody should be asked to guess a buyer, a pain or a
   position before research exists. */
let NEWDOC=null;
function renderNewProduct(){
  const box=$('#np_form'); if(!box||!PSCHEMA) return;
  if(box.dataset.built) return;
  NEWDOC=blankProductDoc();
  box.innerHTML='';
  const keys=new Set(PSCHEMA.create_sections||[]);
  (PSCHEMA.product||[]).filter(sec=>keys.has(sec.key)).forEach(sec=>{
    box.appendChild(sectionEl(sec, NEWDOC[sec.key], sec.key==='identity'));
  });
  box.dataset.built='1';
}
function blankProductDoc(){
  const d={};
  (PSCHEMA.product||[]).forEach(sec=>{ d[sec.key]={};
    sec.fields.forEach(f=>d[sec.key][f.key]=
      {value:(f.kind==='list'||f.kind==='table')?[]:'',state:'empty',
       source:'',ref:'',confidence:''});});
  return d;
}

/* Attach to an existing project or start a new one. Existing matters most: the
   project holding the research is normally older than the product, and that is
   where its segments live. */
async function loadNewProjectPicker(){
  const sel=$('#np_proj'); if(!sel) return;
  const was=sel.value;
  try{
    const j=await (await fetch('/projects')).json();
    sel.innerHTML=(j.projects||[]).map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('')
      +'<option value="__new">＋ new project…</option>';
    if(was&&[...sel.options].some(o=>o.value===was)) sel.value=was;
  }catch(e){ sel.innerHTML='<option value="__new">＋ new project…</option>'; }
  syncProjPicker();
}
function syncProjPicker(){
  const isNew=$('#np_proj').value==='__new';
  $('#np_key').classList.toggle('hide',!isNew);
  $('#np_projwhy').textContent=isNew
    ? 'A new project starts with no VOC and no segments — you will need to run ingest and the segment stage before there is anything to enrich from.'
    : 'Segments already researched under this project become importable on the Segments tab straight away.';
}
$('#np_proj').onchange=syncProjPicker;

$('#np_go').onclick=async()=>{
  const sel=$('#np_proj').value;
  const proj=sel==='__new'?$('#np_key').value.trim():sel;
  if(!proj){ $('#np_msg').textContent='Give the new project a key.'; return; }
  const name=NEWDOC?cv(NEWDOC.identity.name).trim():'';
  if(!name){ $('#np_msg').textContent='Give the product a name.'; return; }
  $('#np_go').disabled=true; $('#np_msg').textContent='creating…';
  try{
    const j=await (await fetch('/product/create',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:proj,doc:NEWDOC})})).json();
    if(j.error){ $('#np_msg').textContent='⚠ '+j.error; $('#np_go').disabled=false; return; }
    $('#np_msg').textContent=`created ${j.project}/${j.product} — opening its sheet`;
    $('#np_key').value=''; $('#np_form').dataset.built=''; renderNewProduct();
    await loadProducts(); await loadNewProjectPicker();
    openSheet(j.project,j.product);
  }catch(e){ $('#np_msg').textContent='⚠ '+e; }
  $('#np_go').disabled=false;
};

// ---------- pipeline ----------
let stage=null;
const STAGES=__STAGES__;
(function(){ const el=$('#stages');
  STAGES.forEach(s=>{ const d=document.createElement('div'); d.className='stage';
    d.innerHTML=`<b>${s.name}</b><small>${s.desc}</small>`+
      (s.costs?'<div class=costs>COSTS API CREDIT</div>':'')+
      `<div class=stageskills id=sk-${s.name}></div>`;
    d.onclick=()=>{ stage=s; $$('.stage').forEach(x=>x.classList.remove('on'));
      d.classList.add('on'); $('#ingestbox').classList.toggle('hide',!s.source);
      const showSeg = s.name==='segment';
      $('#segvocbox').classList.toggle('hide',!showSeg);
      if(showSeg) loadVocFiles();
      const showRefine = s.name==='refine-voc';
      $('#refinevocbox').classList.toggle('hide',!showRefine);
      if(showRefine) loadRefineVocFiles();
      $('#importbox').classList.toggle('hide',s.name!=='import');
      const wantsProduct = ['picc','concepts','brief','run'].includes(s.name);
      $('#prodwrap').classList.toggle('hide',!wantsProduct);
      showOpts(s.name);
      $('#runbtn').disabled=false;
      $('#runhint').textContent=s.costs?'This stage calls the selected model API — tick the approval box.':'Free — pure code.';
    };
    el.appendChild(d);});
})();
fetch('/projects').then(r=>r.json()).then(j=>{
  const p=$('#proj'); p.innerHTML=j.projects.map(x=>`<option>${x}</option>`).join('');
  p.onchange=loadSegs; loadSegs();
});
async function loadSegs(){
  const r=await fetch('/segments?project='+encodeURIComponent($('#proj').value));
  const j=await r.json();
  $('#seg').innerHTML=j.segments.length?j.segments.map(x=>`<option>${x}</option>`).join('')
    :'<option value="">— none yet —</option>';
  $('#seg').onchange=loadPiccs;
  if(stage&&stage.name==='segment')loadVocFiles();
  if(stage&&stage.name==='refine-voc')loadRefineVocFiles();
  loadPiccs();
  loadProds();
}
/* Stages that build on a product (PICC/concepts/briefs) need to know which
   product this project's pipeline is running for, so the segment context and
   product truth injected into the prompt are the right ones. */
async function loadProds(){
  const sel=$('#prod'); if(!sel) return;
  const pr=$('#proj').value, was=sel.value;
  sel.innerHTML='<option value="">— default product —</option>';
  if(!pr) return;
  try{
    const j=await (await fetch('/products?project='+encodeURIComponent(pr))).json();
    (j.products||[]).filter(x=>x.project===pr).forEach(x=>{
      const o=document.createElement('option');
      o.value=x.product; o.textContent=x.product; sel.appendChild(o);});
    if(was&&[...sel.options].some(o=>o.value===was)) sel.value=was;
  }catch(e){}
}
/* The concepts stage builds on exactly one PICC card. Default to the selected
   segment's own, but list every card in the project so a rewritten or set-aside
   variant can be chosen deliberately. */
async function loadPiccs(){
  const sel=$('#piccsel'); if(!sel) return;
  const pr=$('#proj').value, seg=$('#seg').value, was=sel.value;
  sel.innerHTML='<option value="">— this segment\'s own card (default)</option>';
  if(!pr) return;
  try{
    const j=await (await fetch('/piccs?project='+encodeURIComponent(pr))).json();
    const cards=j.cards||[];
    if(!cards.length){
      $('#piccwhy').textContent='No PICC card in this project yet — run the picc stage first.';
      return;
    }
    cards.forEach(c=>{const o=document.createElement('option');
      o.value=c.rel;
      const when=new Date(c.mtime*1000).toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
      o.textContent=(c.segment===seg?'★ ':'')+`${c.label} — ${when}`;
      sel.appendChild(o);});
    if(was&&[...sel.options].some(o=>o.value===was)) sel.value=was;
    const mine=cards.filter(c=>c.segment===seg).length;
    $('#piccwhy').innerHTML=mine
      ? `★ marks this segment's own card. Leave it on default to use that, or pick `+
        `another of the ${cards.length} card(s) in this project.`
      : `<b>No card for <code>${seg}</code> yet</b> — run the picc stage for it, or `+
        `pick one of the ${cards.length} card(s) from another segment.`;
  }catch(e){ $('#piccwhy').textContent='Could not load PICC cards: '+e; }
}
async function loadVocFiles(){
  const pr=$('#proj').value; if(!pr)return;
  const s=$('#segvoc'); const was=s.value;
  s.innerHTML='<option value="">Loading final ingest file…</option>'; s.disabled=true;
  try{
    const j=await (await fetch('/voc-files?project='+encodeURIComponent(pr))).json();
    const files=j.files||[]; s.innerHTML='';
    if(!files.length){s.innerHTML='<option value="">No final ingest file — run ingest first</option>';return;}
    files.forEach(f=>{const o=document.createElement('option');
      const when=new Date(f.mtime*1000).toLocaleString([],{
        dateStyle:'medium',timeStyle:'short'});
      o.value=f.path; o.textContent=`${f.label} — ${when}`; s.appendChild(o);});
    s.disabled=false;
    if(was&&[...s.options].some(o=>o.value===was))s.value=was;
  }catch(e){s.innerHTML='<option value="">Could not load final ingest file</option>';}
}
async function loadRefineVocFiles(){
  const pr=$('#proj').value; if(!pr)return;
  const s=$('#refinevoc'); const was=s.value;
  s.innerHTML='<option value="">Loading refinable VOC files…</option>'; s.disabled=true;
  try{
    const j=await (await fetch('/refine-voc-files?project='+encodeURIComponent(pr))).json();
    const files=j.files||[]; s.innerHTML='';
    if(!files.length){
      s.innerHTML='<option value="">No refinable VOC JSONL — run ingest first</option>';
      return;
    }
    files.forEach(f=>{const o=document.createElement('option');
      const when=new Date(f.mtime*1000).toLocaleString([],{dateStyle:'medium',timeStyle:'short'});
      o.value=f.path; o.textContent=`${f.label} — ${when}`; s.appendChild(o);});
    s.disabled=false;
    if(was&&[...s.options].some(o=>o.value===was))s.value=was;
  }catch(e){s.innerHTML='<option value="">Could not load refinable VOC files</option>';}
}
$('#runbtn').onclick=async()=>{
  if(!stage) return;
  const free = stage.name==='ingest' && $('#rulesonly').checked;
  if(stage.costs && !free && !$('#approve').checked){
    $('#runhint').textContent='Tick the approval box first — this stage spends credit.'; return; }
  const log=$('#log'); log.textContent=''; $('#runbtn').disabled=true;
  const body={stage:stage.name,project:$('#proj').value,segment:$('#seg').value,
              source:(stage&&stage.name==='import')?(importSource||''):$('#ingestpath').value.trim(),
              voc_source:$('#segvoc')?$('#segvoc').value:'',
              refine_source:$('#refinevoc')?$('#refinevoc').value:'',
              approve:$('#approve').checked,
              force:$('#force').checked,
              rules_only:$('#rulesonly').checked,
              n_concepts:+$('#nconcepts').value||0, n_hooks:+$('#nhooks').value||0,
              picc:$('#piccsel')?$('#piccsel').value:'',
              product:$('#prod')?$('#prod').value:'',
              n_briefs:+$('#nbriefs').value||0,
              provider:$('#provider')?$('#provider').value:'',
              model:$('#modelid')?$('#modelid').value.trim():''};
  if(stage.name==='extract'&&$('#extractskill').value){
    body.skills=$('#extractskill').value;
    body.force=true;
  }else if(stage.name==='extract'||stage.name==='run'){
    body.preset=$('#preset').value;
  }
  const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const reader=r.body.getReader(), dec=new TextDecoder();
  while(true){ const {done,value}=await reader.read(); if(done) break;
    log.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight; }
  $('#runbtn').disabled=false;
};

/* ---------- VOC upload ---------- */
let uploaded=null;
function bindDrop(el,input,onFile){
  el.onclick=()=>input.click();
  input.onchange=e=>e.target.files[0]&&onFile(e.target.files[0]);
  el.ondragover=e=>{e.preventDefault();el.classList.add('over');};
  el.ondragleave=()=>el.classList.remove('over');
  el.ondrop=e=>{e.preventDefault();el.classList.remove('over');
    e.dataTransfer.files[0]&&onFile(e.dataTransfer.files[0]);};
}
bindDrop($('#vocdrop'),$('#vocfile'),async f=>{
  const st=$('#vocstate'); st.textContent='Uploading '+f.name+' …';
  const b64=await new Promise(res=>{const r=new FileReader();r.onload=()=>res(r.result);r.readAsDataURL(f);});
  const r=await (await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project:$('#proj').value,filename:f.name,data:b64})})).json();
  if(r.error){st.textContent='⚠ '+r.error;st.style.color='var(--signal)';return;}
  uploaded=r.path; $('#ingestpath').value=r.path;
  st.innerHTML='✓ <b>'+f.name+'</b> ('+(r.bytes/1024).toFixed(0)+' KB) saved into the project — <code>'+r.rel+'</code>';
  st.style.color='var(--accent)';
  $('#vocmsg').textContent=f.name;
});

/* ================= project creation ================= */
$('#npbtn')&&($('#npbtn').onclick=async()=>{
  const m=$('#npmsg');
  const r=await (await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:$('#npname').value.trim(),product:$('#npproduct').value,
                         market:$('#npmarket').value})})).json();
  if(r.error){m.textContent='⚠ '+r.error;m.style.color='var(--signal)';return;}
  m.innerHTML='✓ Created <b>'+r.project+'</b> — no product yet, which is the point. Edit its '+
    'filter regexes in projects/'+r.project+'/project.json, run the research, then add a '+
    'product from the Product tab.';
  m.style.color='var(--accent)';
  $('#npname').value=$('#npproduct').value=$('#npmarket').value='';
  loadProjects&&loadProjects(); renderProjectList();
});

/* ================= VOC upload ================= */
function bindDrop(el,input,onFiles){
  if(!el)return;
  el.onclick=()=>input.click();
  input.onchange=e=>e.target.files.length&&onFiles([...e.target.files]);
  el.ondragover=e=>{e.preventDefault();el.classList.add('over')};
  el.ondragleave=()=>el.classList.remove('over');
  el.ondrop=e=>{e.preventDefault();el.classList.remove('over');
    e.dataTransfer.files.length&&onFiles([...e.dataTransfer.files])};
}
const toB64=f=>new Promise(r=>{const x=new FileReader();x.onload=()=>r(x.result);x.readAsDataURL(f)});
bindDrop($('#vocdrop'),$('#vocfile'),async fs=>{
  const f=fs[0], st=$('#vocstate'); st.textContent='Uploading '+f.name+' …';
  const r=await (await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project:$('#proj').value,filename:f.name,data:await toB64(f)})})).json();
  if(r.error){st.textContent='⚠ '+r.error;st.style.color='var(--signal)';return;}
  $('#ingestpath').value=r.path; $('#vocmsg').textContent=f.name;
  st.innerHTML='✓ saved into the project — <code>'+r.rel+'</code> ('+(r.bytes/1024).toFixed(0)+' KB)';
  st.style.color='var(--accent)';
});

/* ================= import: adopt a run done elsewhere ================= */
let importSource=null;
bindDrop($('#impdrop'),$('#impfile'),async fs=>{
  const f=fs[0], st=$('#impstate'), plan=$('#impplan');
  importSource=null; plan.classList.add('hide'); plan.innerHTML='';
  st.textContent='Reading '+f.name+' \u2026'; st.style.color='';
  const r=await (await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project:$('#proj').value,filename:f.name,kind:'import',
                         data:await toB64(f)})})).json();
  if(r.error){st.textContent='\u26a0 '+r.error;st.style.color='var(--signal)';return;}
  if(!r.plan||!r.plan.length){
    st.textContent='\u26a0 No audience files in there. Expected markdown with a '+
      '"# Name" heading and "### Comment N" blocks.';
    st.style.color='var(--signal)'; return;
  }
  importSource=r.path;
  $('#impmsg').textContent=f.name;
  const items=r.plan.reduce((a,x)=>a+x.items,0);
  st.innerHTML='\u2713 <b>'+r.plan.length+'</b> audience(s), <b>'+items.toLocaleString()+
    '</b> assigned items \u2014 nothing written yet. Press Run to write them.';
  st.style.color='var(--accent)';
  plan.innerHTML='<table class=planlist><tbody>'+r.plan.map(x=>
    '<tr><td><code>'+x.segment_id+'</code></td><td>'+x.name+'</td>'+
    '<td style="text-align:right">'+x.items.toLocaleString()+'</td></tr>').join('')+
    '</tbody></table>';
  plan.classList.remove('hide');
});

/* ================= stage options ================= */
const EXTRACT_PRESET_INFO={
  fast:'07 Pain Points · 08 Pain Moments · 09 Desired Outcomes · 12 Beliefs · '+
       '14 Failed Solutions · 16 Buying Triggers · 18 Objections · 19 Mechanisms · '+
       '20 Desired Proof · 24 Representative VOC',
  standard:'Everything in Fast Test, plus: 10 Emotional States · 11 Psychological Drivers · '+
       '13 Limiting Beliefs · 15 Assumed Solutions · 17 Buying Criteria · '+
       '21 Product Mentions · 22 Competitors · 25 Terminology',
  deep:'Everything in Standard, plus: 23 Offers · 26 Slang. Runs every extraction '+
       'skill from 07 through 26.'
};
function describeExtractPreset(){
  if($('#extracthint'))$('#extracthint').textContent=EXTRACT_PRESET_INFO[$('#preset').value]||'';
}
$('#preset')&&($('#preset').onchange=describeExtractPreset);
describeExtractPreset();

/* ---------------- project settings ---------------- */
var SETSCHEMA=[],SETVALUES={};
function settingsField(f){
  const v=SETVALUES[f.key], id='set-'+f.key.replace(/\./g,'-');
  let input;
  if(f.kind==='choice'){
    input=`<select id="${id}">`+f.options.map(o=>
      `<option value="${o}"${String(v)===o?' selected':''}>${o}</option>`).join('')+`</select>`;
  }else if(f.kind==='list'){
    input=`<textarea id="${id}" placeholder="one per line">${(v||[]).join('\n')}</textarea>`;
  }else if(f.kind==='longtext'){
    input=`<textarea id="${id}">${v==null?'':v}</textarea>`;
  }else if(f.kind==='int'||f.kind==='float'){
    const step=f.kind==='float'?'0.01':'1';
    input=`<input id="${id}" type=number step="${step}"`+
      (f.min!=null?` min="${f.min}"`:'')+(f.max!=null?` max="${f.max}"`:'')+
      ` value="${v==null?'':v}">`;
  }else{
    input=`<input id="${id}" type=text value="${v==null?'':String(v).replace(/"/g,'&quot;')}">`;
  }
  return `<div class=setfield data-key="${f.key}"><label for="${id}">${f.label}</label>`+
    input+(f.help?`<div class=sethelp>${f.help}</div>`:'')+`</div>`;
}
function readSetting(f){
  const el=$('#set-'+f.key.replace(/\./g,'-'));
  return el?el.value:null;
}
function loadSettings(){
  const p=$('#proj').value; if(!p) return;
  fetch('/settings?project='+encodeURIComponent(p)).then(r=>r.json()).then(j=>{
    if(j.error){ $('#settingsfields').innerHTML='<p class=hint>'+j.error+'</p>'; return; }
    SETSCHEMA=j.schema; SETVALUES=j.values;
    $('#settingspath').textContent=j.path;
    const groups=[];
    SETSCHEMA.forEach(f=>{ let g=groups.find(x=>x.name===f.group);
      if(!g){ g={name:f.group,fields:[]}; groups.push(g); } g.fields.push(f); });
    $('#settingsfields').innerHTML=groups.map(g=>
      `<div class=setgroup><h4>${g.name}</h4>`+
      g.fields.map(settingsField).join('')+`</div>`).join('');
    $('#settingsmsg').textContent='';
  });
}
$('#settingstoggle').onclick=()=>{
  const box=$('#settingsbox'), opening=box.classList.contains('hide');
  box.classList.toggle('hide');
  if(opening) loadSettings();
};
$('#settingssave').onclick=()=>{
  const values={};
  SETSCHEMA.forEach(f=>{ const raw=readSetting(f); if(raw!==null) values[f.key]=raw; });
  $('#settingsmsg').textContent='Saving…';
  fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project:$('#proj').value,values})})
  .then(r=>r.json().then(j=>({ok:r.ok,j})))
  .then(({ok,j})=>{
    if(!ok){ $('#settingsmsg').innerHTML='<b class=bad>'+(j.error||'save failed')+'</b>';
             return; }
    SETVALUES=j.values;
    $('#settingsmsg').textContent='Saved — takes effect on the next run.';
  });
};

fetch('/skills').then(r=>r.json()).then(j=>{
  SKILLMAP = j.stage_skills || {};
  Object.keys(SKILLMAP).forEach(name=>{
    const box=$('#sk-'+name); if(!box) return;
    const rows=SKILLMAP[name]||[]; if(!rows.length) return;
    const missing=rows.filter(r=>!r.present).length;
    box.innerHTML =
      `<span class=skilltoggle>${rows.length} skill${rows.length>1?'s':''}`+
      (missing?` · <b class=bad>${missing} missing</b>`:'')+`</span>`+
      `<div class=skillrows>`+rows.map(r=>
        `<div class=skillrow${r.present?'':' bad'}>`+
        `<code>${r.file||'(not found)'}</code>`+
        `<span>${r.label}</span>`+
        `<small>${r.purpose||''}</small>`+
        `<em>${r.present?r.lines+' lines':'MISSING'}</em></div>`).join('')+
      `</div>`;
    box.querySelector('.skilltoggle').onclick=e=>{
      e.stopPropagation(); box.classList.toggle('open'); };
  });
  const s=$('#extractskill');
  (j.extractors||[]).forEach(x=>{
    const o=document.createElement('option');
    o.value=x.n; o.textContent=`${String(x.n).padStart(2,'0')} · ${x.title}`;
    s.appendChild(o);
  });
});
function showOpts(name){
  document.querySelectorAll('.opts').forEach(o=>o.hidden=true);
  const model=['ingest','segment','extract','picc','concepts','brief','run'].includes(name);
  if($('#opt_model'))$('#opt_model').hidden=!model;
  if(name==='extract'||name==='run')$('#opt_extract').hidden=false;
  if(name==='extract')$('#opt_extract_single').hidden=false;
  if(name==='concepts'||name==='run')$('#opt_concepts').hidden=false;
  if(name==='brief'||name==='run')$('#opt_brief').hidden=false;
}
$('#provider')&&($('#provider').onchange=()=>{
  const or=$('#provider').value==='openrouter';
  $('#modelid').value=or?'deepseek/deepseek-v4-flash':'claude-opus-5';
  $('#modelhint').innerHTML=or
    ?'Cheap per token, but <b>no prompt caching and no batch discount</b> — the corpus is re-sent on every request. Verify the exact model id at openrouter.ai/models.'
    :'Prompt caching + Batch API: the corpus is paid for roughly once across all 20 extractions.';
});
$('#provider')&&$('#provider').dispatchEvent(new Event('change'));

/* ================= outputs browser ================= */
const outEsc=t=>String(t??'').replace(/[&<>"']/g,c=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const outWhen=seconds=>new Date(seconds*1000).toLocaleString([],{
  dateStyle:'medium',timeStyle:'short'});
async function loadOutputs(){
  const pr=$('#oproj')&&$('#oproj').value; if(!pr)return;
  const d=await (await fetch('/outputs?project='+encodeURIComponent(pr))).json();
  const by={}; (d.stages||[]).forEach(x=>{(by[x.stage]=by[x.stage]||[]).push(x)});
  const order=['ingest','segment','extract','picc','concepts','brief','render','logs'];
  const old=[...document.querySelectorAll('#olist details.outstage')];
  const hadAccordion=old.length>0;
  const openStages=new Set(old.filter(x=>x.open).map(x=>x.dataset.stage));
  const lookup=[];
  const rowsHtml=rows=>rows.map(x=>{const i=lookup.push(x)-1;return `
    <div class=orow data-oi="${i}">
      <div>${outEsc(x.label)}</div>
      <div class=ormeta>${outWhen(x.mtime)} · ${(x.size/1024).toFixed(0)} KB</div>
    </div>`}).join('');
  const groupHtml=(title,rows,empty)=>`<div class=outgroup-title>${title}</div>`+
    (rows.length?rowsHtml(rows):`<div class=hint style="padding:3px 7px 8px">${empty}</div>`);
  const stages=order.filter(k=>by[k]);
  $('#olist').innerHTML=stages.map((k,n)=>{
    const items=by[k].slice().sort((a,b)=>b.mtime-a.mtime);
    const latest=Math.max(...items.map(x=>x.mtime));
    const open=openStages.has(k)||(!hadAccordion&&n===0)?' open':'';
    let body=rowsHtml(items);
    if(k==='ingest'){
      const final=items.filter(x=>x.role==='final');
      const additional=items.filter(x=>x.role!=='final');
      body=groupHtml('Final file',final,'Run ingest to create the segment-ready file.')+
           groupHtml('Additional files',additional,'No audit or intermediate files yet.');
    }
    return `<details class=outstage data-stage="${k}"${open}>
      <summary><div><div class=osline><span class=osname>${k}</span>
        <span class=oscount>${items.length} file${items.length===1?'':'s'}</span></div>
        <div class=osdate>Latest · ${outWhen(latest)}</div></div></summary>
      <div class=outbody>${body}</div></details>`;
  }).join('')||'<p class=hint style=padding:10px>Nothing produced yet.</p>';
  const accordions=[...document.querySelectorAll('#olist details.outstage')];
  accordions.forEach(panel=>panel.ontoggle=()=>{
    if(panel.open)accordions.forEach(other=>{if(other!==panel)other.open=false;});
  });
  document.querySelectorAll('#olist .orow').forEach(r=>r.onclick=()=>{
    document.querySelectorAll('#olist .orow').forEach(x=>x.classList.remove('on'));
    r.classList.add('on'); const x=lookup[+r.dataset.oi]; viewFile(x.path,x.kind);
  });
  const imp=Object.entries(d.provenance||{}).filter(([k,v])=>v.origin==='imported');
  $('#provwarn').innerHTML=imp.length?`<div class=card style="background:#FDF3F0;
    border-color:var(--signal);margin-bottom:14px"><b style=color:var(--signal)>⚠ Imported evidence</b>
    <p class=hint style=margin-top:6px>${imp.map(([k,v])=>`<b>${outEsc(k)}</b> — ${outEsc(v.detail)}`).join('<br>')}
    <br><br>Copied in, not produced by this project's pipeline. No candidate / validated /
    assignment records exist behind them, so anything built on them inherits an
    unverifiable lineage.</p></div>`:'';
}
/* Render a subset of markdown to HTML, safely.
   Input is escaped first, so no user/model content can become markup — this
   is display only. Supported: headings, paragraphs, bold, italic, inline and
   fenced code, bullet + numbered lists, blockquotes, tables, hr, links. */
const MD_INLINE=[
  [/`([^`]+)`/g, '<code>$1</code>'],
  [/\*\*([^*]+)\*\*/g, '<strong>$1</strong>'],
  [/(^|[^\w*])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>'],
  [/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target=_blank rel=noopener>$1</a>'],
];
function mdInline(s){
  s=outEsc(s);
  MD_INLINE.forEach(([re,rep])=>s=s.replace(re,rep));
  return s;
}
function mdRender(text){
  const src=String(text??'').replace(/\r\n?/g,'\n').split('\n');
  const out=[]; let i=0;
  const flushPara=()=>{ if(buf.length){ out.push('<p>'+buf.join('<br>')+'</p>'); buf=[]; } };
  let buf=[]; let fence=false;
  while(i<src.length){
    const line=src[i];
    // fenced code blocks
    if(/^```/.test(line.trim())){
      flushPara(); fence=!fence; i++; continue;
    }
    if(fence){ out.push('<pre><code>'+outEsc(line)+'</code></pre>'); i++; continue; }
    const t=line.trim();
    // blank line -> paragraph break
    if(!t){ flushPara(); i++; continue; }
    // headings
    const h=/^(#{1,6})\s+(.*)$/.exec(t);
    if(h){ flushPara(); const n=h[1].length;
      out.push(`<h${Math.min(n,4)} style="margin:14px 0 6px">${mdInline(h[2])}</h${Math.min(n,4)}>`);
      i++; continue; }
    // horizontal rule
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(t)){ flushPara();
      out.push('<hr style="border:none;border-top:1.5px solid var(--line);margin:14px 0">');
      i++; continue; }
    // table: a header row then a separator row
    if(/^\|.*\|$/.test(t)&&i+1<src.length&&/^\s*\|?[\s:|-]+\|?\s*$/.test(src[i+1].trim())&&
       /-/.test(src[i+1])){
      flushPara();
      const head=t.split('|').map(c=>c.trim()).filter((c,ix,a)=>ix>0||c);
      const headCells=head.slice(0,head.length);
      i+=2; // consume header + separator
      const rows=[];
      while(i<src.length&&/^\|.*\|$/.test(src[i].trim())){
        const cells=src[i].split('|').map(c=>c.trim());
        rows.push(cells);
        i++;
      }
      let table='<table style="border-collapse:collapse;margin:10px 0;width:100%;font-size:12.5px">';
      table+='<thead><tr>'+headCells.map(c=>`<th style="border:1px solid var(--line);padding:5px 8px;text-align:left;background:var(--surface);white-space:nowrap">${mdInline(c)}</th>`).join('')+'</tr></thead>';
      table+='<tbody>'+rows.map(r=>'<tr>'+headCells.map((_,ix)=>`<td style="border:1px solid var(--line);padding:5px 8px">${mdInline(r[ix]??'')}</td>`).join('')+'</tr>').join('')+'</tbody></table>';
      out.push(table);
      continue;
    }
    // blockquote
    if(/^>\s?/.test(t)){
      const q=[];
      while(i<src.length&&/^>\s?/.test(src[i].trim())){ q.push(src[i].trim().replace(/^>\s?/,'')); i++; }
      out.push('<blockquote style="margin:8px 0;padding:6px 12px;border-left:3px solid var(--accent);background:var(--surface);color:var(--soft);border-radius:0 8px 8px 0">'
        +mdInline(q.join(' '))+'</blockquote>');
      continue;
    }
    // lists
    const b=/^([-*])\s+(.*)$/.exec(t);
    const n=/^(\d+)[.)]\s+(.*)$/.exec(t);
    if(b||n){
      flushPara();
      const ordered=!!n;
      out.push(ordered?'<ol style="margin:8px 0;padding-left:22px">':'<ul style="margin:8px 0;padding-left:22px">');
      while(i<src.length){
        const tt=src[i].trim();
        const mb=/^([-*])\s+(.*)$/.exec(tt);
        const mn=/^(\d+)[.)]\s+(.*)$/.exec(tt);
        const item=ordered?mn:mb;
        if(!item) break;
        out.push(`<li style="margin:2px 0">${mdInline(item[2])}</li>`);
        i++;
      }
      out.push(ordered?'</ol>':'</ul>');
      continue;
    }
    buf.push(mdInline(line));
    i++;
  }
  flushPara();
  return out.join('\n');
}
async function viewFile(path,kind){
  const v=$('#oview');
  if(kind==='image'){v.innerHTML=`<img src="/file?path=${encodeURIComponent(path)}"
    style="max-width:100%;border-radius:10px"><p class=hint>${outEsc(path)}</p>`;return}
  v.innerHTML='<p class=hint>Loading…</p>';
  const d=await (await fetch('/file?path='+encodeURIComponent(path))).json();
  const isMd=/\.(md|markdown)$/i.test(path);
  v.innerHTML=`<p class=hint style=margin:0>${outEsc(path)}${d.clipped?' — first 200KB of '+(d.bytes/1024).toFixed(0)+'KB':''}</p>`+
    (isMd
      ? `<div style="font:13px/1.6 -apple-system,'Helvetica Neue',Inter,sans-serif;background:var(--surface);
         padding:16px 20px;border-radius:9px;max-height:520px;overflow:auto;margin-top:10px">${mdRender(d.text)}</div>`
      : `<pre style="white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;background:var(--surface);
         padding:14px;border-radius:9px;max-height:520px;overflow:auto;margin-top:10px">${outEsc(d.text)}</pre>`);
}
/* ---------- model log export ----------
   The Outputs list shows one row per log FILE, which is unusable at the scale a
   failing stage produces: an 83-chunk ingest with a recovery pass leaves several
   hundred of them and the interesting ones are scattered through the middle.
   This exports a whole stage in one archive instead. */
function logSize(n){
  return n > 1048576 ? (n/1048576).toFixed(1)+' MB' : Math.max(1,Math.round(n/1024))+' KB';
}
async function loadLogStages(){
  const sel=$('#logstage'); if(!sel) return;
  const pr=$('#oproj')&&$('#oproj').value, was=sel.value;
  sel.innerHTML=''; if($('#logexport'))$('#logexport').disabled=true;
  if(!pr) return;
  try{
    const j=await (await fetch('/logs?project='+encodeURIComponent(pr))).json();
    const stages=j.stages||[];
    if(!stages.length){
      sel.innerHTML='<option value="">— no model logs yet —</option>';
      return;
    }
    let total=0, files=0;
    stages.forEach(s=>{total+=s.bytes; files+=s.files;
      const o=document.createElement('option');
      o.value=s.stage;
      o.textContent=`${s.stage} — ${s.runs} request${s.runs===1?'':'s'} (${logSize(s.bytes)})`;
      sel.appendChild(o);});
    // Offer everything as well: "what happened in this project" is a real
    // question, and answering it should not mean exporting stages one at a time.
    const all=document.createElement('option');
    all.value=''; all.textContent=`all stages — ${files} files (${logSize(total)})`;
    sel.appendChild(all);
    if(was!==null&&[...sel.options].some(o=>o.value===was)) sel.value=was;
    $('#logexport').disabled=false;
  }catch(e){ sel.innerHTML='<option value="">— could not read logs —</option>'; }
}
$('#logexport')&&($('#logexport').onclick=()=>{
  const pr=$('#oproj')&&$('#oproj').value;
  if(!pr){ $('#logmsg').textContent='Pick a project first.'; return; }
  const stage=$('#logstage').value;
  // A plain navigation, so the browser's own download UI handles a large
  // archive rather than this buffering it into memory to re-offer it.
  window.location='/logs/export?project='+encodeURIComponent(pr)+
    '&stage='+encodeURIComponent(stage);
});
$('#orefresh')&&($('#orefresh').onclick=()=>{loadOutputs();loadLogStages();});
$('#oproj')&&($('#oproj').onchange=()=>{loadOutputs();loadLogStages();});
/* Populate the output tab's project picker if empty. This runs once but is
   safe to re-fire: setting .innerHTML on an already-populated select is a no-op
   because the option values are the same. */
if($('#oproj')&&!$('#oproj').options.length){
  fetch('/projects').then(r=>r.json()).then(j=>{
    const s=$('#oproj'); s.innerHTML='<option value="">—</option>';
    (j.projects||[]).forEach(p=>{const o=document.createElement('option');
      o.value=o.textContent=p; s.appendChild(o);});
    if((j.projects||[]).length===1){s.value=j.projects[0];s.onchange();}
    else if($('#oproj').options.length===1){s.value=j.projects[0];s.onchange();}
  });
}

/* ================= reference library ================= */
LIB=null; LSEL=new Set(); LDUPE=false; var LSHOWN=0, LCUR=null, LZOOM=1;

async function loadLibrary(){
  LIB=await (await fetch('/library')).json();
  if($('#lcat').options.length<2)
    $('#lcat').innerHTML=['(all)',...LIB.categories].map(c=>`<option>${c}</option>`).join('');
  if($('#limportcat').options.length<1)
    $('#limportcat').innerHTML=LIB.categories.map(c=>`<option>${c}</option>`).join('');
  renderLibrary();
}
function visibleItems(){
  const cat=$('#lcat').value, d=new Set(LDUPE?LIB.duplicates.flat():[]);
  return LIB.items.filter(i=>(cat==='(all)'||i.category===cat)&&(!LDUPE||d.has(i.rel)));
}
function updateLibCount(){
  $('#lstate').innerHTML=`${LSHOWN} shown · ${LIB.items.length} total · `+
    `<b>${LIB.duplicates.length}</b> duplicate group(s) · <b>${LSEL.size}</b> selected`+
    (LIB.risky.length?` · <span style=color:var(--signal)><b>${LIB.risky.length}</b>
      AVIF/HEIC files wearing a .jpg name — macOS and Chrome open them, image APIs
      usually reject them.</span>`:'');
  $('#ldelete').disabled=!LSEL.size;
}
function renderLibrary(){
  const items=visibleItems(); LSHOWN=items.length;
  /* Thumbnails stay clean — no text burned over the artwork. Everything about a
     file lives in the sidebar, where it can be read. */
  $('#lgrid').innerHTML=items.map(i=>
    `<div class="thumb${LSEL.has(i.rel)?' sel':''}" data-rel="${i.rel}" title="${i.name}">
       <img loading=lazy src="/ref?thumb=1&path=${encodeURIComponent(i.rel)}">
       <input type=checkbox class=lpick data-rel="${i.rel}" ${LSEL.has(i.rel)?'checked':''}
         title="Select for deletion"
         style="position:absolute;top:5px;left:5px;width:19px;height:19px;z-index:2;
                cursor:pointer;accent-color:var(--accent)">
       ${i.risky?`<div style="position:absolute;top:5px;right:5px;background:var(--signal);
         color:#fff;font-size:9px;font-weight:700;border-radius:4px;padding:1px 4px">
         ${i.format.toUpperCase()}</div>`:''}
     </div>`).join('')||'<p class=hint>Nothing here.</p>';
  updateLibCount();
  document.querySelectorAll('#lgrid .lpick').forEach(c=>{
    c.onclick=e=>{ e.stopPropagation();
      c.checked?LSEL.add(c.dataset.rel):LSEL.delete(c.dataset.rel);
      c.closest('.thumb').classList.toggle('sel',c.checked); updateLibCount(); };
  });
  document.querySelectorAll('#lgrid .thumb').forEach(t=>{
    t.onclick=e=>{ if(!e.target.classList.contains('lpick')) showDetail(t.dataset.rel); };
  });
}
function showDetail(rel){
  const i=LIB.items.find(x=>x.rel===rel); if(!i)return;
  LCUR=rel; LZOOM=1;
  $('#lpreview').innerHTML=`
    <div style="display:flex;gap:6px;margin-bottom:10px">
      <button class="btn ghost" id=lzo style="padding:6px 13px">−</button>
      <button class="btn ghost" id=lzi style="padding:6px 13px">+</button>
      <button class="btn ghost" id=lz1 style="padding:6px 13px">Fit</button>
      <span class=hint id=lzlabel style="align-self:center;margin:0"></span>
    </div>
    <div id=lzwrap style="overflow:auto;max-height:420px;border:1px solid var(--line);
      border-radius:9px;background:var(--surface)">
      <img id=lzimg src="/ref?path=${encodeURIComponent(rel)}"
        style="display:block;width:100%;transform-origin:top left">
    </div>
    <div style="margin-top:14px;font-size:13px;line-height:1.8">
      <div style="font-weight:700;word-break:break-all">${i.name}</div>
      <div style="color:var(--soft)">
        Category · ${i.category}<br>
        Dimensions · ${i.w}×${i.h} px<br>
        File size · ${(i.bytes/1024).toFixed(0)} KB<br>
        Format · <b>${i.format}</b>${i.mislabelled?
          ` <span style=color:var(--signal)>(named .${i.name.split('.').pop()})</span>`:''}<br>
        Checksum · ${i.hash}<br>
        Path · ${i.rel}
      </div>
      ${i.risky?`<p class=hint style="color:var(--signal);margin-top:10px">
        This is ${i.format.toUpperCase()} with a .jpg name. Image APIs usually reject it —
        convert before using it as a remix reference.</p>`:''}
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn ghost" id=ldetailsel style="padding:8px 15px">
          ${LSEL.has(rel)?'Deselect':'Select for deletion'}</button>
        ${i.risky?`<button class="btn ghost" id=ldetailconv style="padding:8px 15px">
          Convert this to JPEG</button>`:''}
      </div>
    </div>`;
  /* Zoom 1 = fits the sidebar width, and that is the default every time an image
     is opened. The img is width:100%, so scale(1) is exactly "fit" — anything
     above it overflows into the scroll container on purpose. */
  const wrap=$('#lzwrap'), img=$('#lzimg');
  const setZ=()=>{
    img.style.transform=`scale(${LZOOM})`;
    // The wrapper must grow with the scaled image or there is nothing to scroll.
    wrap.scrollTop=wrap.scrollLeft=0;
    img.style.marginBottom=LZOOM>1?`${(LZOOM-1)*img.clientHeight}px`:'0';
    $('#lzlabel').textContent=LZOOM===1?'Fit':LZOOM.toFixed(1).replace(/\.0$/,'')+'×';
  };
  setZ();
  $('#lzi').onclick=()=>{LZOOM=Math.min(LZOOM*1.4,8);setZ()};
  $('#lzo').onclick=()=>{LZOOM=Math.max(LZOOM/1.4,1);setZ()};
  $('#lz1').onclick=()=>{LZOOM=1;setZ()};
  $('#ldetailsel').onclick=()=>{
    LSEL.has(rel)?LSEL.delete(rel):LSEL.add(rel); renderLibrary(); showDetail(rel); };
  if($('#ldetailconv')) $('#ldetailconv').onclick=()=>convert([rel]);
}
async function convert(paths,allRisky){
  const n=allRisky?LIB.risky.length:paths.length;
  if(!n){alert('Nothing to convert — no AVIF/HEIC files here.');return;}
  if(!confirm(`Convert ${n} file(s) to real JPEG?\n\nOriginals are copied to `+
    `references/_originals/ first. Expect each file to get roughly 5-10x bigger — `+
    `AVIF is a far more efficient codec, and that size is the price of a format `+
    `the image APIs accept.`))return;
  $('#lstate').textContent='Converting '+n+' file(s)…';
  const r=await (await fetch('/ref/convert',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(allRisky?{all_risky:true}:{paths})})).json();
  await loadLibrary();
  if(LCUR) showDetail(LCUR);
  $('#lstate').innerHTML+=` · converted <b>${r.converted}</b>`+
    (r.before_bytes?` (${(r.before_bytes/1048576).toFixed(1)}MB → ${(r.after_bytes/1048576).toFixed(1)}MB)`:'')+
    (r.failed.length?` · <span style=color:var(--signal)>${r.failed.length} failed</span>`:'');
}
$('#lcat')&&($('#lcat').onchange=renderLibrary);
$('#ldupes')&&($('#ldupes').onclick=()=>{LDUPE=!LDUPE;
  $('#ldupes').textContent=LDUPE?'Show all':'Show duplicates'; renderLibrary()});
$('#lselall')&&($('#lselall').onclick=()=>{visibleItems().forEach(i=>LSEL.add(i.rel));renderLibrary()});
$('#lselnone')&&($('#lselnone').onclick=()=>{LSEL.clear();renderLibrary()});
$('#lconvert')&&($('#lconvert').onclick=()=>convert(null,true));
$('#ldelete')&&($('#ldelete').onclick=async()=>{
  if(!confirm(`Move ${LSEL.size} image(s) to references/_deleted/?\n\nNot erased — restorable from that folder.`))return;
  const r=await (await fetch('/ref/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[...LSEL]})})).json();
  LSEL.clear(); await loadLibrary();
  $('#lstate').innerHTML+=` · moved ${r.deleted} to references/_deleted/`;
});
$('#limportbtn')&&($('#limportbtn').onclick=()=>$('#limportfile').click());
$('#limportfile')&&($('#limportfile').onchange=async e=>{
  const files=[...e.target.files]; if(!files.length)return;
  $('#lstate').textContent=`Importing ${files.length}…`;
  const payload=[]; for(const f of files) payload.push({name:f.name,data:await toB64(f)});
  const r=await (await fetch('/ref/import',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({category:$('#limportcat').value,files:payload})})).json();
  await loadLibrary();
  $('#lstate').innerHTML+=` · imported ${r.saved.length}`+(r.failed.length?` · ${r.failed.length} failed`:'');
});


/* ================= existing projects ================= */
async function renderProjectList(){
  const el=$('#plist'); if(!el)return;
  const {projects:ps}=await (await fetch('/projects')).json();
  const rows=await Promise.all(ps.map(async n=>{
    const s=await (await fetch('/project/summary?project='+encodeURIComponent(n))).json();
    return {n,s};
  }));
  el.innerHTML=rows.map(({n,s})=>`
    <div style="display:flex;gap:12px;align-items:center;justify-content:space-between;
      padding:11px 0;border-bottom:1px solid var(--line)">
      <div>
        <b>${n}</b>
        <div class=hint style="margin:2px 0 0">
          ${s.evidence||0} segment(s) · ${s.extractions||0} extraction(s) ·
          ${s.renders||0} render(s) · ${((s.bytes||0)/1048576).toFixed(1)} MB
          ${s.segments&&s.segments.length?'<br>'+s.segments.join(', '):''}
        </div>
      </div>
      <button class="btn ghost pdel" data-n="${n}"
        style="border-color:var(--signal);color:var(--signal);padding:8px 15px">Delete</button>
    </div>`).join('')||'<p class=hint>No projects yet.</p>';
  document.querySelectorAll('.pdel').forEach(b=>b.onclick=()=>deleteProject(b.dataset.n,
    rows.find(r=>r.n===b.dataset.n).s));
}
async function deleteProject(name,s){
  const has=(s.extractions||0)+(s.renders||0)+(s.evidence||0);
  let msg=`Delete project "${name}"?\n\n`;
  if(has) msg+=`It contains ${s.evidence||0} evidence file(s), ${s.extractions||0} `+
    `extraction(s) and ${s.renders||0} render(s) — ${((s.bytes||0)/1048576).toFixed(1)} MB.\n`+
    `Extractions cost real money to produce.\n\n`;
  msg+=`It will be moved to projects/_deleted/ , not erased.`;
  if(!confirm(msg))return;
  if(has>20 && !confirm(`Last check — "${name}" is a substantial project.\n\nArchive it?`))return;
  const r=await (await fetch('/project/delete',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})).json();
  const m=$('#npmsg');
  if(r.error){m.textContent='⚠ '+r.error;m.style.color='var(--signal)';return;}
  m.innerHTML=`Archived <b>${name}</b> → <code>${r.archived_to}</code>`;
  m.style.color='var(--accent)';
  await renderProjectList(); loadProjects&&loadProjects();
}

</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", download=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            stages = [{"name": n, "desc": d, "costs": c, "source": s}
                      for n, d, c, s in STAGES]
            page = (PAGE.replace("__COMPLIANCE__", compliance_notes() or
                                 "no medical-causation or treatment claims.")
                        .replace("__SIZES__", "".join(
                            f'<option value="{v}">{k}</option>' for k, v in SIZES.items()))
                        .replace("__STAGES__", json.dumps(stages)))
            return self._send(200, page, "text/html; charset=utf-8")
        if u.path == "/references":
            return self._send(200, json.dumps(list_references()))
        if u.path == "/projects":
            return self._send(200, json.dumps({"projects": projects()}))
        if u.path == "/segments":
            p = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            return self._send(200, json.dumps({"segments": segments(p) if p else []}))
        if u.path == "/project/summary":
            n = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            return self._send(200, json.dumps(project_summary(n) or {}))
        if u.path == "/library":
            return self._send(200, json.dumps(library()))
        if u.path == "/products":
            rows = []
            for p in projects():
                try:
                    products.migrate_legacy(p)
                    for prod in products.list_products(p):
                        rows.append(products.summary(p, prod))
                except products.ProductError:
                    continue
            return self._send(200, json.dumps({"products": rows}))
        if u.path == "/product":
            q = urllib.parse.parse_qs(u.query)
            n = q.get("project", [""])[0]
            try:
                products.migrate_legacy(n)
                prod = products.resolve_product(n, q.get("product", [""])[0])
                doc = products.load(n, prod)
                segs = products.load_segments(n, prod)
                ready = products.readiness(n, prod)
            except products.ProductError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            a, t = products.completeness(doc, products.PRODUCT_SECTIONS)
            return self._send(200, json.dumps(
                {"project": n, "product": prod, "doc": doc, "segments": segs,
                 "readiness": ready, "answered": a, "total": t,
                 "pipeline_segments": [
                     {"project": pr, "slug": sl}
                     for pr in ([n] + [x for x in projects() if x != n])
                     for sl in segments(pr)],
                 "missing_required": products.missing_required(
                     doc, products.PRODUCT_SECTIONS)}))
        if u.path == "/product/schema":
            sch = products.schema()
            sch["enrich_sections"] = [
                {"key": s["key"], "title": s["title"], "fields": len(fs)}
                for s, fs in enrich.enrichable()]
            sch["synth_sections"] = synth.sections()
            return self._send(200, json.dumps(sch))
        if u.path == "/piccs":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            return self._send(200, json.dumps(
                {"cards": picc_cards(pr) if pr else []}))
        if u.path == "/levers":
            q = urllib.parse.parse_qs(u.query)
            try:
                return self._send(200, json.dumps(levers.load(
                    q.get("project", [""])[0], q.get("segment", [""])[0])))
            except levers.LeverError as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if u.path == "/leverschema":
            try:
                return self._send(200, json.dumps({"groups": presets.schema()}))
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e), "groups": []}))
        if u.path == "/presets":
            try:
                items = presets.catalogue()
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e), "groups": []}))
            groups = []
            for _lo, _hi, label in presets.GROUPS:
                rows = [i for i in items if i["group"] == label]
                if rows:
                    groups.append({"label": label, "items": rows})
            return self._send(200, json.dumps({"groups": groups}))
        if u.path == "/outputs":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            return self._send(200, json.dumps(project_outputs(pr) if pr else {}))
        if u.path == "/logs":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            if pr not in projects():
                return self._send(200, json.dumps({"stages": []}))
            return self._send(200, json.dumps({"stages": log_stages(pr)}))
        if u.path == "/logs/export":
            q = urllib.parse.parse_qs(u.query)
            try:
                name, blob = export_logs(q.get("project", [""])[0],
                                         q.get("stage", [""])[0])
            except ValueError as e:
                # A browser navigation, not a fetch — answer in something a
                # person reading a blank tab can act on.
                return self._send(404, str(e), "text/plain")
            return self._send(200, blob, "application/zip", download=name)
        if u.path == "/voc-files":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            if pr not in projects():
                return self._send(200, json.dumps({"error": "unknown project"}))
            return self._send(200, json.dumps({"files": segment_voc_files(pr)}))
        if u.path == "/refine-voc-files":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            if pr not in projects():
                return self._send(200, json.dumps({"error": "unknown project"}))
            return self._send(200, json.dumps({"files": refine_voc_files(pr)}))
        if u.path == "/file":
            q = urllib.parse.parse_qs(u.query)
            rel = q.get("path", [""])[0]
            try:
                full = safe_project_file(rel)
            except remix.RemixError:
                return self._send(404, b"not found", "text/plain")
            if full.lower().endswith(IMG_EXT):
                ctype = mimetypes.guess_type(full)[0] or "image/png"
                return self._send(200, open(full, "rb").read(), ctype)
            # Text: cap what we ship to the browser — some corpora are ~500KB.
            data = open(full, encoding="utf-8", errors="replace").read()
            clipped = len(data) > 200_000
            return self._send(200, json.dumps(
                {"text": data[:200_000], "clipped": clipped,
                 "bytes": os.path.getsize(full)}))
        if u.path == "/settings":
            q = urllib.parse.parse_qs(u.query)
            project = (q.get("project") or [""])[0]
            path = os.path.join(ROOT, "projects", project, "project.json")
            if not SAFE_NAME.match(project or "") or not store.exists(path):
                return self._send(404, json.dumps({"error": "unknown project"}))
            cfg = json.load(open(path, encoding="utf-8"))
            return self._send(200, json.dumps({
                "schema": settings.schema(),
                "values": settings.current(cfg),
                "path": os.path.relpath(path, ROOT)}))
        if u.path == "/skills":
            import cli
            extractors = cli.extractor_skill_manifest()
            by_stage = {name: cli.stage_skill_manifest(name)
                        for name in cli.STAGE_SKILLS}
            # The extract stage's skills are the 20 dimensions themselves, so it
            # is assembled here rather than duplicated into STAGE_SKILLS.
            by_stage["extract"] = [
                {"file": row["file"], "label": f"{row['n']:02d} {row['title']}",
                 "purpose": "one dimension, read from the segment file",
                 "present": row["present"], "lines": row["lines"]}
                for row in extractors]
            by_stage["run"] = by_stage["extract"] + by_stage.get("picc", [])
            return self._send(200, json.dumps({
                "extractors": [{"n": r["n"], "title": r["title"]}
                               for r in extractors],
                "stage_skills": by_stage,
                "presets": {k: v for k, v in cli.PRESETS.items()}}))
        if u.path == "/keys":
            try:
                state = credentials.status()
            except credentials.CredentialStoreError as error:
                return self._send(500, json.dumps({"error": str(error)}))
            return self._send(200, json.dumps(state))
        if u.path == "/ref":
            q = urllib.parse.parse_qs(u.query)
            rel = q.get("path", [""])[0]
            try:
                p = safe_ref_path(rel)
            except remix.RemixError:
                return self._send(404, b"not found", "text/plain")
            if q.get("thumb"):
                p, ctype = thumbnail(p, rel)
            else:
                ctype = mimetypes.guess_type(p)[0] or "image/png"
            return self._send(200, open(p, "rb").read(), ctype)
        self._send(404, b"not found", "text/plain")

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/settings":
            req = self._json()
            project = req.get("project") or ""
            full = os.path.join(ROOT, "projects", project, "project.json")
            if not SAFE_NAME.match(project) or not store.exists(full):
                return self._send(404, json.dumps({"error": "unknown project"}))
            try:
                with _lock:
                    cfg = json.load(open(full, encoding="utf-8"))
                    # All-or-nothing: one bad value writes none of them, so a run
                    # can never start against a half-applied config.
                    updated = settings.apply(cfg, req.get("values") or {})
                    settings.write(full, updated)
            except settings.SettingsError as error:
                return self._send(400, json.dumps({"error": str(error)}))
            except (OSError, ValueError) as error:
                return self._send(500, json.dumps({"error": str(error)}))
            return self._send(200, json.dumps({
                "saved": True, "values": settings.current(updated)}))
        if path == "/keys":
            req = self._json()
            values = {key: req[key] for key in credentials.PROVIDER_ENV if key in req}
            clear = req.get("clear") or ()
            try:
                with _lock:
                    state = credentials.update(values, clear=clear)
            except credentials.CredentialStoreError as error:
                return self._send(500, json.dumps({"error": str(error)}))
            return self._send(200, json.dumps(state))
        if path == "/project":
            req = self._json()
            try:
                cfg = create_project(req.get("name", "").strip(),
                                     req.get("product", "").strip(),
                                     req.get("market", "").strip())
            except ValueError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "project": cfg["name"]}))
        if path == "/project/delete":
            req = self._json()
            try:
                dest = delete_project(req.get("name", ""))
            except (ValueError, OSError) as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, "archived_to": dest}))
        if path == "/upload":
            return self._upload(self._json())
        if path == "/ref/delete":
            req = self._json()
            done, failed = [], []
            for rel in req.get("paths", []):
                try:
                    done.append(delete_reference(rel))
                except Exception as e:
                    failed.append(f"{rel}: {e}")
            return self._send(200, json.dumps(
                {"deleted": len(done), "trash": done, "failed": failed}))
        if path == "/ref/convert":
            req = self._json()
            paths = req.get("paths") or []
            if req.get("all_risky"):
                paths = library()["risky"]
            done, failed, skipped = [], [], []
            before = after = 0
            for rel in paths:
                r = convert_reference(rel)
                if r.get("error"):
                    failed.append(f"{rel}: {r['error']}")
                elif r.get("skipped"):
                    skipped.append(rel)
                else:
                    done.append(rel); before += r["before"]; after += r["after"]
            global _dims_mem
            _dims_mem = {}          # dimensions/format cache is now stale
            return self._send(200, json.dumps(
                {"converted": len(done), "failed": failed, "skipped": len(skipped),
                 "before_bytes": before, "after_bytes": after}))
        if path == "/ref/import":
            req = self._json()
            cat = os.path.basename(req.get("category") or "10_Other")
            dest_dir = os.path.join(REFS, cat)
            os.makedirs(dest_dir, exist_ok=True)
            saved, failed = [], []
            for f in req.get("files", []):
                name = os.path.basename(f.get("name", "") or "ref.png")
                if not name.lower().endswith(IMG_EXT):
                    failed.append(f"{name}: not an image"); continue
                blob = f.get("data", "")
                if blob.startswith("data:") and "," in blob:
                    blob = blob.split(",", 1)[1]
                try:
                    raw = base64.b64decode(blob)
                except Exception:
                    failed.append(f"{name}: could not decode"); continue
                dest = os.path.join(dest_dir, name)
                n = 1
                while store.exists(dest):
                    b, e = os.path.splitext(os.path.join(dest_dir, name))
                    dest = f"{b}_{n}{e}"; n += 1
                open(dest, "wb").write(raw)
                saved.append(os.path.relpath(dest, REFS))
            return self._send(200, json.dumps({"saved": saved, "failed": failed}))
        if path == "/presets/levers":
            req = self._json()
            try:
                p = presets.by_id(req.get("preset", ""))
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps(
                {"preset": {"id": p["id"], "name": p["name"]},
                 "levers": p["levers"]}))
        if path == "/presets/conflicts":
            req = self._json()
            try:
                p = presets.by_id(req.get("preset", ""))
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            out = {}
            for rel in req.get("references", [])[:400]:
                cs = presets.conflicts(p, rel)
                if cs:
                    out[rel] = cs
            return self._send(200, json.dumps(
                {"preset": {"id": p["id"], "name": p["name"]}, "conflicts": out}))
        if path == "/presets/pick":
            req = self._json()
            try:
                keys = _credential_snapshot()
            except credentials.CredentialStoreError as error:
                return self._send(200, json.dumps({"error": str(error)}))
            try:
                with auditlog.scope(project=req.get("project"),
                                    segment=req.get("segment"),
                                    stage="preset_pick", source="studio"):
                    picked = presets.pick(
                        req.get("brief", ""), req.get("reference", ""), keys)
                return self._send(200, json.dumps(picked))
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e)}))
        if path == "/product/create":
            req = self._json()
            try:
                out = add_product(req.get("project", ""), req.get("doc") or {})
            except (ValueError, products.ProductError) as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, **out}))
        if path == "/product/move":
            req = self._json()
            try:
                out = move_product(req.get("project", ""), req.get("product", ""),
                                   req.get("to", ""))
            except (ValueError, products.ProductError) as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({"ok": True, **out}))
        if path == "/product/enrich":
            req = self._json()
            try:
                keys = _credential_snapshot()
            except credentials.CredentialStoreError as error:
                return self._send(200, json.dumps({"error": str(error)}))
            proj = req.get("project", "")
            try:
                prod = products.resolve_product(proj, req.get("product", ""))
                segs = products.load_segments(proj, prod)
            except products.ProductError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            slug = req.get("segment", "")
            seg = next((x for x in segs if x["slug"] == slug), None)
            if not seg:
                return self._send(200, json.dumps(
                    {"error": f"no saved segment {slug!r} — save the segment first"}))
            # The segment's research lives under its pipeline slug; without one
            # there are no extractions to read and nothing to propose from.
            ev = products.value_of(seg["doc"]["identity"].get("evidence_slug")) or slug
            ev_proj = products.value_of(
                seg["doc"]["identity"].get("evidence_project")) or proj
            try:
                out = enrich.suggest(
                    proj, prod, ev, seg["doc"],
                    sections=req.get("sections") or None, keys=keys,
                    dry_run=bool(req.get("dry_run")), evidence_project=ev_proj)
            except enrich.EnrichError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            except products.ProductError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps(out))
        if path == "/product/synth":
            req = self._json()
            try:
                keys = _credential_snapshot()
            except credentials.CredentialStoreError as error:
                return self._send(200, json.dumps({"error": str(error)}))
            proj = req.get("project", "")
            try:
                prod = products.resolve_product(proj, req.get("product", ""))
                segs = products.load_segments(proj, prod)
            except products.ProductError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            slug = req.get("segment", "")
            seg = next((x for x in segs if x["slug"] == slug), None)
            if not seg:
                return self._send(200, json.dumps(
                    {"error": f"no saved segment {slug!r} — save the segment first"}))
            try:
                out = synth.synthesise(
                    proj, prod, slug, seg["doc"],
                    sections_wanted=req.get("sections") or None, keys=keys,
                    dry_run=bool(req.get("dry_run")))
            except (synth.SynthError, products.ProductError) as e:
                return self._send(200, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps(out))
        if path == "/product/save":
            req = self._json()
            proj = req.get("project", "")
            try:
                prod = products.resolve_product(proj, req.get("product", ""))
                doc = products.save(proj, prod, req.get("doc") or {})
                if req.get("segments") is not None:
                    products.save_segments(proj, prod, req["segments"])
                ready = products.readiness(proj, prod)
            except products.ProductError as e:
                return self._send(200, json.dumps({"error": str(e)}))
            a, t = products.completeness(doc, products.PRODUCT_SECTIONS)
            return self._send(200, json.dumps(
                {"ok": True, "answered": a, "total": t, "readiness": ready,
                 "missing_required": products.missing_required(
                     doc, products.PRODUCT_SECTIONS),
                 "sheet": os.path.relpath(products.sheet_path(proj, prod), ROOT)}))
        if path == "/prompt":
            return self._prompt(self._json())
        if path == "/briefs":
            return self._briefs(self._json())
        if path == "/generate":
            return self._generate(self._json())
        if path == "/run":
            return self._run(self._json())
        self._send(404, json.dumps({"error": "unknown endpoint"}))

    def _upload(self, req):
        """Land an uploaded VOC dump inside the project so ingest has a real,
        owned input — rather than a path pointing at some file elsewhere on disk."""
        proj = req.get("project", "")
        if proj not in projects():
            return self._send(200, json.dumps({"error": "unknown project"}))
        name = os.path.basename(req.get("filename", "voc.txt")) or "voc.txt"
        if not name.lower().endswith((".txt", ".jsonl", ".json", ".csv", ".md", ".zip")):
            name += ".txt"
        blob = req.get("data", "")
        if blob.startswith("data:") and "," in blob:
            blob = blob.split(",", 1)[1]
        try:
            raw = base64.b64decode(blob)
        except Exception:
            return self._send(200, json.dumps({"error": "could not decode upload"}))
        if not raw:
            return self._send(200, json.dumps({"error": "empty file"}))
        project_dir = os.path.join(ROOT, "projects", proj)
        if req.get("kind") == "import":
            # An export of someone else's stage 01-06 run. Read it here rather
            # than only at run time: parsing the whole thing costs a fraction of
            # a second, so the plan can be shown before anything is written and
            # a zip of the wrong shape is caught while it is still just a file.
            dest_dir = paths.research(project_dir, "imports")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            open(dest, "wb").write(raw)
            try:
                rows = importer.plan(importer.audience_members(raw))
            except zipfile.BadZipFile:
                return self._send(200, json.dumps({"error": "not a readable zip"}))
            return self._send(200, json.dumps(
                {"ok": True, "path": dest, "bytes": len(raw),
                 "rel": os.path.relpath(dest, ROOT),
                 "plan": [{"slug": r["slug"], "name": r["name"],
                           "items": r["items"], "segment_id": r["segment_id"]}
                          for r in rows]}))
        dest_dir = paths.voc(project_dir, "raw")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        open(dest, "wb").write(raw)
        return self._send(200, json.dumps(
            {"ok": True, "path": dest, "bytes": len(raw),
             "rel": os.path.relpath(dest, ROOT)}))

    def _prompt(self, req):
        """Assemble the prompt without sending it, so it can be reviewed first.

        Generation is the step that costs money and cannot be undone, so the
        exact text goes back to the browser and nothing is sent until it comes
        back confirmed.
        """
        try:
            prompt, chosen, mode = build_image_prompt(req)
        except presets.PresetError as e:
            return self._send(200, json.dumps({"error": str(e)}))
        return self._send(200, json.dumps({
            "prompt": prompt,
            "preset": (f"{chosen['id']} {chosen['name']} · "
                       f"{'preset' if mode == 'preset' else 'reference'} wins"
                       if chosen else None),
            "levers_set": len({k: v for k, v in (req.get("levers") or {}).items()
                               if str(v).strip()}),
        }))

    def _briefs(self, req):
        try:
            keys = _credential_snapshot()
        except credentials.CredentialStoreError as error:
            return self._send(200, json.dumps({"error": str(error)}))
        project = req.get("project") or ""
        segment = req.get("segment") or ""
        try:
            data = levers.load(project, segment)
        except levers.LeverError as e:
            return self._send(200, json.dumps({"error": str(e)}))

        chosen = req.get("chosen") or {}
        missing = levers.missing_required(chosen)
        if missing:
            return self._send(200, json.dumps(
                {"error": "Pick a " + " and a ".join(m.lower() for m in missing)
                          + " first."}))
        lever_text = levers.selection_text(data, chosen)

        preset = None
        if req.get("preset"):
            try:
                preset = presets.by_id(req["preset"])
            except presets.PresetError as e:
                return self._send(200, json.dumps({"error": str(e)}))

        cfg = {}
        try:
            cfg = json.load(open(os.path.join(ROOT, "projects", project,
                                              "project.json"), encoding="utf-8"))
        except Exception:
            pass
        facts = ""
        if cfg.get("facts"):
            try:
                facts = open(os.path.join(ROOT, cfg["facts"]), encoding="utf-8").read()
            except OSError:
                facts = ""

        try:
            n = max(1, min(8, int(req.get("n") or
                                  cfg.get("creative", {}).get("briefs_per_run", 4))))
        except (TypeError, ValueError):
            n = 4

        try:
            with auditlog.scope(project=project, segment=segment,
                                stage="remix_briefs", source="studio"):
                out = briefs.write(
                    lever_text, n=n, keys=keys,
                    product=cfg.get("product", ""), market=cfg.get("market", ""),
                    compliance=compliance_notes(project) if project else "",
                    facts=facts, preset=preset,
                    custom_levers=req.get("levers") or {},
                    extra=req.get("extra") or "")
        except briefs.BriefError as e:
            return self._send(200, json.dumps({"error": str(e)}))
        return self._send(200, json.dumps(out))

    def _generate(self, req):
        try:
            key = credentials.resolve("openai")
        except credentials.CredentialStoreError as error:
            return self._send(200, json.dumps({"error": str(error)}))
        if not key:
            return self._send(200, json.dumps(
                {"error": "No OpenAI key — add one on the Settings tab."}))
        product = req.get("product", "")
        if product.startswith("data:") and "," in product:
            product = product.split(",", 1)[1]
        try:
            prod = base64.b64decode(product)
            if not prod:
                raise ValueError
        except Exception:
            return self._send(200, json.dumps({"error": "no product image"}))
        try:
            ref = safe_ref_path(req.get("reference", ""))
        except remix.RemixError as e:
            return self._send(200, json.dumps({"error": str(e)}))

        # Use the prompt the operator confirmed, verbatim. Rebuilding it here
        # would mean the text they approved and the text that gets sent are
        # produced twice and could differ; the point of the review step is that
        # they cannot.
        try:
            built, chosen, mode = build_image_prompt(req)
        except presets.PresetError as e:
            return self._send(200, json.dumps({"error": str(e)}))
        prompt = (req.get("prompt") or "").strip() or built
        try:
            with auditlog.scope(project=req.get("project"), segment=req.get("segment"),
                                stage="remix_image", source="studio"):
                out = remix.remix_images(
                    prompt,
                    [(os.path.basename(ref), open(ref, "rb").read()), ("product.png", prod)],
                    key, size=req.get("size"))
        except remix.RemixError as e:
            return self._send(200, json.dumps({"error": str(e)}))

        meta = None
        if req.get("strip_exif"):
            before = len(out)
            out, removed = exifstrip.strip(out)
            meta = {"stripped": bool(removed),
                    "detail": exifstrip.describe(removed, before, len(out))}
        self._send(200, json.dumps(
            {"image": "data:image/png;base64," + base64.b64encode(out).decode(),
             "meta": meta,
             "preset": (f"{chosen['id']} {chosen['name']} · "
                        f"{'preset' if mode == 'preset' else 'reference'} wins"
                        if chosen else None)}))

    def _run(self, req):
        """Stream a pipeline stage's output to the browser as it runs."""
        stage = req.get("stage", "")
        valid = {n for n, _, _, _ in STAGES}
        if stage not in valid:
            return self._send(400, json.dumps({"error": "unknown stage"}))

        cmd = [sys.executable, os.path.join(ROOT, "pipeline", "cli.py"),
               "-p", req.get("project") or "montisella"]
        if req.get("approve"):
            cmd.append("--yes")
        cmd.append(stage)
        if req.get("force"):
            cmd.append("--force")
        if req.get("provider"):
            cmd += ["--provider", str(req["provider"])]
        if req.get("model"):
            cmd += ["--model", str(req["model"])]
        if stage in ("extract", "run"):
            if req.get("skills"):
                cmd += ["--skills", str(req["skills"])]
            elif req.get("preset"):
                cmd += ["--preset", str(req["preset"])]
        if stage in ("concepts", "run"):
            if req.get("n_concepts"):
                cmd += ["--concepts", str(int(req["n_concepts"]))]
            if req.get("n_hooks"):
                cmd += ["--hooks", str(int(req["n_hooks"]))]
            if req.get("picc"):
                cmd += ["--picc", str(req["picc"])]
        if stage in ("brief", "run"):
            if req.get("n_briefs"):
                cmd += ["--briefs", str(int(req["n_briefs"]))]
        # PICC/concepts/briefs build on a specific product in this project. Forward
        # the one the operator picked; the CLI resolves the single product when "")
        # is sent, so multi-product projects must make an explicit choice.
        if stage in ("picc", "concepts", "brief", "run"):
            if req.get("product"):
                cmd += ["--product", str(req["product"])]
        if stage == "import":
            source = req.get("source") or ""
            # Filesystem, matching where _upload put it and how the CLI reads it.
            if not (os.path.isfile(source) or os.path.isdir(source)):
                return self._send(200, "Nothing uploaded to import yet.\n",
                                  "text/plain; charset=utf-8")
            # The browser has already been shown the plan and pressed Run, so
            # the CLI's own confirmation would be a second prompt with no
            # terminal to answer it.
            if "--yes" not in cmd:
                cmd.append("--yes")
            cmd.append(source)
        elif stage == "ingest":
            if req.get("rules_only"):
                cmd.append("--rules-only")
            src = req.get("source") or ""
            if not store.exists(src):
                return self._send(200, f"Raw VOC file not found:\n  {src}\n",
                                  "text/plain; charset=utf-8")
            cmd.append(src)
        elif stage == "refine-voc":
            # Project-wide deterministic export: unlike downstream research
            # stages it consumes the completed ingest artefacts, not a segment.
            src = req.get("refine_source") or ""
            if src:
                allowed = {os.path.realpath(row["path"])
                           for row in refine_voc_files(req.get("project") or "")}
                src = os.path.realpath(src)
                if src not in allowed:
                    return self._send(
                        200, "Selected VOC file is not a refinable file in this project.\n",
                        "text/plain; charset=utf-8")
                cmd += ["--source", src]
        elif stage != "segment":
            seg = req.get("segment") or ""
            if not seg:
                return self._send(200, "No segment selected.\n",
                                  "text/plain; charset=utf-8")
            cmd.append(seg)
        elif stage == "segment":
            vsrc = req.get("voc_source") or ""
            if vsrc:
                if not store.exists(vsrc):
                    return self._send(200, f"VOC source not found:\n  {vsrc}\n",
                                      "text/plain; charset=utf-8")
                cmd += ["--source", vsrc]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            # The child CLI resolves env -> private store itself. Do not copy a
            # secret through Studio process state or construct a second policy.
            proc = subprocess.Popen(cmd, cwd=ROOT, env=dict(os.environ), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=1, text=True)
            for line in proc.stdout:
                self.wfile.write(line.encode("utf-8", "replace"))
                self.wfile.flush()
            proc.wait()
            self.wfile.write(f"\n— finished (exit {proc.returncode}) —\n".encode())
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self.wfile.write(f"\nrunner error: {e}\n".encode())
            except Exception:
                pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def credential_line():
    """Which providers have a key, and where a key typed in Settings will land.

    Never the key itself — `credentials.status()` returns presence booleans for
    exactly this reason.

    On a laptop this is barely worth saying: you typed them in, and the store is
    in your home directory. On a container it is the first thing that goes wrong
    and the last thing anyone notices. A provider variable named slightly wrong,
    or a store path that is not on the mounted volume and so is wiped by the next
    deploy, both look exactly like a working studio right up until someone
    generates. Printing it at startup puts the answer in the logs, before the
    question.
    """
    try:
        state = credentials.status()
    except credentials.CredentialStoreError as error:
        return f"  API keys: could not read the store — {error}"
    have = sorted(name for name, present in state.items() if present)
    where = credentials.store_path()
    if not have:
        return f"  No API keys yet — add them on the Settings tab (stored at {where})."
    return f"  API keys: {', '.join(have)} · store {where}"


def main():
    if not store.exists(REFS):
        sys.exit(f"No references/ folder at {REFS}")
    n = sum(len(v) for v in list_references().values())
    url = f"http://localhost:{PORT}"
    print(f"\n  adpipe studio  →  {url}" if HOST == "127.0.0.1"
          else f"\n  adpipe studio  →  {HOST}:{PORT}")
    print(f"  {n} reference ads · {len(projects())} project(s)")
    print(credential_line())
    print("  Ctrl-C to stop.\n")
    # Double-clicking Ad Studio.command should pop the browser; a supervisor that
    # opens its own preview pane should not get a second stray window, and a
    # container has no browser to open at all.
    remote = HOST != "127.0.0.1"
    if os.environ.get("STUDIO_NO_BROWSER") not in ("1", "true", "yes") and not remote:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        Server((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("  stopped.")


if __name__ == "__main__":
    main()
