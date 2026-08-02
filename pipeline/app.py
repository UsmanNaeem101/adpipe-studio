#!/usr/bin/env python3
"""
adpipe studio — the whole pipeline in a browser. No terminal needed.

Three tabs:
  Remix    upload a product photo, write a brief, pick reference layouts, generate
  Pipeline run any stage (ingest -> ... -> render) and watch the output live
  Settings paste your API keys

Keys are typed into the UI and held in memory for the session. They are NEVER
written into this project folder (which syncs to OneDrive). Optionally the browser
can remember them in localStorage so you don't retype — that's opt-in and local.

Start it by double-clicking `Ad Studio.command`, or:
    ./adpipe studio

Standard library only.
"""

from __future__ import annotations

import base64
import http.server
import json
import mimetypes
import os
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import remix  # noqa: E402
import exifstrip  # noqa: E402

REFS = os.path.join(ROOT, "references")
PORT = int(os.environ.get("STUDIO_PORT", "8765"))
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
SIZES = {"4:5 portrait": "1024x1536", "1:1 square": "1024x1024",
         "1.91:1 landscape": "1536x1024"}

# In-memory only. Never persisted server-side, never logged.
KEYS = {"openai": os.environ.get("OPENAI_API_KEY", ""),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openrouter": os.environ.get("OPENROUTER_API_KEY", "")}
_lock = threading.Lock()

STAGES = [
    ("ingest",   "Skills 01-02: filter + deduplicate",         True,  True),
    ("segment",  "Skills 03-06: discover, validate, assign, build", True, False),
    ("extract",  "Skills 07-26: 20 dimensions, batched",       True,  False),
    ("picc",     "Skill 27 + PICC card + 5 angles",            True,  False),
    ("concepts", "10 concepts + hooks + layouts",             True,  False),
    ("brief",    "Production briefs",                         True,  False),
    ("qa",       "Compliance gate",                           False, False),
    ("render",   "Composite copy into layouts -> PNG",        False, False),
    ("run",      "Everything: extract -> ... -> render",      True,  False),
]


def projects():
    d = os.path.join(ROOT, "projects")
    if not os.path.isdir(d):
        return []
    return sorted(p for p in os.listdir(d)
                  if os.path.isdir(os.path.join(d, p)) and not p.startswith(("_", ".")))


def segments(project):
    out = []
    for d in (os.path.join(ROOT, "projects", project, "evidence"),):
        if os.path.isdir(d):
            out += [f[:-4] for f in sorted(os.listdir(d)) if f.endswith(".txt")]
    return out


SAFE_NAME = __import__("re").compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


def create_project(name, product, market):
    """A project is a folder + a project.json. Clone montisella's config so the
    filter regexes and compliance profile are a starting point, not blank."""
    if not SAFE_NAME.match(name or ""):
        raise ValueError("name must be lowercase letters, digits, - or _ (2-41 chars)")
    dest = os.path.join(ROOT, "projects", name)
    if os.path.exists(dest):
        raise ValueError(f"project {name!r} already exists")
    base = os.path.join(ROOT, "projects", "montisella", "project.json")
    cfg = json.load(open(base, encoding="utf-8")) if os.path.exists(base) else {}
    cfg.update({"name": name, "product": product or name, "market": market or ""})
    cfg["_comment"] = ("Per-project config cloned from montisella. REPLACE the filter "
                       "regexes and compliance profile for this niche before ingesting.")
    for sub in ("voc", "evidence", "extractions", "output"):
        os.makedirs(os.path.join(dest, sub), exist_ok=True)
    json.dump(cfg, open(os.path.join(dest, "project.json"), "w", encoding="utf-8"),
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
    if not os.path.isdir(root):
        return {}
    prov = {}
    pp = os.path.join(root, "evidence", "_provenance.json")
    if os.path.exists(pp):
        try:
            prov = json.load(open(pp, encoding="utf-8"))
        except Exception:
            prov = {}
    out = {"stages": [], "provenance": prov}

    def entry(stage, label, path, kind="text"):
        if os.path.exists(path):
            rel = os.path.relpath(path, ROOT)
            out["stages"].append({"stage": stage, "label": label, "path": rel,
                                  "kind": kind,
                                  "size": os.path.getsize(path),
                                  "mtime": os.path.getmtime(path)})

    voc = os.path.join(root, "voc")
    for f, lbl in (("retained_voc.jsonl", "01 retained"),
                   ("rejected_voc.jsonl", "01 rejected"),
                   ("deduplicated_voc.jsonl", "02 deduplicated"),
                   ("duplicate_groups.jsonl", "02 duplicate groups"),
                   ("filtered_voc.jsonl", "corpus the segment stage reads"),
                   ("candidate_segments.json", "03 candidates"),
                   ("validated_segments.json", "04 decisions"),
                   ("segment_assignments.jsonl", "05 assignments"),
                   ("unassigned_evidence.md", "06 unassigned"),
                   ("segment_evidence_manifest.yaml", "06 manifest"),
                   ("assignment_conflicts.jsonl", "06 conflicts"),
                   ("missing_evidence.jsonl", "06 missing")):
        entry("ingest" if f.startswith(("retained", "rejected", "dedup", "duplicate"))
              else "segment", lbl, os.path.join(voc, f))

    ed = os.path.join(root, "evidence")
    if os.path.isdir(ed):
        for f in sorted(os.listdir(ed)):
            if f.endswith(".txt"):
                entry("segment", f"06 evidence · {f[:-4]}", os.path.join(ed, f))

    xd = os.path.join(root, "extractions")
    if os.path.isdir(xd):
        for seg in sorted(os.listdir(xd)):
            d = os.path.join(xd, seg)
            if os.path.isdir(d):
                for f in sorted(os.listdir(d)):
                    entry("extract", f"{seg} · {f[:-3]}", os.path.join(d, f))

    od = os.path.join(root, "output")
    if os.path.isdir(od):
        for seg in sorted(os.listdir(od)):
            d = os.path.join(od, seg)
            if not os.path.isdir(d):
                continue
            for f, st in (("01_picc_card.md", "picc"), ("02_concepts.md", "concepts"),
                          ("concepts.json", "concepts"),
                          ("03_production_brief.md", "brief"),
                          ("plates.json", "brief"), ("remix.json", "brief")):
                entry(st, f"{seg} · {f}", os.path.join(d, f))
            rd = os.path.join(d, "renders")
            if os.path.isdir(rd):
                for f in sorted(os.listdir(rd)):
                    if f.lower().endswith(IMG_EXT):
                        entry("render", f"{seg} · {f}", os.path.join(rd, f), "image")
    return out


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
        with open(path, "rb") as f:
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
        with open(path, "rb") as f:
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
    while os.path.exists(dest):
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
    if not os.path.exists(backup):
        with open(full, "rb") as a, open(backup, "wb") as b:
            b.write(a.read())
    before = os.path.getsize(full)
    tmp = full + ".converting.jpg"
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
             full, "--out", tmp],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError((r.stderr or "sips failed").strip()[:200])
        if real_format(tmp) != "jpeg":
            raise RuntimeError("sips produced a non-JPEG")
        os.replace(tmp, full)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return {"rel": rel, "error": str(e)}
    return {"rel": rel, "from": fmt, "before": before,
            "after": os.path.getsize(full)}


def safe_project_file(rel):
    """Only ever serve files from inside projects/."""
    base = os.path.realpath(os.path.join(ROOT, "projects"))
    full = os.path.realpath(os.path.join(ROOT, rel))
    if not full.startswith(base + os.sep) or not os.path.exists(full):
        raise remix.RemixError("path outside projects/")
    return full


def compliance_notes(project="montisella"):
    try:
        cfg = json.load(open(os.path.join(ROOT, "projects", project, "project.json"),
                             encoding="utf-8"))
        return cfg.get("compliance", {}).get("notes", "")
    except Exception:
        return ""


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
    if not os.path.exists(full):
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
    if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(full_path):
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
 .drop img{max-width:100%;max-height:210px;border-radius:8px}
 .cats{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:11px;
   padding:6px;background:var(--surface)}
 .cat h3{font-size:13px;margin:12px 8px 6px;color:var(--soft);position:sticky;top:0;
   background:var(--surface);padding:4px 0}
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
 .hide{display:none}
</style></head><body>
<header>
  <div class=htop><b>adpipe studio</b><span>voice-of-customer → finished ads</span></div>
  <div class=tabs>
    <button class="tab on" data-t=remix>Remix</button>
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
        <h2>1 · Your product</h2>
        <div class=drop id=drop>
          <div id=dropmsg>Click or drop a product photo</div>
          <img id=preview hidden>
        </div>
        <input type=file id=file accept="image/*" class=hide>
        <p class=hint>A clean shot of the real product. This gets placed into each layout.</p>
      </div>
      <div class=card>
        <h2>2 · Creative brief</h2>
        <label>What each ad should say and feel like</label>
        <textarea id=brief placeholder="Headline: You didn't buy bad pillows, you bought the wrong height.
Subtext: It's set by your shoulder, not by how it feels in the shop.
CTA: Which height are you?
Calm premium bedding brand, deep green accent. Spell 'Montisella' exactly."></textarea>
        <label style="margin-top:14px">Output shape</label>
        <select id=size>__SIZES__</select>
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
        <h2>3 · Pick reference layouts</h2>
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

<!-- ================= PIPELINE ================= -->
<section id=t-pipeline class=hide>
  <div class=card>
    <h2>Run a stage</h2>
    <div class=row style="margin-bottom:16px">
      <div><label>Project</label><select id=proj></select></div>
      <div><label>Segment</label><select id=seg></select></div>
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
      <label>Which dimensions?</label>
      <select id=preset>
        <option value=quick>Quick — 6 · pain points, moments, failed solutions, objections, VOC, terminology</option>
        <option value=standard>Standard — 10 · adds outcomes, emotion, limiting beliefs, mechanism</option>
        <option value=full selected>Full — all 20</option>
        <option value=custom>Custom…</option>
      </select>
      <div id=skillpick hidden style="margin-top:10px;max-height:200px;overflow:auto;
           border:1px solid var(--line);border-radius:9px;padding:10px"></div>
    </div>
    <div id=opt_concepts class=opts style="margin-top:14px" hidden>
      <div class=row>
        <div><label>Concepts</label><input type=number id=nconcepts value=10 min=1 max=30></div>
        <div><label>Hooks each</label><input type=number id=nhooks value=3 min=1 max=6></div>
      </div>
    </div>
    <div id=opt_brief class=opts style="margin-top:14px" hidden>
      <label>Production briefs</label><input type=number id=nbriefs value=4 min=1 max=10>
    </div>
    <div style="margin-top:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap">
      <button class=btn id=runbtn disabled>Run stage</button>
      <label style="font-weight:500;font-size:14px;display:flex;gap:8px;align-items:center;margin:0">
        <input type=checkbox id=approve style="width:auto"> I approve any API spend for this run
      </label>
    </div>
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
      <div><label>Product</label><input id=npproduct placeholder="Montisella lumbar cushion"></div>
    </div>
    <label style=margin-top:10px>Market</label>
    <input id=npmarket placeholder="desk workers with lower back pain, UK/US">
    <button class=btn id=npbtn style=margin-top:14px>Create project</button>
    <p class=hint id=npmsg>Clones montisella's config. <b>Replace the filter regexes and
      compliance profile</b> in <code>projects/&lt;name&gt;/project.json</code> before ingesting —
      they are tuned for shoulder and neck pain, not your niche.</p>

    <h2 style="margin-top:26px">Existing projects</h2>
    <div id=plist></div>
    <p class=hint>Deleting archives the folder to <code>projects/_deleted/</code> —
      nothing is erased, and you can restore it by moving it back.</p>
  </div>
  <div class=card style="max-width:760px">
    <h2>API keys</h2>
    <p class=hint style="margin:0 0 16px">Held in memory while the app runs. Never written
      into this project folder. Tick “remember” to keep them in this browser only.</p>

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
      <label style="font-weight:500;font-size:14px;display:flex;gap:8px;align-items:center;margin:0">
        <input type=checkbox id=remember style="width:auto"> Remember in this browser
      </label>
      <button class="btn ghost" id=clearkeys>Clear</button>
    </div>
    <p class=hint id=keymsg></p>
  </div>
</section>

</div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
var LIB,LSEL,LDUPE,SKILLS;

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
  if(b.dataset.t==='outputs'  && typeof loadOutputs==='function') loadOutputs();
  if(b.dataset.t==='settings' && typeof renderProjectList==='function') renderProjectList();
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
  setPill('openai',j.openai); setPill('anthropic',j.anthropic);
  setPill('openrouter',j.openrouter);
  if($('#remember').checked) localStorage.setItem('adpipe_keys',JSON.stringify(body));
  else localStorage.removeItem('adpipe_keys');
  $('#keymsg').textContent='Saved '+new Date().toLocaleTimeString()+
    ($('#remember').checked?' · remembered in this browser':' · this session only');
}
$('#savekeys').onclick=pushKeys;
$('#clearkeys').onclick=async()=>{ $('#k_openai').value=''; $('#k_anthropic').value='';
  $('#k_openrouter').value='';
  localStorage.removeItem('adpipe_keys'); $('#remember').checked=false; await pushKeys();
  $('#keymsg').textContent='Cleared.'; };
