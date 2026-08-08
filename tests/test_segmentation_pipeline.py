"""Deterministic contracts for the multi-pass Stage 03 pipeline."""

import json
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
                "candidate_key": "Desk workers", "provisional_name": "Desk workers",
                "audience_cue": "computer work", "why_commercially_distinct": "workday",
                "evidence_ids": [3, 4], "cue_terms": ["desk"],
                "discovery_strength": "strong"}]}, {
            "chunk_id": "03a_0000", "evidence_ids": [1, 2], "candidates": [{
                "candidate_key": "desk-workers", "provisional_name": "Office workers",
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
                if job.schema is segmentation.HARVEST_SCHEMA:
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

            second = self.FakeClient(fail_on_call=True)
            with mock.patch.object(cli, "client", return_value=second), \
                    mock.patch.object(cli, "record_provenance"):
                cli.cmd_segment(cfg, self.args())
            self.assertEqual(second.calls, [])


if __name__ == "__main__":
    unittest.main()
