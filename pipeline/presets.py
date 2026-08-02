#!/usr/bin/env python3
"""
Execution presets — the 60-preset canonical lever library, as prompt material.

`reference_docs/execution_levers.md` defines 60 ways to *execute* a static ad:
49 levers per preset across Targeting, Persuasion, Messaging, Proof, Visual
Direction, Offer and Compliance. The levers do not decide the message — the brief
still does that — they decide the psychological and visual execution of it.

This module turns that document into three things the Remix tab needs:

  parse/load()     the document as structured data (memoised; the file is 250KB
                   and perfectly regular, so a plain parse beats a build step)
  prompt_block()   one preset compressed into an instruction the image model can
                   actually follow — visual direction in full, voice and offer as
                   single lines, the Avoid list as hard negatives
  conflicts()      where a preset's visual direction fights the reference layout
                   you picked, so the UI can warn before spending an image credit
  pick()           optional: a cheap text model reads the brief and chooses

Nothing here calls sys.exit — it raises PresetError and lets the caller (web app
or CLI) decide how to surface it.

    python3 pipeline/presets.py --list
    python3 pipeline/presets.py --show 07
    python3 pipeline/presets.py --json > presets.json

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEVERS_MD = os.environ.get(
    "LEVERS_MD", os.path.join(ROOT, "reference_docs", "execution_levers.md"))

# Preset ranges → the label the picker groups them under. 60 flat options is an
# unusable dropdown; these are the document's own thematic runs.
GROUPS = [
    (1, 5,   "Awareness & Identity"),
    (6, 13,  "Belief, Mechanism & Objections"),
    (14, 22, "Product & Proof"),
    (23, 29, "Transformation & Emotion"),
    (30, 35, "Offer, Price & Risk"),
    (36, 41, "Identity, Habit & Outcome"),
    (42, 49, "Conversion Psychology"),
    (50, 60, "Structure & Decision"),
]

# Levers that reach the image prompt, in the order they're printed. The five
# Targeting levers are deliberately mostly absent: they describe who the ad is
# for, which the brief already says, and every extra line dilutes the ones that
# actually change pixels.
VISUAL = ["Visual Style", "Hero Image Type", "Focus Object", "Visual Complexity",
          "Background Style", "Colour Psychology", "Contrast", "Product Visibility",
          "Human Presence", "Facial Expression", "Eye Contact", "Text / Image Ratio",
          "Information Density", "Graphic Style"]
VOICE = ["Tone", "Directness", "Curiosity Level", "Relatability", "Authority",
         "Hype Level", "Reading Level", "Lexicon"]
INTENT = ["Primary Emotion", "Communication Objective", "Primary Angle", "Concept",
          "Brain Target"]
PROOF = ["Proof Visibility", "Claim Specificity", "Evidence Density",
         "Comparison Target"]
OFFER = ["Benefit Priority", "Value Framing", "Price Framing", "CTA Friction",
         "Risk Posture", "Urgency"]

_EMPH = re.compile(r"\*\*(.+?)\*\*")
_H1 = re.compile(r"^# (\d{2})\.\s+(.+?)\s*$")
_H2 = re.compile(r"^## (.+?)\s*$")
_LEVER = re.compile(r"^-\s+\*\*(.+?):\*\*\s*(.*)$")
_BULLET = re.compile(r"^-\s+(?!\*\*)(.+)$")

_cache = None


class PresetError(Exception):
    """Raised instead of exiting — the web app returns it as JSON, the CLI prints."""


def _plain(s):
    """Drop markdown emphasis. The document leans on **bold** for stress, which is
    noise in a dropdown and wasted tokens in an image prompt."""
    return _EMPH.sub(r"\1", s or "").strip()


def group_of(n):
    for lo, hi, label in GROUPS:
        if lo <= n <= hi:
            return label
    return "Other"


def parse(path=None):
    """Read the lever document into 60 dicts. Every preset in the source carries
    the same 11 sections and the same 49 levers; anything that doesn't is a
    document error worth failing loudly on rather than silently half-applying."""
    path = path or LEVERS_MD
    if not os.path.exists(path):
        raise PresetError(
            f"execution lever library not found at {path} — it ships in "
            f"reference_docs/. Set LEVERS_MD to point somewhere else.")
    text = open(path, encoding="utf-8").read()

    presets, cur, section = [], None, None
    for line in text.splitlines():
        m = _H1.match(line)
        if m:
            n = int(m.group(1))
            cur = {"id": m.group(1), "n": n, "name": m.group(2).strip(),
                   "group": group_of(n), "purpose": "", "reaction": "",
                   "levers": {}, "avoid": [], "use_cases": []}
            presets.append(cur)
            section = None
            continue
        if cur is None:
            continue                      # preamble / schema chapter, not a preset
        m = _H2.match(line)
        if m:
            section = m.group(1).strip()
            continue
        m = _LEVER.match(line)
        if m and section:
            cur["levers"][m.group(1).strip()] = _plain(m.group(2))
            continue
        if section == "Purpose":
            s = line.strip()
            if s.startswith("> "):
                if not cur["reaction"]:
                    cur["reaction"] = _plain(s[2:]).strip('"')
            elif s and not cur["purpose"] and not s.startswith(("#", "-", ">")):
                cur["purpose"] = _plain(s)
        elif section == "Avoid":
            m = _BULLET.match(line)
            if m:
                cur["avoid"].append(_plain(m.group(1)))
        elif section == "Ideal Use Cases":
            m = _BULLET.match(line)
            if m:
                cur["use_cases"].append(_plain(m.group(1)))

    if len(presets) != 60:
        raise PresetError(f"expected 60 presets in {path}, parsed {len(presets)}")
    missing = [f"{p['id']} missing {k}"
               for p in presets for k in VISUAL if k not in p["levers"]]
    if missing:
        raise PresetError("lever document is incomplete: " + "; ".join(missing[:5]))
    return presets


def load():
    global _cache
    if _cache is None:
        _cache = parse()
    return _cache


def by_id(pid):
    pid = (pid or "").strip()
    for p in load():
        if p["id"] == pid or p["name"].lower() == pid.lower():
            return p
    raise PresetError(f"no execution preset {pid!r}")


def catalogue():
    """What the picker needs: enough to choose, not the whole lever set."""
    return [{"id": p["id"], "name": p["name"], "group": p["group"],
             "purpose": p["purpose"], "reaction": p["reaction"],
             "visual": {k: p["levers"][k] for k in
                        ("Visual Style", "Hero Image Type", "Human Presence",
                         "Product Visibility", "Information Density")},
             "use_cases": p["use_cases"][:6]}
            for p in load()]


# --------------------------------------------------------------------- prompt

def _line(p, keys):
    return " · ".join(f"{k}: {p['levers'][k]}" for k in keys if p["levers"].get(k))


def prompt_block(preset, conflict_mode="reference"):
    """One preset as an instruction block appended to the remix prompt.

    `conflict_mode` settles the argument the two inputs are always going to have:
    the reference image is a finished ad with its own visual direction, and the
    preset has one too. 'reference' keeps the layout you picked and applies the
    preset inside it; 'preset' lets the model restructure to satisfy the levers.
    """
    p = preset if isinstance(preset, dict) else by_id(preset)
    rule = ("Where this preset and the reference layout disagree, KEEP THE "
            "REFERENCE: its composition, panel structure, proportions and text "
            "placement are fixed. Apply the preset to what fills that structure — "
            "colour, contrast, mood, expression, density, emphasis and copy."
            if conflict_mode == "reference" else
            "Where this preset and the reference layout disagree, FOLLOW THE "
            "PRESET. Treat the reference as a starting point only — you may change "
            "its composition, panel structure and element placement so the preset's "
            "visual direction is fully expressed.")

    out = [
        f"EXECUTION PRESET — {p['id']}. {p['name']}",
        f"Objective: {p['purpose']}",
    ]
    if p["reaction"]:
        out.append(f"The viewer should think: \"{p['reaction']}\"")
    out += [
        "",
        f"VISUAL DIRECTION — {_line(p, VISUAL)}",
        f"INTENT — {_line(p, INTENT)}",
        f"VOICE OF THE COPY — {_line(p, VOICE)}",
        f"PROOF — {_line(p, PROOF)}",
        f"OFFER — {_line(p, OFFER)}",
    ]
    if p["levers"].get("Awareness Level") or p["levers"].get("Belief to Attack"):
        out.append(f"AUDIENCE — Awareness: {p['levers'].get('Awareness Level','—')} · "
                   f"Belief to attack: {p['levers'].get('Belief to Attack','—')}")
    if p["avoid"]:
        out.append("DO NOT — " + "; ".join(p["avoid"]))
    notes = p["levers"].get("Compliance Notes", "")
    if notes:
        out.append(f"PRESET COMPLIANCE — Safety claims: "
                   f"{p['levers'].get('Safety Claims','Conservative')}. {notes}")
    out += ["", rule]
    return "\n".join(out)


# ------------------------------------------------------------------ conflicts

# What each reference category structurally *is*. The preset can restyle a layout;
# it cannot make a hero product shot into a scene with no product in it without
# ceasing to be that layout — which is exactly what's worth warning about.
CATEGORY_PROFILE = {
    "01_Comparison":        {"kind": "a side-by-side comparison",
                             "product": "high", "human": "rare",
                             "density": "high", "urgency": "low", "text": "high"},
    "02_Demonstration":     {"kind": "a product demonstration",
                             "product": "high", "human": "often",
                             "density": "medium", "urgency": "low", "text": "medium"},
    "03_Feature_Breakdown": {"kind": "an annotated feature breakdown",
                             "product": "high", "human": "rare",
                             "density": "high", "urgency": "low", "text": "high"},
    "04_Educational":       {"kind": "an educational explainer",
                             "product": "low", "human": "rare",
                             "density": "high", "urgency": "low", "text": "high"},
    "05_Story_Testimonial": {"kind": "a testimonial / customer story",
                             "product": "medium", "human": "required",
                             "density": "medium", "urgency": "low", "text": "medium"},
    "06_Problem_Solution":  {"kind": "a problem → solution layout",
                             "product": "medium", "human": "often",
                             "density": "medium", "urgency": "low", "text": "medium"},
    "07_Product_Hero":      {"kind": "a product hero shot",
                             "product": "high", "human": "rare",
                             "density": "low", "urgency": "medium", "text": "low"},
    "08_Lifestyle":         {"kind": "a lifestyle scene",
                             "product": "medium", "human": "required",
                             "density": "low", "urgency": "low", "text": "low"},
    "09_Offer":             {"kind": "an offer / discount ad",
                             "product": "high", "human": "rare",
                             "density": "medium", "urgency": "high", "text": "medium"},
}

_LOW = ("none", "minimal", "very low", "low", "optional", "avoided", "absent")
_HIGH = ("required", "essential", "high", "very high", "maximum", "heavy", "busy")


def _is(value, words):
    v = (value or "").strip().lower()
    return any(v.startswith(w) or v == w for w in words)


def _ratio_text(value):
    """'60 / 40' → 60, the text share. Returns None when it isn't a ratio."""
    m = re.match(r"\s*(\d{1,3})\s*/", value or "")
    return int(m.group(1)) if m else None