(function(){ const s=localStorage.getItem('adpipe_keys');
  if(s){ try{ const k=JSON.parse(s); $('#k_openai').value=k.openai||'';
    $('#k_anthropic').value=k.anthropic||''; $('#k_openrouter').value=k.openrouter||'';
    $('#remember').checked=true; pushKeys(); }catch(e){} }
  fetch('/keys').then(r=>r.json()).then(j=>{setPill('openai',j.openai);
    setPill('anthropic',j.anthropic);setPill('openrouter',j.openrouter);});
})();

// ---------- remix ----------
let product=null, selected=new Map();
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
  $('#gohint').textContent=ok?`Will generate ${selected.size} ad(s).`
    :'Add a product photo and pick at least one template.'; }

$('#go').onclick=async()=>{
  const brief=$('#brief').value.trim(), size=$('#size').value;
  $('#resultscard').hidden=false; const res=$('#results'); res.innerHTML='';
  $('#go').disabled=true;
  const jobs=[...selected.entries()];
  const cards=jobs.map(([rel,nice])=>{ const el=document.createElement('div');
    el.className='res'; el.innerHTML=`<div class=spin>generating…<br><small>${nice}</small></div>`;
    res.appendChild(el); return el;});
  for(let i=0;i<jobs.length;i++){
    const [rel,nice]=jobs[i];
    try{
      const r=await fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({product,reference:rel,brief,size,
        strip_exif:$('#stripexif')?$('#stripexif').checked:false})});
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
        cards[i].innerHTML=`<img src="${j.image}"><div class=meta><span>${nice}</span>`+
          `<a download="remix_${nm}.png" href="${j.image}">download</a></div>${badge}`;}
    }catch(e){ cards[i].innerHTML=`<div class=err>✕ ${e}</div>`; }
  }
  $('#go').disabled=false;
};

