#!/usr/bin/env python3
"""Layer 2: what a segment's researcher gets to read.

`evidence/<slug>.txt` is Layer 1 and it is the count -- immutable, one primary
segment per comment, nothing borrowed. It answers "how big is this audience?"
and it must keep answering that honestly, which is exactly why it cannot also
answer the other question a researcher has: "what do these people sound like?"
A fifteen-comment segment starves on Layer 1 while two hundred comments of
directly relevant context sit one edge away.

So retrieval is separated from ontology. `research/<slug>.txt` carries the same
primary evidence in full, plus evidence borrowed from related segments -- every
borrowed item tagged with where it came from and why it was included.

Three rules make that safe:

  1. Borrowed evidence is never counted. The header prints primary and rollup
     counts as separate integers and borrowed items appear under neither. A
     weight is never a multiplier on a count; fractional evidence does not exist.
  2. Weights are quotas, not scores. Each relationship may contribute up to its
     share of a token budget, filled in a fixed relation order, best-scoring
     evidence first. That makes "weight" mean something operational instead of
     something that sounds quantitative.
  3. Config prices relationships; the model only judges them. Stage 04 says a
     relationship is strong, moderate or weak -- a judgement it can make -- and
     the numbers below turn that into budget.

Standard library only.
"""

from __future__ import annotations

import os
import store

# How much of the borrowed-context budget each kind of relationship may claim.
# A parent is worth most: it is the same problem described by more people, and
# it is the reason a narrow child can be researched at all. Siblings share a
# parent but not a context, so they are worth less than either. Measured
# co-occurrence is worth least because nobody vouched for it -- it is a
# statistical neighbour, not a declared one.
DEFAULT_RELATION_WEIGHTS = {
    "parent": 0.60,
    "child": 0.35,
    "sibling": 0.20,
    "adjacent": 0.25,
    "co_occurs": 0.12,
}
# Stage 04's qualitative judgement, priced. Measured edges carry no strength and
# are always taken at full relation weight.
DEFAULT_STRENGTH_FACTORS = {"strong": 1.0, "moderate": 0.75, "weak": 0.5}
DEFAULT_CONTEXT_BUDGET_TOKENS = 8000

# Fixed fill order, so a budget that runs out always runs out in the same place.
RELATION_ORDER = ("parent", "child", "sibling", "adjacent", "co_occurs")


def pack_settings(cfg):
    settings = (cfg or {}).get("segmentation") or {}
    weights = dict(DEFAULT_RELATION_WEIGHTS)
    weights.update(settings.get("relation_weights") or {})
    factors = dict(DEFAULT_STRENGTH_FACTORS)
    factors.update(settings.get("strength_factors") or {})
    return {
        "relation_weights": weights,
        "strength_factors": factors,
        "context_budget_tokens": int(settings.get(
            "pack_context_budget_tokens", DEFAULT_CONTEXT_BUDGET_TOKENS)),
    }


