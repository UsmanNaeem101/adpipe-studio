#!/usr/bin/env python3
"""
QA gate for static ad copy. Run before the copy goes to an image model;
nothing ships without it.

Enforces the four things that actually kill this account or this ad:
  1. NO unsupported medical claim  (Meta health-adjacent policy - hard fail)
  2. NO hallucinated quote          (every verbatim quote must exist in the evidence file)
  3. NO unsupported stat            (every number must be in facts.json or the evidence file)
  4. Structural completeness        (required slots present, plates resolve)

Usage:
    python3 pipeline/qa.py output/desk_workers_posture/concepts.json
    python3 pipeline/qa.py <concepts.json> --evidence evidence/desk_workers_posture.txt

Exit 0 = clean. Exit 1 = at least one FAIL. Warnings never block.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 1. claim safety -------------------------------------------------------
# Hard fails. These are medical-causation claims: they assert the product acts on
# anatomy or on a named condition. The evidence file is FULL of this language
# because sufferers talk that way - that is exactly why it needs a machine gate.
BANNED = [
    (r"\bcorrect(s|ing)?\s+(your\s+)?postur", "claims to correct posture"),
    (r"\brealign(s|ing|ment)?\b", "claims skeletal realignment"),
    # Scoped to a body/condition object on purpose: "that's a workaround, not a fix"
    # is claim-safe copy, "fixes your neck" is not.
    (r"\b(fix(es|ed|ing)?|heal(s|ed|ing)?|cure(s|d)?|treat(s|ed|ing)?|repair(s|ed|ing)?)\s+"
     r"(your\s+|the\s+|their\s+)?"
     r"(neck|back|shoulder|spine|spinal|posture|pain|disc|nerve|muscle|condition|"
     r"tightness|stiffness|injury|damage)\b", "treatment/cure claim"),
    (r"\bnerve\s+(compression|impingement|pain)\b", "nerve-compression claim"),
    (r"\bpinched\s+nerve\b", "named condition"),
    (r"\b(herniat|bulging)\w*\s+disc\b", "named condition"),
    (r"\bsciatica\b|\bradiculopath\w*\b|\bscoliosis\b|\bkyphosis\b|\bstenosis\b",
     "named condition"),
    (r"\b(arthritis|fibromyalgia|ehlers|hypermobility\s+syndrome)\b", "named condition"),
    (r"\bcervical\s+(spine|vertebra|alignment)\b", "anatomical claim"),
    (r"\bspinal\s+align\w*\b|\bspine\s+align\w*\b", "anatomical claim"),
    (r"\bdecompress\w*\b", "anatomical claim"),
    (r"\b(clinically|scientifically)\s+proven\b", "unsubstantiated proof claim"),
    (r"\bFDA[- ]approved\b|\bmedically\s+(approved|certified)\b", "regulatory claim"),
    (r"\b(doctor|physio|chiropractor)[- ]?(recommended|approved)\b",
     "professional endorsement - needs documented substantiation"),
    (r"\b(therapeutic|orthopa?edic)\s+(benefit|effect|device)\b", "medical-device framing"),
    (r"\brelieve[sd]?\s+(your\s+)?(neck|back|shoulder|nerve)\b", "symptom-relief claim"),
    (r"\bprevents?\s+(pain|injury|damage)\b", "prevention claim"),
]

# Warnings: legal in context but frequently drift into a claim. Human reads these.
REVIEW = [
    (r"\bposture\b", "'posture' is fine as the customer's word, not as a promise"),
    (r"\bpain\b", "describing THEIR felt pain is ok; promising its removal is not"),
    (r"\bsupport(s|ing|ed)?\b", "'support' must describe the product, not a body outcome"),
    (r"\btech\s+neck\b", "customer slang - keep descriptive, never 'fixes tech neck'"),
    (r"\bergonomic\b", "fine as a category word; not a proof point on its own"),
]

# Slots that carry visible ad copy. Non-copy slots (images) are skipped.
COPY_SLOT = re.compile(
    r"(hook|headline|subhead|copy|quote|cta|chip|reframe|objection|tag|stamp|"
    r"title|desc|eyebrow|label|risk_|kicker)", re.I
)
IMAGE_SLOT = re.compile(r"(image|plate|avatar|bg|logo_src)", re.I)

NUM = re.compile(r"(?<![\w#])(\d[\d,.]*)\s*(%|nights?|days?|years?|mm|kg/m³|kg|£|\$)?", re.I)


def norm(s):
    """Normalise for quote matching: unify quotes/dashes, collapse whitespace, casefold."""
    s = unicodedata.normalize("NFKD", str(s))
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"<[^>]+>", " ", s)          # strip inline <em>/<mark>
    s = re.sub(r"[^\w\s'-]", " ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def walk_slots(concept):
    for k, v in (concept.get("slots") or {}).items():
        if isinstance(v, str) and v.strip():
            yield k, v


def main():
    ap = argparse.ArgumentParser(description="QA gate for ad copy.")
    ap.add_argument("concepts")
    ap.add_argument("--evidence", help="segment evidence file (default: from concepts.json)")
    ap.add_argument("--facts", help="approved product facts json (default: the "
                                    "facts.json of the project the concepts live in)")
    ap.add_argument("--strict-numbers", action="store_true",
                    help="fail (not warn) on any number missing from facts/evidence")
    args = ap.parse_args()

    doc = store.read_json(args.concepts)
    concepts = doc.get("concepts", doc if isinstance(doc, list) else [])
    base_dir = os.path.dirname(os.path.abspath(args.concepts))

    ev_path = args.evidence or doc.get("evidence_file")
    evidence = ""
    if ev_path:
        p = ev_path if os.path.isabs(ev_path) else os.path.join(ROOT, ev_path)
        if os.path.exists(p):
            evidence = norm(store.read_text(p) or "")
        else:
            print(f"! evidence file not found: {p} - quote checks DISABLED\n")

    # Facts belong to the product, not the pipeline. concepts.json lives at
    # projects/<name>/output/<segment>/, so walk up to the project root rather
    # than falling back to a shared file every product would inherit.
    facts_path = args.facts
    if not facts_path:
        d = base_dir
        for _ in range(4):
            cand = os.path.join(d, "facts.json")
            if os.path.exists(cand):
                facts_path = cand
                break
            d = os.path.dirname(d)
    facts = {}
    if facts_path and os.path.exists(facts_path):
        facts = store.read_json(facts_path)
    else:
        print("! no facts.json found for this project - every number will be "
              "flagged, which is the safe default\n")
    approved_nums = {str(n) for n in facts.get("approved_numbers", [])}

    fails, warns = [], []

    for c in concepts:
        cid = c.get("id", "?")

        for slot, text in walk_slots(c):
            if IMAGE_SLOT.search(slot) or not COPY_SLOT.search(slot):
                continue

            # 1. claim safety
            for pat, why in BANNED:
                m = re.search(pat, text, re.I)
                if m:
                    fails.append(f"[{cid}.{slot}] CLAIM: {why} -> \"{m.group(0)}\"")
            for pat, why in REVIEW:
                m = re.search(pat, text, re.I)
                if m:
                    warns.append(f"[{cid}.{slot}] review '{m.group(0)}' - {why}")

            # 2. verbatim quotes must exist in the evidence corpus
            if re.search(r"(quote|voc)", slot, re.I) and evidence:
                if norm(text) not in evidence:
                    fails.append(
                        f"[{cid}.{slot}] QUOTE not found verbatim in evidence file - "
                        f"either it is paraphrased (mark it as such) or fabricated"
                    )

            # 3. numbers must be approved or present in evidence
            # Clock times ("3:04am") are scene-setting, not stats.
            scan = re.sub(r"\b\d{1,2}:\d{2}\s*(am|pm)?\b", " ", text, flags=re.I)
            for m in NUM.finditer(scan):
                raw = m.group(1).rstrip(".,")
                unit = m.group(2)
                # Bare single digits ("3am", "2 pillows") aren't stats; a digit with a
                # unit ("9%", "30 nights") always is.
                if raw in approved_nums or (len(raw) <= 1 and not unit):
                    continue
                # A number carrying a unit ("93%", "30 nights", "2 years") is a product
                # or offer claim. The VOC corpus can never substantiate one of those -
                # only facts.json can. Bare numbers may fall back to the evidence.
                if not unit and evidence and re.search(rf"\b{re.escape(raw)}\b", evidence):
                    continue
                src = "facts.json" if unit else "facts.json or evidence"
                msg = (f"[{cid}.{slot}] STAT '{m.group(0).strip()}' not in {src} "
                       f"- substantiate or remove")
                (fails if args.strict_numbers else warns).append(msg)

        # 4. structure
        if not c.get("template"):
            fails.append(f"[{cid}] missing 'template'")
        for slot, val in (c.get("slots") or {}).items():
            if IMAGE_SLOT.search(slot) and isinstance(val, str) and val.strip():
                if not re.match(r"^(https?:|data:|file:)", val):
                    if not os.path.exists(os.path.join(base_dir, val)) \
                       and not os.path.exists(os.path.join(ROOT, val)):
                        warns.append(f"[{cid}.{slot}] plate not found: {val}")
        if not c.get("angle"):
            warns.append(f"[{cid}] no 'angle' recorded - needed for post-launch learning")

    for w in warns:
        print(f"  WARN  {w}")
    if warns:
        print()
    for f in fails:
        print(f"  FAIL  {f}")

    print(f"\n{len(concepts)} concepts checked - {len(fails)} fail, {len(warns)} warn")
    if fails:
        print("Not launch-ready. Fix every FAIL; a claim fail is never a copy tweak, "
              "it means the angle needs rebuilding.")
        sys.exit(1)
    print("Claim-safe. Re-read renders for legibility before shipping.")


if __name__ == "__main__":
    main()
