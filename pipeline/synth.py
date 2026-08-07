#!/usr/bin/env python3
"""
§7 — Product × Segment strategy synthesis.

Turns Product Truth and one segment's approved Customer Truth into that
segment's strategy: positioning, the main problem to lead with, primary benefit,
reason to buy, reason to believe, differentiation, market entry point, messaging
territories, creative opportunities and landing page strategy.

Deliberately the second half of a pair. §6 structures evidence and never
concludes; this concludes and never invents evidence. Keeping the boundary
strict is what makes the output worth trusting — strategy is synthesised from
reviewed research rather than from another model's impression of it.

Two things are enforced here rather than hoped for in the prompt:

  Approved input only. Segment fields still sitting at `ai_suggested` are
  unreviewed, so they are excluded and reported. Strategy built on unreviewed
  research is exactly the failure the §6-before-§7 ordering exists to prevent.

  Every benefit must name the Product Fact that licenses it. A customer wanting
  something is not evidence the product does it. The reason-to-believe table
  carries the licensing fact per row, and any row that cannot name one is
  dropped before it reaches the segment.

Outputs arrive as `ai_suggested` for Accept / Edit / Reject, same as §6.

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

# What counts as reviewed. research_derived is included because it came from the
# pipeline rather than from a model guessing about the pipeline.
APPROVED = {"user_approved", "verified_fact", "research_derived"}
UNREVIEWED = {"ai_suggested"}

# The reason-to-believe table's columns, by position.
RTB_BENEFIT, RTB_REASON, RTB_PROOF, RTB_FACT, RTB_CONF = range(5)

SYSTEM = (
    "You turn product facts and researched customer truth into the advertising "
    "strategy for ONE customer segment.\n\n"
    "The rule that governs everything you write:\n"
    "Only Product Truth licenses a product claim. Customer research tells you "
    "what people want, feel, fear and have already tried — it never establishes "
    "that this product can deliver anything. Before writing any benefit, name "
    "the product fact that licenses it. If no product fact licenses it, do not "
    "write the benefit; write the opportunity instead.\n\n"
    "  Customer says   'I want something that doesn't get hot'\n"
    "  Product fact    open-cell latex construction\n"
    "  Licensed        'designed with breathable open-cell latex'\n"
    "  NOT licensed    'never sleep hot again'\n\n"
    "Also true:\n"
    "- This strategy is for this segment only. Another segment of the same "
    "product may need a completely different position, and that is expected.\n"
    "- Anything listed as what the product does NOT do, or as a prohibited "
    "claim, is off limits no matter how well it would sell.\n"
    "- Use the segment's own language where the research supplies it.\n"
    "- If the evidence does not support a field, omit it. An absent field is "
    "honest; an invented one is a liability.\n"
    "- Judge compatibility honestly. If this product does not credibly serve "
    "this segment, say so — a weak fit found now is cheaper than one found "
    "after the ad spend.\n\n"
    "Reply with JSON only, in the shape the user message specifies.")


class SynthError(Exception):
    """Raised for bad input or an unusable model reply."""


def targets(sections=None):
    """The segment fields §7 produces, grouped by section."""
    out = []
    for s in products.SEGMENT_SECTIONS:
        fields = [f for f in s["fields"] if f.get("synth")]
        if not fields:
            continue
        if sections and s["key"] not in sections:
            continue
        out.append((s, fields))
    return out


def sections():
    return [{"key": s["key"], "title": s["title"], "fields": len(fs)}
            for s, fs in targets()]


def _provider(keys):
    if keys.get("anthropic"):
        return "anthropic"
    if keys.get("openrouter"):
        return "openrouter"
    raise SynthError(
        "Strategy synthesis needs an Anthropic or OpenRouter key — add one on "
        "the Settings tab.")


def approved_truth(segment_doc):
    """This segment's reviewed research, what was withheld, and how much of it is
    actually research.

    The count deliberately excludes the identity section: a segment always has a
    name, and a name is not evidence. Without that distinction a freshly created
    segment looks like it has approved truth and strategy runs on nothing.
    """
    kept, unreviewed, researched = [], [], 0
    for s in products.SEGMENT_SECTIONS:
        if s.get("layer") not in ("customer", "segment"):
            continue
        lines = []
        for f in s["fields"]:
            c = (segment_doc.get(s["key"]) or {}).get(f["key"]) or {}
            if not products.is_answered(c):
                continue
            st = c.get("state") if isinstance(c, dict) else ""
            if st in UNREVIEWED:
                unreviewed.append(f"{s['title']} — {f['label']}")
                continue
            if st not in APPROVED:
                continue
            if s.get("layer") == "customer":
                researched += 1
            v = products.value_of(c)
            if isinstance(v, list):
                lines.append(f"**{f['label']}**")
                lines += [f"- {x}" for x in v]
            else:
                lines.append(f"**{f['label']}**  {v}")
        if lines:
            kept.append(f"### {s['title']}\n" + "\n".join(lines))
    return "\n\n".join(kept), unreviewed, researched


def product_facts(product_doc):
    """Facts available to license a claim, and the hard limits on claiming."""
    licensing = []
    for s in products.PRODUCT_SECTIONS:
        for f in s["fields"]:
            c = (product_doc.get(s["key"]) or {}).get(f["key"]) or {}
            if not products.is_answered(c):
                continue
            v = products.value_of(c)
            mark = " [VERIFIED]" if products.licenses_claims(c) else ""
            if isinstance(v, list):
                licensing.append(f"**{s['title']} — {f['label']}**{mark}")
                licensing += [f"- {x}" for x in v]
            elif isinstance(v, str):
                licensing.append(f"**{s['title']} — {f['label']}**{mark}  {v}")
    return "\n".join(licensing)


def build_prompt(product_doc, segment_doc, groups, truth, unreviewed, researched):
    """The exact message sent. Separate so the UI can show it before spending."""
    if not researched:
        raise SynthError(
            "This segment has no approved research yet. Run Enrich from research "
            "and accept some of it first — strategy built on unreviewed evidence "
            "is what the §6-before-§7 order exists to prevent.")

    shape, asked = {}, 0
    for s, fields in groups:
        want = {}
        for f in fields:
            if f["kind"] == "list":
                want[f["key"]] = ["<item>", "…"]
            elif f["kind"] == "table":
                want[f["key"]] = [f.get("columns", [])]
            elif f["kind"] == "choice":
                want[f["key"]] = "one of: " + " | ".join(f.get("options", []))
            elif f["kind"] == "score":
                want[f["key"]] = "<0-10>"
            else:
                want[f["key"]] = "<text>"
            asked += 1
        shape[s["key"]] = want

    name = products.value_of(segment_doc.get("identity", {}).get("name")) or "(unnamed)"
    parts = [
        "PRODUCT TRUTH — the only thing that can license a claim. Items marked "
        "[VERIFIED] are confirmed; the rest are entered but unverified, so lean "
        "on them more cautiously.\n\n" + product_facts(product_doc),
        f"SEGMENT\n{name}",
        "APPROVED CUSTOMER TRUTH for this segment — reviewed research. It tells "
        "you what they want and have tried. It licenses nothing.\n\n" + truth,
    ]
    if unreviewed:
        parts.append(
            "WITHHELD — these segment fields are still unreviewed suggestions and "
            "were deliberately excluded:\n"
            + "\n".join(f"- {u}" for u in unreviewed))
    parts.append(
        "Write this segment's strategy. For every benefit and every reason to "
        "believe, name the product fact that licenses it in the row provided; if "
        "you cannot name one, leave the row out. Omit any field the evidence does "
        "not support.\n\nReturn JSON:\n"
        + json.dumps({"fields": shape,
                      "licensing": {"<section>": {"<field>": "the product fact "
                                                  "licensing this, if it is a claim"}},
                      "confidence": {"<section>": {"<field>": "high|medium|low"}}},
                     indent=2))
    return "\n\n---\n\n".join(parts), asked


def synthesise(project, product, slug, segment_doc, sections_wanted=None, keys=None,
               model=None, dry_run=False):
    """Propose strategy for one segment. Returns suggestions; writes nothing."""
    keys = keys or {}
    groups = targets(sections_wanted)
    if not groups:
        raise SynthError("No sections selected.")
    product_doc = products.load(project, product)
    truth, unreviewed, researched = approved_truth(segment_doc)
    prompt, asked = build_prompt(product_doc, segment_doc, groups, truth,
                                 unreviewed, researched)

    if dry_run:
        return {"prompt": prompt, "asked": asked, "unreviewed": unreviewed,
                "suggestions": {}}

    provider = _provider(keys)
    model = model or MODELS[provider]
    try:
        if provider == "anthropic":
            payload = presets._post_json(
                presets.ANTHROPIC_URL,
                {"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
                {"model": model, "max_tokens": 10000, "system": SYSTEM,
                 "messages": [{"role": "user", "content": prompt}]},
                timeout=300, label="strategy synthesis")
            text = "".join(b.get("text", "") for b in payload.get("content", []))
        else:
            payload = presets._post_json(
                presets.OPENROUTER_URL,
                {"Authorization": f"Bearer {keys['openrouter']}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://localhost/adpipe", "X-Title": "adpipe"},
                {"model": model, "max_tokens": 10000,
                 "messages": [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}]},
                timeout=300, label="strategy synthesis")
            text = payload["choices"][0]["message"]["content"]
        got = presets._extract_json(text)
    except presets.PresetError as e:
        raise SynthError(str(e))

    return {"suggestions": _shape(got, groups, slug),
            "unreviewed": unreviewed, "asked": asked,
            "provider": provider, "model": model, "prompt": prompt}


def _shape(got, groups, slug):
    """Validate the reply, and drop any claim that cannot name its licence.

    The licensing rule is applied here rather than trusted to the model: a
    reason-to-believe row with no licensing product fact is removed, because an
    unlicensed claim reaching the segment sheet is exactly how an unsupported
    promise ends up in an ad.
    """
    fields = got.get("fields") or {}
    conf = got.get("confidence") or {}
    lic = got.get("licensing") or {}
    out, dropped = {}, []
    for s, fs in groups:
        sec = fields.get(s["key"]) or {}
        if not isinstance(sec, dict):
            continue
        for f in fs:
            if f["key"] not in sec:
                continue
            v = products._coerce(f["kind"], sec[f["key"]], f.get("columns"))
            if f["kind"] == "choice" and v and v not in (f.get("options") or []):
                continue
            if f["key"] == "rtb" and f["kind"] == "table":
                keep = [r for r in v if len(r) > RTB_FACT and r[RTB_FACT].strip()]
                dropped += [r[RTB_BENEFIT] for r in v if r not in keep]
                v = keep
            if not v:
                continue
            ref = str((lic.get(s["key"]) or {}).get(f["key"], "")).strip()
            out.setdefault(s["key"], {})[f["key"]] = products.cell(
                v, "ai_suggested", "strategy",
                f"segment:{slug}" + (f" licensed_by:{ref}" if ref else ""),
                str((conf.get(s["key"]) or {}).get(f["key"], "")).strip())
    if not out:
        raise SynthError(
            "The model proposed no usable strategy — the approved research may be "
            "too thin, or the reply did not match the schema.")
    if dropped:
        out.setdefault("_notes", {})["dropped_unlicensed"] = products.cell(
            dropped, "rejected", "strategy", f"segment:{slug}", "")
    return out


def main():
    if "--sections" in sys.argv:
        for s in sections():
            print(f"  {s['key']:<14} {s['title']:<34} {s['fields']} field(s)")
        return
    print("usage: synth.py --sections   (the rest runs from the Product tab)")


if __name__ == "__main__":
    main()
