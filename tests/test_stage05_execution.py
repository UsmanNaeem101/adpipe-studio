"""Stage 05 Anthropic waves, capacity recovery, budgets, and resumability."""

import dataclasses
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
import llm


def assignment(evidence_id):
    return {
        "evidence_id": evidence_id,
        "primary_segment_id": "seg_001",
        "score": 8,
        "winning_margin": 3,
        "cue_types": ["dominant_context_match"],
        "primary_cues": ["desk"],
        "rationale": "dominant context",
        "assignment_status": "assigned",
        "secondary_attributes": [],
    }


def stage05_job(index):
    evidence_id = index + 1
    return llm.Job(
        id=f"05_{index:04d}", prompt=f"assign evidence {evidence_id}",
        max_tokens=16000,
        schema=cli.assignment_schema([evidence_id], ["seg_001"]),
        expected_ids=(evidence_id,), effort="high")


def success(job):
    payload = {"assignments": [assignment(job.expected_ids[0])]}
    return llm.BatchResult(json.dumps(payload), "end_turn", 1000, 2000,
                           "Anthropic")


def assignments_result(rows, wrapper="assignments"):
    return llm.BatchResult(
        json.dumps({wrapper: rows}), "end_turn", 1000, 2000, "Anthropic")


def grammar_limit(job):
    return llm.BatchResult(
        "", "batch_errored", provider="Anthropic", provider_error={
            "type": "invalid_request_error",
            "message": ("Grammar compilation rate limit exceeded. Organization "
                        "exceeded limit: 20 compilations per minute."),
            "custom_id": job.id,
            "batch_id": "batch_test",
        })


class ScriptedClient:
    def __init__(self, handler, native=True):
        self.handler = handler
        self.native_anthropic_batches = native
        self.batch_calls = []
        self.prewarm_calls = 0

    def batch(self, corpus, preamble, jobs):
        self.batch_calls.append(list(jobs))
        return {job.id: self.handler(job, len(self.batch_calls)) for job in jobs}

    def estimate(self, *args, **kwargs):
        return mock.Mock()

    def prewarm(self, *args, **kwargs):
        self.prewarm_calls += 1


