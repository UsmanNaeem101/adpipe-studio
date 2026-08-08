"""Deterministic contracts and bookkeeping for scalable VOC segmentation.

Models discover and compare audiences.  This module owns everything that should
not be probabilistic: evidence tiers, chunk coverage, lineage unions, counts,
representative samples, artifact ordering, and the compact Stage 04 packet.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict


EVIDENCE_TIERS = ("core", "supporting", "context")

# Included in every persisted 03A chunk fingerprint.  Prompt and schema changes
# already alter the fingerprint themselves; this explicit semantic-contract
# version also invalidates cached work when only the code-side quality gate
# changes.
HARVEST_CONTRACT_VERSION = "03a-semantic-contract-v2"

# One-time compatibility contract for the first persisted 03A release.  It is
# kept exact so completed model work can be authenticated by its old fingerprint,
# locally revalidated, and migrated instead of being thrown away after the schema
# gained per-chunk enums and semantic constraints.
LEGACY_HARVEST_SKILL_V1 = """# Harvest Segment Candidates

You are the high-recall harvester in a customer-segmentation pipeline. Read one
deterministic chunk of **Core** VOC and identify every plausible recurring audience
pattern. Missing a real minority audience is worse than returning an extra provisional
candidate.

A segment is an audience whose context, constraint, job-to-be-done, buying situation,
or behaviour would justify different messaging, positioning, targeting, offer, or
product decisions. A symptom, incidental attribute, arbitrary demographic, or one
isolated comment is not a segment.

For each plausible recurring pattern return a compact candidate key, plain-language
name, audience cue, commercial distinction, every supporting evidence ID in this
chunk, short cue terms, and an honest discovery strength. Preserve potentially
valuable minority audiences and avoid aggressive merging; global semantic merging is
Stage 03B's job.

Do not validate, assign all evidence, write polished segment cards, invent evidence,
or cite an ID outside this chunk. Return only the supplied structured output.
"""

# Core requires direct lived experience plus a second meaningful signal.  The
# reason vocabulary is closed by Stage 01, so this mapping is stable and generic.
CORE_SIGNALS = frozenset({
    "specific_problem", "specific_context", "attempted_solution",
    "product_experience", "competitor_experience", "outcome", "objection",
    "buying_trigger", "buying_criterion", "desired_proof", "offer_response",
    "comparison", "workaround", "emotional_signal",
})
SUPPORTING_SIGNALS = CORE_SIGNALS | frozenset({
    "third_person_observation", "belief",
})


def evidence_tier(record):
    """Map one retained Stage 01 judgement to its canonical evidence strength.

    Existing lean production files have no reason codes, so a valid persisted
    tier is retained when that is the only tier evidence available.  Rejected
    rows deliberately have no tier and must never enter production output.
    """
    if record.get("decision") == "reject":
        return None
    reasons = set(record.get("retention_reasons") or [])
    if ("first_person_experience" in reasons
            and reasons.intersection(CORE_SIGNALS)):
        return "core"
    if reasons.intersection(SUPPORTING_SIGNALS):
        return "supporting"
    prior = record.get("tier")
    if not reasons and prior in EVIDENCE_TIERS:
        return prior
    if not reasons:
        raise ValueError(
            f"retained evidence {record.get('id')!r} has neither Stage 01 "
            "retention_reasons nor an existing canonical tier")
    return "context"


def tier_counts(records):
    counts = Counter(row.get("tier") for row in records)
    return {tier: counts[tier] for tier in EVIDENCE_TIERS}


def initial_discovery_records(records, include_supporting=False):
    """The only gate into 03A; Context is never allowed to drive discovery."""
    allowed = {"core", "supporting"} if include_supporting else {"core"}
    return [row for row in records if row.get("tier") in allowed]


HARVEST_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "candidate_key": {
                "type": "string", "minLength": 3,
                "pattern": "^[a-z0-9]+(?:_[a-z0-9]+)*$",
            },
            "provisional_name": {"type": "string", "minLength": 3},
            "audience_cue": {"type": "string", "minLength": 3},
            "why_commercially_distinct": {"type": "string", "minLength": 3},
            "evidence_ids": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "integer"},
            },
            "cue_terms": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "discovery_strength": {"type": "string",
                                   "enum": ["strong", "probable", "emerging", "weak"]},
        },
        "required": ["candidate_key", "provisional_name", "audience_cue",
                     "why_commercially_distinct", "evidence_ids", "cue_terms",
                     "discovery_strength"],
        "additionalProperties": False,
    }}},
    "required": ["candidates"],
    "additionalProperties": False,
}


def harvest_schema(chunk_ids):
    """Return the 03A schema constrained to exactly one chunk's evidence IDs."""
    schema = copy.deepcopy(HARVEST_SCHEMA)
    evidence_items = schema["properties"]["candidates"]["items"][
        "properties"]["evidence_ids"]["items"]
    evidence_items["enum"] = list(chunk_ids)
    return schema