// ---------- pipeline ----------
let stage=null;
const STAGES=__STAGES__;
(function(){ const el=$('#stages');
  STAGES.forEach(s=>{ const d=document.createElement('div'); d.className='stage';
    d.innerHTML=`<b>${s.name}</b><small>${s.desc}</small>`+
      (s.costs?'<div class=costs>COSTS API CREDIT</div>':'');
    d.onclick=()=>{ stage=s; $$('.stage').forEach(x=>x.classList.remove('on'));
      d.classList.add('on'); $('#ingestbox').classList.toggle('hide',!s.source);
      $('#runbtn').disabled=false;
      $('#runhint').textContent=s.costs?'This stage calls the Claude API — tick the approval box.':'Free — pure code.';
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
}
$('#runbtn').onclick=async()=>{
  if(!stage) return;
  const free = stage.name==='ingest' && $('#rulesonly').checked;
  if(stage.costs && !free && !$('#approve').checked){
    $('#runhint').textContent='Tick the approval box first — this stage spends credit.'; return; }
  const log=$('#log'); log.textContent=''; $('#runbtn').disabled=true;
  const body={stage:stage.name,project:$('#proj').value,segment:$('#seg').value,
              source:$('#ingestpath').value.trim(),approve:$('#approve').checked,
              rules_only:$('#rulesonly').checked,
              n_concepts:+$('#nconcepts').value||0, n_hooks:+$('#nhooks').value||0,
              n_briefs:+$('#nbriefs').value||0,
              provider:$('#provider')?$('#provider').value:'',
              model:$('#modelid')?$('#modelid').value.trim():''};
  if(stage.name==='extract'||stage.name==='run'){
    if($('#preset').value==='custom'){
      body.skills=[...document.querySelectorAll('.sk:checked')].map(c=>c.value).join(',');
      if(!body.skills){alert('Pick at least one dimension.');return;}
    } else body.preset=$('#preset').value;
  }
  const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const reader=r.body.getReader(), dec=new TextDecoder();
  while(true){ const {done,value}=await reader.read(); if(done) break;
    log.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight; }
  $('#runbtn').disabled=false;
};

/* ---------- project creation ---------- */
$('#npbtn').onclick=async()=>{
  const name=$('#npname').value.trim();
  const r=await (await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,product:$('#npproduct').value,market:$('#npmarket').value})})).json();
  const m=$('#npmsg');
  if(r.error){m.textContent='⚠ '+r.error;m.style.color='var(--signal)';return;}
  m.textContent='Created '+r.project+' — now edit its project.json filter regexes for this niche.';
  m.style.color='var(--accent)';
  $('#npname').value=$('#npproduct').value=$('#npmarket').value='';
  loadProjects();
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

/* ---------- stage option panels ---------- */
SKILLS=null;
async function loadSkills(){
  if(SKILLS)return SKILLS;
  SKILLS=await (await fetch('/skills')).json();
  $('#skillpick').innerHTML=SKILLS.extractors.map(x=>
    `<label style="font-weight:400;display:flex;gap:8px;align-items:center;margin:3px 0">
      <input type=checkbox class=sk value=${x.n} style=width:auto> ${String(x.n).padStart(2,'0')} ${x.title}</label>`).join('');
  return SKILLS;
}
$('#preset').onchange=async()=>{
  const custom=$('#preset').value==='custom';
  $('#skillpick').hidden=!custom;
  if(custom){const S=await loadSkills();
    const pre=S.presets.standard;
    document.querySelectorAll('.sk').forEach(c=>c.checked=pre.includes(+c.value));}
};
function showOpts(name){
  document.querySelectorAll('.opts').forEach(o=>o.hidden=true);
  if(name==='extract'||name==='run'){$('#opt_extract').hidden=false;loadSkills();}
  if(name==='concepts'||name==='run')$('#opt_concepts').hidden=false;
  if(name==='brief'||name==='run')$('#opt_brief').hidden=false;
}

/* ---------- outputs browser ---------- */
async function loadOutputs(){
  const pr=$('#oproj').value; if(!pr)return;
  const d=await (await fetch('/outputs?project='+encodeURIComponent(pr))).json();
  const byStage={};
  (d.stages||[]).forEach(x=>{(byStage[x.stage]=byStage[x.stage]||[]).push(x);});
  const order=['ingest','segment','extract','picc','concepts','brief','render'];
  $('#olist').innerHTML = order.filter(st=>byStage[st]).map(st=>
    `<div class=cat><h3>${st} — ${byStage[st].length}</h3>`+
    byStage[st].map(x=>`<div class=orow data-p="${x.path}" data-k="${x.kind}"
       style="padding:6px 9px;border-radius:7px;cursor:pointer;font-size:13px">
       ${x.label} <span style="color:var(--soft)">${(x.size/1024).toFixed(0)}KB</span></div>`).join('')+
    `</div>`).join('') || '<p class=hint style=padding:10px>Nothing produced yet.</p>';
  document.querySelectorAll('.orow').forEach(r=>r.onclick=()=>viewFile(r.dataset.p,r.dataset.k));
  const imported=Object.entries(d.provenance||{}).filter(([k,v])=>v.origin==='imported');
  $('#provwarn').innerHTML = imported.length ? `<div class=card
    style="background:#FDF3F0;border-color:var(--signal);margin-bottom:14px">
    <b style=color:var(--signal)>⚠ Imported evidence</b>
    <p class=hint style=margin-top:6px>${imported.map(([k,v])=>
      `<b>${k}</b> — ${v.detail}`).join('<br>')}<br><br>
      These were copied in, not produced by this project's pipeline. There are no
      candidate/validated/assignment records behind them, so anything built on them
      inherits an unverifiable lineage.</p></div>` : '';
}
async function viewFile(path,kind){
  const v=$('#oview');
  if(kind==='image'){v.innerHTML=`<img src="/file?path=${encodeURIComponent(path)}"
      style="max-width:100%;border-radius:10px"><p class=hint>${path}</p>`;return;}
  v.innerHTML='<p class=hint>Loading…</p>';
  const d=await (await fetch('/file?path='+encodeURIComponent(path))).json();
  v.innerHTML=`<p class=hint style=margin:0>${path}${d.clipped?' — showing first 200KB of '+(d.bytes/1024).toFixed(0)+'KB':''}</p>
    <pre style="white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;
      background:var(--surface);padding:14px;border-radius:9px;max-height:520px;
      overflow:auto;margin-top:10px">${d.text.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`;
}
$('#orefresh').onclick=loadOutputs;
$('#oproj').onchange=loadOutputs;


/* ================= project creation ================= */
$('#npbtn')&&($('#npbtn').onclick=async()=>{
  const m=$('#npmsg');
  const r=await (await fetch('/project',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:$('#npname').value.trim(),product:$('#npproduct').value,
                         market:$('#npmarket').value})})).json();
  if(r.error){m.textContent='⚠ '+r.error;m.style.color='var(--signal)';return;}
  m.innerHTML='✓ Created <b>'+r.project+'</b>. Now edit its filter regexes in projects/'+r.project+'/project.json.';
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