def conflicts(preset, reference_rel):
    """Likely fights between one preset and one reference layout.

    Deliberately conservative — it flags the four levers that change what the
    layout *is* (product, people, density, urgency), not every stylistic
    difference, because a warning that fires on everything gets ignored."""
    p = preset if isinstance(preset, dict) else by_id(preset)
    cat = (reference_rel or "").split("/")[0]
    prof = CATEGORY_PROFILE.get(cat)
    if not prof:
        return []
    L, out = p["levers"], []

    def add(lever, note):
        out.append({"lever": lever, "value": L.get(lever, ""), "note": note})

    if _is(L.get("Product Visibility"), _LOW) and prof["product"] == "high":
        add("Product Visibility",
            f"the preset wants the product absent or incidental, but this reference is "
            f"{prof['kind']} built around the product")
    if _is(L.get("Human Presence"), _HIGH) and prof["human"] == "rare":
        add("Human Presence",
            f"the preset requires a person, but this reference is {prof['kind']} and "
            f"usually has none — a face has to be added somewhere")
    if _is(L.get("Human Presence"), ("none",)) and prof["human"] == "required":
        add("Human Presence",
            f"the preset wants no people, but this reference is {prof['kind']} and is "
            f"built around a person")
    if _is(L.get("Information Density"), _LOW) and prof["density"] == "high":
        add("Information Density",
            f"the preset wants a sparse ad, but this reference is {prof['kind']} and "
            f"carries a lot of copy — most of its panels would end up empty")
    if _is(L.get("Information Density"), _HIGH) and prof["density"] == "low":
        add("Information Density",
            f"the preset wants dense information, but this reference is {prof['kind']} "
            f"with little room for it")
    if _is(L.get("Urgency"), ("none",)) and prof["urgency"] == "high":
        add("Urgency",
            "the preset carries no urgency, but this reference is an offer layout whose "
            "structure is a deadline or discount block")
    share = _ratio_text(L.get("Text / Image Ratio", ""))
    if share is not None and share >= 65 and prof["text"] == "low":
        add("Text / Image Ratio",
            f"the preset is text-led ({L.get('Text / Image Ratio')}), but this reference "
            f"is {prof['kind']} that leads with the image")
    return out