def legacy_harvest_schema_v1():
    """Reconstruct the exact static schema used by existing 27-chunk runs."""
    schema = copy.deepcopy(HARVEST_SCHEMA)
    candidate = schema["properties"]["candidates"]["items"]["properties"]
    for name in ("candidate_key", "provisional_name", "audience_cue",
                 "why_commercially_distinct"):
        candidate[name].pop("minLength", None)
        candidate[name].pop("pattern", None)
    evidence = candidate["evidence_ids"]
    evidence.pop("minItems", None)
    evidence.pop("uniqueItems", None)
    cues = candidate["cue_terms"]
    cues.pop("minItems", None)
    cues.pop("uniqueItems", None)
    cues["items"].pop("minLength", None)
    return schema

CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "slug": {"type": "string"},
            "name": {"type": "string"},
            "definition": {"type": "string"},
            "commercial_distinction": {"type": "string"},
            "inclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "exclusion_criteria": {"type": "array", "items": {"type": "string"}},
            "merged_candidate_keys": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string"},
            },
            "merged_aliases": {"type": "array", "items": {"type": "string"}},
            "core_evidence_ids": {"type": "array", "items": {"type": "integer"}},
            "discovery_status": {"type": "string",
                                 "enum": ["strong_candidate", "probable_candidate",
                                          "emerging_candidate", "weak_candidate"]},
        },
        "required": ["candidate_id", "slug", "name", "definition",
                     "commercial_distinction", "inclusion_criteria",
                     "exclusion_criteria", "merged_candidate_keys",
                     "merged_aliases", "core_evidence_ids", "discovery_status"],
        "additionalProperties": False,
    }}},
    "required": ["candidates"],
    "additionalProperties": False,
}


def consolidate_schema(provisional_keys):
    """Return the 03B schema constrained to exact source-catalogue keys.

    Canonical IDs, slugs and names deliberately remain model-created. Only the
    lineage field is closed: it identifies existing 03A records and therefore
    must copy their keys verbatim rather than paraphrasing or canonicalizing
    them.
    """
    schema = copy.deepcopy(CONSOLIDATE_SCHEMA)
    lineage = schema["properties"]["candidates"]["items"][
        "properties"]["merged_candidate_keys"]
    lineage["items"]["enum"] = list(provisional_keys)
    return schema

EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {"matches": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer"},
            "candidate_ids": {"type": "array", "items": {"type": "string"}},
            "match_strength": {"type": "string",
                               "enum": ["strong", "corroborating", "none"]},
        },
        "required": ["evidence_id", "candidate_ids", "match_strength"],
        "additionalProperties": False,
    }}},
    "required": ["matches"],
    "additionalProperties": False,
}

NOVELTY_SCHEMA = {
    "type": "object",
    "properties": {"audits": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "evidence_id": {"type": "integer"},
            "status": {"type": "string",
                       "enum": ["covered", "ambiguous", "possible_new_candidate"]},
            "candidate_key": {"type": "string"},
            "provisional_name": {"type": "string"},
            "audience_cue": {"type": "string"},
            "commercial_distinction": {"type": "string"},
        },
        "required": ["evidence_id", "status", "candidate_key", "provisional_name",
                     "audience_cue", "commercial_distinction"],
        "additionalProperties": False,
    }}},
    "required": ["audits"],
    "additionalProperties": False,
}


