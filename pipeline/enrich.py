#!/usr/bin/env python3
"""
§6 — Enrich a segment from research.

Turns the 27 extraction outputs into structured Segment Truth. That is all it
does. It stores what customers said, how often, and where it came from; it never
concludes what to do about it.

    Research says   "customers complain memory foam gets hot"
    This stores     the complaint, its evidence, its frequency
    It must NOT     conclude "position us as the cooler alternative"

That conclusion is §7's job, and keeping the boundary strict is what makes the
strategy step trustworthy: it synthesises from reviewed evidence rather than
from another model's opinion about evidence.

Two guardrails are enforced here rather than left to the prompt:

  - only fields marked `enrich` in the segment schema may be proposed, so a
    strategic field cannot be filled by an evidence pass
  - a field already `user_approved` or `verified_fact` is never proposed for,
    so review work cannot be silently overwritten

Everything proposed comes back as `ai_suggested` with its source references, for
Accept / Edit / Reject. Nothing is written until the operator saves.

Standard library only. Anthropic if a key is present, else OpenRouter.
"""

from __future__ import annotations

import json
import os
import sys

import presets
import products

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODELS = {"anthropic": "claude-opus-5", "openrouter": "deepseek/deepseek-v4"}

# States that mean a person has already settled this field.
PROTECTED = {"user_approved", "verified_fact"}

SYSTEM = (
    "You structure voice-of-customer research into segment knowledge for an "
    "advertising system.\n\n"
    "You are an evidence step, not a strategy step. Record what the research "
    "says about this segment. Do not decide what the brand should do about it, "
    "do not write positioning, angles or copy, and do not recommend how to "
    "compete. A complaint is recorded as a complaint.\n\n"
    "Rules you never break:\n"
    "- Use only the extraction outputs provided. Add nothing from general "
    "knowledge.\n"
    "- A customer wanting something is never evidence that the product does it. "
    "Never turn a desire into a product capability.\n"
    "- Keep naturally repeatable things as separate items. Do not compress a "
    "list of objections into one sentence.\n"
    "- Quote customers verbatim or not at all.\n"
    "- If the research does not support a field, omit that field entirely. An "
    "absent field is correct; an invented one is not.\n"
    "- Prefer the segment's own words over paraphrase.\n\n"
    "Reply with JSON only, in the shape the user message specifies.")


class EnrichError(Exception):
    """Raised for bad input or an unusable model reply."""


def enrichable(sections=None):
    """The segment fields §6 is allowed to propose, grouped by section."""
    out = []
    for s in products.SEGMENT_SECTIONS:
        fields = [f for f in s["fields"] if f.get("enrich")]
        if not fields:
            continue
        if sections and s["key"] not in sections:
            continue
        out.append((s, fields))
    return out


def recommended_sections():
    return [s["key"] for s, _ in enrichable()]


def _provider(keys):
    if keys.get("anthropic"):
        return "anthropic"
    if keys.get("openrouter"):
        return "openrouter"
    raise EnrichError(
        "Enrichment needs an Anthropic or OpenRouter key — add one on the "
        "Settings tab.")


def read_extractions(project, slug, skills=None):
    """The segment's extraction outputs, optionally only the ones asked for."""
    d = os.path.join(ROOT, "projects", project, "extractions", slug)
    if not os.path.isdir(d):
        raise EnrichError(
            f"No extractions for segment {slug!r}. Run the extract stage first.")
    want = {s.strip().zfill(2) for s in (skills or "").split(",") if s.strip()}
    parts = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".md"):
            continue
        if want and f[:2] not in want:
            continue
        parts.append(f"## {f[:-3]}\n\n"
                     + open(os.path.join(d, f), encoding="utf-8").read())
    if not parts:
        raise EnrichError(f"No extraction files matched under {slug}.")
    return "\n\n---\n\n".join(parts)


def _skipped(segment_doc, groups):
    """Fields a person has already settled — reported, never proposed for."""
    out = []
    for s, fields in groups:
        for f in fields:
            c = (segment_doc.get(s["key"]) or {}).get(f["key"]) or {}
            if isinstance(c, dict) and c.get("state") in PROTECTED:
                out.append({"section": s["key"], "field": f["key"],
                            "label": f"{s['title']} — {f['label']}",
                            "state": c.get("state")})
    return out


