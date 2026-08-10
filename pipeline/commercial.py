"""Deterministic contracts for post-segmentation commercial synthesis.

Stages 03--06 establish research clusters and primary evidence provenance.  This
module deliberately starts after those stages: models may make semantic grouping
and synthesis decisions, while code owns identity, exact coverage, evidence
unions, counts, validation, and the operator-facing research pack.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter, defaultdict


COMMERCIAL_CONTRACT_VERSION = "commercial-synthesis-v1"
MAPPING_DISPOSITIONS = (
    "parent",
    "subsegment",
    "research_watchlist",
    "excluded_from_commercial_taxonomy",
)
# `attribute` and `journey_state` used to be dispositions here, and attaching
# one handed its evidence to whichever canonical parent absorbed it: `medication
# takers` at 120 comments silently inflated that audience's headline. The code
# had no better option -- Stage 05 had already given the cluster evidence, and
# folding it into a parent was the only lossless move left.
#
# Registers removed the cause upstream. Attributes and journey states are facets
# now: they tag comments inside a segment and never become research segments, so
# there is nothing left for this stage to demote. Rather than reject the
# disposition at runtime, it is simply no longer expressible.
ATTACHED_DISPOSITIONS = frozenset({"parent", "subsegment"})
# Of those, the ones a canonical audience may claim as its *own* evidence. A
# subsegment's evidence belongs to the branch, not to the node.
OWN_DISPOSITIONS = frozenset({"parent"})
SIGNAL_DIMENSIONS = (
    "pain_points",
    "desired_outcomes",
    "failed_solutions",
    "triggers",
    "beliefs",
    "emotional_states",
    "objections",
    "buying_triggers",
    "representative_language",
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_SEGMENT_NUMBER_RE = re.compile(r"(\d+)$")


class CommercialContractError(ValueError):
    """A model response broke an exact identity/coverage contract."""


def _unique(values):
    return list(dict.fromkeys(values))


def _string_array(min_items=0):
    result = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "uniqueItems": True,
    }
    if min_items:
        result["minItems"] = min_items
    return result


def stage07_schema(segment_ids):
    """Schema whose lineage fields accept only current immutable Stage-04 IDs."""
    segment_ids = list(segment_ids)
    canonical = {
        "type": "object",
        "properties": {
            "canonical_key": {
                "type": "string", "minLength": 3,
                "pattern": "^[a-z0-9]+(?:_[a-z0-9]+)*$",
            },
            "slug": {
                "type": "string", "minLength": 3,
                "pattern": "^[a-z0-9]+(?:_[a-z0-9]+)*$",
            },
            "name": {"type": "string", "minLength": 3},
            "definition": {"type": "string", "minLength": 10},
            "commercial_distinction": {"type": "string", "minLength": 10},
            "source_segment_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "enum": segment_ids},
            },
            "subsegments": _string_array(),
            "attributes": _string_array(),
            "inclusion_criteria": _string_array(1),
            "exclusion_criteria": _string_array(1),
        },
        "required": [
            "canonical_key", "slug", "name", "definition",
            "commercial_distinction", "source_segment_ids", "subsegments",
            "attributes", "inclusion_criteria", "exclusion_criteria",
        ],
        "additionalProperties": False,
    }
    mapping = {
        "type": "object",
        "properties": {
            "segment_id": {"type": "string", "enum": segment_ids},
            "disposition": {"type": "string", "enum": list(MAPPING_DISPOSITIONS)},
            "canonical_key": {"type": "string"},
            "label": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 3},
        },
        "required": ["segment_id", "disposition", "canonical_key", "label",
                     "rationale"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "canonical_segments": {
                "type": "array", "minItems": 1, "items": canonical,
            },
            "research_segment_mappings": {
                "type": "array", "minItems": len(segment_ids), "items": mapping,
            },
        },
        "required": ["canonical_segments", "research_segment_mappings"],
        "additionalProperties": False,
    }


def stage08a_schema(evidence_ids):
    """Chunk-local signal extraction schema with exact evidence provenance."""
    evidence_ids = list(evidence_ids)
    quote = {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer", "enum": evidence_ids},
            "quote": {"type": "string", "minLength": 1},
        },
        "required": ["evidence_id", "quote"],
        "additionalProperties": False,
    }
    signal = {
        "type": "object",
        "properties": {
            "dimension": {"type": "string", "enum": list(SIGNAL_DIMENSIONS)},
            "label": {"type": "string", "minLength": 2},
            "evidence_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "integer", "enum": evidence_ids},
            },
            "representative_quotes": {
                "type": "array", "uniqueItems": True, "items": quote,
            },
            "typical_outcome": {"type": "string"},
            "common_complaint": {"type": "string"},
        },
        "required": ["dimension", "label", "evidence_ids",
                     "representative_quotes", "typical_outcome",
                     "common_complaint"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"signals": {"type": "array", "items": signal}},
        "required": ["signals"],
        "additionalProperties": False,
    }


def stage08b_schema(signal_ids, evidence_ids):
    """Segment-local coalescing schema constrained to Stage-08A signal IDs."""
    signal_ids = list(signal_ids)
    evidence_ids = list(evidence_ids)
    theme = {
        "type": "object",
        "properties": {
            "dimension": {"type": "string", "enum": list(SIGNAL_DIMENSIONS)},
            "label": {"type": "string", "minLength": 2},
            "source_signal_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "enum": signal_ids},
            },
            "representative_evidence_ids": {
                "type": "array", "uniqueItems": True,
                "items": {"type": "integer", "enum": evidence_ids},
            },
            "summary": {"type": "string"},
            "typical_outcome": {"type": "string"},
            "common_complaint": {"type": "string"},
        },
        "required": ["dimension", "label", "source_signal_ids",
                     "representative_evidence_ids", "summary",
                     "typical_outcome", "common_complaint"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "themes": {"type": "array", "items": theme},
        },
        "required": ["themes"],
        "additionalProperties": False,
    }


def assigned_rows(assignments):
    return [row for row in assignments if row.get("assignment_status") == "assigned"]


def evidence_by_research_segment(assignments):
    grouped = defaultdict(list)
    for row in assigned_rows(assignments):
        grouped[row["primary_segment_id"]].append(row["evidence_id"])
    return {key: sorted(set(values)) for key, values in grouped.items()}


def build_stage07_cards(validated, decisions, assignments, evidence_by_id,
                        representative_limit=5):
    """Build bounded evidence-backed cards; no full-corpus global prompt dump."""
    grouped = defaultdict(list)
    attributes = defaultdict(Counter)
    for row in assigned_rows(assignments):
        grouped[row["primary_segment_id"]].append(row)
        for attribute in row.get("secondary_attributes") or []:
            if isinstance(attribute, str):
                attributes[row["primary_segment_id"]][attribute] += 1
            elif isinstance(attribute, dict):
                label = attribute.get("label") or attribute.get("name")
                if label:
                    attributes[row["primary_segment_id"]][label] += 1
    decision_by_id = {row["segment_id"]: row for row in decisions}
    cards = []
    for segment in sorted(validated, key=lambda row: row["segment_id"]):
        segment_id = segment["segment_id"]
        rows = sorted(grouped.get(segment_id, []), key=lambda row: (
            -row.get("score", 0), -row.get("winning_margin", 0),
            row["evidence_id"]))
        tier_counter = Counter(
            evidence_by_id[row["evidence_id"]].get("tier", "context")
            for row in rows if row["evidence_id"] in evidence_by_id)
        representatives = []
        for assignment in rows[:representative_limit]:
            evidence = evidence_by_id[assignment["evidence_id"]]
            representatives.append({
                "evidence_id": evidence["id"],
                "tier": evidence.get("tier", "context"),
                "text": evidence["text"],
            })
        decision = decision_by_id.get(segment_id, {})
        cards.append({
            "segment_id": segment_id,
            "slug": segment["slug"],
            "name": segment["name"],
            "definition": segment["definition"],
            "commercial_distinction": segment.get("commercial_distinction", ""),
            "inclusion_criteria": segment.get("inclusion_criteria", []),
            "exclusion_criteria": segment.get("exclusion_criteria", []),
            "assigned_evidence_count": len(rows),
            "tier_counts": {
                tier: tier_counter[tier]
                for tier in ("core", "supporting", "context")
            },
            "representative_assigned_voc": representatives,
            "secondary_attributes": [
                {"label": label, "evidence_count": count}
                for label, count in attributes[segment_id].most_common(12)
            ],
            "stage04_lineage": {
                "status": decision.get("status", "validated"),
                "merged_into": decision.get("merged_into") or [],
                "split_into": decision.get("split_into") or [],
                "rationale": decision.get("rationale", ""),
            },
        })
    return cards


def _stable_canonical_id(source_ids):
    """Derive identity only from immutable source IDs, never semantic labels."""
    if not source_ids:
        raise CommercialContractError("canonical segment has no source segment IDs")
    candidates = []
    for segment_id in source_ids:
        match = _SEGMENT_NUMBER_RE.search(segment_id)
        if not match:
            raise CommercialContractError(
                f"research segment ID {segment_id!r} has no stable numeric suffix")
        candidates.append((int(match.group(1)), segment_id))
    number, _ = min(candidates)
    return f"cseg_{number:03d}"


def finalize_stage07(payload, validated, assignments, evidence_by_id):
    """Validate complete mapping, assign canonical IDs, and prove no evidence loss."""
    expected_ids = [row["segment_id"] for row in validated]
    expected_set = set(expected_ids)
    canonical_rows = payload.get("canonical_segments")
    mappings = payload.get("research_segment_mappings")
    if not isinstance(canonical_rows, list) or not isinstance(mappings, list):
        raise CommercialContractError(
            "Stage 07 requires canonical_segments and research_segment_mappings arrays")

    mapped_ids = [row.get("segment_id") for row in mappings
                  if isinstance(row, dict)]
    unknown = sorted(set(mapped_ids) - expected_set)
    missing = sorted(expected_set - set(mapped_ids))
    duplicates = sorted(key for key, count in Counter(mapped_ids).items()
                        if count > 1)
    if unknown or missing or duplicates or len(mapped_ids) != len(expected_ids):
        raise CommercialContractError(
            "Stage 07 research mapping coverage failed: "
            f"unknown={unknown}, missing={missing}, duplicates={duplicates}")

    canonical_by_key = {}
    for row in canonical_rows:
        key = row.get("canonical_key") if isinstance(row, dict) else None
        if not key or not _SLUG_RE.fullmatch(key):
            raise CommercialContractError(f"invalid canonical_key {key!r}")
        if key in canonical_by_key:
            raise CommercialContractError(f"duplicate canonical_key {key!r}")
        source_ids = row.get("source_segment_ids") or []
        bad = sorted(set(source_ids) - expected_set)
        if bad:
            raise CommercialContractError(
                f"canonical {key!r} references unknown segment IDs {bad}")
        if len(source_ids) != len(set(source_ids)):
            raise CommercialContractError(
                f"canonical {key!r} repeats source segment IDs")
        canonical_by_key[key] = row

    attached_by_key = defaultdict(list)
    own_by_key = defaultdict(list)
    parent_by_key = Counter()
    for mapping in mappings:
        disposition = mapping.get("disposition")
        key = mapping.get("canonical_key") or ""
        if disposition not in MAPPING_DISPOSITIONS:
            raise CommercialContractError(
                f"research segment {mapping.get('segment_id')!r} has invalid "
                f"disposition {disposition!r}")
        if disposition in OWN_DISPOSITIONS and key in canonical_by_key:
            own_by_key[key].append(mapping["segment_id"])
        if disposition in ATTACHED_DISPOSITIONS:
            if key not in canonical_by_key:
                raise CommercialContractError(
                    f"research segment {mapping['segment_id']!r} references "
                    f"unknown canonical_key {key!r}")
            attached_by_key[key].append(mapping["segment_id"])
            if disposition == "parent":
                parent_by_key[key] += 1
        elif key:
            raise CommercialContractError(
                f"{disposition} research segment {mapping['segment_id']!r} must "
                "not reference a canonical segment")

    for key, canonical in canonical_by_key.items():
        declared = set(canonical["source_segment_ids"])
        attached = set(attached_by_key[key])
        if declared != attached:
            raise CommercialContractError(
                f"canonical {key!r} lineage mismatch: declared-only="
                f"{sorted(declared-attached)}, mapping-only={sorted(attached-declared)}")
        if not parent_by_key[key]:
            raise CommercialContractError(
                f"canonical {key!r} has no research segment marked parent")

    research_evidence = evidence_by_research_segment(assignments)
    mapping_by_id = {row["segment_id"]: row for row in mappings}
    key_to_id = {
        key: _stable_canonical_id(row["source_segment_ids"])
        for key, row in canonical_by_key.items()
    }
    if len(set(key_to_id.values())) != len(key_to_id):
        raise CommercialContractError(
            "canonical identity collision; source segment lineages overlap anchors")

    final_canonical = []
    all_canonical_evidence = set()
    for key, row in sorted(canonical_by_key.items(),
                           key=lambda pair: key_to_id[pair[0]]):
        source_ids = sorted(row["source_segment_ids"])
        evidence_ids = sorted({evidence_id for source_id in source_ids
                               for evidence_id in research_evidence.get(source_id, [])})
        if not evidence_ids:
            raise CommercialContractError(
                f"canonical {key!r} has no assigned evidence; preserve its research "
                "clusters as watchlist/excluded metadata instead")
        all_canonical_evidence.update(evidence_ids)
        # Two integers, never summed. `own` is what this audience can claim on
        # its own account; `rollup` is what its branch covers once subsegments
        # are included. Reporting only the rollup is how a parent ends up
        # claiming its children's people; reporting only its own hides that the
        # branch is bigger than the node.
        own_ids = sorted({evidence_id for source_id in sorted(own_by_key[key])
                          for evidence_id in research_evidence.get(source_id, [])})
        tiers = Counter(evidence_by_id[evidence_id].get("tier", "context")
                        for evidence_id in evidence_ids)
        final_canonical.append({
            "canonical_segment_id": key_to_id[key],
            "slug": row["slug"],
            "name": row["name"],
            "definition": row["definition"],
            "commercial_distinction": row["commercial_distinction"],
            "source_segment_ids": source_ids,
            "subsegments": row["subsegments"],
            "attributes": row["attributes"],
            "inclusion_criteria": row["inclusion_criteria"],
            "exclusion_criteria": row["exclusion_criteria"],
            "own_evidence_ids": own_ids,
            "own_evidence_count": len(own_ids),
            "rollup_evidence_count": len(evidence_ids),
            # Retained under its original name because Stage 08 synthesises over
            # the whole branch: a theme may legitimately cite a subsegment's
            # comment. It is the rollup, and the renderer now says so.
            "primary_evidence_ids": evidence_ids,
            "primary_evidence_count": len(evidence_ids),
            "tier_counts": {tier: tiers[tier]
                            for tier in ("core", "supporting", "context")},
        })

    final_mappings = []
    preserved_noncanonical = set()
    disposition_counts = Counter()
    for segment_id in sorted(expected_ids):
        mapping = mapping_by_id[segment_id]
        evidence_ids = research_evidence.get(segment_id, [])
        disposition = mapping["disposition"]
        disposition_counts[disposition] += 1
        if disposition not in ATTACHED_DISPOSITIONS:
            preserved_noncanonical.update(evidence_ids)
        final_mappings.append({
            "segment_id": segment_id,
            "disposition": disposition,
            "canonical_segment_id": (
                key_to_id[mapping["canonical_key"]]
                if disposition in ATTACHED_DISPOSITIONS else None),
            "label": mapping["label"],
            "rationale": mapping["rationale"],
            "assigned_evidence_count": len(evidence_ids),
            "evidence_ids": evidence_ids,
        })

    assigned_evidence = {row["evidence_id"] for row in assigned_rows(assignments)}
    preserved = all_canonical_evidence | preserved_noncanonical
    if preserved != assigned_evidence:
        raise CommercialContractError(
            "Stage 07 evidence preservation failed: lost="
            f"{sorted(assigned_evidence-preserved)}, invented="
            f"{sorted(preserved-assigned_evidence)}")

    catalogue = {
        "canonical_segments": final_canonical,
        "canonical_segment_count": len(final_canonical),
        "assigned_evidence_count": len(assigned_evidence),
    }
    mapping_artifact = {
        "research_segment_count": len(expected_ids),
        "assigned_evidence_count": len(assigned_evidence),
        "preserved_evidence_count": len(preserved),
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "mappings": final_mappings,
    }
    return catalogue, mapping_artifact


def validate_stage07_artifacts(catalogue, mapping, validated, assignments):
    """Authenticate persisted finalized artifacts before downstream reuse."""
    expected_segments = {row["segment_id"] for row in validated}
    mappings = mapping.get("mappings") if isinstance(mapping, dict) else None
    canonical_rows = (catalogue.get("canonical_segments")
                      if isinstance(catalogue, dict) else None)
    if not isinstance(mappings, list) or not isinstance(canonical_rows, list):
        raise CommercialContractError("persisted Stage 07 artifacts have wrong shape")
    got = [row.get("segment_id") for row in mappings if isinstance(row, dict)]
    if Counter(got) != Counter(expected_segments):
        raise CommercialContractError(
            "persisted Stage 07 mapping does not cover every research segment once")
    known_canonical = {row["canonical_segment_id"] for row in canonical_rows}
    research_evidence = evidence_by_research_segment(assignments)
    preserved = set()
    attached = defaultdict(set)
    for row in mappings:
        expected_evidence = research_evidence.get(row["segment_id"], [])
        if row.get("evidence_ids") != expected_evidence:
            raise CommercialContractError(
                f"persisted mapping evidence changed for {row['segment_id']}")
        if row.get("assigned_evidence_count") != len(expected_evidence):
            raise CommercialContractError(
                f"persisted mapping count is false for {row['segment_id']}")
        preserved.update(expected_evidence)
        canonical_id = row.get("canonical_segment_id")
        if row.get("disposition") in ATTACHED_DISPOSITIONS:
            if canonical_id not in known_canonical:
                raise CommercialContractError(
                    f"persisted mapping cites unknown canonical ID {canonical_id!r}")
            attached[canonical_id].update(expected_evidence)
        elif canonical_id is not None:
            raise CommercialContractError(
                f"persisted noncanonical mapping {row['segment_id']} has a parent")
    for row in canonical_rows:
        evidence_ids = row.get("primary_evidence_ids") or []
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CommercialContractError(
                f"persisted canonical {row.get('canonical_segment_id')} repeats evidence")
        if set(evidence_ids) != attached[row["canonical_segment_id"]]:
            raise CommercialContractError(
                f"persisted canonical {row['canonical_segment_id']} has a false evidence union")
        if row.get("primary_evidence_count") != len(evidence_ids):
            raise CommercialContractError(
                f"persisted canonical {row['canonical_segment_id']} has a false count")
    assigned = {row["evidence_id"] for row in assigned_rows(assignments)}
    if preserved != assigned:
        raise CommercialContractError(
            "persisted Stage 07 artifacts do not preserve all assigned evidence")


def validate_stage08a(payload, allowed_evidence):
    """Validate chunk provenance and require verbatim quote containment."""
    allowed = set(allowed_evidence)
    signals = payload.get("signals") if isinstance(payload, dict) else None
    if not isinstance(signals, list):
        return
    for position, signal in enumerate(signals):
        if not isinstance(signal, dict):
            continue
        evidence_ids = signal.get("evidence_ids") or []
        unknown = sorted(set(evidence_ids) - allowed)
        if unknown:
            raise CommercialContractError(
                f"Stage 08A signal {position} cites out-of-chunk evidence {unknown}")
        for quote in signal.get("representative_quotes") or []:
            if not isinstance(quote, dict):
                continue
            evidence_id = quote.get("evidence_id")
            text = allowed_evidence.get(evidence_id, {}).get("text", "")
            if (evidence_id not in evidence_ids or evidence_id not in allowed
                    or quote.get("quote", "") not in text):
                raise CommercialContractError(
                    f"Stage 08A signal {position} has an uncited/non-verbatim "
                    f"quote for evidence {evidence_id!r}")


def catalogue_stage08a(results):
    """Assign stable chunk-derived signal IDs without semantic string matching."""
    catalogue = []
    for result in sorted(results, key=lambda row: row["chunk_id"]):
        for index, signal in enumerate(result["signals"], 1):
            catalogue.append({
                "signal_id": f"{result['chunk_id']}_sig_{index:04d}",
                **copy.deepcopy(signal),
            })
    return catalogue


def finalize_stage08(canonical, signal_catalogue, payload, evidence_by_id):
    """Require complete signal lineage and derive every theme count in code."""
    themes = payload.get("themes") if isinstance(payload, dict) else None
    if not isinstance(themes, list):
        raise CommercialContractError("Stage 08B requires a themes array")
    by_signal = {row["signal_id"]: row for row in signal_catalogue}
    cited = []
    for index, theme in enumerate(themes):
        if not isinstance(theme, dict):
            raise CommercialContractError(f"Stage 08B theme {index} is not an object")
        source_ids = theme.get("source_signal_ids") or []
        invalid = sorted(set(source_ids) - set(by_signal))
        if invalid:
            raise CommercialContractError(
                f"Stage 08B theme {index} cites unknown signal IDs {invalid}")
        cited.extend(source_ids)
    missing = sorted(set(by_signal) - set(cited))
    duplicates = sorted(key for key, count in Counter(cited).items() if count > 1)
    if missing or duplicates:
        raise CommercialContractError(
            f"Stage 08B signal coverage failed: missing={missing}, "
            f"duplicates={duplicates}")

    grouped = {dimension: [] for dimension in SIGNAL_DIMENSIONS}
    for theme in themes:
        source_rows = [by_signal[key] for key in theme["source_signal_ids"]]
        dimensions = {row["dimension"] for row in source_rows}
        if dimensions != {theme["dimension"]}:
            raise CommercialContractError(
                f"Stage 08B theme {theme['label']!r} merges different dimensions "
                f"{sorted(dimensions)}")
        evidence_ids = sorted({evidence_id for row in source_rows
                               for evidence_id in row["evidence_ids"]})
        allowed_representatives = set(evidence_ids)
        chosen = theme.get("representative_evidence_ids") or []
        if set(chosen) - allowed_representatives:
            raise CommercialContractError(
                f"Stage 08B theme {theme['label']!r} selects evidence outside its "
                "source signals")
        quote_candidates = []
        for row in source_rows:
            quote_candidates.extend(row.get("representative_quotes") or [])
        by_quote_evidence = defaultdict(list)
        for quote in quote_candidates:
            by_quote_evidence[quote["evidence_id"]].append(quote["quote"])
        quote_ids = chosen or [quote["evidence_id"] for quote in quote_candidates[:5]]
        representative_quotes = []
        for evidence_id in _unique(quote_ids)[:5]:
            if by_quote_evidence[evidence_id]:
                representative_quotes.append({
                    "evidence_id": evidence_id,
                    "quote": by_quote_evidence[evidence_id][0],
                })
        grouped[theme["dimension"]].append({
            "label": theme["label"],
            "evidence_count": len(evidence_ids),
            "evidence_ids": evidence_ids,
            "representative_quotes": representative_quotes,
            "summary": theme.get("summary", ""),
            "typical_outcome": theme.get("typical_outcome", ""),
            "common_complaint": theme.get("common_complaint", ""),
        })
    for values in grouped.values():
        values.sort(key=lambda row: (-row["evidence_count"], row["label"].lower()))
    return {
        "canonical_segment_id": canonical["canonical_segment_id"],
        "slug": canonical["slug"],
        "name": canonical["name"],
        "primary_evidence_count": canonical["primary_evidence_count"],
        **grouped,
    }


def validate_synthesis(synthesis, canonical):
    """Defence-in-depth check for persisted Stage-08 artifacts."""
    if synthesis.get("canonical_segment_id") != canonical["canonical_segment_id"]:
        raise CommercialContractError("Stage 08 synthesis has the wrong canonical ID")
    allowed = set(canonical["primary_evidence_ids"])
    for dimension in SIGNAL_DIMENSIONS:
        for theme in synthesis.get(dimension, []):
            evidence_ids = theme.get("evidence_ids") or []
            if len(evidence_ids) != len(set(evidence_ids)):
                raise CommercialContractError(
                    f"{dimension} theme {theme.get('label')!r} repeats evidence IDs")
            if theme.get("evidence_count") != len(set(evidence_ids)):
                raise CommercialContractError(
                    f"{dimension} theme {theme.get('label')!r} has a false count")
            if set(evidence_ids) - allowed:
                raise CommercialContractError(
                    f"{dimension} theme {theme.get('label')!r} cites evidence "
                    "outside the canonical segment")


def _section(lines, title, values, formatter=None):
    lines.extend([title, "-" * 72])
    if not values:
        lines.append("- No recurring pattern met the evidence threshold.")
    else:
        for value in values:
            lines.append(formatter(value) if formatter else f"- {value}")
    lines.append("")


def _theme_line(theme):
    suffix = f": {theme['evidence_count']:,} evidence item(s)"
    detail = theme.get("summary") or ""
    return f"- {theme['label']}{suffix}" + (f" — {detail}" if detail else "")


def _solution_line(theme):
    line = _theme_line(theme)
    extras = []
    if theme.get("typical_outcome"):
        extras.append(f"typical outcome: {theme['typical_outcome']}")
    if theme.get("common_complaint"):
        extras.append(f"common complaint: {theme['common_complaint']}")
    return line + (f" ({'; '.join(extras)})" if extras else "")


def render_segment(canonical, synthesis, evidence_by_id):
    """Render one clean operator-facing segment file from audited artifacts."""
    own = canonical.get("own_evidence_count", canonical["primary_evidence_count"])
    rollup = canonical.get("rollup_evidence_count",
                           canonical["primary_evidence_count"])
    lines = [canonical["name"].upper(), "=" * 72, "",
             f"Canonical segment ID: {canonical['canonical_segment_id']}",
             f"Primary evidence items (this audience only): {own:,}"]
    if rollup != own:
        lines.append(f"Rollup with subsegments:                    {rollup:,}")
        lines.append("")
        lines.append("The two are separate on purpose. This audience's own "
                     "evidence is what it")
        lines.append("can claim; the rollup is what its branch covers. Do not "
                     "add them together.")
    lines.append("")
    lines.extend(["SEGMENT DEFINITION", "-" * 72, canonical["definition"], ""])
    lines.extend(["COMMERCIAL DISTINCTION", "-" * 72,
                  canonical["commercial_distinction"], ""])
    _section(lines, "SUBSEGMENTS", canonical.get("subsegments") or [])
    _section(lines, "ATTRIBUTES", canonical.get("attributes") or [])
    _section(lines, "TOP PAIN POINTS", synthesis.get("pain_points", []), _theme_line)
    _section(lines, "DESIRED OUTCOMES", synthesis.get("desired_outcomes", []), _theme_line)
    _section(lines, "FAILED / TRIED SOLUTIONS", synthesis.get("failed_solutions", []),
             _solution_line)
    _section(lines, "COMMON TRIGGERS / CONTEXTS", synthesis.get("triggers", []),
             _theme_line)
    _section(lines, "BELIEFS / CAUSAL THEORIES", synthesis.get("beliefs", []),
             _theme_line)
    _section(lines, "EMOTIONAL DRIVERS", synthesis.get("emotional_states", []),
             _theme_line)
    _section(lines, "OBJECTIONS / BUYING CONCERNS", synthesis.get("objections", []),
             _theme_line)
    _section(lines, "BUYING TRIGGERS / SOLUTION SIGNALS",
             synthesis.get("buying_triggers", []), _theme_line)
    language = []
    for theme in synthesis.get("representative_language", []):
        for quote in theme.get("representative_quotes") or []:
            language.append(f'- "{quote["quote"]}" [evidence {quote["evidence_id"]}]')
    _section(lines, "REPRESENTATIVE VOC LANGUAGE", _unique(language), lambda value: value)
    lines.extend(["EVIDENCE ITEMS", "-" * 72])
    for index, evidence_id in enumerate(canonical["primary_evidence_ids"], 1):
        evidence = evidence_by_id[evidence_id]
        lines.extend([
            f"[{index}]", f"EVIDENCE ID: {evidence_id}",
            f"TIER: {evidence.get('tier', 'context')}",
            "TYPE: comment", f"TITLE: {evidence.get('title') or ''}",
            f"URL: {evidence.get('url') or ''}", f"TEXT: {evidence['text']}", "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def render_index(project_name, refined_count, assigned_count, unassigned_count,
                 research_segment_count, canonical_rows, synthesis_by_id,
                 filenames):
    title = project_name.replace("_", " ").upper() + " — COMMERCIAL SEGMENT INDEX"
    lines = [title, "=" * 72, "", "Method:",
             "- Every retained VOC item has at most one primary Stage-05 audience assignment.",
             "- Stage-07 research clusters are coalesced into commercial parent segments.",
             "- Pain points and other VOC signals may overlap within a segment.",
             "- Exact duplicate evidence IDs are counted once per canonical segment.", "",
             f"Refined evidence items: {refined_count:,}",
             f"Assigned primary evidence: {assigned_count:,}",
             f"Unassigned: {unassigned_count:,}",
             f"Research-level validated segments: {research_segment_count:,}",
             f"Canonical commercial segments: {len(canonical_rows):,}", "",
             "VALIDATED COMMERCIAL SEGMENTS", "-" * 72]
    for index, canonical in enumerate(canonical_rows, 1):
        synthesis = synthesis_by_id[canonical["canonical_segment_id"]]
        own = canonical.get("own_evidence_count",
                            canonical["primary_evidence_count"])
        rollup = canonical.get("rollup_evidence_count",
                               canonical["primary_evidence_count"])
        count_line = f"   Primary evidence items: {own:,}"
        if rollup != own:
            count_line += f" (rollup with subsegments: {rollup:,})"
        lines.extend([f"{index}. {canonical['name']}", count_line,
                      "   Top pain points:"])
        pains = synthesis.get("pain_points", [])[:5]
        if pains:
            lines.extend(f"   - {row['label']} ({row['evidence_count']:,})"
                         for row in pains)
        else:
            lines.append("   - No recurring pain pattern extracted")
        lines.extend([f"   File: {filenames[canonical['canonical_segment_id']]}", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_watchlist(mapping, validated_by_id):
    rows = [row for row in mapping["mappings"]
            if row["disposition"] in {
                "research_watchlist", "excluded_from_commercial_taxonomy"}]
    lines = ["RESEARCH WATCHLIST", "=" * 72, "",
             "Research clusters retained outside the commercial parent taxonomy.", ""]
    for row in rows:
        original = validated_by_id[row["segment_id"]]
        lines.extend([
            f"{original['name']} ({row['segment_id']})",
            f"Disposition: {row['disposition']}",
            f"Assigned evidence items: {row['assigned_evidence_count']:,}",
            f"Rationale: {row['rationale']}", "",
        ])
    if not rows:
        lines.append("No research clusters are currently on the watchlist or excluded.")
    return "\n".join(lines).rstrip() + "\n"


def _write_text_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _write_json_atomic(path, value):
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def render_research_pack(final_dir, project_name, refined_count, assigned_count,
                         unassigned_count, validated, catalogue, mapping,
                         synthesis_by_id, evidence_by_id):
    """Deterministically replace only files owned by Stage 09."""
    os.makedirs(final_dir, exist_ok=True)
    machine_dir = os.path.join(final_dir, "_machine")
    synthesis_machine = os.path.join(machine_dir, "synthesis")
    os.makedirs(synthesis_machine, exist_ok=True)
    prior_manifest = os.path.join(final_dir, ".stage09_manifest.json")
    prior_files = []
    if os.path.isfile(prior_manifest):
        try:
            with open(prior_manifest, encoding="utf-8") as handle:
                prior_files = json.load(handle).get("generated_files", [])
        except (OSError, ValueError, TypeError):
            prior_files = []

    canonical_rows = catalogue["canonical_segments"]
    filenames = {
        row["canonical_segment_id"]: f"{index:02d}_{row['slug']}.txt"
        for index, row in enumerate(canonical_rows, 1)
    }
    generated = []
    for row in canonical_rows:
        synthesis = synthesis_by_id[row["canonical_segment_id"]]
        filename = filenames[row["canonical_segment_id"]]
        _write_text_atomic(os.path.join(final_dir, filename),
                           render_segment(row, synthesis, evidence_by_id))
        generated.append(filename)
        machine_name = row["canonical_segment_id"] + ".json"
        _write_json_atomic(os.path.join(synthesis_machine, machine_name), synthesis)
        generated.append(os.path.join("_machine", "synthesis", machine_name))

    index = render_index(
        project_name, refined_count, assigned_count, unassigned_count,
        len(validated), canonical_rows, synthesis_by_id, filenames)
    _write_text_atomic(os.path.join(final_dir, "00_segment_index.txt"), index)
    generated.append("00_segment_index.txt")
    _write_text_atomic(
        os.path.join(final_dir, "_research_watchlist.txt"),
        render_watchlist(mapping, {row["segment_id"]: row for row in validated}))
    generated.append("_research_watchlist.txt")
    _write_json_atomic(os.path.join(machine_dir, "canonical_segments.json"), catalogue)
    generated.append(os.path.join("_machine", "canonical_segments.json"))
    _write_json_atomic(os.path.join(machine_dir, "commercial_mapping.json"), mapping)
    generated.append(os.path.join("_machine", "commercial_mapping.json"))

    for relative in set(prior_files) - set(generated):
        target = os.path.abspath(os.path.join(final_dir, relative))
        if os.path.commonpath([os.path.abspath(final_dir), target]) != os.path.abspath(final_dir):
            continue
        if os.path.isfile(target):
            os.remove(target)
    manifest = {"generated_files": sorted(generated)}
    _write_json_atomic(prior_manifest, manifest)
    return index, filenames
