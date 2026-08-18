#!/usr/bin/env python3
"""
Generate the background/product plates for a segment's ads.

Plates are images ONLY — no text ever. Image models cannot spell reliably, so
every word in the finished ad is composited by render.py on top of the plate.
A plate that comes back with lettering in it is a defect; regenerate it.

    export OPENAI_API_KEY=sk-...
    python3 pipeline/plates.py projects/montisella/output/side_sleepers_night_pain/plates.json

    --only C01,C05      just those concepts
    --dry-run           print what would be generated and the cost, spend nothing
    --wire              after generating, write the filenames into concepts.json

Uses only the standard library — no extra packages. Supports OpenAI (default) and
any endpoint speaking the same images API.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import re

import credentials

import auditlog
import paths
import store

API_URL = os.environ.get("IMAGE_API_URL", "https://api.openai.com/v1/images/generations")
MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
# Portrait. render.py crops with object-fit: cover, so this only needs the right
# shape for a 4:5 ad, not the exact pixel dimensions.
SIZE = os.environ.get("IMAGE_SIZE", "1024x1536")
# Rough list price for gpt-image-1 at 1024x1536, quality=high. Verify against
# current OpenAI pricing — this is only here so --dry-run gives you a ballpark.
USD_PER_IMAGE = float(os.environ.get("IMAGE_USD", "0.17"))

BANNED_IN_PROMPT = ("text", "word", "caption", "label", "logo", "typography")


# ------------------------------------------------------------------ catalogue

# The Plates table a production brief ends with: | Concept | Slot | Prompt |.
# plates.json is the same content already extracted, so it wins when present;
# this is for the segment whose brief has been written but not yet extracted.
PLATE_ROW = re.compile(r"^\|\s*(?P<concept>C?\d+[A-Za-z]?)\s*\|\s*`?(?P<slot>[a-z_]+)`?"
                       r"\s*\|\s*(?P<prompt>.+?)\s*\|\s*$", re.M)


def plates_from_brief(text):
    """Plate prompts read out of a production brief's own table.

    Deliberately strict about the row shape rather than clever: a brief holds
    several markdown tables, and a loose match turns the QA checklist into
    image prompts nobody asked to pay for.
    """
    return [{"concept": m.group("concept"), "slot": m.group("slot"),
             "prompt": m.group("prompt").strip()}
            for m in PLATE_ROW.finditer(str(text or ""))
            if len(m.group("prompt").strip()) > 40]


def catalogue(project_dir):
    """Every segment in a project that has plate prompts ready to generate.

    Two sources, in order of trust: plates.json, which is the prompts already
    extracted and is what plates.py itself reads, then the brief's own table for
    a segment whose brief exists but has not been extracted yet. Reporting which
    one answered matters — one is a file somebody checked, the other is a regex
    over prose.
    """
    out = []
    assets = paths.assets(project_dir)
    if not store.exists(assets):
        return out
    for segment in sorted(store.dirs_in(assets)):
        seg_dir = os.path.join(assets, segment)
        rows, source = [], None
        pj = os.path.join(seg_dir, "plates.json")
        if store.exists(pj):
            data = store.read_json(pj, {}) or {}
            rows = [r for r in (data.get("plates") or [])
                    if isinstance(r, dict) and str(r.get("prompt") or "").strip()]
            source = "plates.json"
        if not rows:
            bp = os.path.join(seg_dir, "03_production_brief.md")
            if store.exists(bp):
                rows = plates_from_brief(store.read_text(bp) or "")
                source = "brief" if rows else None
        if not rows:
            continue
        out.append({
            "segment": segment,
            "source": source,
            "plates": [{"concept": str(r.get("concept") or ""),
                        "slot": str(r.get("slot") or "image"),
                        "prompt": str(r.get("prompt") or "").strip()}
                       for r in rows],
        })
    return out


def generate(prompt: str, key: str, retries: int = 3) -> bytes:
    request_body = {
        "model": MODEL,
        "prompt": prompt,
        "size": SIZE,
        "n": 1,
    }
    audit = auditlog.start("openai", MODEL, "image_generation", request_body,
                           metadata={"endpoint": API_URL, "retries": retries})
    body = json.dumps(request_body).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                payload = json.load(r)
            item = payload["data"][0]
            if "b64_json" in item:
                data = base64.b64decode(item["b64_json"])
                logged = {**payload, "data": [{**item, "b64_json":
                          f"[saved as response.png · {len(data)} bytes]"}]}
            else:
                with urllib.request.urlopen(item["url"], timeout=300) as img:
                    data = img.read()
                logged = payload
            audit.binary_response(data, logged)
            return data
        except urllib.error.HTTPError as e:
            detail_full = e.read().decode("utf-8", "replace")
            detail = detail_full[:400]
            audit.event("http_error", detail_full, status=e.code,
                        attempt=attempt + 1)
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                wait = 2 ** attempt * 5
                print(f"      {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            if e.code == 401:
                sys.exit("  401 — OPENAI_API_KEY was rejected. Check the key.")
            if e.code == 400:
                sys.exit(f"  400 — the API rejected the prompt:\n  {detail}")
            sys.exit(f"  HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            audit.event("network_error", str(e.reason), attempt=attempt + 1)
            if attempt < retries - 1:
                time.sleep(5)
                continue
            sys.exit(f"  Network error: {e.reason}")
        except Exception as e:
            audit.error(e, attempt=attempt + 1)
            raise
    sys.exit("  Gave up after retries.")


def main():
    ap = argparse.ArgumentParser(description="Generate ad plates (images, never text).")
    ap.add_argument("plates", help="path to plates.json")
    ap.add_argument("--only", help="comma-separated concept ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wire", action="store_true",
                    help="write generated filenames into concepts.json")
    ap.add_argument("--force", action="store_true", help="regenerate existing plates")
    ap.add_argument("--no-strip-exif", action="store_true",
                    help="keep EXIF; default is to strip it")
    args = ap.parse_args()

    manifest = os.path.abspath(args.plates)
    parts = manifest.split(os.sep)
    if "projects" in parts:
        n = parts.index("projects")
        if n + 1 < len(parts):
            auditlog.set_context(project=parts[n + 1], stage="plates", source="plates_cli")

    doc = store.read_json(args.plates)
    base = os.path.dirname(os.path.abspath(args.plates))
    out = os.path.join(base, "plates")

    jobs = doc["plates"]
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        jobs = [j for j in jobs if j["concept"] in want]

    todo = []
    for j in jobs:
        dest = os.path.join(out, j["file"])
        if os.path.exists(dest) and not args.force:
            continue
        # A plate must not contain lettering. Every prompt should say so.
        if not any(b in j["prompt"].lower() for b in BANNED_IN_PROMPT):
            print(f"  ! {j['file']}: prompt does not forbid text — add 'no text' to it")
        todo.append((j, dest))

    if not todo:
        print("  all plates present (--force to regenerate)")
        return

    print(f"  {len(todo)} plates to generate at {SIZE} via {MODEL}")
    print(f"  estimated cost: ~${len(todo) * USD_PER_IMAGE:.2f} "
          f"(at ${USD_PER_IMAGE:.2f}/image — verify against current pricing)")
    if args.dry_run:
        for j, dest in todo:
            print(f"    {j['concept']}.{j['slot']:12} -> {j['file']}")
        return

    try:
        key = credentials.resolve("openai")
    except credentials.CredentialStoreError as error:
        sys.exit(str(error))
    if not key:
        sys.exit(
            "No OpenAI key. Add one on the Settings tab of the studio, or:\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "See docs/IMAGE_SETUP.md for where to put it permanently."
        )

    for i, (j, dest) in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {j['concept']}.{j['slot']} …", flush=True)
        data = generate(j["prompt"], key)
        note = ""
        if not args.no_strip_exif:
            import exifstrip
            before = len(data)
            data, removed = exifstrip.strip(data)
            note = "  " + exifstrip.describe(removed, before, len(data))
        store.write_bytes(dest, data)
        print(f"      -> plates/{j['file']}{note}")

    if args.wire:
        cp = os.path.join(base, "concepts.json")
        cdoc = store.read_json(cp)
        by_id = {c["id"]: c for c in cdoc["concepts"]}
        for j, _ in todo:
            c = by_id.get(j["concept"])
            if c:
                c["slots"][j["slot"]] = f"plates/{j['file']}"
        store.write_json(cp, cdoc)
        print(f"\n  wired {len(todo)} plates into concepts.json")

    print("\n  Now re-render:  ./adpipe render <segment>")
    print("  Check each plate for stray lettering before you ship — that's the one")
    print("  failure mode the compositor cannot catch.")


if __name__ == "__main__":
    main()
