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

    def test_supporting_and_context_do_not_enter_initial_discovery(self):
        rows = [record(1, "core"), record(2, "supporting"), record(3, "context")]
        self.assertEqual(
            [row["id"] for row in segmentation.initial_discovery_records(rows)], [1])
        self.assertEqual(
            [row["id"] for row in segmentation.initial_discovery_records(
                rows, include_supporting=True)], [1, 2])

    def test_03a_exact_aggregation_preserves_lineage_and_order(self):
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

        self.assertEqual(len(catalogue), 1)
        self.assertEqual(catalogue[0]["evidence_ids"], [1, 2, 3, 4])
        self.assertEqual(catalogue[0]["chunk_ids"], ["03a_0000", "03a_0001"])
        self.assertEqual(catalogue[0]["unique_evidence_count"], 4)
        self.assertEqual(len(catalogue[0]["provenance"]), 2)

    def test_03b_merges_aliases_and_code_restores_complete_evidence_union(self):
        provisional = [{
            "candidate_key": "desk", "evidence_ids": [1, 2],
            "chunk_ids": ["03a_0000"], "aliases": ["Desk workers"]}, {
            "candidate_key": "wfh", "evidence_ids": [3, 4],
            "chunk_ids": ["03a_0001"], "aliases": ["Remote workers"]}]
        model_row = {
            "candidate_id": "cand_desk", "slug": "desk_workers", "name": "Desk workers",
            "definition": "People whose workday context dominates the problem",
            "commercial_distinction": "Workday ergonomic messaging",
            "inclusion_criteria": ["desk work"], "exclusion_criteria": ["incidental desk"],
            "merged_candidate_keys": ["desk", "wfh"], "merged_aliases": ["Office staff"],
            # Deliberately incomplete: code, not model memory, owns the union.
            "core_evidence_ids": [1], "discovery_status": "strong_candidate"}

        final = segmentation.finalize_consolidated([model_row], provisional)

        self.assertEqual(final[0]["core_evidence_ids"], [1, 2, 3, 4])
        self.assertCountEqual(final[0]["merged_aliases"],
                              ["Desk workers", "Remote workers", "Office staff"])
        statuses = segmentation.CONSOLIDATE_SCHEMA["properties"]["candidates"][
            "items"]["properties"]["discovery_status"]["enum"]
        self.assertNotIn("Validated", statuses)
        self.assertNotIn("validated", statuses)

    def test_03c_keeps_recurring_novelty_and_ignores_isolated_proposal(self):
        audits = [
            {"evidence_id": 1, "status": "possible_new_candidate",
             "candidate_key": "dental_professionals", "provisional_name": "Dentists",
             "audience_cue": "clinical posture", "commercial_distinction": "chairside work",
             "origin_chunk": "03c_0000"},
            {"evidence_id": 2, "status": "possible_new_candidate",
             "candidate_key": "dental professionals", "provisional_name": "Dental staff",
             "audience_cue": "clinical posture", "commercial_distinction": "chairside work",
             "origin_chunk": "03c_0001"},
            {"evidence_id": 3, "status": "possible_new_candidate",
             "candidate_key": "one_off", "provisional_name": "One person",
             "audience_cue": "isolated", "commercial_distinction": "none",
             "origin_chunk": "03c_0001"},
        ]
        novelty = segmentation.novelty_catalogue(audits)
        self.assertEqual([row["candidate_key"] for row in novelty],
                         ["novel_dental_professionals"])
        self.assertEqual(novelty[0]["evidence_ids"], [1, 2])

    def test_counts_and_representatives_are_computed_deterministically(self):
        candidates = [{
            "candidate_id": "desk", "slug": "desk", "name": "Desk",
            "definition": "desk", "commercial_distinction": "work",
            "inclusion_criteria": [], "exclusion_criteria": [],
            "merged_candidate_keys": ["desk"], "merged_aliases": [],
            "core_evidence_ids": [2, 1], "discovery_status": "strong_candidate"}]
        rows = [record(1, "core", thread="t1", subreddit="a"),
                record(2, "core", thread="t2", subreddit="a"),
                record(3, "supporting", thread="t2", subreddit="b"),
                record(4, "context")]
        matches = [
            {"evidence_id": 1, "candidate_ids": ["desk"], "match_strength": "strong"},
            {"evidence_id": 2, "candidate_ids": ["desk"], "match_strength": "strong"},
            {"evidence_id": 3, "candidate_ids": ["desk"],
             "match_strength": "corroborating"},
            {"evidence_id": 4, "candidate_ids": ["desk"],
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

    def test_03a_semantic_guard_rejects_generic_labels(self):
        base = {
            "candidate_key": "desk_workers", "provisional_name": "Desk workers",
            "audience_cue": "desk work", "why_commercially_distinct": "workday",
            "evidence_ids": [1], "cue_terms": ["desk"],
            "discovery_strength": "probable",
        }
        for key in ("harvest_candidates_from_core", "candidate", "segment", "audience"):
            with self.subTest(candidate_key=key), self.assertRaises(ValueError):
                segmentation.validate_harvest_rows([{**base, "candidate_key": key}], [1])
        for name in ("candidate", "segment", "audience"):
            with self.subTest(provisional_name=name), self.assertRaises(ValueError):
                segmentation.validate_harvest_rows(
                    [{**base, "provisional_name": name}], [1])

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
            "candidate_id": "c", "representative_evidence_ids": [1],
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

    def test_03a_semantic_failure_is_regenerated_before_persistence(self):
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
        self.assertEqual(client.calls, 2)
        self.assertEqual(result[0]["candidates"], good["candidates"])
        self.assertEqual(saved["candidates"], good["candidates"])

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
        compatible_fingerprints = {
            job.id: {cli._legacy_job_fingerprint_v1(
                job, chunk["evidence_ids"],
                segmentation.LEGACY_HARVEST_SKILL_V1)}
            for job, chunk in zip(legacy_jobs, chunks)}
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
                    "fingerprint": next(iter(compatible_fingerprints[job.id])),
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
                    cached_migrator=lambda _job, rows, _fingerprint:
                        segmentation.migrate_legacy_harvest_rows(rows))

            client.estimate.assert_not_called()
            client.prewarm.assert_not_called()
            client.batch.assert_not_called()
            self.assertEqual(results[26]["candidates"][0]["evidence_ids"], [27])
            self.assertNotIn("03A CONTRACT ERROR", output.getvalue())
            for n, job in enumerate(jobs[:26]):
                with open(os.path.join(tmp, job.id + ".json"),
                          encoding="utf-8") as fh:
                    migrated = json.load(fh)
                self.assertEqual(migrated["candidates"], [candidate([n + 1])])
                self.assertIn("migrated_from_fingerprint", migrated)
            with open(os.path.join(tmp, "03a_0026.json"), encoding="utf-8") as fh:
                cleaned = json.load(fh)
            self.assertEqual(cleaned["candidates"], [candidate([27])])
            self.assertEqual(
                cleaned["provenance_cleanup"][0]["removed_evidence_ids"], [999])

    def test_stage06_preserves_evidence_tier_in_file_and_manifest(self):
        segment = {"slug": "desk", "name": "Desk", "definition": "desk context",
                   "inclusion_criteria": ["desk"], "exclusion_criteria": []}
        assignment = {"evidence_id": 1, "primary_segment_id": "desk", "score": 8,
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
                elif job.schema is segmentation.EXPANSION_SCHEMA:
                    payload = {"matches": [{
                        "evidence_id": eid, "candidate_ids": ["cand_desk"],
                        "match_strength": "strong" if eid in (1, 2, 3)
                        else "corroborating"} for eid in job.expected_ids]}
                elif job.schema is cli.ASSIGN_SCHEMA:
                    payload = {"assignments": [{
                        "evidence_id": eid, "primary_segment_id": "desk_workers",
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
            if schema is segmentation.CONSOLIDATE_SCHEMA:
                payload = {"candidates": [{
                    "candidate_id": "cand_desk", "slug": "desk_workers",
                    "name": "Desk workers", "definition": "Desk work is dominant",
                    "commercial_distinction": "workday ergonomic messaging",
                    "inclusion_criteria": ["desk work is dominant"],
                    "exclusion_criteria": ["incidental desk mention"],
                    "merged_candidate_keys": ["desk_workers"],
                    "merged_aliases": [], "core_evidence_ids": [1],
                    "discovery_status": "strong_candidate"}]}
            elif schema is cli.VALIDATION_SCHEMA:
                payload = {"decisions": [{
                    "slug": "desk_workers", "status": "validated",
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
            self.assertTrue(all(
                ceiling == 12000 for job_id, ceiling in
                first.max_tokens_by_job.items() if job_id.startswith("03a_")))

            second = self.FakeClient(fail_on_call=True)
            with mock.patch.object(cli, "client", return_value=second), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())
            self.assertEqual(second.calls, [])


if __name__ == "__main__":
    unittest.main()