def estimated_tokens(text):
    """The same char-based estimate the rest of the pipeline budgets with."""
    return max(len(text) // 4, 1)


def related_edges(segment_id, graph, measured_edges):
    """Every relationship this segment can borrow through, in fill order.

    Sibling relationships are derived rather than declared: two segments that
    name the same parent are siblings, and asking Stage 04 to enumerate that
    would be asking it to restate what it already said.
    """
    graph = graph or {"specialises": [], "adjacent": []}
    parents, children, found = {}, {}, []
    for edge in graph.get("specialises", []):
        parents[edge["from_segment_id"]] = (edge["to_segment_id"],
                                            edge.get("strength", "moderate"))
        children.setdefault(edge["to_segment_id"], []).append(
            (edge["from_segment_id"], edge.get("strength", "moderate")))

    if segment_id in parents:
        parent_id, strength = parents[segment_id]
        found.append(("parent", parent_id, strength))
        for sibling_id, sibling_strength in sorted(children.get(parent_id, [])):
            if sibling_id != segment_id:
                found.append(("sibling", sibling_id, sibling_strength))
    for child_id, strength in sorted(children.get(segment_id, [])):
        found.append(("child", child_id, strength))
    for edge in graph.get("adjacent", []):
        pair = edge["segment_ids"]
        if segment_id in pair:
            other = pair[0] if pair[1] == segment_id else pair[1]
            found.append(("adjacent", other, edge.get("strength", "moderate")))
    for edge in measured_edges or []:
        pair = edge["segment_ids"]
        if segment_id in pair:
            other = pair[0] if pair[1] == segment_id else pair[1]
            found.append(("co_occurs", other, "strong"))

    # Each neighbour is borrowed through its strongest single relationship, so
    # an adjacency that is also measured does not get two quotas.
    best = {}
    for relation, other, strength in found:
        rank = RELATION_ORDER.index(relation)
        if other not in best or rank < best[other][0]:
            best[other] = (rank, relation, strength)
    return sorted(((relation, other, strength)
                   for other, (_rank, relation, strength) in best.items()),
                  key=lambda item: (RELATION_ORDER.index(item[0]), item[1]))


def quota(relation, strength, settings):
    """Tokens this one relationship may contribute to the borrowed section."""
    weight = settings["relation_weights"].get(relation, 0.0)
    factor = settings["strength_factors"].get(strength, 0.75)
    return int(settings["context_budget_tokens"] * weight * factor)


def rollup_counts(segment_id, graph, counts):
    """Primary count for this segment, and for it plus all its descendants.

    Two integers, never summed into one. A parent's own evidence is what it can
    claim; the rollup is what its branch covers. Reporting only the second is
    the inflation this whole layer exists to avoid, and reporting only the first
    hides that a branch is larger than any node in it.
    """
    children = {}
    for edge in (graph or {}).get("specialises", []):
        children.setdefault(edge["to_segment_id"], []).append(
            edge["from_segment_id"])
    seen, stack, total = set(), [segment_id], 0
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        total += counts.get(node, 0)
        stack.extend(children.get(node, []))
    return counts.get(segment_id, 0), total


def _render_item(row, evidence, origin=None):
    lines = [f"[{row['evidence_id']}] TYPE: comment",
             f"TITLE: {evidence.get('title') or ''}",
             f"URL: {evidence.get('url') or ''}",
             f"EVIDENCE TIER: {evidence.get('tier', 'context')}",
             f"ASSIGNMENT SCORE: {row.get('score', 0)}"]
    if origin:
        relation, other_slug, strength = origin
        lines.append(f"BORROWED FROM: {other_slug} ({relation}, {strength})")
    if row.get("facet_ids"):
        lines.append("FACETS: " + ", ".join(row["facet_ids"]))
    lines.append(f"TEXT: {evidence['text']}")
    return "\n".join(lines) + "\n\n"


def _ordered(rows):
    return sorted(rows, key=lambda row: (-row.get("score", 0),
                                         -row.get("winning_margin", 0),
                                         row["evidence_id"]))


def build_pack(segment, segments_by_id, rows_by_segment, evidence_by_id,
               graph, measured, settings):
    """Render one segment's research pack, and report exactly what it borrowed."""
    segment_id = segment["segment_id"]
    primary = _ordered(rows_by_segment.get(segment_id, []))
    counts = {other: len(rows) for other, rows in rows_by_segment.items()}
    own, rollup = rollup_counts(segment_id, graph, counts)

    borrowed, provenance = [], []
    budget = settings["context_budget_tokens"]
    spent = 0
    for relation, other_id, strength in related_edges(segment_id, graph, measured):
        other = segments_by_id.get(other_id)
        if other is None:
            continue
        allowance = min(quota(relation, strength, settings), budget - spent)
        if allowance <= 0:
            continue
        used, taken = 0, 0
        for row in _ordered(rows_by_segment.get(other_id, [])):
            evidence = evidence_by_id.get(row["evidence_id"])
            if not evidence:
                continue
            rendered = _render_item(
                row, evidence, (relation, other["slug"], strength))
            cost = estimated_tokens(rendered)
            if used + cost > allowance:
                break
            borrowed.append(rendered)
            used += cost
            taken += 1
        spent += used
        provenance.append({
            "segment_id": other_id, "slug": other["slug"], "relation": relation,
            "strength": strength, "quota_tokens": quota(relation, strength, settings),
            "items_borrowed": taken, "tokens_used": used,
        })

    header = [
        segment["name"].upper(), "=" * 72, "",
        f"Segment ID: {segment_id}",
        f"Segment slug: {segment['slug']}",
        "",
        "COUNTS",
        "-" * 72,
        f"Primary evidence items (this segment only): {own}",
        f"Rollup with child segments:                 {rollup}",
        f"Borrowed context items:                     {len(borrowed)}",
        "",
        "Primary is the count. Borrowed context is language, not size: it is",
        "drawn from related segments to show how these people talk, and it is",
        "counted under neither number above.",
        "",
    ]
    if provenance:
        header += ["BORROWED FROM", "-" * 72]
        for row in provenance:
            header.append(
                f"  - {row['slug']} ({row['relation']}, {row['strength']}): "
                f"{row['items_borrowed']} item(s), {row['tokens_used']} of "
                f"{row['quota_tokens']} token quota")
        header.append("")
    header += [f"SEGMENT DEFINITION\n{'-' * 72}\n{segment['definition']}", ""]

    body = ["PRIMARY EVIDENCE", "-" * 72, ""]
    for row in primary:
        evidence = evidence_by_id.get(row["evidence_id"])
        if evidence:
            body.append(_render_item(row, evidence))
    if borrowed:
        body += ["BORROWED CONTEXT", "-" * 72,
                 "Not this segment's evidence. Do not count these items.", ""]
        body.extend(borrowed)
    text = "\n".join(header) + "\n" + "\n".join(body)
    return text.rstrip() + "\n", {
        "segment_id": segment_id, "slug": segment["slug"],
        "primary_evidence_count": own, "rollup_evidence_count": rollup,
        "borrowed_item_count": len(borrowed), "borrowed_tokens": spent,
        "context_budget_tokens": budget, "borrowed_from": provenance,
    }


def write_packs(directory, segments, rows_by_segment, evidence_by_id, graph,
                measured, settings):
    """Write one pack per segment and return the manifest, sorted by segment ID."""
    segments_by_id = {row["segment_id"]: row for row in segments}
    manifest = []
    for segment in sorted(segments, key=lambda row: row["segment_id"]):
        text, report = build_pack(
            segment, segments_by_id, rows_by_segment, evidence_by_id, graph,
            measured, settings)
        path = os.path.join(directory, f"{segment['slug']}.txt")
        store.write_text(path, text)
        report["path"] = path
        manifest.append(report)
    return manifest
