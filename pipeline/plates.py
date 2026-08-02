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

API_URL = os.environ.get("IMAGE_API_URL", "https://api.openai.com/v1/images/generations")
MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
# Portrait. render.py crops with object-fit: cover, so this only needs the right
# shape for a 4:5 ad, not the exact pixel dimensions.
SIZE = os.environ.get("IMAGE_SIZE", "1024x1536")
# Rough list price for gpt-image-1 at 1024x1536, quality=high. Verify against
# current OpenAI pricing — this is only here so --dry-run gives you a ballpark.
USD_PER_IMAGE = float(os.environ.get("IMAGE_USD", "0.17"))

BANNED_IN_PROMPT = ("text", "word", "caption", "label", "logo", "typography")


def generate(prompt: str, key: str, retries: int = 3) -> bytes:
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "size": SIZE,
        "n": 1,
    }).encode()
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
                return base64.b64decode(item["b64_json"])
            with urllib.request.urlopen(item["url"], timeout=300) as img:
                return img.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
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
            if attempt < retries - 1:
                time.sleep(5)
                continue
            sys.exit(f"  Network error: {e.reason}")
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

    doc = json.load(open(args.plates, encoding="utf-8"))
    base = os.path.dirname(os.path.abspath(args.plates))
    out = os.path.join(base, "plates")
    os.makedirs(out, exist_ok=True)

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

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit(
            "OPENAI_API_KEY is not set.\n"
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
        open(dest, "wb").write(data)
        print(f"      -> plates/{j['file']}{note}")

    if args.wire:
        cp = os.path.join(base, "concepts.json")
        cdoc = json.load(open(cp, encoding="utf-8"))
        by_id = {c["id"]: c for c in cdoc["concepts"]}
        for j, _ in todo:
            c = by_id.get(j["concept"])
            if c:
                c["slots"][j["slot"]] = f"plates/{j['file']}"
        json.dump(cdoc, open(cp, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print(f"\n  wired {len(todo)} plates into concepts.json")

    print("\n  Now re-render:  ./adpipe render <segment>")
    print("  Check each plate for stray lettering before you ship — that's the one")
    print("  failure mode the compositor cannot catch.")


if __name__ == "__main__":
    main()