# ------------------------------------------------------------------ AI picker

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENROUTER_URL = os.environ.get(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
PICK_MODELS = {"anthropic": "claude-haiku-4-5-20251001",
               "openrouter": "deepseek/deepseek-v4-flash"}

_PICK_SYSTEM = (
    "You choose the execution preset for a static advertisement. You are given a "
    "catalogue of 60 presets and a creative brief. Pick the ONE preset whose "
    "purpose and visual direction best execute that brief in the given reference "
    "layout. Prefer the preset that matches the brief's job (educate / agitate / "
    "prove / reassure / convert), not the one whose name sounds closest. "
    "Reply with JSON only: {\"id\": \"NN\", \"why\": \"one short sentence\"}")


def _post_json(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise PresetError("the key for the auto-picker was rejected (401).")
        if e.code == 402:
            raise PresetError("that account has insufficient credit (402).")
        raise PresetError(f"auto-pick failed — HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise PresetError(f"auto-pick failed — network error: {e.reason}")


def _extract_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise PresetError(f"the model didn't return JSON: {(text or '')[:160]}")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise PresetError(f"the model returned unreadable JSON: {e}")


def pick(brief, reference_rel="", keys=None, model=None):
    """Let a cheap text model choose the preset. Anthropic if a key is present,
    otherwise OpenRouter. Returns {id, name, why, provider, model}."""
    keys = keys or {}
    provider = "anthropic" if keys.get("anthropic") else (
        "openrouter" if keys.get("openrouter") else None)
    if not provider:
        raise PresetError(
            "Auto-pick needs an Anthropic or OpenRouter key — add one on the "
            "Settings tab, or choose a preset yourself.")
    model = model or PICK_MODELS[provider]

    lines = [f"{p['id']} {p['name']} ({p['group']}) — {p['purpose']}"
             for p in load()]
    cat = (reference_rel or "").split("/")[0]
    prof = CATEGORY_PROFILE.get(cat)
    user = (f"CATALOGUE\n" + "\n".join(lines) +
            f"\n\nREFERENCE LAYOUT: {prof['kind'] if prof else 'unclassified'}"
            f"\n\nBRIEF\n{(brief or '').strip() or '(no brief given)'}")

    if provider == "anthropic":
        payload = _post_json(
            ANTHROPIC_URL,
            {"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01",
             "Content-Type": "application/json"},
            {"model": model, "max_tokens": 300, "system": _PICK_SYSTEM,
             "messages": [{"role": "user", "content": user}]})
        text = "".join(b.get("text", "") for b in payload.get("content", []))
    else:
        payload = _post_json(
            OPENROUTER_URL,
            {"Authorization": f"Bearer {keys['openrouter']}",
             "Content-Type": "application/json",
             "HTTP-Referer": "https://localhost/adpipe", "X-Title": "adpipe"},
            {"model": model, "max_tokens": 300,
             "messages": [{"role": "system", "content": _PICK_SYSTEM},
                          {"role": "user", "content": user}]})
        text = payload["choices"][0]["message"]["content"]

    got = _extract_json(text)
    pid = str(got.get("id", "")).strip().zfill(2)
    p = by_id(pid)
    return {"id": p["id"], "name": p["name"], "group": p["group"],
            "why": str(got.get("why", "")).strip(),
            "provider": provider, "model": model}


# ---------------------------------------------------------------------- CLI

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Inspect the execution preset library.")
    ap.add_argument("--list", action="store_true", help="all 60, by group")
    ap.add_argument("--show", metavar="ID", help="the prompt block for one preset")
    ap.add_argument("--mode", default="reference", choices=("reference", "preset"),
                    help="who wins a preset/layout conflict (default: reference)")
    ap.add_argument("--against", metavar="REF",
                    help="a reference path like 07_Product_Hero/591.png — "
                         "reports conflicts with --show")
    ap.add_argument("--json", action="store_true", help="the whole library as JSON")
    args = ap.parse_args()

    try:
        if args.json:
            print(json.dumps(load(), indent=2)); return
        if args.show:
            p = by_id(args.show)
            print(prompt_block(p, args.mode))
            if args.against:
                cs = conflicts(p, args.against)
                print(f"\n--- conflicts with {args.against} ---")
                print("\n".join(f"  ! {c['lever']} ({c['value']}): {c['note']}"
                                for c in cs) or "  none")
            return
        last = None
        for p in load():
            if p["group"] != last:
                last = p["group"]
                print(f"\n{last}")
            print(f"  {p['id']}  {p['name']:<22} {p['purpose'][:74]}")
        print(f"\n  {len(load())} presets · {len(load()[0]['levers'])} levers each")
    except PresetError as e:
        raise SystemExit(f"  {e}")


if __name__ == "__main__":
    main()
