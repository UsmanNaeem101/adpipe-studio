#!/usr/bin/env python3
"""
The product sheet — what is being sold, as structured data.

A product sheet answers, before a penny is spent on ads: who is this for, what
exactly does it solve, can it be sold profitably, why buy it here instead of
Amazon, and can ads actually be made for it. The pipeline reads the rendered
Markdown; the Product tab edits the structured form.

Two files per project, and the direction between them matters:

    projects/<name>/product.json      the answers — the source of truth
    projects/<name>/product_sheet.md  rendered from them, read by picc and brief

The Markdown is derived, never hand-edited as the master, so the form and the
sheet cannot drift apart. Nothing here is specific to any one product: the
schema below is the whole vocabulary, and a fresh project starts empty rather
than inheriting another product's answers.

    python3 pipeline/products.py --schema
    python3 pipeline/products.py --render montisella

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Field kinds the form renders and the sheet knows how to print:
#   text   one line
#   long   a paragraph
#   list   one item per line -> bullets
#   table  fixed columns, any number of rows
#   score  0-10
#   choice one of a fixed set
SECTIONS = [
    {
        "key": "identity", "title": "Product identity",
        "help": "The basic \"what is this?\". Everything downstream quotes it.",
        "fields": [
            {"key": "name", "label": "Product name", "kind": "text",
             "placeholder": "Open-cell latex ergonomic pillow", "required": True},
            {"key": "category", "label": "Product category", "kind": "text",
             "placeholder": "Sleep / bedding / ergonomic pillow / wellness"},
            {"key": "type", "label": "Product type", "kind": "text",
             "placeholder": "Problem-solving, feel-good, convenience, beauty, posture, comfort"},
            {"key": "definition", "label": "One-line product definition", "kind": "long",
             "required": True,
             "placeholder": "A supportive latex pillow designed to keep side sleepers' "
                            "neck and spine aligned while staying cooler than dense memory foam."},
            {"key": "features", "label": "Core physical features", "kind": "list",
             "placeholder": "Material\nSize\nShape\nFirmness\nCover\nWeight\nWashability"},
            {"key": "variants", "label": "Variants", "kind": "list",
             "placeholder": "Sizes\nHeights\nColours\nFirmness levels\nBundles"},
        ],
    },
    {
        "key": "customer", "title": "Customer fit",
        "help": "Who it is for — and, just as load-bearing, who it is not for.",
        "fields": [
            {"key": "primary_buyer", "label": "Primary buyer", "kind": "long",
             "required": True,
             "placeholder": "The person most likely to buy now. e.g. Side sleepers who "
                            "wake with neck stiffness and suspect their pillow."},
            {"key": "secondary_buyers", "label": "Secondary buyers", "kind": "list",
             "placeholder": "People who may buy but need different messaging"},
            {"key": "excluded_buyers", "label": "Excluded buyers", "kind": "list",
             "placeholder": "Stomach sleepers\nPeople expecting a cure for a medical condition"},
            {"key": "segments", "label": "Use-case segments", "kind": "table",
             "columns": ["Segment", "Main reason they care"]},
            {"key": "entry_primary", "label": "Market entry — primary", "kind": "text"},
            {"key": "entry_secondary", "label": "Market entry — secondary", "kind": "text"},
            {"key": "entry_expansion", "label": "Market entry — expansion angle", "kind": "text"},
        ],
    },
    {
        "key": "problems", "title": "Problem hierarchy",
        "help": "Separate what they say from what is physically happening, how it "
                "feels, what it means about them, and why they have not bought yet. "
                "The PICC card turns this into persuasion.",
        "fields": [
            {"key": "surface", "label": "Surface problem — what they say", "kind": "long"},
            {"key": "functional", "label": "Functional problem — what is physically happening",
             "kind": "long"},
            {"key": "emotional", "label": "Emotional problem — what it makes them feel",
             "kind": "long"},
            {"key": "identity", "label": "Identity / social problem", "kind": "long"},
            {"key": "buying", "label": "Buying problem — why they have not bought yet",
             "kind": "long"},
        ],
    },
    {
        "key": "main_problem", "title": "Main problem it solves",
        "help": "One dominant answer, not ten equal ones. One main problem, "
                "multiple supporting benefits.",
        "fields": [
            {"key": "dominant", "label": "The one main problem", "kind": "long",
             "required": True,
             "placeholder": "Helps side sleepers keep their neck aligned overnight so "
                            "they stop waking up stiff, sore and unrested."},
            {"key": "supporting", "label": "Supporting benefits", "kind": "list"},
        ],
    },
    {
        "key": "mechanism", "title": "Mechanism reality",
        "help": "How it actually works, in plain physical terms. Not the marketing "
                "mechanism — picc turns this into that later.",
        "fields": [
            {"key": "how", "label": "What makes it work", "kind": "list", "required": True},
            {"key": "limits", "label": "What it does NOT do", "kind": "list",
             "placeholder": "The honest limits. Anything here must never be claimed."},
        ],
    },
    {
        "key": "offer", "title": "Offer and price point",
        "fields": [
            {"key": "economics", "label": "Unit economics", "kind": "table",
             "columns": ["Metric", "Value"],
             "rows_hint": ["Product cost", "Shipping cost", "Total COGS", "Selling price",
                           "Compare-at price", "Gross margin", "Break-even CPA",
                           "Target CPA", "Target ROAS", "Bundle AOV target"]},
            {"key": "bundles", "label": "Bundle strategy", "kind": "list",
             "placeholder": "1 pillow\n2 pillows\nPillow + cover\nFamily bundle"},
            {"key": "structure", "label": "Offer structure", "kind": "list",
             "placeholder": "BOGO discount\nFree pillowcase\nFree shipping\n30-night trial"},
        ],
    },
    {
        "key": "evidence", "title": "Market evidence",
        "help": "Is there actual demand, or is it imagined? Signals only — the VOC "
                "pipeline supplies the customer's own words separately.",
        "fields": [
            {"key": "signals", "label": "Demand signals", "kind": "list",
             "placeholder": "Amazon review volume\nTikTok Shop sales\nCompetitor ad activity\n"
                            "Search volume\nReddit pain discussions\nMeta Ad Library ads"},
            {"key": "notes", "label": "What the evidence actually says", "kind": "long"},
        ],
    },
    {
        "key": "competitors", "title": "Competitor map",
        "help": "Who already sells this and how they position it.",
        "fields": [
            {"key": "table", "label": "Competitors", "kind": "table",
             "columns": ["Competitor", "Price", "Material", "Main claim", "Offer",
                         "Reviews", "Weakness"]},
            {"key": "angles", "label": "Angles they use", "kind": "list"},
            {"key": "complaints", "label": "What their buyers complain about", "kind": "list"},
            {"key": "gap", "label": "The gap you can own", "kind": "long"},
        ],
    },
    {
        "key": "differentiation", "title": "Differentiation / reason to exist",
        "help": "Why buy yours — including why not just buy it on Amazon.",
        "fields": [
            {"key": "vs_default", "label": "Against the default option", "kind": "long"},
            {"key": "vs_category", "label": "Against the main category alternative", "kind": "long"},
            {"key": "vs_cheap", "label": "Against cheap versions", "kind": "long"},
            {"key": "vs_premium", "label": "Against premium brands", "kind": "long"},
            {"key": "vs_amazon", "label": "Why here instead of Amazon", "kind": "long"},
            {"key": "position", "label": "Market position, in one sentence", "kind": "long",
             "required": True,
             "placeholder": "The cooler, springier ergonomic pillow for side sleepers "
                            "who hate the hot, stiff feel of memory foam."},
        ],
    },
    {
        "key": "objections", "title": "Objections and risks",
        "fields": [
            {"key": "buyer", "label": "Buyer objections", "kind": "list", "required": True},
            {"key": "business", "label": "Business risks", "kind": "list",
             "placeholder": "Return rate from personal fit\nSize mismatch\nMedical claim risk\n"
                            "Amazon comparison\nBulky shipping\nSupplier inconsistency"},
        ],
    },
    {
        "key": "claims", "title": "Claim safety",
        "help": "Ads inherit this. Anything in 'risky' must never reach copy — and a "
                "claim being safe here still does not license a number: only the "
                "approved facts do that.",
        "fields": [
            {"key": "safe", "label": "Safe claims", "kind": "list", "required": True},
            {"key": "risky", "label": "Risky claims — never use", "kind": "list",
             "required": True,
             "placeholder": "Cures neck pain\nFixes migraines\nTreats sleep apnea\n"
                            "Guaranteed pain relief\nMedical-grade correction"},
        ],
    },
    {
        "key": "creative", "title": "Creative feasibility",
        "help": "Can ads actually be made for it? If you cannot get to 20 angles, "
                "the product may be the problem.",
        "fields": [
            {"key": "demonstrable", "label": "Can it be demonstrated visually?",
             "kind": "choice", "options": ["Yes", "Partly", "No"]},
            {"key": "demos", "label": "Best demo ideas", "kind": "list"},
            {"key": "creators", "label": "UGC creator types", "kind": "list"},
            {"key": "angle_depth", "label": "Can you get to 20 angles?", "kind": "choice",
             "options": ["Yes", "Maybe", "No"]},
        ],
    },
    {
        "key": "landing", "title": "Landing page requirements",
        "help": "What the PDP must contain, so nobody builds a pretty but weak page.",
        "fields": [
            {"key": "sections", "label": "Required sections", "kind": "list",
             "placeholder": "Hero with clear promise\nProblem section\nMechanism section\n"
                            "Comparison table\nSize/fit guide\nWho it is for / not for\n"
                            "Review wall\nFAQ\nGuarantee and returns\nBundle offer"},
        ],
    },
    {
        "key": "supplier", "title": "Fulfilment and supplier",
        "help": "Material, dimensions and certification decide whether a claim is "
                "honest, so this is not just logistics.",
        "fields": [
            {"key": "details", "label": "Supplier data", "kind": "table",
             "columns": ["Field", "Value"],
             "rows_hint": ["Supplier name", "MOQ", "Sample ordered", "Sample quality notes",
                           "Product dimensions", "Weight", "Packaging size", "Shipping time",
                           "Private label options", "Certifications", "Return address",
                           "Replacement policy", "Defect rate", "Raw footage available"]},
        ],
    },
    {
        "key": "scorecard", "title": "Product scorecard",
        "help": "Score each 0-10, then commit to a verdict.",
        "fields": [
            {"key": "problem_intensity", "label": "Problem intensity", "kind": "score"},
            {"key": "visual_demo", "label": "Visual demo potential", "kind": "score"},
            {"key": "market_demand", "label": "Market demand", "kind": "score"},
            {"key": "margin", "label": "Margin", "kind": "score"},
            {"key": "differentiation", "label": "Differentiation", "kind": "score"},
            {"key": "amazon_resistance", "label": "Amazon resistance", "kind": "score"},
            {"key": "return_risk", "label": "Return risk (10 = low risk)", "kind": "score"},
            {"key": "supplier_confidence", "label": "Supplier confidence", "kind": "score"},
            {"key": "angle_depth", "label": "Creative angle depth", "kind": "score"},
            {"key": "brand_potential", "label": "Long-term brand potential", "kind": "score"},
            {"key": "verdict", "label": "Verdict", "kind": "choice",
             "options": ["Test", "Research more", "Needs better offer",
                         "Needs better supplier", "Reject"]},
            {"key": "verdict_why", "label": "Why", "kind": "long"},
        ],
    },
]

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


class ProductError(Exception):
    """Raised for bad input. Callers decide how to surface it."""


def schema():
    return SECTIONS


def _field_map():
    return {(s["key"], f["key"]): f for s in SECTIONS for f in s["fields"]}


def blank():
    """An empty sheet — every section present, nothing answered."""
    return {s["key"]: {f["key"]: ([] if f["kind"] in ("list", "table") else "")
                       for f in s["fields"]} for s in SECTIONS}


def product_dir(project):
    if not SAFE_NAME.match(project or ""):
        raise ProductError("bad project name")
    return os.path.join(ROOT, "projects", project)


def doc_path(project):
    return os.path.join(product_dir(project), "product.json")


def sheet_path(project):
    return os.path.join(product_dir(project), "product_sheet.md")


def load(project):
    """The stored answers, merged over a blank so a new field is never missing."""
    doc = blank()
    p = doc_path(project)
    if os.path.exists(p):
        try:
            stored = json.load(open(p, encoding="utf-8"))
        except ValueError as e:
            raise ProductError(f"product.json is not valid JSON: {e}")
        for sk, fields in (stored or {}).items():
            if sk in doc and isinstance(fields, dict):
                for fk, v in fields.items():
                    if fk in doc[sk]:
                        doc[sk][fk] = v
    return doc


def _clean(doc):
    """Keep only known fields, coerced to the kind the schema declares.

    The form posts whatever the browser had; this is what decides what is
    stored, so an unknown key or a string where a table belongs cannot make it
    into the sheet the pipeline reads.
    """
    fm = _field_map()
    out = blank()
    for sk, fields in (doc or {}).items():
        for fk, v in (fields or {}).items():
            f = fm.get((sk, fk))
            if not f:
                continue
            kind = f["kind"]
            if kind == "list":
                if isinstance(v, str):
                    v = [ln.strip() for ln in v.splitlines()]
                v = [str(x).strip() for x in (v or []) if str(x).strip()]
            elif kind == "table":
                rows = []
                for r in (v or []):
                    if isinstance(r, dict):
                        r = [r.get(c, "") for c in f["columns"]]
                    cells = [str(c).strip() for c in (r or [])][:len(f["columns"])]
                    cells += [""] * (len(f["columns"]) - len(cells))
                    if any(cells):
                        rows.append(cells)
                v = rows
            elif kind == "score":
                try:
                    v = "" if str(v).strip() == "" else str(max(0, min(10, int(float(v)))))
                except (TypeError, ValueError):
                    v = ""
            else:
                v = str(v or "").strip()
            out[sk][fk] = v
    return out


def missing_required(doc):
    """Required fields with no answer, as human labels."""
    out = []
    for s in SECTIONS:
        for f in s["fields"]:
            if f.get("required") and not doc.get(s["key"], {}).get(f["key"]):
                out.append(f"{s['title']} — {f['label']}")
    return out


def completeness(doc):
    """How much of the sheet is answered, as (answered, total)."""
    total = answered = 0
    for s in SECTIONS:
        for f in s["fields"]:
            total += 1
            if doc.get(s["key"], {}).get(f["key"]):
                answered += 1
    return answered, total


def to_markdown(doc, project=""):
    """Render the sheet the pipeline reads. Unanswered fields are omitted, so an
    empty section does not read as a deliberate blank."""
    name = doc.get("identity", {}).get("name") or project or "Product"
    out = [f"# {name} — Product Sheet", ""]
    answered, total = completeness(doc)
    out += [f"_{answered} of {total} fields answered._", ""]

    for s in SECTIONS:
        vals = doc.get(s["key"], {})
        body = []
        for f in s["fields"]:
            v = vals.get(f["key"])
            if not v:
                continue
            if f["kind"] == "list":
                body.append(f"**{f['label']}**")
                body += [f"- {x}" for x in v]
                body.append("")
            elif f["kind"] == "table":
                body.append(f"**{f['label']}**")
                body.append("")
                body.append("| " + " | ".join(f["columns"]) + " |")
                body.append("|" + "|".join("---" for _ in f["columns"]) + "|")
                for row in v:
                    body.append("| " + " | ".join(row) + " |")
                body.append("")
            elif f["kind"] == "score":
                body.append(f"- **{f['label']}:** {v}/10")
            else:
                body.append(f"**{f['label']}**")
                body.append("")
                body.append(str(v))
                body.append("")
        if body:
            out.append(f"## {s['title']}")
            out.append("")
            out += body
            out.append("")

    missing = missing_required(doc)
    if missing:
        out += ["## Not yet answered", "",
                "These are unanswered. Treat anything depending on them as unknown — "
                "do not infer a value.", ""]
        out += [f"- {m}" for m in missing]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def save(project, doc):
    """Store the answers and re-render the Markdown the pipeline reads."""
    d = product_dir(project)
    if not os.path.isdir(d):
        raise ProductError(f"no project {project!r}")

    # First structured save over a hand-written sheet: the render is built only
    # from the form, so anything written straight into the Markdown would be
    # gone. Keep a copy rather than discovering that afterwards.
    sheet, docp = sheet_path(project), doc_path(project)
    if os.path.exists(sheet) and not os.path.exists(docp):
        backup = os.path.join(d, "product_sheet.previous.md")
        if not os.path.exists(backup):
            with open(sheet, encoding="utf-8") as src, \
                 open(backup, "w", encoding="utf-8") as dst:
                dst.write(src.read())

    clean = _clean(doc)
    json.dump(clean, open(docp, "w", encoding="utf-8"), indent=2)
    open(sheet, "w", encoding="utf-8").write(to_markdown(clean, project))
    return clean


def summary(project):
    """One row for the product list."""
    doc = load(project)
    answered, total = completeness(doc)
    sc = doc.get("scorecard", {})
    scores = [int(sc[k]) for k in sc
              if k not in ("verdict", "verdict_why") and str(sc.get(k)).strip().isdigit()]
    return {
        "project": project,
        "name": doc.get("identity", {}).get("name") or "",
        "definition": doc.get("identity", {}).get("definition") or "",
        "position": doc.get("differentiation", {}).get("position") or "",
        "verdict": sc.get("verdict") or "",
        "score": round(sum(scores) / len(scores), 1) if scores else None,
        "answered": answered, "total": total,
        "missing_required": len(missing_required(doc)),
        "has_sheet": os.path.exists(sheet_path(project)),
    }


def main():
    if "--schema" in sys.argv:
        print(json.dumps(SECTIONS, indent=2)); return
    if "--render" in sys.argv:
        i = sys.argv.index("--render")
        proj = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        print(to_markdown(load(proj), proj)); return
    n = sum(len(s["fields"]) for s in SECTIONS)
    print(f"  {len(SECTIONS)} sections, {n} fields")
    for s in SECTIONS:
        print(f"    {s['title']:<34} {len(s['fields'])} fields")


if __name__ == "__main__":
    main()
