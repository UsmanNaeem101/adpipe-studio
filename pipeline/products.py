#!/usr/bin/env python3
"""
The Product system — three layers, kept apart on purpose.

    Layer 1  Product Truth        known before research; true regardless of who
                                  is being sold to. Material, mechanism, price,
                                  supplier, what it does NOT do.
    Layer 2  Customer Truth       discovered by research; belongs to a Segment.
                                  Pains, outcomes, failed solutions, objections.
    Layer 3  Strategic Conclusion  Product Truth + one Segment's Customer Truth.
                                  Positioning, primary benefit, reason to buy.

The rule that makes this worth the structure: only Product Truth may license a
product claim. Customer Truth identifies an opportunity — "I want something that
doesn't get hot" — and Product Truth decides whether the product can credibly
satisfy it. A customer's desire is never evidence of a capability.

A product is not sold to one buyer. It has Markets (optional grouping) and
Segments (the real unit), and each Segment carries its own research, its own
strategy and its own PICC branch. There is deliberately no "Primary Buyer" field
at product level: collapsing many segments into one profile is the thing this
module exists to prevent.

    projects/<proj>/products/<product>/product.json      Product Truth
    projects/<proj>/products/<product>/segments.json     the Segment objects
    projects/<proj>/products/<product>/product_sheet.md  rendered, read by pipeline
    projects/<proj>/products/<product>/segments/<s>.md   per-segment strategy

A project holds several products; a product holds several segments.

Both Markdown files are derived from the JSON, never the master.

    python3 pipeline/products.py --schema
    python3 pipeline/products.py --render <project>

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where a value came from. Preserved per field because the guardrail depends on
# it: a benefit sourced from VOC cannot license a claim, one sourced from the
# supplier can.
SOURCES = ["merchant", "supplier", "testing", "research", "strategy", "user"]

# What is known about a value. EMPTY and ai_suggested are not answers yet;
# verified_fact is the only state that licenses a product claim.
STATES = [
    "empty",                  # nothing entered
    "ai_suggested",           # proposed by enrichment, not yet accepted
    "user_approved",          # a person accepted or wrote it
    "verified_fact",          # confirmed against supplier/testing — licenses claims
    "research_derived",       # came out of the research pipeline
    "unvalidated_hypothesis",  # a guess made before research; never Customer Truth
    "needs_verification",     # in use but unconfirmed
    "rejected",               # considered and turned down
]

CLAIM_LICENSING_STATES = {"verified_fact"}

# ---------------------------------------------------------------- Layer 1
# Product Truth only. Nothing customer-related belongs here — the create screen
# renders exactly these, and the spec is explicit that a merchant must not be
# asked to guess a buyer, a pain or a position before research exists.
PRODUCT_SECTIONS = [
    {"key": "identity", "title": "Product identity", "stage": "create",
     "help": "What the thing is. The only genuinely required part.",
     "fields": [
         {"key": "name", "label": "Product name", "kind": "text", "required": True},
         {"key": "category", "label": "Category", "kind": "text", "required": True,
          "placeholder": "Sleep / bedding / ergonomic pillow"},
         {"key": "type", "label": "Product type", "kind": "text", "required": True,
          "placeholder": "Problem-solving / convenience / comfort"},
         {"key": "definition", "label": "One-line definition", "kind": "long",
          "required": True},
         {"key": "product_url", "label": "Product URL", "kind": "text"},
         {"key": "supplier_url", "label": "Supplier URL", "kind": "text"},
         {"key": "images", "label": "Image URLs or paths", "kind": "list"},
     ]},
    {"key": "facts", "title": "Product facts", "stage": "create",
     "help": "Physical truth from the supplier, merchant or your own testing.",
     "fields": [
         {"key": "features", "label": "Core physical features", "kind": "list",
          "required": True},
         {"key": "materials", "label": "Materials", "kind": "list", "required": True},
         {"key": "construction", "label": "Construction", "kind": "long"},
         {"key": "limitations", "label": "Known limitations", "kind": "list",
          "required": True},
         {"key": "does_not_do", "label": "What the product does NOT do", "kind": "list",
          "required": True,
          "placeholder": "The honest negative space. Anything here must never be claimed."},
         {"key": "dimensions", "label": "Dimensions", "kind": "text"},
         {"key": "weight", "label": "Weight", "kind": "text"},
         {"key": "variants", "label": "Variants", "kind": "list"},
         {"key": "certifications", "label": "Certifications", "kind": "list"},
         {"key": "care", "label": "Washability / adjustability", "kind": "text"},
     ]},
    {"key": "mechanism", "title": "Mechanism reality", "stage": "create",
     "help": "How it physically works. Not what customers believe about it — that "
             "is Customer Truth and lives on the Segment.",
     "fields": [
         {"key": "how_works", "label": "How the product works", "kind": "list",
          "required": True},
         {"key": "components", "label": "Mechanism components", "kind": "list"},
         {"key": "why_matters", "label": "Why each component matters", "kind": "long"},
         {"key": "limits", "label": "Mechanism limitations", "kind": "list"},
     ]},
    {"key": "claims", "title": "Claims", "stage": "create",
     "help": "Product-level and claim-licensing. Segment research may never add to "
             "supported claims — only Product Truth can.",
     "fields": [
         {"key": "supported", "label": "Supported claims", "kind": "list",
          "required": True},
         {"key": "unsupported", "label": "Unsupported claims", "kind": "list"},
         {"key": "prohibited", "label": "Prohibited claims", "kind": "list",
          "required": True,
          "placeholder": "Cures X\nTreats Y\nGuaranteed relief"},
         {"key": "medical", "label": "Medical restrictions", "kind": "long"},
         {"key": "wording", "label": "Claim wording restrictions", "kind": "long"},
     ]},
    {"key": "offer", "title": "Offer", "stage": "create",
     "fields": [
         {"key": "selling_price", "label": "Selling price", "kind": "text"},
         {"key": "compare_at", "label": "Compare-at price", "kind": "text"},
         {"key": "structure", "label": "Offer structure", "kind": "list"},
         {"key": "bundles", "label": "Bundles", "kind": "list"},
         {"key": "guarantee", "label": "Guarantee", "kind": "text"},
         {"key": "returns", "label": "Returns", "kind": "text"},
         {"key": "shipping", "label": "Shipping", "kind": "text"},
         {"key": "trial", "label": "Trial", "kind": "text"},
     ]},
    {"key": "economics", "title": "Economics", "stage": "later",
     "fields": [
         {"key": "table", "label": "Unit economics", "kind": "table",
          "columns": ["Metric", "Value"],
          "rows_hint": ["Product cost", "Shipping cost", "Packaging", "Fees", "COGS",
                        "Gross margin", "Contribution margin", "Break-even CPA",
                        "Target CPA", "Target ROAS", "Target AOV"]},
     ]},
    {"key": "proof", "title": "Proof", "stage": "later",
     "help": "Product-level proof. Customer proof (reviews, UGC) attaches to segments.",
     "fields": [
         {"key": "verified", "label": "Verified product facts", "kind": "list"},
         {"key": "supplier_proof", "label": "Supplier proof", "kind": "list"},
         {"key": "testing", "label": "Testing", "kind": "list"},
         {"key": "demos", "label": "Demonstrations available", "kind": "list"},
         {"key": "media", "label": "Product photos / videos", "kind": "list"},
     ]},
    {"key": "features_benefits", "title": "Feature → benefit mapping", "stage": "later",
     "help": "One feature may serve different segments for different reasons. Claim "
             "status is what decides whether the benefit may be advertised.",
     "fields": [
         {"key": "map", "label": "Mapping", "kind": "table",
          "columns": ["Feature", "Functional effect", "Potential benefit",
                      "Relevant segments", "Evidence", "Claim status"]},
     ]},
    {"key": "creative", "title": "Creative feasibility (product-level)", "stage": "later",
     "fields": [
         {"key": "demonstrable", "label": "Can it be demonstrated?", "kind": "choice",
          "options": ["Yes", "Partly", "No"]},
         {"key": "demo_ideas", "label": "Best product demo ideas", "kind": "list"},
         {"key": "visual_features", "label": "Best visual features", "kind": "list"},
         {"key": "before_after", "label": "Before/after capability", "kind": "long"},
         {"key": "comparison", "label": "Comparison capability", "kind": "long"},
         {"key": "restrictions", "label": "Creative restrictions", "kind": "list"},
         {"key": "media", "label": "Available media / raw footage", "kind": "list"},
     ]},
    {"key": "supplier", "title": "Supplier and fulfilment", "stage": "later",
     "help": "Material, dimensions and certification decide whether a claim is "
             "honest, so this is not only logistics.",
     "fields": [
         {"key": "table", "label": "Supplier data", "kind": "table",
          "columns": ["Field", "Value"],
          "rows_hint": ["Supplier name", "Supplier URL", "MOQ", "Sample ordered",
                        "Sample status", "Sample notes", "Shipping time",
                        "Private label", "Customisation", "Packaging",
                        "Certifications", "Return address", "Replacement policy",
                        "Defect rate", "Raw footage available"]},
     ]},
    {"key": "competitors", "title": "Competitor map", "stage": "later",
     "fields": [
         {"key": "table", "label": "Competitors", "kind": "table",
          "columns": ["Competitor", "Price", "Material", "Main claim", "Main angle",
                      "Offer", "Reviews", "Weaknesses", "Relevant segment"]},
     ]},
    {"key": "scorecard", "title": "Product scorecard", "stage": "later",
     "help": "Product-level only. Segments carry their own opportunity scores.",
     "fields": [
         {"key": "problem_solving", "label": "Problem-solving potential", "kind": "score"},
         {"key": "visual_demo", "label": "Visual demo potential", "kind": "score"},
         {"key": "market_demand", "label": "Market demand", "kind": "score"},
         {"key": "margin", "label": "Margin", "kind": "score"},
         {"key": "differentiation", "label": "Product differentiation", "kind": "score"},
         {"key": "amazon_resistance", "label": "Amazon resistance", "kind": "score"},
         {"key": "return_risk", "label": "Return risk (10 = low)", "kind": "score"},
         {"key": "supplier_confidence", "label": "Supplier confidence", "kind": "score"},
         {"key": "creative_depth", "label": "Creative depth", "kind": "score"},
         {"key": "brand_potential", "label": "Long-term brand potential", "kind": "score"},
         {"key": "verdict", "label": "Final verdict", "kind": "choice",
          "options": ["Test", "Prioritise", "Research more", "Needs better offer",
                      "Needs better supplier", "Reject"]},
     ]},
    {"key": "hypothesis", "title": "Initial target hypothesis", "stage": "create",
     "help": "Optional, and never Customer Truth. Anything here is a guess made "
             "before research and is labelled UNVALIDATED HYPOTHESIS everywhere it "
             "appears, until research validates it into a Segment.",
     "fields": [
         {"key": "guess", "label": "Who you think will buy, and why", "kind": "long"},
     ]},
]

# ---------------------------------------------------------------- Layer 2 + 3
# Everything here is per-Segment. Customer Truth comes from research; strategy is
# synthesised from it plus Product Truth.
SEGMENT_SECTIONS = [
    {"key": "identity", "title": "Segment", "layer": "segment",
     "fields": [
         {"key": "name", "label": "Segment name", "kind": "text", "required": True},
         {"key": "market", "label": "Parent market", "kind": "text"},
         {"key": "description", "label": "Description", "kind": "long", "enrich": True},
         {"key": "status", "label": "Validation status", "kind": "choice",
          "options": ["Discovered", "Validated", "Active", "Paused", "Rejected",
                      "Merged"]},
         {"key": "priority", "label": "Priority", "kind": "choice",
          "options": ["High", "Medium", "Low"]},
         {"key": "awareness", "label": "Awareness level", "kind": "choice", "enrich": True,
          "options": ["Unaware", "Problem aware", "Solution aware", "Product aware",
                      "Most aware"]},
         {"key": "evidence_project", "label": "Research project", "kind": "text",
          "placeholder": "which project holds this segment's evidence and "
                         "extractions (default: this product's own project)"},
         {"key": "evidence_slug", "label": "Pipeline segment slug", "kind": "text",
          "placeholder": "the evidence/<slug>.txt this segment's research lives under"},
     ]},
    {"key": "evidence", "title": "Evidence", "layer": "customer",
     "fields": [
         {"key": "summary", "label": "Evidence summary", "kind": "long", "enrich": True},
         {"key": "voc_volume", "label": "VOC volume", "kind": "text", "enrich": True},
         {"key": "sources", "label": "Research sources", "kind": "list"},
         {"key": "refs", "label": "Research extraction references", "kind": "list"},
     ]},
    {"key": "fit", "title": "Customer fit", "layer": "customer",
     "fields": [
         {"key": "who", "label": "Who this segment is", "kind": "long", "enrich": True},
         {"key": "why_care", "label": "Why they care", "kind": "long", "enrich": True},
         {"key": "primary_use_case", "label": "Primary use case", "kind": "text", "enrich": True},
         {"key": "secondary_use_cases", "label": "Secondary use cases", "kind": "list", "enrich": True},
         {"key": "context", "label": "Situational context", "kind": "long", "enrich": True},
         {"key": "product_fit", "label": "Relevant product fit", "kind": "long"},
         {"key": "excluded", "label": "Excluded / poor-fit buyers", "kind": "list", "enrich": True},
         {"key": "entry_point", "label": "Market entry point for this segment",
          "kind": "long", "synth": True},
     ]},
    {"key": "problems", "title": "Problem hierarchy", "layer": "customer",
     "help": "Different segments legitimately have different main problems.",
     "fields": [
         {"key": "main", "label": "Main problem", "kind": "long", "synth": True},
         {"key": "surface", "label": "Surface problem — what they say", "kind": "long"},
         {"key": "functional", "label": "Functional problem", "kind": "long"},
         {"key": "emotional", "label": "Emotional problem", "kind": "long"},
         {"key": "identity", "label": "Identity / social problem", "kind": "long"},
         {"key": "buying", "label": "Buying problem — why not yet", "kind": "long"},
     ]},
    {"key": "voice", "title": "Customer truth (from research)", "layer": "customer",
     "help": "Raw customer intelligence, kept multi-valued. This is evidence, not "
             "strategy: a complaint is stored as a complaint, never as a conclusion "
             "about how to position against it.",
     "fields": [
         {"key": "pain_points", "label": "Pain points", "kind": "list", "enrich": True,
          "skills": "07"},
         {"key": "pain_moments", "label": "Pain moments", "kind": "list", "enrich": True,
          "skills": "08"},
         {"key": "desired_outcomes", "label": "Desired outcomes", "kind": "list",
          "enrich": True, "skills": "09"},
         {"key": "failed_solutions", "label": "Failed solutions", "kind": "list",
          "enrich": True, "skills": "14"},
         {"key": "assumed_solutions", "label": "Assumed solutions", "kind": "list",
          "enrich": True, "skills": "15"},
         {"key": "beliefs", "label": "Beliefs", "kind": "list", "enrich": True,
          "skills": "12"},
         {"key": "limiting_beliefs", "label": "Limiting beliefs", "kind": "list",
          "enrich": True, "skills": "13"},
         {"key": "triggers", "label": "Buying triggers", "kind": "list", "enrich": True,
          "skills": "16"},
         {"key": "criteria", "label": "Purchase criteria", "kind": "list",
          "enrich": True, "skills": "17"},
         {"key": "use_cases", "label": "Use cases", "kind": "list", "enrich": True,
          "skills": "07,08,09"},
         {"key": "voc_language", "label": "VOC language", "kind": "list",
          "enrich": True, "skills": "24,25,26"},
         {"key": "competitor_complaints", "label": "Competitor complaints",
          "kind": "list", "enrich": True, "skills": "22"},
         {"key": "market_gap", "label": "Ignored complaints / market gap",
          "kind": "list", "enrich": True, "skills": "22,18"},
         {"key": "proof_expectations", "label": "Proof expectations", "kind": "list",
          "enrich": True, "skills": "20"},
     ]},
    {"key": "benefits", "title": "Benefits", "layer": "strategy",
     "help": "Each must be licensed by a Product Fact. If none licenses it, it is "
             "not a benefit — it is a customer wish.",
     "fields": [
         {"key": "primary", "label": "Primary benefit", "kind": "long", "synth": True},
         {"key": "secondary", "label": "Secondary benefits", "kind": "list", "synth": True},
         {"key": "rtb", "label": "Reason to believe", "kind": "table", "synth": True,
          "columns": ["Benefit", "Reason to believe", "Proof", "Licensing product fact",
                      "Confidence"]},
     ]},
    {"key": "objections", "title": "Objections", "layer": "customer",
     "fields": [
         {"key": "buyer", "label": "Buyer objections", "kind": "list"},
         {"key": "trust", "label": "Trust objections", "kind": "list"},
         {"key": "price", "label": "Price objections", "kind": "list"},
         {"key": "fit", "label": "Fit objections", "kind": "list"},
         {"key": "failed_solutions", "label": "Previous failed solutions", "kind": "list"},
     ]},
    {"key": "strategy", "title": "Strategic conclusions", "layer": "strategy",
     "help": "Only valid once Product Truth and this segment's Customer Truth both "
             "exist. Never guessed beforehand.",
     "fields": [
         {"key": "position", "label": "Segment-specific market position", "kind": "long", "synth": True},
         {"key": "reason_to_buy", "label": "Primary reason this segment should buy",
          "kind": "long", "synth": True},
         {"key": "vs_default", "label": "Against their default solution", "kind": "long", "synth": True},
         {"key": "vs_failed", "label": "Against their failed solutions", "kind": "long", "synth": True},
         {"key": "vs_amazon", "label": "Against Amazon alternatives", "kind": "long", "synth": True},
         {"key": "vs_competitors", "label": "Against named competitors", "kind": "long", "synth": True},
         {"key": "territories", "label": "Messaging territories", "kind": "list", "synth": True},
     ]},
    {"key": "creative", "title": "Creative (segment-level)", "layer": "strategy",
     "fields": [
         {"key": "demo_ideas", "label": "Best demo ideas for this segment", "kind": "list", "synth": True},
         {"key": "metaphors", "label": "Visual metaphors", "kind": "list", "synth": True},
         {"key": "ugc_types", "label": "UGC creator types", "kind": "list", "synth": True},
         {"key": "scenarios", "label": "Relatable scenarios", "kind": "list", "synth": True},
         {"key": "pain_viz", "label": "Pain visualisations", "kind": "list", "synth": True},
         {"key": "outcome_viz", "label": "Desired-outcome visualisations", "kind": "list", "synth": True},
         {"key": "territory_count", "label": "Credible creative territories", "kind": "score"},
     ]},
    {"key": "landing", "title": "Landing page strategy", "layer": "strategy",
     "help": "Segment-specific. The same product sells behind a different hero, "
             "pain section and proof order depending on who arrived.",
     "fields": [
         {"key": "hero_promise", "label": "Hero promise", "kind": "long", "synth": True},
         {"key": "problem_section", "label": "Problem section", "kind": "long", "synth": True},
         {"key": "mechanism_framing", "label": "Mechanism framing", "kind": "long", "synth": True},
         {"key": "comparison", "label": "Comparison section", "kind": "long", "synth": True},
         {"key": "proof_order", "label": "Proof order", "kind": "list", "synth": True},
         {"key": "objections_to_address", "label": "Objections to address", "kind": "list",
          "synth": True},
         {"key": "who_for", "label": "Who it is for", "kind": "list", "synth": True},
         {"key": "who_not_for", "label": "Who it is NOT for", "kind": "list", "synth": True},
         {"key": "trust", "label": "Trust elements", "kind": "list", "synth": True},
     ]},
    {"key": "compatibility", "title": "Product × segment compatibility", "layer": "strategy",
     "help": "Checked per segment. An incompatible pair must not generate PICCs or "
             "concepts, and the reason is recorded rather than silently skipped.",
     "fields": [
         {"key": "status", "label": "Compatibility", "kind": "choice", "synth": True,
          "options": ["Strong fit", "Plausible fit", "Weak fit", "Needs evidence",
                      "Incompatible"]},
         {"key": "why", "label": "Why", "kind": "long", "synth": True},
     ]},
    {"key": "scorecard", "title": "Segment opportunity scorecard", "layer": "strategy",
     "fields": [
         {"key": "pain_intensity", "label": "Pain intensity", "kind": "score"},
         {"key": "demand_strength", "label": "Demand strength", "kind": "score"},
         {"key": "voc_volume", "label": "VOC volume", "kind": "score"},
         {"key": "purchase_intent", "label": "Purchase intent", "kind": "score"},
         {"key": "competition", "label": "Competition (10 = low)", "kind": "score"},
         {"key": "product_fit", "label": "Product fit", "kind": "score"},
         {"key": "differentiation", "label": "Differentiation potential", "kind": "score"},
         {"key": "creative_depth", "label": "Creative depth", "kind": "score"},
         {"key": "proof_availability", "label": "Proof availability", "kind": "score"},
         {"key": "compliance_risk", "label": "Compliance risk (10 = low)", "kind": "score"},
         {"key": "verdict", "label": "Segment verdict", "kind": "choice",
          "options": ["Prioritise", "Test", "Research more", "Weak fit", "Pause",
                      "Reject"]},
     ]},
]

# Which sections the lightweight create screen shows. The spec is explicit that
# nothing customer-related may be required before research exists.
CREATE_SECTIONS = [s["key"] for s in PRODUCT_SECTIONS if s.get("stage") == "create"]

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
SLUG_BAD = re.compile(r"[^a-z0-9]+")


class ProductError(Exception):
    """Raised for bad input. Callers decide how to surface it."""


# ------------------------------------------------------------------ values

def cell(value=None, state="empty", source="", ref="", confidence=""):
    """One field: the value plus where it came from and how far to trust it."""
    return {"value": value if value is not None else "", "state": state,
            "source": source, "ref": ref, "confidence": confidence}


def value_of(c):
    return c.get("value") if isinstance(c, dict) else c


def is_answered(c):
    v = value_of(c)
    return bool(v) if not isinstance(v, str) else bool(v.strip())


def licenses_claims(c):
    """Only a verified product fact may license a product claim."""
    return isinstance(c, dict) and c.get("state") in CLAIM_LICENSING_STATES


def _blank_for(kind):
    return [] if kind in ("list", "table") else ""


def blank_product():
    return {s["key"]: {f["key"]: cell(_blank_for(f["kind"]))
                       for f in s["fields"]} for s in PRODUCT_SECTIONS}


def blank_segment():
    return {s["key"]: {f["key"]: cell(_blank_for(f["kind"]))
                       for f in s["fields"]} for s in SEGMENT_SECTIONS}


def _coerce(kind, v, columns=None):
    if kind == "list":
        if isinstance(v, str):
            v = v.splitlines()
        return [str(x).strip() for x in (v or []) if str(x).strip()]
    if kind == "table":
        cols = columns or []
        rows = []
        for r in (v or []):
            if isinstance(r, dict):
                r = [r.get(c, "") for c in cols]
            cells = [str(c).strip() for c in (r or [])][:len(cols)]
            cells += [""] * (len(cols) - len(cells))
            if any(cells):
                rows.append(cells)
        return rows
    if kind == "score":
        try:
            return "" if str(v).strip() == "" else str(max(0, min(10, int(float(v)))))
        except (TypeError, ValueError):
            return ""
    return str(v or "").strip()


def _clean(doc, sections):
    """Keep only known fields, coerced to their declared kind, provenance intact.

    Whatever the browser posts, this decides what is stored — an unknown key, a
    bogus state or a string where a table belongs cannot reach the rendered sheet
    the pipeline reads.
    """
    out = {s["key"]: {f["key"]: cell(_blank_for(f["kind"])) for f in s["fields"]}
           for s in sections}
    fm = {(s["key"], f["key"]): f for s in sections for f in s["fields"]}
    for sk, fields in (doc or {}).items():
        for fk, raw in (fields or {}).items():
            f = fm.get((sk, fk))
            if not f:
                continue
            c = raw if isinstance(raw, dict) and "value" in raw else {"value": raw}
            v = _coerce(f["kind"], c.get("value"), f.get("columns"))
            state = c.get("state") if c.get("state") in STATES else None
            if not state:
                state = "user_approved" if v else "empty"
            if not v:
                state = "empty" if state not in ("rejected",) else state
            out[sk][fk] = cell(v, state,
                               c.get("source") if c.get("source") in SOURCES else "",
                               str(c.get("ref") or "").strip(),
                               str(c.get("confidence") or "").strip())
    return out


# ------------------------------------------------------------------ paths

def project_dir(project):
    if not SAFE_NAME.match(project or ""):
        raise ProductError("bad project name")
    return os.path.join(ROOT, "projects", project)


def products_root(project):
    return os.path.join(project_dir(project), "products")


def product_dir(project, product):
    """One folder per product. A project holds several — the same VOC project can
    carry a pillow and a mattress topper, each with its own truth and segments."""
    if not SAFE_NAME.match(product or ""):
        raise ProductError("bad product id")
    return os.path.join(products_root(project), product)


def list_products(project):
    d = products_root(project)
    if not os.path.isdir(d):
        return []
    return sorted(p for p in os.listdir(d)
                  if os.path.isdir(os.path.join(d, p)) and not p.startswith((".", "_")))


def resolve_product(project, product=None):
    """Which product a request means. Explicit wins; a project with exactly one
    product needs no argument; more than one and the caller must say."""
    if product:
        return product
    have = list_products(project)
    if len(have) == 1:
        return have[0]
    if not have:
        raise ProductError(f"project {project!r} has no products yet")
    raise ProductError(
        f"project {project!r} has {len(have)} products ({', '.join(have)}) — "
        f"say which one")


def migrate_legacy(project):
    """Move a one-product-per-project layout into products/<slug>/.

    The earlier implementation stored product.json at the project root. Rather
    than leave those unreachable, fold them into the new layout once, keyed by
    the product name that was already there.
    """
    root = project_dir(project)
    legacy = os.path.join(root, "product.json")
    if not os.path.exists(legacy) or list_products(project):
        return None
    try:
        stored = json.load(open(legacy, encoding="utf-8"))
    except ValueError:
        return None
    name = value_of((stored.get("identity") or {}).get("name")) or project
    slug = slugify(name)[:40] or "product"
    d = product_dir(project, slug)
    os.makedirs(d, exist_ok=True)
    os.replace(legacy, os.path.join(d, "product.json"))
    for fn in ("product_sheet.md", "product_sheet.previous.md", "segments.json"):
        src = os.path.join(root, fn)
        if os.path.exists(src):
            os.replace(src, os.path.join(d, fn))
    return slug


def doc_path(project, product):
    return os.path.join(product_dir(project, product), "product.json")


def segments_path(project, product):
    return os.path.join(product_dir(project, product), "segments.json")


def sheet_path(project, product):
    return os.path.join(product_dir(project, product), "product_sheet.md")


def segment_sheet_path(project, product, slug):
    return os.path.join(product_dir(project, product), "segments", f"{slug}.md")


def slugify(name):
    s = SLUG_BAD.sub("_", (name or "").lower()).strip("_")
    return s or "segment"


# ------------------------------------------------------------------ load/save

def load(project, product):
    doc = blank_product()
    p = doc_path(project, product)
    if os.path.exists(p):
        try:
            stored = json.load(open(p, encoding="utf-8"))
        except ValueError as e:
            raise ProductError(f"product.json is not valid JSON: {e}")
        doc = _clean(stored, PRODUCT_SECTIONS)
    return doc


def load_segments(project, product):
    p = segments_path(project, product)
    if not os.path.exists(p):
        return []
    try:
        rows = json.load(open(p, encoding="utf-8"))
    except ValueError as e:
        raise ProductError(f"segments.json is not valid JSON: {e}")
    out = []
    for r in (rows or []):
        slug = r.get("slug") or slugify(
            value_of((r.get("doc") or {}).get("identity", {}).get("name")))
        out.append({"slug": slug, "doc": _clean(r.get("doc") or {}, SEGMENT_SECTIONS)})
    return out


def save(project, product, doc):
    d = product_dir(project, product)
    os.makedirs(d, exist_ok=True)
    sheet, docp = sheet_path(project, product), doc_path(project, product)
    # A hand-written sheet predates the form. The render is built only from the
    # form, so keep a copy rather than discovering the loss afterwards.
    if os.path.exists(sheet) and not os.path.exists(docp):
        backup = os.path.join(d, "product_sheet.previous.md")
        if not os.path.exists(backup):
            open(backup, "w", encoding="utf-8").write(
                open(sheet, encoding="utf-8").read())
    clean = _clean(doc, PRODUCT_SECTIONS)
    json.dump(clean, open(docp, "w", encoding="utf-8"), indent=2)
    open(sheet, "w", encoding="utf-8").write(product_markdown(clean, product))
    return clean


def save_segments(project, product, rows):
    d = product_dir(project, product)
    os.makedirs(d, exist_ok=True)
    out, seen = [], set()
    for r in (rows or []):
        doc = _clean(r.get("doc") or {}, SEGMENT_SECTIONS)
        name = value_of(doc["identity"]["name"])
        if not name:
            continue
        slug = r.get("slug") or slugify(name)
        while slug in seen:
            slug += "_2"
        seen.add(slug)
        out.append({"slug": slug, "doc": doc})
    json.dump(out, open(segments_path(project, product), "w", encoding="utf-8"), indent=2)

    sd = os.path.join(d, "segments")
    os.makedirs(sd, exist_ok=True)
    for r in out:
        open(segment_sheet_path(project, product, r["slug"]), "w",
             encoding="utf-8").write(
            segment_markdown(r["doc"], load(project, product), r["slug"]))
    return out


# ------------------------------------------------------------------ render

def _render_fields(section, vals, out):
    body = []
    for f in section["fields"]:
        c = vals.get(f["key"])
        if not is_answered(c):
            continue
        v = value_of(c)
        tag = ""
        st = c.get("state") if isinstance(c, dict) else ""
        if st == "unvalidated_hypothesis":
            tag = "  _[UNVALIDATED HYPOTHESIS — not customer truth]_"
        elif st == "ai_suggested":
            tag = "  _[AI suggested — not yet approved]_"
        elif st == "needs_verification":
            tag = "  _[needs verification]_"
        elif st == "verified_fact":
            tag = "  _[verified product fact]_"
        if f["kind"] == "list":
            body.append(f"**{f['label']}**{tag}")
            body += [f"- {x}" for x in v]
            body.append("")
        elif f["kind"] == "table":
            body.append(f"**{f['label']}**{tag}")
            body.append("")
            body.append("| " + " | ".join(f["columns"]) + " |")
            body.append("|" + "|".join("---" for _ in f["columns"]) + "|")
            for row in v:
                body.append("| " + " | ".join(row) + " |")
            body.append("")
        elif f["kind"] == "score":
            body.append(f"- **{f['label']}:** {v}/10")
        else:
            body.append(f"**{f['label']}**{tag}")
            body.append("")
            body.append(str(v))
            body.append("")
    if body:
        out.append(f"## {section['title']}")
        out.append("")
        out += body
        out.append("")


def product_markdown(doc, project=""):
    """Product Truth as the pipeline reads it."""
    name = value_of(doc.get("identity", {}).get("name")) or project or "Product"
    a, t = completeness(doc, PRODUCT_SECTIONS)
    out = [f"# {name} — Product Truth", "",
           f"_{a} of {t} fields answered. This file is Product Truth only: what is "
           f"true about the product regardless of who it is sold to. Customer "
           f"insight and positioning live per segment._", ""]
    for s in PRODUCT_SECTIONS:
        _render_fields(s, doc.get(s["key"], {}), out)

    missing = missing_required(doc, PRODUCT_SECTIONS)
    if missing:
        out += ["## Not yet answered", "",
                "Treat anything depending on these as unknown — do not infer a value.",
                ""] + [f"- {m}" for m in missing] + [""]
    out += ["## Claim rule", "",
            "Only Product Truth licenses a product claim. Customer research may "
            "identify an opportunity; it never establishes that this product can "
            "satisfy it. For any benefit, name the product fact that licenses it — "
            "if none exists, do not make the claim.", ""]
    return "\n".join(out).rstrip() + "\n"


def segment_markdown(seg, product=None, slug=""):
    """One segment's Customer Truth and strategy."""
    name = value_of(seg.get("identity", {}).get("name")) or slug or "Segment"
    a, t = completeness(seg, SEGMENT_SECTIONS)
    compat = value_of(seg.get("compatibility", {}).get("status"))
    out = [f"# {name} — Segment Strategy", "",
           f"_{a} of {t} fields answered._", ""]
    if compat:
        out += [f"**Product × segment compatibility: {compat}**", ""]
        if compat == "Incompatible":
            out += ["> This pair is marked incompatible. Do not generate PICC cards "
                    "or concepts for it.", ""]
    for s in SEGMENT_SECTIONS:
        _render_fields(s, seg.get(s["key"], {}), out)
    out += ["## Scope rule", "",
            "Everything above applies to this segment only. Do not carry it to "
            "another segment unless the evidence says it genuinely applies there "
            "too.", ""]
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ status