def evidence_text(records, include_tier=False):
    blocks = []
    for row in records:
        tier = f" [{row.get('tier', 'context')}]" if include_tier else ""
        blocks.append(f"[{row['id']}]{tier} {row['text']}")
    return "\n\n".join(blocks)


def harvest_prompt(records):
    """Render the complete 03A user turn from one evidence chunk."""
    return (
        "Search this evidence chunk for zero or more recurring audience "
        "patterns. This chunk is not itself a segment: do not name the "
        "chunk and do not assign every item. Return separate candidates "
        "when distinct audience groups recur, cite only the IDs that "
        "genuinely support each candidate, and leave unrelated evidence "
        "unassigned. Every cited ID must be one shown below.\n\n"
        + evidence_text(records, include_tier=True)
    )


def legacy_harvest_prompt_v1(records):
    """Render the exact user turn used by the first persisted 03A release."""
    return (
        "Harvest provisional audience candidates from this complete "
        "discovery-evidence chunk. Every cited ID must be one shown below.\n\n"
        + evidence_text(records, include_tier=True)
    )


def migrate_legacy_harvest_rows(rows):
    """Apply only the normalization aggregation already applied to V1 keys."""
    migrated = []
    for row in rows:
        item = dict(row)
        item["candidate_key"] = _key(item.get("candidate_key"))
        migrated.append(item)
    return migrated


