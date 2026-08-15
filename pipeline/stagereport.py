#!/usr/bin/env python3
"""Human-readable stage reports — the pipeline showing its work.

Every judgement in these reports was already being made; every number was
already being computed. What was missing was somewhere a person could read them.
The JSON artifacts are exact and unreadable; the console output scrolls past.

Run the same stages as a chat and you get markdown back: which candidates were
found and which pile each went in, which died at the recurrence gate and why,
what the floors removed, and an arithmetic reconciliation proving nothing was
lost between stages. That readability is not decoration — it is how an operator
notices that a gate reported zero, which is the failure mode this whole pipeline
keeps rediscovering.

So these render from the artifacts rather than from a second pass over the
model's output: a report can restate a number but never invent one.

Standard library only.
"""

from __future__ import annotations

import os
from collections import Counter


def _table(headers, rows, aligns=None):
    aligns = aligns or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(aligns) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return out


def _quote(text, limit=160):
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def discovery_report(project, partition, catalogue, candidates, evidence_by_id,
                     minimum_evidence=2):
    """Stage 03 — what was found, which pile it went in, what died at the gate."""
    lines = [f"# Stage 03 — Find the audiences ({project})", "", "## Run summary", ""]
    lines += [
        f"- Segment-register candidates: **{partition.get('segment_candidates', 0):,}**",
        f"- Attribute / journey candidates: "
        f"**{partition.get('facet_candidates', 0):,}** "
        f"({len(partition.get('provisional_facets') or []):,} after grouping)",
        f"- Symptom candidates (handed to the pain-point skills): "
        f"**{partition.get('symptom_candidates', 0):,}**",
        f"- Consolidated audiences carried into Stage 04: **{len(candidates):,}**",
        "",
        "## Recurrence gate", "",
        f"A candidate needs at least **{minimum_evidence}** separate comments to "
        "exist. One comment is a person, not an audience.", "",
        f"- Provisional discoveries: **{len(catalogue):,}**",
        f"- Retained after grouping and consolidation: **{len(candidates):,}**",
        "",
    ]

    symptoms = partition.get("symptoms") or []
    if symptoms:
        lines += ["### Symptoms — left the taxonomy, not deleted", ""]
        lines += _table(
            ["Candidate", "Comments"],
            [(row.get("candidate_key", "?"), len(row.get("evidence_ids") or []))
             for row in sorted(symptoms,
                               key=lambda r: -len(r.get("evidence_ids") or []))[:20]],
            ["---", "---:"])
        lines.append("")

    facets = partition.get("provisional_facets") or []
    if facets:
        lines += ["### Attributes and journey states — became tags", ""]
        lines += _table(
            ["Facet", "Type", "Comments"],
            [(row["facet_key"], row["facet_type"], row["unique_evidence_count"])
             for row in sorted(facets, key=lambda r: -r["unique_evidence_count"])[:20]],
            ["---", "---", "---:"])
        lines.append("")

    lines += ["## Audiences discovered", ""]
    for row in sorted(candidates, key=lambda r: -r.get("unique_evidence_count", 0)):
        lines += [
            f"### {row['name']}", "",
            f"- **Who:** {row.get('definition', '').strip()}",
            f"- **Why talk to them differently:** "
            f"{row.get('commercial_distinction', '').strip()}",
            f"- **Support:** {row.get('unique_evidence_count', 0):,} comments · "
            f"{row.get('unique_thread_count', 0):,} conversations · "
            f"{row.get('unique_subreddit_count', 0):,} communities",
            f"- **Discovery confidence:** {row.get('discovery_status', 'unknown')}",
        ]
        quotes = [evidence_by_id[eid] for eid in
                  (row.get("representative_evidence_ids") or [])[:2]
                  if eid in evidence_by_id]
        if quotes:
            lines.append("- **Their words:**")
            for eid, item in zip(row["representative_evidence_ids"], quotes):
                lines.append(f'  - `{eid}` — "{_quote(item.get("text"))}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def validation_report(project, decisions, validated, facets, graph, floors):
    """Stage 04 — every decision, every rationale, and what the floors removed."""
    tally = Counter(row.get("status") for row in decisions)
    lines = [f"# Stage 04 — Validate the audiences ({project})", "",
             "## Decisions", ""]
    lines += _table(["Outcome", "Candidates"],
                    [(k, v) for k, v in tally.most_common()], ["---", "---:"])
    lines += ["", f"**Validated and carried forward: {len(validated):,}**", "",
              "## Floors", "",
              f"- Root audience: at least **{floors[0]}** conversations and "
              f"**{floors[1]}** comments.",
              f"- Child audience: at least **{floors[2]}** conversations and "
              f"**{floors[3]}** comments, plus recurring language its parent "
              "does not already use.", "",
              "A candidate drawn from very few conversations cannot be validated "
              "however good the other signals look — sixty comments from four "
              "threads is four people and their repliers.", ""]

    demoted = [row for row in decisions
               if "demoted by evidence floor" in (row.get("rationale") or "")]
    if demoted:
        lines += ["### Removed by a floor", ""]
        for row in demoted:
            lines.append(f"- `{row['segment_id']}` — {row['rationale']}")
        lines.append("")

    reclassified = [row for row in decisions
                    if row.get("status") == "reclassified_as_facet"]
    if reclassified:
        lines += ["### Reclassified as facets — a response to the problem, "
                  "not an audience", "",
                  "Moved before any evidence was assigned, which is the only "
                  "point at which it costs nothing.", ""]
        lines += _table(
            ["Candidate", "Became", "Type", "Why"],
            [(row["segment_id"], row.get("facet_key") or "—",
              row.get("facet_type") or "—", row.get("rationale", "")[:90])
             for row in reclassified])
        lines.append("")

    if facets:
        lines += ["## Facet vocabulary", "",
                  "Tags a comment can carry inside whatever audience owns it. "
                  "They are never counted as audiences.", ""]
        lines += _table(
            ["Facet", "Type", "Discovery comments"],
            [(row["name"], row["facet_type"], row["discovery_evidence_count"])
             for row in facets], ["---", "---", "---:"])
        lines.append("")

    graph = graph or {}
    if graph.get("specialises") or graph.get("adjacent"):
        by_id = {row["segment_id"]: row["slug"] for row in validated}
        lines += ["## How the audiences relate", ""]
        for edge in graph.get("specialises", []):
            child = by_id.get(edge["from_segment_id"], edge["from_segment_id"])
            parent = by_id.get(edge["to_segment_id"], edge["to_segment_id"])
            lines.append(f"- `{child}` sits **inside** `{parent}` "
                         f"({edge.get('strength', 'moderate')})")
        for edge in graph.get("adjacent", []):
            left, right = edge["segment_ids"]
            lines.append(f"- `{by_id.get(left, left)}` **beside** "
                         f"`{by_id.get(right, right)}` "
                         f"({edge.get('strength', 'moderate')})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def assignment_report(project, rows, validated, min_score, min_margin):
    """Stage 05 — the score distribution and what the bar actually removed."""
    assigned = [row for row in rows if row.get("assignment_status") == "assigned"]
    scores = Counter(row.get("score", 0) for row in rows)
    below = sum(count for score, count in scores.items() if score < min_score)
    statuses = Counter(row.get("assignment_status") for row in rows)
    by_id = {row["segment_id"]: row for row in validated}

    lines = [f"# Stage 05 — Give every comment one home ({project})", "",
             "## The assignment bar", "",
             f"A comment is assigned only when the winning audience scores at "
             f"least **{min_score}** and beats second place by **{min_margin}**.",
             "",
             f"- Comments judged: **{len(rows):,}**",
             f"- Assigned: **{len(assigned):,}**",
             f"- Unassigned: **{len(rows) - len(assigned):,}**",
             f"- Scored below the bar: **{below:,}**", ""]
    if not below:
        lines += ["> Nothing scored below the bar. Either the corpus is unusually "
                  "clean or the bar did not run — worth checking, because this is "
                  "the gate that historically gets skipped.", ""]

    lines += ["### Score distribution", ""]
    lines += _table(["Score", "Comments", "Above the bar"],
                    [(score, count, "yes" if score >= min_score else "no")
                     for score, count in sorted(scores.items(), reverse=True)],
                    ["---:", "---:", "---"])
    lines += ["", "### Outcomes", ""]
    lines += _table(["Status", "Comments"],
                    [(k, f"{v:,}") for k, v in statuses.most_common()],
                    ["---", "---:"])

    per_segment = Counter(row.get("primary_segment_id") for row in assigned)
    lines += ["", "## Where they went", ""]
    lines += _table(
        ["Audience", "Comments"],
        [(by_id[sid]["name"] if sid in by_id else sid, f"{count:,}")
         for sid, count in per_segment.most_common()], ["---", "---:"])
    return "\n".join(lines).rstrip() + "\n"


def build_report(project, report, unassigned_count, missing_count, total_evidence):
    """Stage 06 — the integrity gates, with the arithmetic shown.

    `report` is Stage 06's own (segment_id, slug, count, threads, tiers) tuples,
    so the reconciliation below is checked against what was actually written
    rather than against what the stage intended to write.
    """
    assigned = sum(row[2] for row in report)
    lines = [f"# Stage 06 — Build one file per audience ({project})", "",
             "## Integrity gates", "",
             f"- Deduplicated evidence items: **{total_evidence:,}**",
             f"- Written into audience files: **{assigned:,}**",
             f"- Unassigned: **{unassigned_count:,}**",
             f"- Missing from the evidence join: **{missing_count:,}**",
             f"- Reconciliation: **{assigned:,} + {unassigned_count:,} = "
             f"{assigned + unassigned_count:,}** of {total_evidence:,}", ""]
    balanced = assigned + unassigned_count + missing_count == total_evidence
    lines += [f"**No-double-membership gate: PASS.** The build fails closed if a "
              f"comment carries two active assignments, so reaching this report "
              f"means none did.",
              "",
              ("**Reconciliation: PASS.**" if balanced else
               "**Reconciliation: MISMATCH — the numbers above do not add up.**"),
              "", "## Audience files", ""]
    lines += _table(
        ["Audience", "Comments", "Conversations", "File"],
        [(slug, f"{count:,}", f"{threads:,}", f"`evidence/{slug}.txt`")
         for _sid, slug, count, threads, _tiers in
         sorted(report, key=lambda r: -r[2])], ["---", "---:", "---:", "---"])
    return "\n".join(lines).rstrip() + "\n"


def write(directory, name, text):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)
    return path
