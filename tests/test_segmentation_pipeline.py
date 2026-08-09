"""Deterministic contracts for the multi-pass Stage 03 pipeline."""

import dataclasses
import json
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import segmentation
import llm
from llm import BatchResult, Job


def record(evidence_id, tier="core", text=None, thread=None, subreddit=None):
    return {"id": evidence_id, "text": text or f"voice {evidence_id}",
            "tier": tier, "thread_id": thread, "subreddit": subreddit}


class SegmentationBookkeepingTests(unittest.TestCase):
    @staticmethod
    def harvest_candidate(key="desk_workers", evidence_ids=None):
        return {
            "candidate_key": key, "provisional_name": "Desk workers",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": list(evidence_ids or [1]), "cue_terms": ["desk"],
            "discovery_strength": "probable",
        }

    @staticmethod
    def provisional(key="desk", evidence_ids=None, candidate_id=None):
        return {
            "candidate_id": candidate_id or f"03a_0000_c{key[-1:] if key[-1:].isdigit() else '000'}",
            "candidate_key": key,
            "evidence_ids": list(evidence_ids or [1, 2]),
            "chunk_ids": ["03a_0000"],
            "aliases": [key.replace("_", " ").title()],
        }

    @staticmethod
    def consolidated(source_ids=None, slug="new_canonical_segment"):
        return {
            "slug": slug,
            "name": "New canonical segment", "definition": "A merged audience",
            "commercial_distinction": "Distinct messaging",
            "inclusion_criteria": ["relevant context"],
            "exclusion_criteria": ["incidental mention"],
            "source_candidate_ids": list(source_ids or ["03a_0000_c000"]),
            "merged_aliases": [],
            "discovery_status": "strong_candidate",
        }

    def test_dynamic_harvest_schema_enum_is_exactly_the_chunk_ids(self):
        schema = segmentation.harvest_schema([9, 3, 17])
        evidence = schema["properties"]["candidates"]["items"][
            "properties"]["evidence_ids"]
        self.assertEqual(evidence["items"]["enum"], [9, 3, 17])
        self.assertEqual(evidence["minItems"], 1)
        self.assertTrue(evidence["uniqueItems"])
        self.assertNotIn(
            "enum", segmentation.HARVEST_SCHEMA["properties"]["candidates"]
            ["items"]["properties"]["evidence_ids"]["items"])

    def test_harvest_contract_accepts_ids_inside_chunk(self):
        segmentation.validate_harvest_rows(
            [self.harvest_candidate(evidence_ids=[3, 9])], [3, 9, 17],
            chunk_id="03a_0000")

    def test_harvest_contract_reports_one_or_several_outside_ids(self):
        for bad_ids in ([3, 99], [3, 98, 99]):
            with self.subTest(bad_ids=bad_ids), self.assertRaises(
                    segmentation.HarvestContractError) as ctx:
                segmentation.validate_harvest_rows(
                    [self.harvest_candidate(evidence_ids=bad_ids)], [3, 9, 17],
                    chunk_id="03a_0007")
            self.assertEqual(ctx.exception.chunk_id, "03a_0007")
            self.assertEqual(ctx.exception.invalid_evidence_ids,
                             sorted(set(bad_ids) - {3, 9, 17}))

    def test_provenance_cleanup_removes_one_impossible_id_locally(self):
        row = self.harvest_candidate(evidence_ids=[3, 99, 9])
        events = segmentation.clean_harvest_provenance(
            [row], [3, 9, 17], chunk_id="03a_0007")
        self.assertEqual(row["evidence_ids"], [3, 9])
        self.assertEqual(events, [{
            "chunk_id": "03a_0007",
            "candidate": "desk_workers",
            "removed_evidence_ids": [99],
            "remaining_evidence_count": 2,
        }])

    def test_provenance_cleanup_preserves_semantics_and_valid_id_order(self):
        row = self.harvest_candidate(evidence_ids=[9, 98, 3, 99, 17])
        semantic_fields = {key: value for key, value in row.items()
                           if key != "evidence_ids"}
        events = segmentation.clean_harvest_provenance(
            [row], [3, 9, 17], chunk_id="03a_0008")
        self.assertEqual(row["evidence_ids"], [9, 3, 17])
        self.assertEqual(
            {key: value for key, value in row.items() if key != "evidence_ids"},
            semantic_fields)
        self.assertEqual(events[0]["removed_evidence_ids"], [98, 99])

    def test_provenance_cleanup_with_zero_valid_ids_requires_repair(self):
        row = self.harvest_candidate(evidence_ids=[98, 99])
        original = dict(row, evidence_ids=list(row["evidence_ids"]))
        with self.assertRaises(segmentation.HarvestProvenanceFailure) as ctx:
            segmentation.clean_harvest_provenance(
                [row], [3, 9, 17], chunk_id="03a_0009")
        self.assertEqual(row, original)
        self.assertEqual(ctx.exception.invalid_evidence_ids, [98, 99])

    def test_harvest_contract_rejects_duplicate_ids(self):
        with self.assertRaises(segmentation.HarvestContractError):
            segmentation.validate_harvest_rows(
                [self.harvest_candidate(evidence_ids=[3, 3])], [3, 9],
                chunk_id="03a_0000")

    def test_03a_chunks_cover_every_core_item_exactly_once(self):
        rows = [record(n, text="x" * (20 + n)) for n in range(1, 20)]
        chunks = segmentation.chunk_by_tokens(rows, 40, "03a")

        segmentation.assert_exact_chunk_coverage(chunks, [row["id"] for row in rows])
        self.assertGreater(len(chunks), 1)
        self.assertEqual([eid for chunk in chunks for eid in chunk["evidence_ids"]],
                         list(range(1, 20)))

    def test_03e_prompt_composition_reports_exact_rendered_regions(self):
        catalogue = json.dumps([
            {"segment_id": "seg_0001", "name": "Desk workers"},
            {"segment_id": "seg_0002", "name": "Lifters"},
        ], indent=2)
        schema = segmentation.expansion_schema(["seg_0001", "seg_0002"])
        payloads = ["x" * 80, "y" * 160, "z" * 240]
        jobs = [Job(
            id=f"03e_{index:04d}", prompt=cli.STAGE03E_EVIDENCE_PREFIX + payload,
            max_tokens=12000, schema=schema,
            expected_ids=tuple(range(index)))
            for index, payload in enumerate(payloads, 1)]

        composition = cli._stage03e_prompt_composition(
            "expand skill", catalogue, jobs, payloads)

        self.assertEqual(composition["job_id"], "03e_0002")
        self.assertEqual(composition["candidate_count"], 2)
        self.assertEqual(composition["batch_count"], 3)
        self.assertEqual(composition["candidate_catalogue"], len(catalogue) // 4)
        self.assertEqual(composition["evidence_batch"], 40)
        self.assertEqual(composition["evidence_item_range"], (1, 2, 3))
        self.assertEqual(composition["evidence_token_range"], (20, 40, 60))
        self.assertEqual(
            composition["total"],
            composition["instructions"] + composition["candidate_catalogue"] +
            composition["evidence_batch"] + composition["schema_other"])

        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            cli._print_stage03e_prompt_composition(composition)
        rendered = output.getvalue()
        self.assertIn("03E prompt composition per request", rendered)
        self.assertIn("2 candidates; repeated per request", rendered)
        self.assertIn("1/2/3 min/median/max", rendered)

    def test_03e_none_strength_invariant_is_normalized_locally(self):
        valid_none = {
            "evidence_id": 1, "segment_ids": [], "match_strength": "none"}
        contradictory = {
            "evidence_id": 2, "segment_ids": ["seg_001"],
            "match_strength": "none", "untouched": "preserve me"}
        rows = [valid_none, contradictory]

        events = segmentation.normalize_expansion_matches(
            rows, chunk_id="03e_0000")

        self.assertEqual(valid_none, {
            "evidence_id": 1, "segment_ids": [], "match_strength": "none"})
        self.assertEqual(contradictory, {
            "evidence_id": 2, "segment_ids": [], "match_strength": "none",
            "untouched": "preserve me"})
        self.assertEqual(events, [{
            "chunk_id": "03e_0000", "evidence_id": 2,
            "removed_segment_ids": ["seg_001"],
        }])
        segmentation.validate_match_rows(rows, ["seg_001"])

    def test_03e_strong_or_corroborating_empty_keeps_existing_contract(self):
        rows = [
            {"evidence_id": 1, "segment_ids": [], "match_strength": "strong"},
            {"evidence_id": 2, "segment_ids": [],
             "match_strength": "corroborating"},
        ]
        self.assertEqual(segmentation.normalize_expansion_matches(rows), [])
        segmentation.validate_match_rows(rows, ["seg_001"])

    def test_53_cached_03e_batches_normalize_without_model_calls(self):
        chunks = [{
            "chunk_id": f"03e_{index:04d}", "evidence_ids": [index + 1],
            "estimated_tokens": 4, "records": [record(index + 1)],
        } for index in range(53)]
        schema = segmentation.expansion_schema(["seg_001"])
        jobs = [Job(
            id=chunk["chunk_id"], prompt="match evidence", max_tokens=12000,
            schema=schema, expected_ids=tuple(chunk["evidence_ids"]))
            for chunk in chunks]
        events = []

        def cleaner(current_job, rows):
            cleaned = segmentation.normalize_expansion_matches(
                rows, chunk_id=current_job.id)
            events.extend(cleaned)
            return cleaned

        with tempfile.TemporaryDirectory() as tmp:
            before = {}
            for index, (job, chunk) in enumerate(zip(jobs, chunks)):
                row = {
                    "evidence_id": index + 1,
                    "segment_ids": (["seg_001"] if index == 22 else []),
                    "match_strength": "none",
                }
                saved = {
                    "chunk_id": job.id, "evidence_ids": chunk["evidence_ids"],
                    "estimated_input_tokens": 4,
                    "fingerprint": cli._job_fingerprint(
                        job, chunk["evidence_ids"], "03e skill"),
                    "matches": [row],
                }
                path = os.path.join(tmp, job.id + ".json")
                cli._json_atomic(path, saved)
                with open(path, "rb") as fh:
                    before[job.id] = fh.read()

            client = mock.Mock()
            results = cli._run_persisted_segment_jobs(
                client, "03e skill", jobs, chunks, "matches", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03E",
                row_cleaner=cleaner,
                local_cleanup_key="deterministic_normalization")

            client.estimate.assert_not_called()
            client.prewarm.assert_not_called()
            client.batch.assert_not_called()
            self.assertEqual(len(results), 53)
            self.assertEqual(len(events), 1)
            self.assertEqual(results[22]["matches"][0]["segment_ids"], [])
            for job in jobs:
                path = os.path.join(tmp, job.id + ".json")
                with open(path, "rb") as fh:
                    after = fh.read()
                if job.id == "03e_0022":
                    self.assertNotEqual(after, before[job.id])
                    with open(path, encoding="utf-8") as fh:
                        migrated = json.load(fh)
                    self.assertEqual(
                        migrated["deterministic_normalization"][0]
                        ["removed_segment_ids"], ["seg_001"])
                else:
                    self.assertEqual(after, before[job.id])

            all_rows = [row for result in results for row in result["matches"]]
            segmentation.validate_match_rows(all_rows, ["seg_001"])

    def test_supporting_and_context_do_not_enter_initial_discovery(self):
        rows = [record(1, "core"), record(2, "supporting"), record(3, "context")]
        self.assertEqual(
            [row["id"] for row in segmentation.initial_discovery_records(rows)], [1])
        self.assertEqual(
            [row["id"] for row in segmentation.initial_discovery_records(
                rows, include_supporting=True)], [1, 2])

    def test_03a_machine_ids_preserve_each_discovery_and_stable_order(self):
        chunks = [{
            "chunk_id": "03a_0001", "evidence_ids": [3, 4], "candidates": [{
                "candidate_key": "desk_workers", "provisional_name": "Desk workers",
                "audience_cue": "computer work", "why_commercially_distinct": "workday",
                "evidence_ids": [3, 4], "cue_terms": ["desk"],
                "discovery_strength": "strong"}]}, {
            "chunk_id": "03a_0000", "evidence_ids": [1, 2], "candidates": [{
                "candidate_key": "desk_workers", "provisional_name": "Office workers",
                "audience_cue": "sitting", "why_commercially_distinct": "ergonomics",
                "evidence_ids": [1, 2], "cue_terms": ["computer"],
                "discovery_strength": "probable"}]}]

        catalogue = segmentation.aggregate_harvest(chunks)

        self.assertEqual(len(catalogue), 2)
        self.assertEqual([row["candidate_id"] for row in catalogue],
                         ["03a_0000_c000", "03a_0001_c000"])
        self.assertEqual([row["candidate_key"] for row in catalogue],
                         ["desk_workers", "desk_workers"])
        self.assertEqual(catalogue[0]["evidence_ids"], [1, 2])
        renamed = json.loads(json.dumps(chunks))
        renamed[0]["candidates"][0]["candidate_key"] = "renamed freely"
        self.assertEqual(
            [row["candidate_id"] for row in segmentation.aggregate_harvest(renamed)],
            ["03a_0000_c000", "03a_0001_c000"])

    def test_03b_merges_aliases_and_code_restores_complete_evidence_union(self):
        provisional = [{
            "candidate_id": "03a_0000_c000", "candidate_key": "desk",
            "evidence_ids": [1, 2],
            "chunk_ids": ["03a_0000"], "aliases": ["Desk workers"]}, {
            "candidate_id": "03a_0001_c000", "candidate_key": "wfh",
            "evidence_ids": [3, 4],
            "chunk_ids": ["03a_0001"], "aliases": ["Remote workers"]}]
        model_row = {
            "slug": "desk_workers", "name": "Desk workers",
            "definition": "People whose workday context dominates the problem",
            "commercial_distinction": "Workday ergonomic messaging",
            "inclusion_criteria": ["desk work"], "exclusion_criteria": ["incidental desk"],
            "source_candidate_ids": ["03a_0000_c000", "03a_0001_c000"],
            "merged_aliases": ["Office staff"],
            "discovery_status": "strong_candidate"}

        final = segmentation.finalize_consolidated([model_row], provisional)

        self.assertEqual(final[0]["core_evidence_ids"], [1, 2, 3, 4])
        self.assertCountEqual(final[0]["merged_aliases"],
                              ["Desk workers", "Remote workers", "Office staff"])
        statuses = segmentation.CONSOLIDATE_SCHEMA["properties"]["candidates"][
            "items"]["properties"]["discovery_status"]["enum"]
        self.assertNotIn("Validated", statuses)
        self.assertNotIn("validated", statuses)

    def test_dynamic_03b_schema_enum_is_exactly_the_machine_candidate_ids(self):
        ids = ["03a_0000_c000", "03a_0001_c000"]
        schema = segmentation.consolidate_schema(ids)
        candidate = schema["properties"]["candidates"]["items"]["properties"]
        lineage = candidate["source_candidate_ids"]
        self.assertEqual(lineage["items"]["enum"], ids)
        self.assertEqual(lineage["minItems"], 1)
        self.assertTrue(lineage["uniqueItems"])
        self.assertNotIn("enum", candidate["slug"])
        self.assertNotIn(
            "enum", segmentation.CONSOLIDATE_SCHEMA["properties"]["candidates"]
            ["items"]["properties"]["source_candidate_ids"]["items"])

    def test_stage05_schema_uses_machine_segment_ids_not_semantic_slugs(self):
        schema = cli.assignment_schema([101, 102], ["seg_001", "seg_002"])
        fields = schema["properties"]["assignments"]["items"]["properties"]
        self.assertEqual(fields["evidence_id"]["enum"], [101, 102])
        self.assertEqual(fields["primary_segment_id"]["enum"],
                         ["", "seg_001", "seg_002"])
        self.assertNotIn("desk_workers", fields["primary_segment_id"]["enum"])

    def test_03b_accepts_exact_lineage_while_canonical_slug_may_be_new(self):
        provisional = [self.provisional("desk", candidate_id="03a_0000_c000"),
                       self.provisional("wfh", [3, 4], "03a_0001_c000")]
        final = segmentation.finalize_consolidated(
            [self.consolidated(
                ["03a_0000_c000", "03a_0001_c000"],
                "remote_desk_professionals")],
            provisional)
        self.assertEqual(final[0]["slug"], "remote_desk_professionals")
        self.assertEqual(final[0]["segment_id"], "seg_001")
        self.assertEqual(final[0]["source_candidate_ids"],
                         ["03a_0000_c000", "03a_0001_c000"])
        self.assertEqual(final[0]["core_evidence_ids"], [1, 2, 3, 4])

    def test_03b_reports_one_or_several_invalid_machine_ids(self):
        provisional = [self.provisional("desk", candidate_id="03a_0000_c000")]
        for ids, expected in ((["03a_0000_c000", "invented"], ["invented"]),
                              (["invented_one", "invented_two"],
                               ["invented_one", "invented_two"])):
            with self.subTest(ids=ids), self.assertRaises(
                    segmentation.ConsolidationContractError) as ctx:
                segmentation.finalize_consolidated(
                    [self.consolidated(ids)], provisional)
            self.assertEqual(ctx.exception.invalid_references[0]["invalid_ids"],
                             expected)

    def test_semantic_candidate_key_rename_does_not_break_machine_lineage(self):
        provisional = [self.provisional(
            "validation_gaslit", candidate_id="03a_0000_c000")]
        renamed = json.loads(json.dumps(provisional))
        renamed[0]["candidate_key"] = "people seeking clinical validation"
        final = segmentation.finalize_consolidated(
            [self.consolidated(["03a_0000_c000"])], renamed)
        self.assertEqual(final[0]["source_candidate_ids"], ["03a_0000_c000"])

    def test_legacy_03b_is_recovered_from_exact_saved_provenance(self):
        provisional = [
            self.provisional("same_semantic_key", [1, 2], "03a_0000_c000"),
            self.provisional("same_semantic_key", [3, 4], "03a_0007_c003"),
        ]
        legacy_catalogue = [{
            "candidate_key": "same_semantic_key",
            "provenance": [
                {"chunk_id": "03a_0000", "result_index": 0},
                {"chunk_id": "03a_0007", "result_index": 3},
            ],
        }]
        legacy = self.consolidated(["unused"], "renamed_canonical_slug")
        legacy["candidate_id"] = "seg_001"
        legacy["merged_candidate_keys"] = ["same_semantic_key"]
        legacy.pop("source_candidate_ids")

        recovered = segmentation.migrate_legacy_consolidated(
            [legacy], legacy_catalogue, provisional)

        self.assertEqual(recovered[0]["segment_id"], "seg_001")
        self.assertEqual(recovered[0]["slug"], "renamed_canonical_slug")
        self.assertEqual(recovered[0]["source_candidate_ids"],
                         ["03a_0000_c000", "03a_0007_c003"])
        self.assertEqual(recovered[0]["core_evidence_ids"], [1, 2, 3, 4])

    def test_03b_post_validation_catches_provider_schema_drift(self):
        # Even if a provider ignores the enum in response_format, the local
        # deterministic validator remains authoritative.
        with self.assertRaises(segmentation.ConsolidationContractError):
            segmentation.validate_consolidated_lineage(
                [self.consolidated(["provider_invented_key"])],
                [self.provisional("desk", candidate_id="03a_0000_c000")])

    def test_03b_complete_reviewer_preserves_100_row_catalogue(self):
        provisional = [self.provisional(
            f"source_{index}", [index + 1], f"03a_0000_c{index:03d}")
            for index in range(100)]
        original_rows = []
        for index in range(100):
            row = self.consolidated(
                [f"03a_0000_c{index:03d}"], f"canonical_slug_{index}")
            row["name"] = f"Canonical audience {index}"
            original_rows.append(row)
        original_rows[2]["source_candidate_ids"] = ["invented_two"]
        original_rows[77]["source_candidate_ids"] = ["invented_seventy_seven"]
        original = {"candidates": original_rows}
        untouched_before = json.loads(json.dumps(original_rows[50]))
        repair = json.loads(json.dumps(original))
        for index in (2, 77):
            repair["candidates"][index]["source_candidate_ids"] = [
                f"03a_0000_c{index:03d}"]

        merged = segmentation.merge_consolidation_repair(original, repair)
        segmentation.validate_consolidated_lineage(
            merged["candidates"], provisional)
        final = segmentation.finalize_consolidated(
            merged["candidates"], provisional)

        self.assertEqual(len(repair["candidates"]), 100)
        self.assertEqual(len(merged["candidates"]), 100)
        self.assertEqual(len(final), 100)
        self.assertEqual(merged["candidates"][50], untouched_before)
        self.assertEqual(merged["candidates"][2]["slug"], "canonical_slug_2")
        self.assertEqual(merged["candidates"][77]["slug"],
                         "canonical_slug_77")
        self.assertEqual(merged["candidates"][2]["source_candidate_ids"],
                         ["03a_0000_c002"])
        self.assertEqual(merged["candidates"][77]["source_candidate_ids"],
                         ["03a_0000_c077"])

    def test_03b_partial_reviewer_output_cannot_replace_collection(self):
        first = self.consolidated(["invalid_one"], "first")
        second = self.consolidated(["invalid_two"], "second")
        repair = json.loads(json.dumps(first))
        repair["source_candidate_ids"] = ["03a_0000_c000"]

        with self.assertRaisesRegex(ValueError, "CATASTROPHIC DROP"):
            segmentation.merge_consolidation_repair(
                {"candidates": [first, second]}, {"candidates": [repair]})

    def test_03c_keeps_recurring_novelty_and_ignores_isolated_proposal(self):
        audits = [
            {"evidence_id": 1, "status": "possible_new_candidate",
             "candidate_key": "dental_professionals", "provisional_name": "Dentists",
             "audience_cue": "clinical posture", "commercial_distinction": "chairside work",
             "origin_chunk": "03c_0000"},
            {"evidence_id": 2, "status": "possible_new_candidate",
             "candidate_key": "dental_professionals", "provisional_name": "Dental staff",
             "audience_cue": "clinical posture", "commercial_distinction": "chairside work",
             "origin_chunk": "03c_0001"},
            {"evidence_id": 3, "status": "possible_new_candidate",
             "candidate_key": "one_off", "provisional_name": "One person",
             "audience_cue": "isolated", "commercial_distinction": "none",
             "origin_chunk": "03c_0001"},
        ]
        novelty = segmentation.novelty_catalogue(audits)
        self.assertEqual([row["candidate_key"] for row in novelty],
                         ["dental_professionals"])
        self.assertEqual(novelty[0]["candidate_id"], "03c_novel_c000")
        self.assertEqual(novelty[0]["evidence_ids"], [1, 2])

    def test_counts_and_representatives_are_computed_deterministically(self):
        candidates = [{
            "segment_id": "seg_001", "slug": "desk", "name": "Desk",
            "definition": "desk", "commercial_distinction": "work",
            "inclusion_criteria": [], "exclusion_criteria": [],
            "source_candidate_ids": ["03a_0000_c000"], "merged_aliases": [],
            "core_evidence_ids": [2, 1], "discovery_status": "strong_candidate"}]
        rows = [record(1, "core", thread="t1", subreddit="a"),
                record(2, "core", thread="t2", subreddit="a"),
                record(3, "supporting", thread="t2", subreddit="b"),
                record(4, "context")]
        matches = [
            {"evidence_id": 1, "segment_ids": ["seg_001"], "match_strength": "strong"},
            {"evidence_id": 2, "segment_ids": ["seg_001"], "match_strength": "strong"},
            {"evidence_id": 3, "segment_ids": ["seg_001"],
             "match_strength": "corroborating"},
            {"evidence_id": 4, "segment_ids": ["seg_001"],
             "match_strength": "corroborating"},
        ]
        first = segmentation.assemble_candidate_evidence(candidates, matches, rows)
        second = segmentation.assemble_candidate_evidence(candidates, matches, rows)
        self.assertEqual(first, second)
        card = first[0]
        self.assertEqual((card["core_evidence_count"],
                          card["supporting_evidence_count"],
                          card["context_evidence_count"]), (2, 1, 1))
        self.assertEqual(card["unique_thread_count"], 2)
        self.assertEqual(card["unique_subreddit_count"], 2)
        self.assertTrue(card["representative_evidence_ids"])

    def test_mixed_audience_fixture_keeps_groups_separate_and_unrelated_unassigned(self):
        fixture_path = os.path.join(
            ROOT, "tests", "fixtures", "03a_mixed_audiences.json")
        with open(fixture_path, encoding="utf-8") as fh:
            fixture = json.load(fh)
        ids = [row["id"] for row in fixture["records"]]

        segmentation.validate_harvest_rows(fixture["candidates"], ids)
        claimed = {eid for row in fixture["candidates"] for eid in row["evidence_ids"]}
        self.assertEqual(len(fixture["candidates"]), 3)
        self.assertEqual(claimed, set(ids) - set(fixture["unassigned_evidence_ids"]))
        self.assertNotIn(999, claimed)
        self.assertEqual(segmentation.harvest_full_chunk_claims([{
            "chunk_id": "03a_0000", "evidence_ids": ids,
            "candidates": fixture["candidates"],
        }]), [])

    def test_harvest_prompt_says_search_not_assign_and_renders_all_evidence(self):
        rows = [record(1, text="desk worker"), record(2, text="unrelated parcel")]
        prompt = segmentation.harvest_prompt(rows)
        self.assertIn("zero or more recurring audience patterns", prompt)
        self.assertIn("chunk is not itself a segment", prompt)
        self.assertIn("leave unrelated evidence unassigned", prompt)
        self.assertIn("[1] [core] desk worker", prompt)
        self.assertIn("[2] [core] unrelated parcel", prompt)

    def test_03a_code_does_not_interpret_semantic_labels(self):
        base = {
            "candidate_key": "desk_workers", "provisional_name": "Desk workers",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": [1], "cue_terms": ["desk"],
            "discovery_strength": "probable",
        }
        for key in ("candidate", "renamed semantic concept", "Desk Workers"):
            with self.subTest(candidate_key=key):
                row = {**base, "candidate_key": key}
                segmentation.validate_harvest_rows([row], [1])
                assigned = segmentation.assign_harvest_candidate_ids(
                    [row], "03a_0007")
                self.assertEqual(assigned[0]["candidate_id"], "03a_0007_c000")

    def test_03a_evidence_invariants_allow_overlap_and_no_total_coverage(self):
        rows = [{
            "candidate_key": "desk_workers", "provisional_name": "Desk workers",
            "audience_cue": "desk", "why_commercially_distinct": "work",
            "evidence_ids": [1, 2], "cue_terms": ["desk"],
            "discovery_strength": "strong"}, {
            "candidate_key": "remote_workers", "provisional_name": "Remote workers",
            "audience_cue": "home office", "why_commercially_distinct": "remote setup",
            "evidence_ids": [2, 3], "cue_terms": ["home office"],
            "discovery_strength": "probable"}]
        segmentation.validate_harvest_rows(rows, [1, 2, 3, 4])
        self.assertNotIn(4, {eid for row in rows for eid in row["evidence_ids"]})

        for evidence_ids in ([], [1, 1], [1, 99]):
            with self.subTest(evidence_ids=evidence_ids), self.assertRaises(ValueError):
                segmentation.validate_harvest_rows(
                    [{**rows[0], "evidence_ids": evidence_ids}], [1, 2, 3, 4])

    def test_aggregate_reports_specialized_contract_error_not_plain_value_error(self):
        result = {
            "chunk_id": "03a_0004", "evidence_ids": [1, 2],
            "candidates": [self.harvest_candidate(
                "strength_training_pain_resolvers", [1, 999])],
        }
        with self.assertRaises(segmentation.HarvestContractError) as ctx:
            segmentation.aggregate_harvest([result])
        self.assertEqual(ctx.exception.chunk_id, "03a_0004")
        self.assertEqual(ctx.exception.candidate,
                         "strength_training_pain_resolvers")
        self.assertEqual(ctx.exception.invalid_evidence_ids, [999])

    def test_single_meaningful_full_chunk_claim_is_review_signal_not_rejection(self):
        candidate = {
            "candidate_key": "dental_professionals",
            "provisional_name": "Dental professionals",
            "audience_cue": "chairside work",
            "why_commercially_distinct": "profession-specific positioning",
            "evidence_ids": [1, 2], "cue_terms": ["dentist"],
            "discovery_strength": "strong"}
        segmentation.validate_harvest_rows([candidate], [1, 2])
        claims = segmentation.harvest_full_chunk_claims([{
            "chunk_id": "03a_0000", "evidence_ids": [1, 2],
            "candidates": [candidate]}])
        self.assertEqual(claims[0]["candidate_key"], "dental_professionals")

    def test_stage04_packet_contains_metrics_and_bounded_representatives_only(self):
        rows = [record(n, text=("REP" if n == 1 else f"RAW_SECRET_{n}"))
                for n in range(1, 20)]
        candidate = {
            "segment_id": "seg_001", "representative_evidence_ids": [1],
            "core_evidence_count": 19, "supporting_evidence_count": 0,
            "context_evidence_count": 0, "unique_thread_count": 0,
            "unique_subreddit_count": 0}
        packet = segmentation.stage04_packet([candidate], rows)
        self.assertIn('"core_evidence_count": 19', packet)
        self.assertIn("REP", packet)
        self.assertNotIn("RAW_SECRET_2", packet)


class SegmentationArtifactTests(unittest.TestCase):
    def test_03a_contract_version_invalidates_old_fingerprint(self):
        job = Job(id="03a_0000", prompt="same", schema=segmentation.HARVEST_SCHEMA)
        old = cli._job_fingerprint(job, [1, 2], "skill")
        current = cli._job_fingerprint(
            job, [1, 2], "skill", segmentation.HARVEST_CONTRACT_VERSION)
        self.assertNotEqual(old, current)

    def test_completed_chunk_is_reused_without_model_call(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "estimated_tokens": 4, "records": [record(1), record(2)]}
        job = Job(id="03a_0000", prompt="harvest", max_tokens=12000,
                  schema=segmentation.HARVEST_SCHEMA)
        with tempfile.TemporaryDirectory() as tmp:
            saved = {"chunk_id": job.id, "evidence_ids": [1, 2],
                     "estimated_input_tokens": 4,
                     "fingerprint": cli._job_fingerprint(job, [1, 2], "skill"),
                     "candidates": []}
            cli._json_atomic(os.path.join(tmp, job.id + ".json"), saved)
            client = mock.Mock()
            results = cli._run_persisted_segment_jobs(
                client, "skill", [job], [chunk], "candidates", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03A")
        self.assertEqual(results, [saved])
        client.batch.assert_not_called()
        client.estimate.assert_not_called()

    def test_03a_budget_exhaustion_uses_adaptive_retry_policy(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "estimated_tokens": 4, "records": [record(1), record(2)]}
        job = Job(id="03a_0000", prompt="harvest", max_tokens=12000,
                  schema=segmentation.HARVEST_SCHEMA)
        payload = {"candidates": [{
            "candidate_key": "desk", "provisional_name": "Desk",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": [1, 2], "cue_terms": ["desk"],
            "discovery_strength": "strong"}]}

        class Client:
            def __init__(self):
                self.attempts = []

            def estimate(self, *args, **kwargs):
                return mock.Mock()

            def prewarm(self, *args, **kwargs):
                pass

            def batch(self, _corpus, _preamble, jobs):
                current = jobs[0]
                self.attempts.append(current.max_tokens)
                if current.max_tokens == 12000:
                    return {current.id: BatchResult("", "length", 12000, 12000)}
                return {current.id: BatchResult(json.dumps(payload), "stop")}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            client = Client()
            result = cli._run_persisted_segment_jobs(
                client, "skill", [job], [chunk], "candidates", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03A")
        self.assertEqual(client.attempts, [12000, 16000])
        self.assertEqual(result[0]["candidates"][0]["candidate_key"], "desk")

    def test_03a_complete_malformed_json_uses_shape_repair(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "estimated_tokens": 4, "records": [record(1), record(2)]}
        job = Job(id="03a_0000", prompt="harvest", max_tokens=12000,
                  schema=segmentation.HARVEST_SCHEMA)

        class Client:
            def __init__(self):
                self.calls = 0

            def estimate(self, *args, **kwargs):
                return mock.Mock()

            def prewarm(self, *args, **kwargs):
                pass

            def batch(self, _corpus, _preamble, jobs):
                self.calls += 1
                current = jobs[0]
                if self.calls == 1:
                    return {current.id: BatchResult("complete prose", "stop")}
                return {current.id: BatchResult('{"candidates":[]}', "stop")}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            client = Client()
            result = cli._run_persisted_segment_jobs(
                client, "skill", [job], [chunk], "candidates", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03A")
        self.assertEqual(client.calls, 2)
        self.assertEqual(result[0]["candidates"], [])

    def test_03a_semantic_label_is_not_rewritten_by_local_code(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2, 3],
                 "estimated_tokens": 4,
                 "records": [record(1), record(2), record(3)]}
        job = Job(id="03a_0000", prompt="search audiences", max_tokens=12000,
                  schema=segmentation.HARVEST_SCHEMA)
        bad = {"candidates": [{
            "candidate_key": "audience", "provisional_name": "audience",
            "audience_cue": "whole chunk", "why_commercially_distinct": "audience",
            "evidence_ids": [1, 2, 3], "cue_terms": ["audience"],
            "discovery_strength": "strong"}]}
        good = {"candidates": [{
            "candidate_key": "desk_workers", "provisional_name": "Desk workers",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": [1, 2], "cue_terms": ["desk"],
            "discovery_strength": "probable"}]}

        class Client:
            def __init__(self):
                self.calls = 0

            def estimate(self, *args, **kwargs):
                return mock.Mock()

            def prewarm(self, *args, **kwargs):
                pass

            def batch(self, _corpus, _preamble, jobs):
                self.calls += 1
                payload = bad if self.calls == 1 else good
                return {jobs[0].id: BatchResult(json.dumps(payload), "stop")}

        def validator(_job, rows):
            segmentation.validate_harvest_rows(rows, chunk["evidence_ids"])

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            client = Client()
            result = cli._run_persisted_segment_jobs(
                client, "skill", [job], [chunk], "candidates", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03A",
                row_validator=validator, repair_guidance="regenerate semantically",
                contract_version=segmentation.HARVEST_CONTRACT_VERSION)
            with open(os.path.join(tmp, "03a_0000.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result[0]["candidates"], bad["candidates"])
        self.assertEqual(saved["candidates"], bad["candidates"])

    def test_fresh_provenance_only_violation_adds_no_repair_model_call(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "estimated_tokens": 4, "records": [record(1), record(2)]}
        job = Job(id=chunk["chunk_id"], prompt="search audiences", max_tokens=12000,
                  schema=segmentation.harvest_schema(chunk["evidence_ids"]))
        candidate = {
            "candidate_key": "desk_workers", "provisional_name": "Desk workers",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": [1, 999], "cue_terms": ["desk"],
            "discovery_strength": "probable",
        }

        class Client:
            def __init__(self):
                self.calls = 0

            def estimate(self, *_args, **_kwargs):
                return mock.Mock()

            def prewarm(self, *_args, **_kwargs):
                pass

            def batch(self, _corpus, _preamble, work):
                self.calls += 1
                return {work[0].id: BatchResult(
                    json.dumps({"candidates": [candidate]}), "stop")}

        def cleaner(current_job, rows):
            return segmentation.clean_harvest_provenance(
                rows, chunk["evidence_ids"], chunk_id=current_job.id)

        def validator(current_job, rows):
            segmentation.validate_harvest_rows(
                rows, chunk["evidence_ids"], chunk_id=current_job.id)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            client = Client()
            result = cli._run_persisted_segment_jobs(
                client, "skill", [job], [chunk], "candidates", tmp,
                os.path.join(tmp, "failures"), SimpleNamespace(yes=True), "03A",
                row_validator=validator, row_cleaner=cleaner,
                repair_guidance="repair broader contract errors",
                contract_version=segmentation.HARVEST_CONTRACT_VERSION)
        self.assertEqual(client.calls, 1)  # discovery only; no repair request
        self.assertEqual(result[0]["candidates"][0]["evidence_ids"], [1])

    def test_cached_all_outside_ids_routes_to_structured_repair(self):
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "estimated_tokens": 4, "records": [record(1), record(2)]}
        job = Job(id=chunk["chunk_id"], prompt="search audiences", max_tokens=12000,
                  schema=segmentation.harvest_schema(chunk["evidence_ids"]))

        def candidate(evidence_ids):
            return {
                "candidate_key": "desk_workers", "provisional_name": "Desk workers",
                "audience_cue": "desk work",
                "why_commercially_distinct": "workday",
                "evidence_ids": evidence_ids, "cue_terms": ["desk"],
                "discovery_strength": "probable",
            }

        class Client:
            def __init__(self):
                self.calls = 0

            def estimate(self, *_args, **_kwargs):
                return mock.Mock()

            def prewarm(self, *_args, **_kwargs):
                pass

            def batch(self, _corpus, _preamble, work):
                self.calls += 1
                return {work[0].id: BatchResult(json.dumps(
                    {"candidates": [candidate([1])]}), "stop")}

        def cleaner(current_job, rows):
            return segmentation.clean_harvest_provenance(
                rows, chunk["evidence_ids"], chunk_id=current_job.id)

        def validator(current_job, rows):
            segmentation.validate_harvest_rows(
                rows, chunk["evidence_ids"], chunk_id=current_job.id)

        with tempfile.TemporaryDirectory() as tmp:
            saved = {
                "chunk_id": job.id, "evidence_ids": chunk["evidence_ids"],
                "estimated_input_tokens": 4,
                "fingerprint": cli._job_fingerprint(
                    job, chunk["evidence_ids"], "skill",
                    segmentation.HARVEST_CONTRACT_VERSION),
                "candidates": [candidate([999])],
            }
            cli._json_atomic(os.path.join(tmp, job.id + ".json"), saved)
            output = io.StringIO()
            client = Client()
            with mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch("sys.stdout", output):
                result = cli._run_persisted_segment_jobs(
                    client, "skill", [job], [chunk], "candidates", tmp,
                    os.path.join(tmp, "failures"), SimpleNamespace(yes=True),
                    "03A", row_validator=validator, row_cleaner=cleaner,
                    repair_guidance="repair unsupported candidate",
                    contract_version=segmentation.HARVEST_CONTRACT_VERSION)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result[0]["candidates"], [candidate([1])])
        self.assertIn("03A PROVENANCE FAILURE", output.getvalue())
        self.assertIn("all cited evidence IDs were outside the chunk",
                      output.getvalue())

    def test_26_good_cached_chunks_and_one_provenance_cleanup_use_no_model(self):
        chunks = [{
            "chunk_id": f"03a_{n:04d}", "evidence_ids": [n + 1],
            "estimated_tokens": 4, "records": [record(n + 1)],
        } for n in range(27)]
        jobs = [Job(
            id=chunk["chunk_id"],
            prompt=segmentation.harvest_prompt(chunk["records"]), max_tokens=12000,
            schema=segmentation.harvest_schema(chunk["evidence_ids"]))
            for chunk in chunks]
        legacy_jobs = [dataclasses.replace(
            job, prompt=segmentation.legacy_harvest_prompt_v1(chunk["records"]),
            schema=segmentation.legacy_harvest_schema_v1())
            for job, chunk in zip(jobs, chunks)]
        legacy_v2_jobs = [dataclasses.replace(
            job, schema=segmentation.legacy_harvest_schema_v2(
                chunk["evidence_ids"])) for job, chunk in zip(jobs, chunks)]
        compatible_fingerprints = {
            job.id: {cli._legacy_job_fingerprint_v1(
                job, chunk["evidence_ids"],
                segmentation.LEGACY_HARVEST_SKILL_V1),
                cli._job_fingerprint(
                    legacy_v2, chunk["evidence_ids"],
                    segmentation.LEGACY_HARVEST_SKILL_V2,
                    segmentation.HARVEST_CONTRACT_VERSION)}
            for job, legacy_v2, chunk in zip(
                legacy_jobs, legacy_v2_jobs, chunks)}
        allowed = {chunk["chunk_id"]: chunk["evidence_ids"] for chunk in chunks}

        def candidate(evidence_ids):
            return {
                "candidate_key": "desk_workers",
                "provisional_name": "Desk workers",
                "audience_cue": "desk work",
                "why_commercially_distinct": "workday",
                "evidence_ids": list(evidence_ids), "cue_terms": ["desk"],
                "discovery_strength": "probable",
            }

        def validator(job, rows):
            segmentation.validate_harvest_rows(
                rows, allowed[job.id], chunk_id=job.id)

        def cleaner(job, rows):
            return segmentation.clean_harvest_provenance(
                rows, allowed[job.id], chunk_id=job.id)

        with tempfile.TemporaryDirectory() as tmp:
            for job, chunk in zip(jobs, chunks):
                evidence_ids = ([chunk["evidence_ids"][0], 999]
                                if job.id == "03a_0026"
                                else chunk["evidence_ids"])
                saved = {
                    "chunk_id": job.id,
                    "evidence_ids": chunk["evidence_ids"],
                    "estimated_input_tokens": 4,
                    "fingerprint": (
                        cli._legacy_job_fingerprint_v1(
                            legacy_jobs[0], chunk["evidence_ids"],
                            segmentation.LEGACY_HARVEST_SKILL_V1)
                        if job.id == "03a_0000" else
                        cli._job_fingerprint(
                            legacy_v2_jobs[int(job.id[-4:])],
                            chunk["evidence_ids"],
                            segmentation.LEGACY_HARVEST_SKILL_V2,
                            segmentation.HARVEST_CONTRACT_VERSION)),
                    "candidates": [candidate(evidence_ids)],
                }
                if job.id == "03a_0000":
                    saved["candidates"][0]["candidate_key"] = "DESK_WORKERS"
                cli._json_atomic(os.path.join(tmp, job.id + ".json"), saved)

            client = mock.Mock()
            output = io.StringIO()
            with mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch("sys.stdout", output):
                results = cli._run_persisted_segment_jobs(
                    client, "skill", jobs, chunks, "candidates", tmp,
                    os.path.join(tmp, "failures"), SimpleNamespace(yes=True),
                    "03A", row_validator=validator,
                    row_cleaner=cleaner,
                    repair_guidance="preserve candidates and valid IDs",
                    contract_version=segmentation.HARVEST_CONTRACT_VERSION,
                    compatible_fingerprints=compatible_fingerprints,
                    cached_migrator=lambda job, rows, _fingerprint:
                        segmentation.assign_harvest_candidate_ids(
                            segmentation.migrate_legacy_harvest_rows(rows), job.id))

            client.estimate.assert_not_called()
            client.prewarm.assert_not_called()
            client.batch.assert_not_called()
            self.assertEqual(results[26]["candidates"][0]["evidence_ids"], [27])
            self.assertNotIn("03A CONTRACT ERROR", output.getvalue())
            for n, job in enumerate(jobs[:26]):
                with open(os.path.join(tmp, job.id + ".json"),
                          encoding="utf-8") as fh:
                    migrated = json.load(fh)
                expected = candidate([n + 1])
                if n == 0:
                    expected["candidate_key"] = "DESK_WORKERS"
                expected["candidate_id"] = f"03a_{n:04d}_c000"
                self.assertEqual(migrated["candidates"], [expected])
                self.assertIn("migrated_from_fingerprint", migrated)
            with open(os.path.join(tmp, "03a_0026.json"), encoding="utf-8") as fh:
                cleaned = json.load(fh)
            expected = candidate([27])
            expected["candidate_id"] = "03a_0026_c000"
            self.assertEqual(cleaned["candidates"], [expected])
            self.assertEqual(
                cleaned["provenance_cleanup"][0]["removed_evidence_ids"], [999])

    def test_stage06_preserves_evidence_tier_in_file_and_manifest(self):
        segment = {"segment_id": "seg_001", "slug": "desk", "name": "Desk",
                   "definition": "desk context",
                   "inclusion_criteria": ["desk"], "exclusion_criteria": []}
        assignment = {"evidence_id": 1, "primary_segment_id": "seg_001", "score": 8,
                      "winning_margin": 3, "cue_types": ["dominant_context_match"],
                      "primary_cues": ["desk"], "rationale": "dominant",
                      "assignment_status": "assigned", "secondary_attributes": []}
        source = {"id": 1, "text": "I hurt after desk work", "tier": "supporting",
                  "thread_id": "t1", "url": "https://example.test", "title": "title"}
        with tempfile.TemporaryDirectory() as tmp:
            voc = os.path.join(tmp, "research", "voc")
            os.makedirs(voc)
            with mock.patch.object(cli, "record_provenance"):
                cli.build_evidence_files({"_dir": tmp}, [segment], [assignment],
                                         {1: source}, voc)
            with open(os.path.join(tmp, "research", "evidence", "desk.txt"),
                      encoding="utf-8") as fh:
                evidence = fh.read()
            with open(os.path.join(voc, "segment_evidence_manifest.yaml"),
                      encoding="utf-8") as fh:
                manifest = fh.read()
        self.assertIn("EVIDENCE TIER: supporting", evidence)
        self.assertIn("Segment ID: seg_001", evidence)
        self.assertIn("supporting_count: 1", manifest)
        self.assertIn("core_count: 0", manifest)


class SegmentCommandIntegrationTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, fail_on_call=False):
            self.fail_on_call = fail_on_call
            self.calls = []
            self.max_tokens_by_job = {}

        def estimate(self, *args, **kwargs):
            if self.fail_on_call:
                raise AssertionError("completed segmentation step was rerun")
            return mock.Mock()

        def prewarm(self, *args, **kwargs):
            if self.fail_on_call:
                raise AssertionError("completed segmentation step was rerun")

        def batch(self, corpus, preamble, jobs):
            if self.fail_on_call:
                raise AssertionError("completed segmentation step was rerun")
            self.calls.extend(job.id for job in jobs)
            replies = {}
            for job in jobs:
                if job.id.startswith("03a_"):
                    self.max_tokens_by_job[job.id] = job.max_tokens
                    payload = {"candidates": [{
                        "candidate_key": "desk_workers",
                        "provisional_name": "Desk workers",
                        "audience_cue": "desk work dominates the problem context",
                        "why_commercially_distinct": "workday messaging",
                        "evidence_ids": list(job.expected_ids or (1, 2, 3)),
                        "cue_terms": ["desk", "computer"],
                        "discovery_strength": "strong"}]}
                elif job.id.startswith("03e_"):
                    payload = {"matches": [{
                        "evidence_id": eid, "segment_ids": ["seg_001"],
                        "match_strength": "strong" if eid in (1, 2, 3)
                        else "corroborating"} for eid in job.expected_ids]}
                elif job.id.startswith("05_"):
                    payload = {"assignments": [{
                        "evidence_id": eid, "primary_segment_id": "seg_001",
                        "score": 8, "winning_margin": 3,
                        "cue_types": ["dominant_context_match"],
                        "primary_cues": ["desk"], "rationale": "dominant context",
                        "assignment_status": "assigned", "secondary_attributes": []}
                        for eid in job.expected_ids]}
                else:
                    raise AssertionError(f"unexpected batch schema for {job.id}")
                replies[job.id] = BatchResult(json.dumps(payload), "stop")
            return replies

        def one_result(self, corpus, preamble, prompt, max_tokens, schema, **kwargs):
            if self.fail_on_call:
                raise AssertionError("completed segmentation step was rerun")
            job_id = kwargs.get("job_id")
            self.calls.append(job_id)
            self.max_tokens_by_job[job_id] = max_tokens
            if job_id in ("03b_consolidate", "03b_novelty_consolidate"):
                payload = {"candidates": [{
                    "slug": "desk_workers",
                    "name": "Desk workers", "definition": "Desk work is dominant",
                    "commercial_distinction": "workday ergonomic messaging",
                    "inclusion_criteria": ["desk work is dominant"],
                    "exclusion_criteria": ["incidental desk mention"],
                    "source_candidate_ids": ["03a_0000_c000"],
                    "merged_aliases": [],
                    "discovery_status": "strong_candidate"}]}
            elif job_id == "04_validate":
                payload = {"decisions": [{
                    "segment_id": "seg_001", "status": "validated",
                    "rationale": "independent recurring context",
                    "merged_into": "", "split_into": []}]}
            else:
                raise AssertionError(f"unexpected single schema for {job_id}")
            return BatchResult(json.dumps(payload), "stop")

    @staticmethod
    def args():
        return SimpleNamespace(
            source=None, rediscover=False, reassign=False, from_stage=None,
            novelty_supporting=False, segment_debug=False, yes=True,
            provider=None, model=None, effort=None)

    def project(self, root):
        voc = os.path.join(root, "research", "voc")
        os.makedirs(voc)
        rows = [record(1, "core", "I hurt after computer work", "t1", "office"),
                record(2, "core", "My desk job causes this", "t2", "pain"),
                record(3, "core", "WFH sitting triggers it", "t3", "office"),
                record(4, "supporting", "Ergonomic advice can help", "t4", "office"),
                record(5, "context", "What about desk posture?", "t5", "pain")]
        cli._write_jsonl(os.path.join(voc, cli.PRODUCTION_VOC_FILE), rows)
        cli._write_jsonl(os.path.join(voc, cli.AUDIT_VOC_FILE), rows)
        return {"_dir": root, "name": "test", "segmentation": {
            "03a_chunk_tokens": 10000, "03e_chunk_tokens": 10000,
            "03c_chunk_tokens": 10000, "05_chunk_tokens": 10000}}

    def test_complete_pipeline_persists_contracts_and_second_run_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            first = self.FakeClient()
            with mock.patch.object(cli, "client", return_value=first), \
                    mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())

            discovery = os.path.join(tmp, "research", "segments", "discovery")
            for name in ("03a_candidate_catalogue.json",
                         "03b_consolidated_candidates.json",
                         "03_candidate_evidence.json", "03c_novelty_results.json",
                         "discovered_segments.json"):
                self.assertTrue(os.path.isfile(os.path.join(discovery, name)), name)
            with open(os.path.join(discovery, "discovered_segments.json"),
                      encoding="utf-8") as fh:
                candidates = json.load(fh)
            self.assertEqual(candidates[0]["core_evidence_count"], 3)
            self.assertEqual(candidates[0]["supporting_evidence_count"], 1)
            self.assertEqual(candidates[0]["context_evidence_count"], 1)
            self.assertTrue(candidates[0]["representative_evidence_ids"])
            self.assertEqual(first.max_tokens_by_job["03b_consolidate"], 64000)
            self.assertEqual(first.max_tokens_by_job["04_validate"], 64000)
            self.assertTrue(all(
                ceiling == 12000 for job_id, ceiling in
                first.max_tokens_by_job.items() if job_id.startswith("03a_")))

            second = self.FakeClient(fail_on_call=True)
            with mock.patch.object(cli, "client", return_value=second), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())
            self.assertEqual(second.calls, [])

    def test_interrupted_stage05_resume_reuses_chunks_without_rerunning_stage04(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            first = self.FakeClient()
            with mock.patch.object(cli, "client", return_value=first), \
                    mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())

            voc = os.path.join(tmp, "research", "voc")
            os.unlink(os.path.join(voc, "segment_assignments.jsonl"))
            os.unlink(os.path.join(voc, "segment_assignments.jsonl.meta.json"))

            resume = self.FakeClient(fail_on_call=True)
            with mock.patch.object(cli, "client", return_value=resume), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())

            self.assertEqual(resume.calls, [])


if __name__ == "__main__":
    unittest.main()
