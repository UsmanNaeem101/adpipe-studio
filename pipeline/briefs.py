#!/usr/bin/env python3
"""
Write ad briefs from the levers you picked.

The Remix tab has two ways in. Either you write the brief yourself, or you pick
levers off the segment's extractions — pain point, desired outcome, objection,
the segment's own words — and this module turns them into N briefs you can
choose between. Each brief carries both halves of a static ad: the copy, and a
visual brief for the plate it sits on.

What it will not do is invent evidence. Everything about the customer comes from
the lever text the caller assembled out of the extraction files; the model's job
is to choose an angle and phrase it, not to add facts. Quotes may only be used
verbatim, and the compliance rules go in as hard constraints because this is the
one step that writes claims.

    python3 pipeline/briefs.py --demo       # prompt only, no API call

Standard library only. Talks to Anthropic if a key is present, else OpenRouter.
"""

from __future__ import annotations

import json
import os
import sys

import presets

WRITE_MODELS = {"anthropic": "claude-opus-5",
                "openrouter": "deepseek/deepseek-v4-flash-0731"}

SYSTEM = (
    "You write briefs for static direct-response advertisements. You are given "
    "voice-of-customer levers taken from real comments, and you write briefs that "
    "execute them.\n\n"
    "Rules you never break:\n"
    "- Every claim about the customer's experience traces to the levers given. "
    "Add no symptom, statistic, duration or outcome that is not there.\n"
    "- Quotes are verbatim from the levers or absent. Never paraphrase into "
    "quotation marks.\n"
    "- No numbers, percentages or durations unless they appear in the levers or "
    "the product facts.\n"
    "- Compare against a category, never a named competitor.\n"
    "- The visual brief describes a PLATE ONLY: scene, product, mood, light. It "
    "contains no text, words, lettering, signage or UI — copy is composited "
    "separately.\n\n"
    "Reply with JSON only, matching the shape the user message asks for.")


class BriefError(Exception):
    """Raised for bad input or a model that returned something unusable."""


def _provider(keys):
    if keys.get("anthropic"):
        return "anthropic"
    if keys.get("openrouter"):
        return "openrouter"
    raise BriefError(
        "Writing briefs needs an Anthropic or OpenRouter key — add one on the "
        "Settings tab.")


def build_prompt(lever_text, n=4, product="", market="", compliance="",
                 facts="", preset=None, custom_levers=None, extra=""):
    """Assemble the exact user message. Separated from the call so the UI can
    show it, and so `--demo` can print it without spending anything."""
    if not (lever_text or "").strip():
        raise BriefError("No levers selected — pick at least a pain point and a "
                         "desired outcome.")
    parts = [f"PRODUCT\n{product or '(unspecified)'}",
             f"MARKET\n{market or '(unspecified)'}"]
    if facts.strip():
        parts.append("APPROVED PRODUCT FACTS — anything marked NEEDS INPUT is NOT "
                     "usable in an ad; do not invent a value for it:\n" + facts.strip())
    parts.append("VOICE-OF-CUSTOMER LEVERS — the only source for what the customer "
                 "experiences:\n" + lever_text.strip())
    if compliance.strip():
        parts.append("COMPLIANCE — never claim any of this:\n" + compliance.strip())

    if preset:
        parts.append(presets.prompt_block(preset, "preset"))
    block = presets.custom_block(custom_levers or {})
    if block:
        parts.append(block)
    if extra.strip():
        parts.append("ADDITIONAL DIRECTION FROM THE OPERATOR\n" + extra.strip())

    parts.append(
        f"Write {n} distinct briefs for a static ad. Distinct means a different "
        f"angle or a different entry point into the same levers — not the same "
        f"idea reworded. Return JSON:\n"
        '{"briefs":[{"id":"B1","angle":"one line — what this ad argues",'
        '"headline":"the in-image hook, at most ~10 words",'
        '"subtext":"one supporting line, at most ~20 words",'
        '"cta":"the call to action",'
        '"why":"which levers this executes and why it should land",'
        '"visual_brief":"the plate: scene, product placement, mood, light, '
        'colour. No text of any kind."}]}')
    return "\n\n---\n\n".join(parts)


def write(lever_text, n=4, keys=None, model=None, **kw):
    """Generate N briefs. Returns {"briefs": [...], "provider", "model", "prompt"}."""
    keys = keys or {}
    provider = _provider(keys)
    model = model or WRITE_MODELS[provider]
    prompt = build_prompt(lever_text, n, **kw)

    try:
        if provider == "anthropic":
            payload = presets._post_json(
                presets.ANTHROPIC_URL,
                {"x-api-key": keys["anthropic"], "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
                {"model": model, "max_tokens": 4000, "system": SYSTEM,
                 "messages": [{"role": "user", "content": prompt}]},
                timeout=180, label="brief writing")
            text = "".join(b.get("text", "") for b in payload.get("content", []))
        else:
            payload = presets._post_json(
                presets.OPENROUTER_URL,
                {"Authorization": f"Bearer {keys['openrouter']}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://localhost/adpipe", "X-Title": "adpipe"},
                {"model": model, "max_tokens": 4000,
                 "messages": [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}]},
                timeout=180, label="brief writing")
            text = payload["choices"][0]["message"]["content"]
        got = presets._extract_json(text)
    except presets.PresetError as e:
        raise BriefError(str(e))

    rows = got.get("briefs") or []
    if not isinstance(rows, list) or not rows:
        raise BriefError("the model returned no briefs")

    out = []
    for i, b in enumerate(rows[:n], 1):
        if not isinstance(b, dict):
            continue
        out.append({
            "id": str(b.get("id") or f"B{i}"),
            "angle": str(b.get("angle", "")).strip(),
            "headline": str(b.get("headline", "")).strip(),
            "subtext": str(b.get("subtext", "")).strip(),
            "cta": str(b.get("cta", "")).strip(),
            "why": str(b.get("why", "")).strip(),
            "visual_brief": str(b.get("visual_brief", "")).strip(),
        })
    if not out:
        raise BriefError("the model's briefs were unreadable")
    for b in out:
        b["image_brief"] = as_image_brief(b)
    return {"briefs": out, "provider": provider, "model": model, "prompt": prompt}


def as_image_brief(brief):
    """One brief flattened into the text the image step takes as its BRIEF."""
    lines = []
    if brief.get("headline"):
        lines.append(f"Headline: {brief['headline']}")
    if brief.get("subtext"):
        lines.append(f"Subtext: {brief['subtext']}")
    if brief.get("cta"):
        lines.append(f"CTA: {brief['cta']}")
    if brief.get("visual_brief"):
        lines.append(f"Visual direction: {brief['visual_brief']}")
    return "\n".join(lines)


def main():
    if "--demo" not in sys.argv:
        sys.exit("usage: briefs.py --demo   (prints the assembled prompt only)")
    demo = ("### Pain point\n- **Unable to sleep on the affected shoulder** — "
            "People cannot lie on the injured side without the pain waking them.\n"
            "  > “I haven't slept on my right side in months.”\n\n"
            "### Desired outcome\n- **Sleep through the night** — One full night "
            "without the shoulder waking them.")
    print(build_prompt(demo, 4, product="Montisella latex ergonomic pillow",
                       market="shoulder and neck pain sufferers (UK/US)",
                       compliance="no medical-causation or treatment claims.",
                       custom_levers={"Tone": "No-Nonsense"}))


if __name__ == "__main__":
    main()