def build_prompt(product_doc, segment_doc, corpus, groups, protected):
    """The exact message sent. Separated so the UI can show it before spending."""
    skip = {(p["section"], p["field"]) for p in protected}
    shape, asked = {}, 0
    for s, fields in groups:
        want = {}
        for f in fields:
            if (s["key"], f["key"]) in skip:
                continue
            kind = f["kind"]
            if kind == "list":
                want[f["key"]] = ["<item>", "…"]
            elif kind == "choice":
                want[f["key"]] = "one of: " + " | ".join(f.get("options", []))
            else:
                want[f["key"]] = "<text>"
            asked += 1
        if want:
            shape[s["key"]] = want
    if not asked:
        raise EnrichError(
            "Every enrichable field is already approved — nothing left to propose. "
            "Reject or clear a field first if you want it re-proposed.")

    name = products.value_of(segment_doc.get("identity", {}).get("name")) or "(unnamed)"
    desc = products.value_of(segment_doc.get("identity", {}).get("description")) or ""

    parts = [
        "PRODUCT TRUTH — for context only. Do not restate it, do not evaluate it, "
        "and do not let it colour what the research says. Customers are allowed "
        "to want things this product does not do; record that faithfully.\n\n"
        + products.product_markdown(product_doc),
        f"SEGMENT\n{name}" + (f"\n{desc}" if desc else ""),
        "RESEARCH EXTRACTIONS — the only source for everything below:\n\n" + corpus,
        "Fill only the fields in this shape, using the research above. Omit any "
        "field the research does not support — do not guess, and do not pad a "
        "list to look complete. Keep list items short and concrete, in the "
        "segment's own words where possible.\n\n"
        "Return JSON:\n"
        + json.dumps({"fields": shape,
                      "confidence": {"<section>": {"<field>": "high|medium|low"}},
                      "sources": {"<section>": {"<field>": "which extraction "
                                                "skills supported it, e.g. 07,14"}}},
                     indent=2),
    ]
    return "\n\n---\n\n".join(parts), asked


def suggest(project, product, slug, segment_doc, sections=None, keys=None,
            model=None, dry_run=False):
    """Propose values for one segment. Returns suggestions; writes nothing."""
    keys = keys or {}
    groups = enrichable(sections)
    if not groups:
        raise EnrichError("No sections selected.")

    product_doc = products.load(project, product)
    skills = ",".join(f.get("skills", "") for _, fs in groups for f in fs)
    corpus = read_extractions(project, slug, skills if skills.strip(",") else None)
    protected = _skipped(segment_doc, groups)
    prompt, asked = build_prompt(product_doc, segment_doc, corpus, groups, protected)

    if dry_run:
        return {"prompt": prompt, "asked": asked, "skipped": protected,
                "suggestions": {}}

    provider = _provider(keys)
    model = model or MODELS[provider]
    try:
        if provider == "anthropic":
            payload = presets._post_json(
                presets.ANTHROPIC_URL,
                {"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
                {"model": model, "max_tokens": 8000, "system": SYSTEM,
                 "messages": [{"role": "user", "content": prompt}]},
                timeout=300, label="research enrichment")
            text = "".join(b.get("text", "") for b in payload.get("content", []))
        else:
            payload = presets._post_json(
                presets.OPENROUTER_URL,
                {"Authorization": f"Bearer {keys['openrouter']}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://localhost/adpipe", "X-Title": "adpipe"},
                {"model": model, "max_tokens": 8000,
                 "messages": [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}]},
                timeout=300, label="research enrichment")
            text = payload["choices"][0]["message"]["content"]
        got = presets._extract_json(text)
    except presets.PresetError as e:
        raise EnrichError(str(e))

    return {"suggestions": _shape(got, groups, protected, slug),
            "skipped": protected, "asked": asked,
            "provider": provider, "model": model, "prompt": prompt}


def _shape(got, groups, protected, slug):
    """Validate the reply against the schema and wrap it as ai_suggested cells.

    Anything the model returned for a field it was not asked about, or in the
    wrong shape, is dropped rather than stored — the schema decides what a
    segment can hold, not the reply.
    """
    skip = {(p["section"], p["field"]) for p in protected}
    fields = got.get("fields") or {}
    conf = got.get("confidence") or {}
    srcs = got.get("sources") or {}
    out = {}
    for s, fs in groups:
        sec = fields.get(s["key"]) or {}
        if not isinstance(sec, dict):
            continue
        for f in fs:
            if (s["key"], f["key"]) in skip or f["key"] not in sec:
                continue
            v = products._coerce(f["kind"], sec[f["key"]], f.get("columns"))
            if f["kind"] == "choice" and v and v not in (f.get("options") or []):
                continue
            if not v:
                continue
            ref = str((srcs.get(s["key"]) or {}).get(f["key"], "")).strip()
            out.setdefault(s["key"], {})[f["key"]] = products.cell(
                v, "ai_suggested", "research",
                f"segment:{slug}" + (f" skills:{ref}" if ref else ""),
                str((conf.get(s["key"]) or {}).get(f["key"], "")).strip())
    if not out:
        raise EnrichError(
            "The model proposed nothing usable — the research may not cover this "
            "segment, or the reply did not match the schema.")
    return out


def main():
    if "--sections" in sys.argv:
        for s, fs in enrichable():
            print(f"  {s['key']:<14} {s['title']:<34} {len(fs)} field(s)")
        return
    print("usage: enrich.py --sections   (the rest runs from the Product tab)")


if __name__ == "__main__":
    main()
