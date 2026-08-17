#!/usr/bin/env python3
"""
Remix a reference ad into a finished ad for your product, in one shot.

Sends the image model TWO images — a reference ad from your swipe file and a photo
of your product — plus an instruction ("recreate this layout with my product,
headline = X, subtext = Y"). The model returns a complete ad, text included.

This is the alternative to the render.py compositor. Compared to it:
  + uses ANY of the 221 reference ads, not just the 6 hand-built templates
  + no template authoring — the reference image IS the template
  - the model writes the text as pixels, so qa.py CANNOT verify it is claim-safe.
    YOU must read every output against the compliance rules before shipping.
  - spelling, exact brand font, and long subtext are best-effort, not guaranteed.

    export OPENAI_API_KEY=sk-...
    python3 pipeline/remix.py projects/montisella/output/<segment>/remix.json
    python3 pipeline/remix.py <remix.json> --only C02 --dry-run

Standard library only. Talks to the OpenAI image-edit endpoint (gpt-image-1),
which accepts multiple input images; override IMAGE_* env vars for another provider.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request

import credentials
import uuid

import auditlog
import store

EDIT_URL = os.environ.get("IMAGE_EDIT_URL", "https://api.openai.com/v1/images/edits")
MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
SIZE = os.environ.get("IMAGE_SIZE", "1024x1536")   # portrait, ~4:5
USD_PER_IMAGE = float(os.environ.get("IMAGE_USD", "0.19"))

# Text that must be present in a remix instruction so the model preserves layout
# and doesn't just paint a scene. A prompt missing all of these is probably wrong.
LAYOUT_WORDS = ("layout", "same composition", "recreate", "structure", "arrangement")


class RemixError(Exception):
    """Raised by the core so callers decide how to surface it — the CLI prints and
    exits, the web app returns it as JSON. The core never calls sys.exit itself."""


def _multipart(fields, files):
    """Build a multipart/form-data body by hand (no `requests` dependency).
    `files` is a list of (field_name, filename, data_bytes) — the image-edit
    endpoint takes the reference and product images under a repeated `image[]`."""
    boundary = "----adpipe" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
        body += f"{v}\r\n".encode()
    for field, fn, data in files:
        ctype = mimetypes.guess_type(fn)[0] or "image/png"
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{field}"; '
                 f'filename="{fn}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return boundary, bytes(body)


def remix_images(prompt, images, key, size=None, retries=3):
    """Core: send in-memory images to the edit endpoint, return the result bytes.
    `images` is a list of (filename, bytes). Shared by the CLI and the web app."""
    files = [("image[]", fn, data) for fn, data in images]
    request_log = {
        "model": MODEL, "prompt": prompt, "size": size or SIZE, "n": 1,
        "images": [{"filename": fn, "bytes": len(data),
                    "sha256": __import__("hashlib").sha256(data).hexdigest()}
                   for fn, data in images],
    }
    audit = auditlog.start("openai", MODEL, "image_edit", request_log,
                           metadata={"endpoint": EDIT_URL, "retries": retries})
    boundary, body = _multipart(
        {"model": MODEL, "prompt": prompt, "size": size or SIZE, "n": "1"}, files)
    req = urllib.request.Request(
        EDIT_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
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
                time.sleep(2 ** attempt * 5); continue
            if e.code == 401:
                raise RemixError("401 — the image API key was rejected.")
            raise RemixError(f"HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            audit.event("network_error", str(e.reason), attempt=attempt + 1)
            if attempt < retries - 1:
                time.sleep(5); continue
            raise RemixError(f"Network error: {e.reason}")
        except Exception as e:
            audit.error(e, attempt=attempt + 1)
            raise
    raise RemixError("Gave up after retries.")


def remix(prompt, image_paths, key, retries=3):
    for p in image_paths:
        if not os.path.exists(p):
            raise RemixError(f"image not found: {p}")
    images = [(os.path.basename(p), store.read_bytes(p)) for p in image_paths]
    return remix_images(prompt, images, key, retries=retries)


def main():
    ap = argparse.ArgumentParser(description="Remix reference ads with your product.")
    ap.add_argument("manifest", help="path to remix.json")
    ap.add_argument("--only", help="comma-separated ids")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-strip-exif", action="store_true",
                    help="keep EXIF; default is to strip it")
    args = ap.parse_args()

    manifest = os.path.abspath(args.manifest)
    parts = manifest.split(os.sep)
    if "projects" in parts:
        n = parts.index("projects")
        if n + 1 < len(parts):
            auditlog.set_context(project=parts[n + 1], stage="remix", source="remix_cli")

    doc = store.read_json(args.manifest)
    base = os.path.dirname(os.path.abspath(args.manifest))
    out = os.path.join(base, "remixes")

    product = doc.get("product_image", "")
    prod_path = product if os.path.isabs(product) else os.path.join(base, product)

    jobs = doc["ads"]
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        jobs = [j for j in jobs if j["id"] in want]

    todo = []
    for j in jobs:
        dest = os.path.join(out, f"{j['id']}.png")
        if os.path.exists(dest) and not args.force:
            continue
        ref = j["reference"] if os.path.isabs(j["reference"]) \
            else os.path.join(base, j["reference"])
        if not any(w in j["prompt"].lower() for w in LAYOUT_WORDS):
            print(f"  ! {j['id']}: prompt doesn't ask to keep the layout — "
                  f"add 'recreate this layout' or the model may ignore the reference")
        todo.append((j, ref, dest))

    if not todo:
        print("  all remixes present (--force to redo)"); return

    print(f"  {len(todo)} ads to remix at {SIZE} via {MODEL}")
    print(f"  estimated cost: ~${len(todo) * USD_PER_IMAGE:.2f} "
          f"(verify against current pricing)")
    print(f"  product image: {product or 'MISSING — set product_image in remix.json'}")

    if args.dry_run:
        for j, ref, _ in todo:
            print(f"    {j['id']}: {os.path.basename(ref)} + product -> {j['id']}.png")
        return

    if not product:
        sys.exit("\n  remix.json has no product_image. This method needs a photo of "
                 "your product to put into the ad.")
    if not os.path.exists(prod_path):
        sys.exit(f"\n  product image not found: {prod_path}")

    try:
        key = credentials.resolve("openai")
    except credentials.CredentialStoreError as error:
        sys.exit(str(error))
    if not key:
        sys.exit("\n  No OpenAI key. Add one on the Settings tab of the studio, "
                 "or export OPENAI_API_KEY — see docs/IMAGE_SETUP.md.")

    for i, (j, ref, dest) in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {j['id']}: {os.path.basename(ref)} + product …",
              flush=True)
        try:
            data = remix(j["prompt"], [ref, prod_path], key)
        except RemixError as e:
            sys.exit(f"  {e}")
        note = ""
        if not args.no_strip_exif:
            import exifstrip
            before = len(data)
            data, removed = exifstrip.strip(data)
            note = "  " + exifstrip.describe(removed, before, len(data))
        store.write_bytes(dest, data)
        print(f"      -> remixes/{j['id']}.png{note}")

    print("\n  ⚠️  COMPLIANCE: qa.py cannot read text baked into an image. Open every")
    print("      remix and check the headline/subtext against the rules in")
    print("      projects/<project>/project.json → compliance, BEFORE you ship.")
    print("      Also check spelling of 'Montisella' and any numbers.")


if __name__ == "__main__":
    main()
