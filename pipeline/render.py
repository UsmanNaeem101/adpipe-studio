#!/usr/bin/env python3
"""
Composite ad copy into slotted HTML layouts and render to PNG via headless Chrome.

Text rendering in image models is unreliable, so copy is NEVER rendered by an image
model. The image model produces the background/product plate only; this script drops
the plate into a layout template and composites real, spell-correct text on top.

Usage:
    python3 pipeline/render.py output/desk_workers_posture/concepts.json
    python3 pipeline/render.py <concepts.json> --only C01,C04 --format 1x1
    python3 pipeline/render.py <concepts.json> --list-slots double_hook_proof_chip

Deps: none. Uses the system Chrome already on this Mac.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(ROOT, "pipeline")
TEMPLATES = os.path.join(PIPELINE, "templates")

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


# ---------------------------------------------------------------- templating

def render_template(tpl, data):
    """Mustache-lite: {{key}} escaped, {{{key}}} raw, {{#key}}..{{/key}} if truthy,
    {{^key}}..{{/key}} if falsy. Deliberately tiny - no dependencies, no logic in
    templates beyond show/hide."""

    def lookup(key):
        cur = data
        for part in key.strip().split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    # Sections first (innermost-last via non-greedy repeated passes).
    section = re.compile(r"\{\{([#^])\s*([\w.]+)\s*\}\}(.*?)\{\{/\s*\2\s*\}\}", re.S)
    for _ in range(12):  # bounded nesting
        new = section.sub(
            lambda m: (m.group(3) if lookup(m.group(2)) else "")
            if m.group(1) == "#"
            else ("" if lookup(m.group(2)) else m.group(3)),
            tpl,
        )
        if new == tpl:
            break
        tpl = new

    tpl = re.sub(r"\{\{\{\s*([\w.]+)\s*\}\}\}", lambda m: str(lookup(m.group(1)) or ""), tpl)
    tpl = re.sub(
        r"\{\{\s*([\w.]+)\s*\}\}",
        lambda m: html.escape(str(lookup(m.group(1)) or "")),
        tpl,
    )
    return tpl


def brand_css(brand):
    """Flatten brand.json into CSS custom properties."""
    lines = []
    for group in ("color", "type", "layout"):
        for k, v in brand.get(group, {}).items():
            lines.append(f"  --{group[:1]}-{k.replace('_', '-')}: {v};")
    css = ":root{\n" + "\n".join(lines) + "\n}"

    base = os.path.join(TEMPLATES, "_base.css")
    if os.path.exists(base):
        css += "\n" + open(base, encoding="utf-8").read()
    return css


DOC = """<!doctype html><html><head><meta charset="utf-8">
<style>:root{{--w:{w}px;--h:{h}px}}
{css}
</style></head><body>
{body}
<div id="__overflow__"></div>
<script>
/* Measure every slot after layout and report the ones whose text is actually
   clipped. render.py reads this in a --dump-dom pass so a truncated headline
   fails QA instead of quietly shipping.

   Measured in WHOLE LINES, not pixels. Glyph ascenders/descenders push
   scrollHeight a few px past clientHeight on any healthy block, and a box one
   line too short overflows by a similar few px — so no pixel tolerance can tell
   a sliced descender from normal overshoot. Comparing how many lines the content
   needs against how many the box can hold separates them exactly. */
(function () {{
  var bad = [];
  document.querySelectorAll('[data-slot]').forEach(function (el) {{
    var cs = getComputedStyle(el);
    var lh = parseFloat(cs.lineHeight);
    if (!lh || !isFinite(lh)) lh = parseFloat(cs.fontSize) * 1.2 || 20;
    // Padding is part of scrollHeight/clientHeight but is not text — leaving it in
    // makes a padded single-line box (a CTA pill) look like it needs three lines.
    var padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    var needed   = Math.round(Math.max(el.scrollHeight - padY, 0) / lh);
    var capacity = Math.max(Math.floor((el.clientHeight - padY + 2) / lh), 1);
    var clipped  = needed > capacity ||
                   el.scrollWidth > el.clientWidth + Math.max(8, lh * 0.35);
    if (clipped) bad.push(el.getAttribute('data-slot'));
  }});
  document.getElementById('__overflow__').textContent = JSON.stringify(bad);
}})();
</script>
</body></html>"""


def wrap_document(body, css, w, h):
    return DOC.format(w=w, h=h, css=css, body=body)


def template_slots(path):
    """Every {{...}} name a template references, minus internals."""
    src = open(path, encoding="utf-8").read()
    names = set(re.findall(r"\{\{[{#^]?\s*([\w.]+)\s*\}?\}\}", src))
    return sorted(n for n in names if not n.startswith("__"))


# ---------------------------------------------------------------- rendering

def find_chrome():
    # The error below has always told people to set CHROME, and nothing ever
    # read it. On a container that is the only way to name a browser that is
    # neither in /Applications nor first on PATH.
    override = str(os.environ.get("CHROME") or "").strip()
    if override:
        if os.path.exists(override) or shutil.which(override):
            return override
        sys.exit(f"CHROME is set to {override!r}, which is not there.")
    for c in CHROME_CANDIDATES:
        if os.path.exists(c):
            return c
    found = shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    sys.exit("No Chrome/Chromium found. Install Google Chrome, or set CHROME env var.")


def chrome_run(chrome, html_path, extra):
    cmd = [
        chrome, "--headless", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--allow-file-access-from-files", "--virtual-time-budget=4000",
        "--default-background-color=00000000",
    ] + extra + [f"file://{html_path}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def check_overflow(chrome, html_path, w, h):
    """Second pass: let the page measure its own text and report clipped slots.
    This is the 'headline readable' QA gate - a clipped hook is a dead ad."""
    res = chrome_run(chrome, html_path, ["--dump-dom", f"--window-size={w},{h}"])
    m = re.search(r'id="__overflow__"[^>]*>(.*?)</div>', res.stdout, re.S)
    if not m:
        return []
    try:
        return json.loads(html.unescape(m.group(1)) or "[]")
    except json.JSONDecodeError:
        return []


def resolve_asset(val, base_dir):
    """Turn a relative plate path into an absolute file:// URL Chrome can load."""
    if not val or re.match(r"^(https?:|data:|file:)", str(val)):
        return val
    for cand in (os.path.join(base_dir, val), os.path.join(ROOT, val)):
        if os.path.exists(cand):
            return "file://" + os.path.abspath(cand)
    return val  # leave as-is; QA will flag the missing plate


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Composite concepts into ad PNGs.")
    ap.add_argument("concepts", nargs="?", help="path to concepts.json")
    ap.add_argument("--out", help="output dir (default: <concepts dir>/renders)")
    ap.add_argument("--only", help="comma-separated concept ids, e.g. C01,C04")
    ap.add_argument("--format", help="override format for all (4x5|1x1|9x16)")
    ap.add_argument("--list-slots", metavar="TEMPLATE", help="print a template's slots and exit")
    ap.add_argument("--list-templates", action="store_true")
    ap.add_argument("--no-overflow-check", action="store_true")
    args = ap.parse_args()

    brand = json.load(open(os.path.join(PIPELINE, "brand.json"), encoding="utf-8"))

    if args.list_templates:
        for f in sorted(os.listdir(TEMPLATES)):
            if f.endswith(".html") and not f.startswith("_"):
                print(f"{f[:-5]:32} slots: {', '.join(template_slots(os.path.join(TEMPLATES, f)))}")
        return

    if args.list_slots:
        p = os.path.join(TEMPLATES, args.list_slots.replace(".html", "") + ".html")
        if not os.path.exists(p):
            sys.exit(f"No such template: {args.list_slots}")
        print("\n".join(template_slots(p)))
        return

    if not args.concepts:
        ap.error("concepts.json is required (or use --list-templates)")

    concepts_path = os.path.abspath(args.concepts)
    base_dir = os.path.dirname(concepts_path)
    doc = json.load(open(concepts_path, encoding="utf-8"))
    concepts = doc.get("concepts", doc if isinstance(doc, list) else [])

    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        concepts = [c for c in concepts if c.get("id") in want]
        if not concepts:
            sys.exit(f"No concepts matched --only {args.only}")

    out_dir = args.out or os.path.join(base_dir, "renders")
    os.makedirs(out_dir, exist_ok=True)
    chrome = os.environ.get("CHROME") or find_chrome()
    css = brand_css(brand)

    ok, failed, warnings = 0, 0, []

    for c in concepts:
        cid = c.get("id", f"C{ok + failed + 1:02d}")
        tpl_name = c.get("template")
        tpl_path = os.path.join(TEMPLATES, str(tpl_name) + ".html")
        if not tpl_name or not os.path.exists(tpl_path):
            print(f"  [{cid}] SKIP - unknown template {tpl_name!r}")
            failed += 1
            continue

        fmt = args.format or c.get("format", "4x5")
        dims = brand["formats"].get(fmt)
        if not dims:
            print(f"  [{cid}] SKIP - unknown format {fmt!r}")
            failed += 1
            continue
        w, h = dims["w"], dims["h"]

        slots = dict(c.get("slots", {}))
        for k, v in list(slots.items()):
            if k in ("image", "plate", "bg", "logo", "product_image") or str(k).endswith("_image"):
                slots[k] = resolve_asset(v, base_dir)

        data = {
            **slots,
            "__css__": css,
            "__w__": w,
            "__h__": h,
            "brand_name": brand.get("brand_name", ""),
            "logo_text": brand.get("logo_text", ""),
        }

        filled = wrap_document(render_template(open(tpl_path, encoding="utf-8").read(), data), css, w, h)

        # Temp HTML lives beside the assets so relative refs still resolve.
        fd, tmp_html = tempfile.mkstemp(suffix=".html", dir=out_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(filled)

        png = os.path.join(out_dir, f"{cid}_{tpl_name}_{fmt}.png")
        try:
            if not args.no_overflow_check:
                for slot in check_overflow(chrome, tmp_html, w, h):
                    msg = f"[{cid}] text overflows slot '{slot}' - shorten the copy"
                    warnings.append(msg)

            res = chrome_run(chrome, tmp_html, [f"--screenshot={png}", f"--window-size={w},{h}"])
            if os.path.exists(png):
                print(f"  [{cid}] {os.path.basename(png)}  ({w}x{h}, {tpl_name})")
                ok += 1
            else:
                print(f"  [{cid}] FAILED\n{res.stderr[-400:]}")
                failed += 1
        finally:
            os.remove(tmp_html)

    if warnings:
        print("\nLayout warnings:")
        for wmsg in warnings:
            print(f"  ! {wmsg}")

    print(f"\n{ok} rendered, {failed} failed -> {out_dir}")
    if failed or warnings:
        sys.exit(1)


if __name__ == "__main__":
    main()