def missing_required(doc, sections):
    out = []
    for s in sections:
        for f in s["fields"]:
            if f.get("required") and not is_answered(doc.get(s["key"], {}).get(f["key"])):
                out.append(f"{s['title']} — {f['label']}")
    return out


def completeness(doc, sections):
    total = answered = 0
    for s in sections:
        for f in s["fields"]:
            total += 1
            if is_answered(doc.get(s["key"], {}).get(f["key"])):
                answered += 1
    return answered, total


def section_completeness(doc, sections, keys=None):
    a = t = 0
    for s in sections:
        if keys and s["key"] not in keys:
            continue
        for f in s["fields"]:
            t += 1
            if is_answered(doc.get(s["key"], {}).get(f["key"])):
                a += 1
    return a, t


def readiness(project, product):
    """What each downstream module can actually run on, per segment.

    Readiness differs by segment on purpose: a barely-researched segment must
    not inherit a well-researched one's green light.
    """
    doc = load(project, product)
    segs = load_segments(project, product)
    facts_a, facts_t = section_completeness(
        doc, PRODUCT_SECTIONS, {"identity", "facts", "mechanism", "claims"})
    prod_pct = round(100 * facts_a / facts_t) if facts_t else 0

    rows = []
    for s in segs:
        d = s["doc"]
        research_a, research_t = section_completeness(
            d, SEGMENT_SECTIONS, {"evidence", "fit", "problems", "objections"})
        strat_a, strat_t = section_completeness(
            d, SEGMENT_SECTIONS, {"benefits", "strategy", "creative"})
        research = round(100 * research_a / research_t) if research_t else 0
        strat = round(100 * strat_a / strat_t) if strat_t else 0
        compat = value_of(d.get("compatibility", {}).get("status")) or ""
        blocked = compat == "Incompatible"
        rows.append({
            "slug": s["slug"],
            "name": value_of(d["identity"]["name"]),
            "status": value_of(d["identity"]["status"]),
            "compatibility": compat,
            "research": research, "strategy": strat,
            # A module is ready when the things it reads are actually there.
            "picc": 0 if blocked else min(prod_pct, research),
            "concepts": 0 if blocked else min(prod_pct, research, strat),
            "briefs": 0 if blocked else min(prod_pct, research, strat),
            "blocked": blocked,
        })
    return {"product_facts": prod_pct, "segments": rows}