/* ================= stage options ================= */
SKILLS=null;
async function loadSkills(){
  if(SKILLS)return SKILLS;
  SKILLS=await (await fetch('/skills')).json();
  $('#skillpick').innerHTML=SKILLS.extractors.map(x=>
    `<label style="font-weight:400;display:flex;gap:8px;align-items:center;margin:3px 0;font-size:13px">
      <input type=checkbox class=sk value=${x.n} style=width:auto> ${String(x.n).padStart(2,'0')} ${x.title}</label>`).join('');
  return SKILLS;
}
$('#preset')&&($('#preset').onchange=async()=>{
  const c=$('#preset').value==='custom'; $('#skillpick').hidden=!c;
  if(c){const S=await loadSkills();
    document.querySelectorAll('.sk').forEach(x=>x.checked=S.presets.standard.includes(+x.value));}
});
function showOpts(name){
  document.querySelectorAll('.opts').forEach(o=>o.hidden=true);
  const model=['ingest','segment','extract','picc','concepts','brief','run'].includes(name);
  if($('#opt_model'))$('#opt_model').hidden=!model;
  if(name==='extract'||name==='run'){$('#opt_extract').hidden=false;loadSkills();}
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
async function loadOutputs(){
  const pr=$('#oproj')&&$('#oproj').value; if(!pr)return;
  const d=await (await fetch('/outputs?project='+encodeURIComponent(pr))).json();
  const by={}; (d.stages||[]).forEach(x=>{(by[x.stage]=by[x.stage]||[]).push(x)});
  const order=['ingest','segment','extract','picc','concepts','brief','render'];
  $('#olist').innerHTML=order.filter(k=>by[k]).map(k=>
    `<div class=cat><h3>${k} — ${by[k].length}</h3>`+by[k].map(x=>
      `<div class=orow data-p="${x.path}" data-k="${x.kind}" style="padding:6px 9px;
        border-radius:7px;cursor:pointer;font-size:13px">${x.label}
        <span style=color:var(--soft)>${(x.size/1024).toFixed(0)}KB</span></div>`).join('')+
    `</div>`).join('')||'<p class=hint style=padding:10px>Nothing produced yet.</p>';
  document.querySelectorAll('.orow').forEach(r=>r.onclick=()=>viewFile(r.dataset.p,r.dataset.k));
  const imp=Object.entries(d.provenance||{}).filter(([k,v])=>v.origin==='imported');
  $('#provwarn').innerHTML=imp.length?`<div class=card style="background:#FDF3F0;
    border-color:var(--signal);margin-bottom:14px"><b style=color:var(--signal)>⚠ Imported evidence</b>
    <p class=hint style=margin-top:6px>${imp.map(([k,v])=>`<b>${k}</b> — ${v.detail}`).join('<br>')}
    <br><br>Copied in, not produced by this project's pipeline. No candidate / validated /
    assignment records exist behind them, so anything built on them inherits an
    unverifiable lineage.</p></div>`:'';
}
async function viewFile(path,kind){
  const v=$('#oview');
  if(kind==='image'){v.innerHTML=`<img src="/file?path=${encodeURIComponent(path)}"
    style="max-width:100%;border-radius:10px"><p class=hint>${path}</p>`;return}
  v.innerHTML='<p class=hint>Loading…</p>';
  const d=await (await fetch('/file?path='+encodeURIComponent(path))).json();
  const esc=t=>t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  v.innerHTML=`<p class=hint style=margin:0>${path}${d.clipped?' — first 200KB of '+(d.bytes/1024).toFixed(0)+'KB':''}</p>
    <pre style="white-space:pre-wrap;font:12px/1.5 ui-monospace,Menlo,monospace;background:var(--surface);
      padding:14px;border-radius:9px;max-height:520px;overflow:auto;margin-top:10px">${esc(d.text)}</pre>`;
}
$('#orefresh')&&($('#orefresh').onclick=loadOutputs);
$('#oproj')&&($('#oproj').onchange=loadOutputs);

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

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
        if u.path == "/outputs":
            pr = urllib.parse.parse_qs(u.query).get("project", [""])[0]
            return self._send(200, json.dumps(project_outputs(pr) if pr else {}))
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
        if u.path == "/skills":
            import cli
            return self._send(200, json.dumps({
                "extractors": [{"n": n, "title": cli.SKILL_TITLES.get(n, str(n))}
                               for n in cli.EXTRACTORS],
                "presets": {k: v for k, v in cli.PRESETS.items()}}))
        if u.path == "/keys":
            with _lock:
                return self._send(200, json.dumps(
                    {k: bool(v) for k, v in KEYS.items()}))
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
        if path == "/keys":
            req = self._json()
            with _lock:
                for k in ("openai", "anthropic", "openrouter"):
                    if k in req:
                        KEYS[k] = (req[k] or "").strip()
                return self._send(200, json.dumps({k: bool(v) for k, v in KEYS.items()}))
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
                while os.path.exists(dest):
                    b, e = os.path.splitext(os.path.join(dest_dir, name))
                    dest = f"{b}_{n}{e}"; n += 1
                open(dest, "wb").write(raw)
                saved.append(os.path.relpath(dest, REFS))
            return self._send(200, json.dumps({"saved": saved, "failed": failed}))
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
        if not name.lower().endswith((".txt", ".jsonl", ".json", ".csv", ".md")):
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
        dest_dir = os.path.join(ROOT, "projects", proj, "voc", "raw")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        open(dest, "wb").write(raw)
        return self._send(200, json.dumps(
            {"ok": True, "path": dest, "bytes": len(raw),
             "rel": os.path.relpath(dest, ROOT)}))

    def _generate(self, req):
        with _lock:
            key = KEYS.get("openai", "")
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

        prompt = (
            "Recreate this reference ad's exact layout, composition and text placement, "
            "but replace the product with the item shown in the second image and replace "
            "all copy with the brief below. Keep the reference's structure and "
            "proportions. Render every word spelled exactly as written; put no text "
            "anywhere the brief doesn't specify.\n\nBRIEF:\n"
            + (req.get("brief") or "").strip())
        try:
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
             "meta": meta}))

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
        if stage in ("brief", "run"):
            if req.get("n_briefs"):
                cmd += ["--briefs", str(int(req["n_briefs"]))]
        if stage == "ingest":
            if req.get("rules_only"):
                cmd.append("--rules-only")
            src = req.get("source") or ""
            if not os.path.exists(src):
                return self._send(200, f"Raw VOC file not found:\n  {src}\n",
                                  "text/plain; charset=utf-8")
            cmd.append(src)
        elif stage != "segment":
            seg = req.get("segment") or ""
            if not seg:
                return self._send(200, "No segment selected.\n",
                                  "text/plain; charset=utf-8")
            cmd.append(seg)

        env = dict(os.environ)
        with _lock:
            if KEYS.get("anthropic"):
                env["ANTHROPIC_API_KEY"] = KEYS["anthropic"]
            if KEYS.get("openai"):
                env["OPENAI_API_KEY"] = KEYS["openai"]
            if KEYS.get("openrouter"):
                env["OPENROUTER_API_KEY"] = KEYS["openrouter"]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
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


def main():
    if not os.path.isdir(REFS):
        sys.exit(f"No references/ folder at {REFS}")
    n = sum(len(v) for v in list_references().values())
    url = f"http://localhost:{PORT}"
    print(f"\n  adpipe studio  →  {url}")
    print(f"  {n} reference ads · {len(projects())} project(s)")
    print("  Add your API keys on the Settings tab. Ctrl-C to stop.\n")
    # Double-clicking Ad Studio.command should pop the browser; a supervisor that
    # opens its own preview pane should not get a second stray window.
    if os.environ.get("STUDIO_NO_BROWSER") not in ("1", "true", "yes"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        Server(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("  stopped.")


if __name__ == "__main__":
    main()