class Stage05ExecutionTests(unittest.TestCase):
    def test_wrapper_only_drift_is_normalized_without_changing_rows(self):
        rows = [assignment(1), assignment(2)]
        payloads = [
            rows,
            {"results": rows},
            {"metadata": {"model": "haiku"}, "result": {"rows": rows}},
            [{"payload": {"assignments": rows}}],
            {"assignments": {"assignments": rows}},
            {"left": rows, "duplicate": rows},
        ]

        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__):
                normalized, event = cli._normalize_stage05_assignment_wrapper(
                    json.dumps(payload))
                self.assertIsNotNone(event)
                self.assertEqual(json.loads(normalized), {"assignments": rows})

    def test_ambiguous_different_assignment_arrays_are_not_normalized(self):
        raw = json.dumps({
            "first": [assignment(1)],
            "second": [assignment(2)],
        })

        normalized, event = cli._normalize_stage05_assignment_wrapper(raw)

        self.assertEqual(normalized, raw)
        self.assertIsNone(event)

    def test_incomplete_semantic_content_is_not_normalized_as_wrapper_drift(self):
        raw = json.dumps({"assignments": [assignment(1)]})

        normalized, event = cli._normalize_stage05_assignment_wrapper(raw)

        self.assertEqual(normalized, raw)
        self.assertIsNone(event)

    def test_runner_normalizes_wrapper_only_response_with_zero_extra_calls(self):
        job = stage05_job(0)
        client = ScriptedClient(
            lambda _job, _call: assignments_result(
                [assignment(1)], wrapper="results"), native=False)
        runner = cli._Stage05BatchRunner(
            client, cli.Stage05ExecutionStats(), interval_seconds=0)

        replies = runner("prefix", "preamble", [job])

        self.assertEqual(len(client.batch_calls), 1)
        self.assertEqual(
            cli._decode_job_rows(job, replies[job.id].text, "assignments"),
            [assignment(1)])
        self.assertIn(job.id, runner.normalization_events)

    def test_53_native_anthropic_jobs_are_split_into_safe_waves(self):
        jobs = [stage05_job(index) for index in range(53)]
        client = ScriptedClient(lambda job, _call: success(job))
        stats = cli.Stage05ExecutionStats()
        runner = cli._Stage05BatchRunner(
            client, stats, interval_seconds=0)

        replies = runner("cached prefix", "preamble", jobs)

        self.assertEqual([len(wave) for wave in client.batch_calls],
                         [10, 10, 10, 10, 10, 3])
        self.assertEqual(set(replies), {job.id for job in jobs})
        self.assertTrue(all(len(wave) <= 10 for wave in client.batch_calls))

    def test_grammar_compilation_limit_retries_only_affected_job(self):
        jobs = [stage05_job(0), stage05_job(1)]

        def handler(job, call):
            if job.id == "05_0001" and call == 1:
                return grammar_limit(job)
            return success(job)

        client = ScriptedClient(handler)
        stats = cli.Stage05ExecutionStats()
        runner = cli._Stage05BatchRunner(
            client, stats, interval_seconds=0, grammar_retry_attempts=2)

        replies = runner("cached prefix", "preamble", jobs)

        self.assertEqual([[job.id for job in wave]
                          for wave in client.batch_calls],
                         [["05_0000", "05_0001"], ["05_0001"]])
        self.assertEqual(stats.grammar_rate_limit_retries, 1)
        self.assertEqual(stats.grammar_retried_ids, {"05_0001"})
        self.assertEqual(replies["05_0001"].stop_reason, "end_turn")

    def test_grammar_compilation_retries_are_bounded(self):
        job = stage05_job(0)
        client = ScriptedClient(lambda current, _call: grammar_limit(current))
        stats = cli.Stage05ExecutionStats()
        runner = cli._Stage05BatchRunner(
            client, stats, interval_seconds=0, grammar_retry_attempts=2)

        replies = runner("cached prefix", "preamble", [job])

        self.assertEqual(len(client.batch_calls), 3)
        self.assertEqual(stats.grammar_rate_limit_retries, 2)
        self.assertTrue(cli._stage05_grammar_rate_limited(replies[job.id]))

    def test_stage05_output_exhaustion_promotes_16k_to_24k(self):
        request = stage05_job(0)

        def handler(job, _call):
            if job.max_tokens == 16000:
                return llm.BatchResult("", "max_tokens", 12000, 16000,
                                       "Anthropic")
            return success(job)

        client = ScriptedClient(handler, native=False)
        with tempfile.TemporaryDirectory() as tmp:
            results, final_jobs = cli._run_adaptive_stage(
                client, "prefix", "preamble", [request], tmp,
                stage="05", key="assignments",
                token_tiers=cli.STAGE05_TOKEN_TIERS)

        self.assertEqual([wave[0].max_tokens for wave in client.batch_calls],
                         [16000, 24000])
        self.assertEqual(final_jobs[0].max_tokens, 24000)
        self.assertEqual(results[request.id].stop_reason, "end_turn")

    def test_stage05_32k_output_exhaustion_is_terminal(self):
        request = stage05_job(0)
        request = dataclasses.replace(request, max_tokens=32000)
        client = ScriptedClient(
            lambda _job, _call: llm.BatchResult(
                "partial", "max_tokens", 25000, 32000, "Anthropic"),
            native=False)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError):
                cli._run_adaptive_stage(
                    client, "prefix", "preamble", [request], tmp,
                    stage="05", key="assignments",
                    token_tiers=cli.STAGE05_TOKEN_TIERS)
        self.assertEqual(len(client.batch_calls), 1)

    def test_interrupted_stage05_reuses_completed_and_submits_only_unfinished(self):
        jobs = [stage05_job(index) for index in range(3)]
        chunks = [{
            "chunk_id": job.id,
            "evidence_ids": list(job.expected_ids),
            "estimated_tokens": 10,
        } for job in jobs]
        args = SimpleNamespace(yes=True, segment_debug=False)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            artifacts = os.path.join(tmp, "artifacts")
            diagnostics = os.path.join(tmp, "diagnostics")
            first = ScriptedClient(lambda job, _call: success(job), native=False)
            cli._run_stage05_assignments(
                first, "prefix", jobs, chunks, artifacts, diagnostics,
                args, tmp)
            os.unlink(os.path.join(artifacts, "05_0002.json"))

            second = ScriptedClient(lambda job, _call: success(job), native=False)
            result = cli._run_stage05_assignments(
                second, "prefix", jobs, chunks, artifacts, diagnostics,
                args, tmp)

        self.assertEqual([[job.id for job in wave]
                          for wave in second.batch_calls], [["05_0002"]])
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["chunk_id"], "05_0000")
        self.assertEqual(result[1]["chunk_id"], "05_0001")

    def test_incomplete_chunk_gets_one_complete_reviewer_and_is_persisted(self):
        job = llm.Job(
            id="05_0000", prompt="assign evidence 1 and 2", max_tokens=16000,
            schema=cli.assignment_schema([1, 2], ["seg_001"]),
            expected_ids=(1, 2), effort="high")
        chunk = {"chunk_id": job.id, "evidence_ids": [1, 2],
                 "estimated_tokens": 10}
        args = SimpleNamespace(yes=True, segment_debug=False)

        def handler(current, _call):
            if current.prompt.startswith("RECOVERY TASK"):
                self.assertIn("COMPLETE corrected assignment artifact",
                              current.prompt)
                reconsidered = assignment(1)
                reconsidered["rationale"] = "reviewer tried to change this"
                return assignments_result([reconsidered, assignment(2)])
            return assignments_result([assignment(1)])

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            client = ScriptedClient(handler, native=False)
            artifact_dir = os.path.join(tmp, "artifacts")
            result = cli._run_stage05_assignments(
                client, "validated catalogue", [job], [chunk], artifact_dir,
                os.path.join(tmp, "diagnostics"), args, tmp)

            resumed = ScriptedClient(
                lambda _job, _call: self.fail("completed chunk was rerun"),
                native=False)
            second = cli._run_stage05_assignments(
                resumed, "validated catalogue", [job], [chunk], artifact_dir,
                os.path.join(tmp, "diagnostics"), args, tmp)

        self.assertEqual(len(client.batch_calls), 2)
        self.assertEqual(client.batch_calls[1][0].id, job.id)
        self.assertEqual(len(result[0]["assignments"]), 2)
        self.assertEqual(result[0]["assignments"][0], assignment(1))
        self.assertTrue(result[0]["structured_repair"])
        self.assertEqual(result[0]["incomplete_coverage_merge"], {
            "kind": "incomplete_coverage",
            "preserved_existing_rows": 1,
            "imported_evidence_ids": [2],
            "overwritten_existing_rows": 0,
        })
        self.assertEqual(second[0]["assignments"], result[0]["assignments"])
        self.assertEqual(resumed.batch_calls, [])

    def test_wrong_reviewer_shape_stops_after_one_bounded_attempt(self):
        job = llm.Job(
            id="05_0000", prompt="assign evidence 1 and 2", max_tokens=16000,
            schema=cli.assignment_schema([1, 2], ["seg_001"]),
            expected_ids=(1, 2), effort="high")
        chunk = {"chunk_id": job.id, "evidence_ids": [1, 2],
                 "estimated_tokens": 10}
        args = SimpleNamespace(yes=True, segment_debug=False)
        client = ScriptedClient(
            lambda _job, _call: assignments_result([assignment(1)]),
            native=False)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True), \
                self.assertRaises(cli.BatchOutputError) as ctx:
            cli._run_stage05_assignments(
                client, "validated catalogue", [job], [chunk],
                os.path.join(tmp, "artifacts"),
                os.path.join(tmp, "diagnostics"), args, tmp)

        self.assertEqual(len(client.batch_calls), 2)
        self.assertIn("bounded repair attempts: 1/1", str(ctx.exception))

    def test_52_completed_chunks_are_reused_while_only_one_is_reviewed(self):
        jobs = [stage05_job(index) for index in range(53)]
        chunks = [{"chunk_id": job.id,
                   "evidence_ids": list(job.expected_ids),
                   "estimated_tokens": 10} for job in jobs]
        args = SimpleNamespace(yes=True, segment_debug=False)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "confirm", return_value=True):
            artifacts = os.path.join(tmp, "artifacts")
            diagnostics = os.path.join(tmp, "diagnostics")
            initial = ScriptedClient(lambda job, _call: success(job), native=False)
            cli._run_stage05_assignments(
                initial, "prefix", jobs, chunks, artifacts, diagnostics,
                args, tmp)
            os.unlink(os.path.join(artifacts, "05_0052.json"))

            resumed = ScriptedClient(lambda job, _call: success(job), native=False)
            result = cli._run_stage05_assignments(
                resumed, "prefix", jobs, chunks, artifacts, diagnostics,
                args, tmp)

        self.assertEqual([[job.id for job in call]
                          for call in resumed.batch_calls], [["05_0052"]])
        self.assertEqual(len(result), 53)

    def test_legacy_paid_success_is_migrated_from_audit_without_model_call(self):
        job = stage05_job(0)
        chunk = {"chunk_id": job.id, "evidence_ids": [1],
                 "estimated_tokens": 10}
        args = SimpleNamespace(yes=True, segment_debug=False)
        prefix = "stable assignment prefix"
        payload = {"assignments": [assignment(1)]}

        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(
                tmp, "logs", "model", "2026-08-09",
                "120000.000000_pipeline_batch_job_05_0000_test")
            os.makedirs(audit)
            with open(os.path.join(audit, "request.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({
                    "provider": "anthropic", "job_id": job.id,
                    "operation": "pipeline_batch_job",
                    "request": {
                        "system": [
                            {"type": "text", "text": cli.SEGMENT_PREAMBLE},
                            {"type": "text", "text": prefix,
                             "cache_control": {"type": "ephemeral"}},
                        ],
                        "messages": [{"role": "user", "content": job.prompt}],
                        "output_config": {"format": {
                            "type": "json_schema", "schema": job.schema}},
                    },
                }, fh)
            with open(os.path.join(audit, "response.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({
                    "text": json.dumps(payload),
                    "metadata": {"stop_reason": "end_turn", "usage": {
                        "output_tokens": 2000,
                        "output_tokens_details": {"thinking_tokens": 1000}}},
                }, fh)

            client = ScriptedClient(
                lambda _job, _call: self.fail("model call was not expected"))
            result = cli._run_stage05_assignments(
                client, prefix, [job], [chunk],
                os.path.join(tmp, "artifacts"),
                os.path.join(tmp, "diagnostics"), args, tmp)

        self.assertEqual(client.batch_calls, [])
        self.assertEqual(result[0]["assignments"], [assignment(1)])
        self.assertIn("migrated_from_model_audit", result[0])

    def test_completed_incomplete_audit_goes_directly_to_one_reviewer(self):
        job = llm.Job(
            id="05_0000", prompt="assign evidence 1 and 2", max_tokens=16000,
            schema=cli.assignment_schema([1, 2], ["seg_001"]),
            expected_ids=(1, 2), effort="high")
        chunk = {"chunk_id": job.id, "evidence_ids": [1, 2],
                 "estimated_tokens": 10}
        args = SimpleNamespace(yes=True, segment_debug=False)
        prefix = "stable assignment prefix"

        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(
                tmp, "logs", "model", "2026-08-09",
                "120000.000000_pipeline_batch_job_05_0000_incomplete")
            os.makedirs(audit)
            with open(os.path.join(audit, "request.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({
                    "provider": "anthropic", "job_id": job.id,
                    "operation": "pipeline_batch_job",
                    "request": {
                        "max_tokens": 24000,
                        "system": [
                            {"type": "text", "text": cli.SEGMENT_PREAMBLE},
                            {"type": "text", "text": prefix,
                             "cache_control": {"type": "ephemeral"}},
                        ],
                        "messages": [{"role": "user", "content": job.prompt}],
                        "output_config": {"format": {
                            "type": "json_schema", "schema": job.schema}},
                    },
                }, fh)
            with open(os.path.join(audit, "response.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({
                    "text": json.dumps({"assignments": [assignment(1)]}),
                    "metadata": {"stop_reason": "end_turn", "usage": {
                        "output_tokens": 2000,
                        "output_tokens_details": {"thinking_tokens": 1000}}},
                }, fh)

            def reviewer(current, _call):
                self.assertTrue(current.prompt.startswith("RECOVERY TASK"))
                self.assertEqual(current.max_tokens, 24000)
                return assignments_result([assignment(1), assignment(2)])

            client = ScriptedClient(reviewer, native=False)
            with mock.patch.object(llm, "confirm", return_value=True):
                result = cli._run_stage05_assignments(
                    client, prefix, [job], [chunk],
                    os.path.join(tmp, "artifacts"),
                    os.path.join(tmp, "diagnostics"), args, tmp)

        self.assertEqual(len(client.batch_calls), 1)
        self.assertEqual(len(result[0]["assignments"]), 2)
        self.assertTrue(result[0]["structured_repair"])


if __name__ == "__main__":
    unittest.main()