def summary(project, product):
    doc = load(project, product)
    a, t = completeness(doc, PRODUCT_SECTIONS)
    segs = load_segments(project, product)
    sc = doc.get("scorecard", {})
    scores = [int(value_of(sc[k])) for k in sc
              if k != "verdict" and str(value_of(sc.get(k))).strip().isdigit()]
    return {
        "project": project, "product": product,
        "name": value_of(doc["identity"]["name"]) or "",
        "definition": value_of(doc["identity"]["definition"]) or "",
        "verdict": value_of(sc.get("verdict")) or "",
        "score": round(sum(scores) / len(scores), 1) if scores else None,
        "answered": a, "total": t,
        "missing_required": len(missing_required(doc, PRODUCT_SECTIONS)),
        "segments": len(segs),
        "active_segments": sum(
            1 for s in segs
            if value_of(s["doc"]["identity"]["status"]) in ("Validated", "Active")),
    }


def schema():
    return {"product": PRODUCT_SECTIONS, "segment": SEGMENT_SECTIONS,
            "create_sections": CREATE_SECTIONS, "states": STATES, "sources": SOURCES}


def main():
    if "--schema" in sys.argv:
        print(json.dumps(schema(), indent=2)); return
    if "--render" in sys.argv:
        i = sys.argv.index("--render")
        proj = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        prod = sys.argv[i + 2] if len(sys.argv) > i + 2 else resolve_product(proj)
        print(product_markdown(load(proj, prod), prod))
        for s in load_segments(proj, prod):
            print("\n" + "=" * 70 + "\n")
            print(segment_markdown(s["doc"], None, s["slug"]))
        return
    pf = sum(len(s["fields"]) for s in PRODUCT_SECTIONS)
    sf = sum(len(s["fields"]) for s in SEGMENT_SECTIONS)
    print(f"  Product Truth : {len(PRODUCT_SECTIONS)} sections, {pf} fields "
          f"({len(CREATE_SECTIONS)} sections on the create screen)")
    print(f"  Segment       : {len(SEGMENT_SECTIONS)} sections, {sf} fields")
    print(f"  states: {', '.join(STATES)}")


if __name__ == "__main__":
    main()