def chunk_by_tokens(records, target_tokens, prefix="chunk"):
    """Chunk in stable input order using the exact formatter's char estimate."""
    if target_tokens < 1:
        raise ValueError("chunk token target must be positive")
    chunks, current, chars = [], [], 0
    for row in records:
        rendered = evidence_text([row], include_tier=True)
        cost = max(len(rendered), 4)
        if current and (chars + cost) // 4 > target_tokens:
            chunks.append(current)
            current, chars = [], 0
        current.append(row)
        chars += cost + 2
    if current:
        chunks.append(current)
    return [{"chunk_id": f"{prefix}_{n:04d}",
             "estimated_tokens": max(len(evidence_text(rows, True)) // 4, 1),
             "evidence_ids": [row["id"] for row in rows],
             "records": rows}
            for n, rows in enumerate(chunks)]


def assert_exact_chunk_coverage(chunks, expected_ids):
    seen = [eid for chunk in chunks for eid in chunk["evidence_ids"]]
    counts = Counter(seen)
    missing = set(expected_ids) - set(seen)
    extra = set(seen) - set(expected_ids)
    repeated = {eid for eid, count in counts.items() if count != 1}
    if missing or extra or repeated or len(seen) != len(expected_ids):
        raise ValueError(
            f"chunk coverage failed: {len(missing)} missing, {len(extra)} unknown, "
            f"{len(repeated)} repeated")


def _key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


_GENERIC_HARVEST_KEYS = frozenset({
    "audience", "candidate", "segment", "harvest_candidates_from_core",
})
_GENERIC_HARVEST_NAMES = frozenset({"audience", "candidate", "segment"})
_CLEAN_CANDIDATE_KEY = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class HarvestContractError(ValueError):
    """A model-produced 03A row violated a repairable chunk contract."""

    def __init__(self, reason, *, chunk_id=None, candidate=None,
                 invalid_evidence_ids=None):
        super().__init__(reason)
        self.chunk_id = chunk_id
        self.candidate = candidate
        self.invalid_evidence_ids = list(invalid_evidence_ids or [])


class HarvestProvenanceFailure(HarvestContractError):
    """Every citation for one candidate is impossible for its source chunk."""


def clean_harvest_provenance(rows, chunk_ids, chunk_id=None):
    """Remove only impossible integer citations, in place, and report each edit.

    This is deliberately narrower than validation. A non-list, non-integer, or
    duplicate citation is a broader schema violation and is left untouched for
    the structured repair path. If an otherwise provenance-only cleanup would
    leave a candidate unsupported, nothing is changed and repair is required.
    """
    allowed = set(chunk_ids)
    planned = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        evidence_ids = row.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            continue
        # Do not let provenance cleanup accidentally conceal type or uniqueness
        # violations. Those are separate contracts with their existing recovery.
        if (any(type(evidence_id) is not int for evidence_id in evidence_ids)
                or len(evidence_ids) != len(set(evidence_ids))):
            continue
        removed = [evidence_id for evidence_id in evidence_ids
                   if evidence_id not in allowed]
        if not removed:
            continue
        valid = [evidence_id for evidence_id in evidence_ids
                 if evidence_id in allowed]
        candidate = str(row.get("candidate_key") or "").strip() or "unknown"
        if not valid:
            raise HarvestProvenanceFailure(
                f"03A candidate {candidate!r} has no in-chunk supporting evidence; "
                "all cited evidence IDs were outside the chunk",
                chunk_id=chunk_id, candidate=candidate,
                invalid_evidence_ids=removed)
        planned.append((row, candidate, removed, valid))

    events = []
    for row, candidate, removed, valid in planned:
        row["evidence_ids"] = valid
        events.append({
            "chunk_id": chunk_id,
            "candidate": candidate,
            "removed_evidence_ids": removed,
            "remaining_evidence_count": len(valid),
        })
    return events


def validate_harvest_rows(rows, chunk_ids, chunk_id=None):
    """Reject structurally valid but unusable 03A candidate claims.

    This deliberately stays small and deterministic.  The prompt owns semantic
    judgement; code proves only invariants that cannot be matters of opinion.
    Evidence may overlap between candidates and input IDs may remain unassigned.
    """
    allowed = set(chunk_ids)
    seen_keys = set()
    for row in rows:
        raw_key = str(row.get("candidate_key") or "").strip()
        key = _key(raw_key)
        name = str(row.get("provisional_name") or "").strip()
        if not _CLEAN_CANDIDATE_KEY.fullmatch(raw_key):
            raise HarvestContractError(
                f"03A candidate key {raw_key!r} must be a lowercase snake_case "
                "audience slug", chunk_id=chunk_id, candidate=raw_key)
        if key in _GENERIC_HARVEST_KEYS:
            raise HarvestContractError(
                f"03A candidate key {raw_key!r} is a generic task label",
                chunk_id=chunk_id, candidate=raw_key)
        if _key(name) in _GENERIC_HARVEST_NAMES:
            raise HarvestContractError(
                f"03A provisional name {name!r} is not an audience label",
                chunk_id=chunk_id, candidate=raw_key)
        if key in seen_keys:
            raise HarvestContractError(
                f"03A returned duplicate candidate key {raw_key!r}",
                chunk_id=chunk_id, candidate=raw_key)
        seen_keys.add(key)

        evidence_ids = row.get("evidence_ids") or []
        if not evidence_ids:
            raise HarvestContractError(
                f"03A candidate {raw_key!r} has no supporting evidence",
                chunk_id=chunk_id, candidate=raw_key)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise HarvestContractError(
                f"03A candidate {raw_key!r} repeats a supporting evidence ID",
                chunk_id=chunk_id, candidate=raw_key)
        cited = set(evidence_ids)
        unknown = sorted(cited - allowed)
        if unknown:
            raise HarvestContractError(
                f"03A candidate {raw_key!r} cites "
                f"{len(unknown)} evidence ID(s) outside its chunk",
                chunk_id=chunk_id, candidate=raw_key,
                invalid_evidence_ids=unknown)


def harvest_full_chunk_claims(chunk_results):
    """Return singleton candidates claiming every ID, for operator review.

    Such a result can be legitimate for a genuinely homogeneous chunk, so it is
    telemetry rather than a rejection rule.  Repetition across many chunks is a
    useful signal that the model is naming chunks instead of finding audiences.
    """
    claims = []
    for result in chunk_results:
        candidates = result.get("candidates") or []
        if len(candidates) != 1:
            continue
        if set(candidates[0].get("evidence_ids") or []) == set(result["evidence_ids"]):
            claims.append({
                "chunk_id": result["chunk_id"],
                "candidate_key": candidates[0].get("candidate_key"),
                "evidence_count": len(result["evidence_ids"]),
            })
    return claims


def aggregate_harvest(chunk_results, minimum_evidence=2):
    """Merge only exact candidate keys; semantic merging belongs to 03B."""
    grouped = {}
    for result in sorted(chunk_results, key=lambda row: row["chunk_id"]):
        chunk_id = result["chunk_id"]
        validate_harvest_rows(
            result["candidates"], result["evidence_ids"], chunk_id=chunk_id)
        for index, row in enumerate(result["candidates"]):
            key = _key(row["candidate_key"])
            if not key:
                raise ValueError("03A returned an empty candidate key")
            item = grouped.setdefault(key, {
                "candidate_key": key, "occurrence_count": 0,
                "evidence_ids": set(), "chunk_ids": set(), "aliases": set(),
                "audience_cues": set(), "commercial_cues": set(),
                "cue_terms": set(), "strengths": [], "provenance": [],
            })
            item["occurrence_count"] += 1
            item["evidence_ids"].update(row["evidence_ids"])
            item["chunk_ids"].add(chunk_id)
            item["aliases"].add(row["provisional_name"])
            item["audience_cues"].add(row["audience_cue"])
            item["commercial_cues"].add(row["why_commercially_distinct"])
            item["cue_terms"].update(row["cue_terms"])
            item["strengths"].append(row["discovery_strength"])
            item["provenance"].append({"chunk_id": chunk_id, "result_index": index})
    output = []
    for key in sorted(grouped):
        item = grouped[key]
        if len(item["evidence_ids"]) < minimum_evidence:
            continue
        output.append({
            "candidate_key": key,
            "occurrence_count": item["occurrence_count"],
            "unique_evidence_count": len(item["evidence_ids"]),
            "unique_chunk_count": len(item["chunk_ids"]),
            "evidence_ids": sorted(item["evidence_ids"]),
            "chunk_ids": sorted(item["chunk_ids"]),
            "aliases": sorted(item["aliases"]),
            "audience_cues": sorted(item["audience_cues"]),
            "commercial_cues": sorted(item["commercial_cues"]),
            "cue_terms": sorted(item["cue_terms"]),
            "strengths": item["strengths"],
            "provenance": item["provenance"],
        })
    return output


class ConsolidationContractError(ValueError):
    """A 03B result invented or rewrote one or more 03A lineage keys."""

    def __init__(self, invalid_references):
        self.invalid_references = invalid_references
        count = sum(len(row["invalid_keys"]) for row in invalid_references)
        super().__init__(
            f"03B referenced {count} unknown provisional key(s) across "
            f"{len(invalid_references)} canonical candidate(s)")


def validate_consolidated_lineage(rows, provisional_catalogue):
    """Require exact, nonempty, unique 03A keys in every 03B lineage list."""
    allowed = {row["candidate_key"] for row in provisional_catalogue}
    invalid_references = []
    for row in rows:
        if not isinstance(row, dict):
            continue  # The JSON-schema validator reports the broader shape error.
        keys = row.get("merged_candidate_keys")
        if not isinstance(keys, list):
            continue
        if not keys:
            raise ValueError("03B returned an empty merged_candidate_keys list")
        if any(not isinstance(key, str) for key in keys):
            raise ValueError("03B returned a non-string provisional key")
        if len(keys) != len(set(keys)):
            raise ValueError("03B returned duplicate merged provisional keys")
        invalid = list(dict.fromkeys(key for key in keys if key not in allowed))
        if invalid:
            invalid_references.append({
                "candidate_id": row.get("candidate_id"),
                "slug": row.get("slug"),
                "invalid_keys": invalid,
            })
    if invalid_references:
        raise ConsolidationContractError(invalid_references)


def merge_consolidation_repair(original_payload, repaired_payload):
    """Patch only 03B lineage fields into the complete original collection.

    A repair response may contain only the affected candidates. Treating that
    response as a replacement silently collapsed a 208-row catalogue to two
    rows. Candidate identity and every non-lineage field are therefore owned by
    the original completed response; repair may change only
    `merged_candidate_keys`, keyed by an existing `candidate_id`.
    """
    original_rows = original_payload.get("candidates")
    repaired_rows = repaired_payload.get("candidates")
    if not isinstance(original_rows, list) or not isinstance(repaired_rows, list):
        raise ValueError("03B collection repair requires candidate arrays")
    original_ids = [row.get("candidate_id") for row in original_rows
                    if isinstance(row, dict)]
    if len(original_ids) != len(original_rows) or len(set(original_ids)) != len(
            original_ids):
        raise ValueError("03B original collection has invalid candidate identity")
    repaired_ids = [row.get("candidate_id") for row in repaired_rows
                    if isinstance(row, dict)]
    if len(repaired_ids) != len(repaired_rows) or len(set(repaired_ids)) != len(
            repaired_ids):
        raise ValueError("03B repair has invalid or duplicate candidate identity")
    unknown = [candidate_id for candidate_id in repaired_ids
               if candidate_id not in set(original_ids)]
    if unknown:
        raise ValueError(
            f"03B repair introduced unknown candidate ID {unknown[0]!r}")

    merged = copy.deepcopy(original_payload)
    by_id = {row["candidate_id"]: row for row in merged["candidates"]}
    for repaired in repaired_rows:
        if "merged_candidate_keys" not in repaired:
            raise ValueError(
                f"03B repair omitted lineage for {repaired['candidate_id']!r}")
        by_id[repaired["candidate_id"]]["merged_candidate_keys"] = copy.deepcopy(
            repaired["merged_candidate_keys"])

    if len(merged["candidates"]) != len(original_rows):
        raise ValueError(
            "03B CONTRACT REPAIR CATASTROPHIC DROP: candidate count changed "
            f"from {len(original_rows)} to {len(merged['candidates'])}")
    return merged


def finalize_consolidated(rows, provisional_catalogue):
    """Replace model-estimated evidence lineage with exact deterministic unions."""
    validate_consolidated_lineage(rows, provisional_catalogue)
    provisional = {row["candidate_key"]: row for row in provisional_catalogue}
    seen_ids, seen_slugs, output = set(), set(), []
    for row in rows:
        if row["candidate_id"] in seen_ids or row["slug"] in seen_slugs:
            raise ValueError("03B returned duplicate candidate IDs or slugs")
        keys = list(row["merged_candidate_keys"])
        evidence = sorted({eid for key in keys for eid in provisional[key]["evidence_ids"]})
        if not evidence:
            raise ValueError(f"03B candidate {row['candidate_id']!r} has no Core lineage")
        chunks = sorted({chunk for key in keys for chunk in provisional[key]["chunk_ids"]})
        aliases = sorted(set(row["merged_aliases"]) |
                         {alias for key in keys for alias in provisional[key]["aliases"]})
        item = dict(row)
        item["merged_candidate_keys"] = keys
        item["merged_aliases"] = aliases
        item["core_evidence_ids"] = evidence
        item["unique_evidence_count"] = len(evidence)
        item["unique_03a_chunk_count"] = len(chunks)
        item["source_03a_chunks"] = chunks
        output.append(item)
        seen_ids.add(row["candidate_id"])
        seen_slugs.add(row["slug"])
    return sorted(output, key=lambda row: row["candidate_id"])


def validate_match_rows(rows, candidate_ids):
    allowed = set(candidate_ids)
    for row in rows:
        matches = row.get("candidate_ids") or []
        unknown = set(matches) - allowed
        if unknown:
            raise ValueError(f"evidence {row.get('evidence_id')} matched unknown candidate")
        if row.get("match_strength") == "none" and matches:
            raise ValueError("a none-strength expansion match cannot name candidates")


def assemble_candidate_evidence(candidates, match_rows, records):
    """Union model matches, then compute every count and sample in code."""
    candidate_ids = [row["candidate_id"] for row in candidates]
    validate_match_rows(match_rows, candidate_ids)
    by_id = {row["id"]: row for row in records}
    order = {row["id"]: n for n, row in enumerate(records)}
    sets = {cid: {tier: set() for tier in EVIDENCE_TIERS} for cid in candidate_ids}
    for candidate in candidates:
        sets[candidate["candidate_id"]]["core"].update(candidate["core_evidence_ids"])
    for row in match_rows:
        evidence = by_id.get(row["evidence_id"])
        if evidence is None or row["match_strength"] == "none":
            continue
        tier = evidence.get("tier", "context")
        for candidate_id in row["candidate_ids"]:
            sets[candidate_id][tier].add(row["evidence_id"])

    output = []
    for candidate in candidates:
        item = dict(candidate)
        tier_sets = sets[candidate["candidate_id"]]
        for tier in EVIDENCE_TIERS:
            ids = sorted(tier_sets[tier], key=lambda eid: order.get(eid, 10**12))
            item[f"{tier}_evidence_ids"] = ids
            item[f"{tier}_evidence_count"] = len(ids)
        all_ids = [eid for tier in EVIDENCE_TIERS for eid in item[f"{tier}_evidence_ids"]]
        threads = {by_id[eid].get("thread_id") for eid in all_ids
                   if eid in by_id and by_id[eid].get("thread_id")}
        subreddits = {by_id[eid].get("subreddit") for eid in all_ids
                      if eid in by_id and by_id[eid].get("subreddit")}
        item["unique_evidence_count"] = len(set(all_ids))
        item["unique_thread_count"] = len(threads)
        item["unique_subreddit_count"] = len(subreddits)
        representatives = (item["core_evidence_ids"][:4]
                           + item["supporting_evidence_ids"][:2]
                           + item["context_evidence_ids"][:1])
        item["representative_evidence_ids"] = list(dict.fromkeys(representatives))
        if not item["representative_evidence_ids"]:
            raise ValueError(f"candidate {item['candidate_id']!r} has no evidence")
        output.append(item)
    return output


def novelty_catalogue(audits, minimum_evidence=2):
    grouped = defaultdict(list)
    for row in audits:
        if row.get("status") == "possible_new_candidate":
            grouped[_key(row.get("candidate_key"))].append(row)
    output = []
    for key in sorted(grouped):
        rows = grouped[key]
        evidence = sorted({row["evidence_id"] for row in rows})
        if not key or len(evidence) < minimum_evidence:
            continue
        chunks = sorted({row.get("origin_chunk") for row in rows
                         if row.get("origin_chunk")})
        output.append({
            "candidate_key": "novel_" + key,
            "occurrence_count": len(rows),
            "unique_evidence_count": len(evidence),
            "unique_chunk_count": len(chunks),
            "evidence_ids": evidence,
            "chunk_ids": chunks,
            "aliases": sorted({row["provisional_name"] for row in rows}),
            "audience_cues": sorted({row["audience_cue"] for row in rows}),
            "commercial_cues": sorted({row["commercial_distinction"] for row in rows}),
            "cue_terms": [], "strengths": ["emerging"],
            "provenance": [{"chunk_id": row.get("origin_chunk"),
                            "evidence_id": row["evidence_id"]} for row in rows],
        })
    return output


def stage04_packet(candidates, records):
    """Compact cards + deterministic metrics + bounded verbatim evidence."""
    by_id = {row["id"]: row for row in records}
    packet = []
    for candidate in candidates:
        card = dict(candidate)
        reps = []
        for evidence_id in candidate["representative_evidence_ids"]:
            row = by_id.get(evidence_id)
            if row:
                reps.append({key: row.get(key) for key in
                             ("id", "text", "tier", "thread_id", "subreddit")})
        card["representative_evidence"] = reps
        packet.append(card)
    return json.dumps({"candidates": packet}, ensure_ascii=False, indent=2)
