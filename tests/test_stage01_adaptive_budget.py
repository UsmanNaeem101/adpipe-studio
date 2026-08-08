"""Regression tests for Stage 01's live output-token tier state machine."""

import dataclasses
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm


def filter_job(index):
    evidence_id = index + 1
    return llm.Job(
        id=f"f{index:04d}", prompt=f"classify [{evidence_id}]",
        max_tokens=cli.FILTER_TOKEN_TIERS[0], schema=cli.FILTER_SCHEMA,
        expected_ids=(evidence_id,), effort="low", reasoning_max_tokens=2000)


def valid_result(job, completion_tokens=100):
    return llm.BatchResult(json.dumps({"records": [{
        "evidence_id": job.expected_ids[0],
        "decision": "retain",
        "retention_reasons": ["specific_problem"],
        "rejection_reasons": [],
    }]}), "stop", reasoning_tokens=10,
        completion_tokens=completion_tokens)


class ScriptedClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.attempts = {}

    def batch(self, _corpus, _preamble, jobs):
        self.calls.append(list(jobs))
        out = {}
        for job in jobs:
            attempt = self.attempts.get(job.id, 0)
            self.attempts[job.id] = attempt + 1
            out[job.id] = self.reply(job, attempt)
        return out


class AdaptiveStage01BudgetTests(unittest.TestCase):
    def execute(self, client, jobs, wave_size=1, threshold=0.85):
        with tempfile.TemporaryDirectory() as tmp:
            results, final_jobs = cli._run_stage01_adaptive(
                client, "corpus", "preamble", jobs, tmp,
                headroom_threshold=threshold, wave_size=wave_size)
            saved = sorted(os.listdir(tmp))
        return results, final_jobs, saved

    @staticmethod
    def budgets(client):
        return [[job.max_tokens for job in wave] for wave in client.calls]

    @staticmethod
    def ids(client):
        return [[job.id for job in wave] for wave in client.calls]

    def test_initial_success_uses_12k_once(self):
        client = ScriptedClient(lambda job, _attempt: valid_result(job))
        results, final_jobs, saved = self.execute(client, [filter_job(0)])
        self.assertEqual(self.budgets(client), [[12000]])
        self.assertEqual(list(results), ["f0000"])
        self.assertEqual(final_jobs[0].max_tokens, 12000)
        self.assertEqual(saved, [])

    def test_output_exhaustion_promotes_to_the_next_tier(self):
        client = ScriptedClient(
            lambda job, attempt: (llm.BatchResult("", "length", 11900, 12000)
                                  if attempt == 0 else valid_result(job)))
        _results, final_jobs, saved = self.execute(client, [filter_job(0)])
        self.assertEqual(self.budgets(client), [[12000], [16000]])
        self.assertEqual(final_jobs[0].max_tokens, 16000)
        self.assertEqual(saved, ["f0000.budget_12000.txt"])

    def test_promoted_floor_persists_for_unstarted_jobs(self):
        def reply(job, attempt):
            if job.id == "f0000" and attempt == 0:
                return llm.BatchResult("", "length")
            return valid_result(job)

        client = ScriptedClient(reply)
        self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
        self.assertEqual(self.budgets(client), [[12000], [16000], [16000]])
        self.assertEqual(self.ids(client), [["f0000"], ["f0000"], ["f0001"]])

    def test_multiple_exhaustions_walk_the_coarse_tiers(self):
        def reply(job, _attempt):
            return (valid_result(job) if job.max_tokens == 32000
                    else llm.BatchResult("{", "max_tokens"))

        client = ScriptedClient(reply)
        _results, final_jobs, _saved = self.execute(client, [filter_job(0)])
        self.assertEqual(self.budgets(client), [[12000], [16000], [24000], [32000]])
        self.assertEqual(final_jobs[0].max_tokens, 32000)

    def test_valid_high_utilisation_proactively_promotes(self):
        client = ScriptedClient(
            lambda job, _attempt: valid_result(
                job, 10201 if job.id == "f0000" else 100))
        self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
        self.assertEqual(self.budgets(client), [[12000], [16000]])

    def test_utilisation_must_be_strictly_above_threshold(self):
        client = ScriptedClient(
            lambda job, _attempt: valid_result(
                job, 10200 if job.id == "f0000" else 100))
        self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
        self.assertEqual(self.budgets(client), [[12000], [12000]])

    def test_malformed_schema_or_provider_failures_do_not_promote(self):
        failures = (
            llm.BatchResult('{"records":[]}', "stop", 100, 11999),
            llm.BatchResult('{"records":[{"evidence_id":1}]}', "stop", 100, 11999),
            llm.BatchResult("", "refusal", 0, 11999),
            llm.BatchResult("", "content_filter", 0, 11999),
            llm.BatchResult("", "request_error:URLError", 0, 11999),
        )
        for failure in failures:
            with self.subTest(stop=failure.stop_reason, text=failure.text):
                def reply(job, _attempt):
                    return failure if job.id == "f0000" else valid_result(job)

                client = ScriptedClient(reply)
                self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
                self.assertEqual(self.budgets(client), [[12000], [12000]])

    def test_retries_change_only_max_tokens(self):
        original = filter_job(0)
        client = ScriptedClient(
            lambda job, attempt: (llm.BatchResult("", "length")
                                  if attempt == 0 else valid_result(job)))
        self.execute(client, [original])
        retried = client.calls[1][0]
        for field in dataclasses.fields(llm.Job):
            if field.name == "max_tokens":
                self.assertEqual(getattr(retried, field.name), 16000)
            else:
                self.assertEqual(getattr(retried, field.name),
                                 getattr(original, field.name), field.name)

    def test_32k_exhaustion_fails_after_four_attempts(self):
        client = ScriptedClient(lambda _job, _attempt: llm.BatchResult("", "length"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._run_stage01_adaptive(
                    client, "corpus", "preamble", [filter_job(0)], tmp,
                    wave_size=1)
            saved = sorted(os.listdir(tmp))
        self.assertEqual(self.budgets(client), [[12000], [16000], [24000], [32000]])
        self.assertEqual(len(client.calls), 4)  # terminal guard: no infinite loop
        self.assertIn("hard ceiling exhausted", str(ctx.exception))
        self.assertIn("32k", str(ctx.exception))
        self.assertEqual(saved, [
            "f0000.budget_12000.txt", "f0000.budget_16000.txt",
            "f0000.budget_24000.txt", "f0000.budget_32000.txt"])

    def test_rolling_wave_finishes_inflight_work_then_retries_before_new_work(self):
        def reply(job, attempt):
            if job.id == "f0000" and attempt == 0:
                return llm.BatchResult("", "length")
            return valid_result(job)

        client = ScriptedClient(reply)
        self.execute(client, [filter_job(i) for i in range(6)], wave_size=4)
        self.assertEqual(self.ids(client), [
            ["f0000", "f0001", "f0002", "f0003"],
            ["f0000"],
            ["f0004", "f0005"],
        ])
        self.assertEqual(self.budgets(client), [
            [12000, 12000, 12000, 12000], [16000], [16000, 16000]])
        # The other three requests were already in flight and are accepted at
        # 12k; no request first launched after the promotion uses 12k.
        self.assertEqual(client.attempts["f0001"], 1)
        self.assertEqual(client.attempts["f0002"], 1)
        self.assertEqual(client.attempts["f0003"], 1)

    def test_retry_can_promote_again_before_next_pending_wave(self):
        def reply(job, attempt):
            if job.id == "f0000" and attempt < 2:
                return llm.BatchResult("", "length")
            return valid_result(job)

        client = ScriptedClient(reply)
        self.execute(client, [filter_job(i) for i in range(5)], wave_size=4)
        self.assertEqual(self.ids(client), [
            ["f0000", "f0001", "f0002", "f0003"],
            ["f0000"], ["f0000"], ["f0004"]])
        self.assertEqual(self.budgets(client), [
            [12000] * 4, [16000], [24000], [24000]])

    def test_empty_stage_is_a_no_op(self):
        client = ScriptedClient(lambda job, _attempt: valid_result(job))
        results, jobs, saved = self.execute(client, [])
        self.assertEqual((results, jobs, saved), ({}, [], []))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
