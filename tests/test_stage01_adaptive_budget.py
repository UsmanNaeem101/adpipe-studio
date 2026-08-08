"""Regression tests for the Stage 01/02 output-token tier state machine."""

import contextlib
import dataclasses
import io
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
        max_tokens=cli.ADAPTIVE_TOKEN_TIERS[0], schema=cli.FILTER_SCHEMA,
        expected_ids=(evidence_id,), effort="low", reasoning_max_tokens=2000)


def valid_result(job, completion_tokens=100):
    return llm.BatchResult(json.dumps({"records": [{
        "evidence_id": job.expected_ids[0],
        "decision": "retain",
        "retention_reasons": ["specific_problem"],
        "rejection_reasons": [],
    }]}), "stop", reasoning_tokens=10,
        completion_tokens=completion_tokens)


def dedup_job(index):
    return llm.Job(
        id=f"d{index:04d}", prompt=f"deduplicate chunk {index}",
        max_tokens=cli.ADAPTIVE_TOKEN_TIERS[0], schema=cli.DEDUP_SCHEMA,
        effort="high", reasoning_max_tokens=3500)


def valid_dedup_result(_job, completion_tokens=100):
    return llm.BatchResult(
        json.dumps({"groups": []}), "stop", reasoning_tokens=20,
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


class Stage01TestBase(unittest.TestCase):
    def execute(self, client, jobs, wave_size=1, debug=False, stage="01"):
        self.stats = cli.AdaptiveStageStats(stage=stage, total=len(jobs))
        runner = (cli._run_stage01_adaptive if stage == "01"
                  else cli._run_stage02_adaptive)
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(stream):
                results, final_jobs = runner(
                    client, "corpus", "preamble", jobs, tmp,
                    wave_size=wave_size, stats=self.stats, debug=debug)
            saved = sorted(os.listdir(tmp))
        self.output = stream.getvalue()
        return results, final_jobs, saved

    @staticmethod
    def budgets(client):
        return [[job.max_tokens for job in wave] for wave in client.calls]

    @staticmethod
    def ids(client):
        return [[job.id for job in wave] for wave in client.calls]


class AdaptiveStage01BudgetTests(Stage01TestBase):

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

    def test_valid_high_utilisation_does_not_promote(self):
        client = ScriptedClient(
            lambda job, _attempt: valid_result(
                job, 11999 if job.id == "f0000" else 100))
        self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
        self.assertEqual(self.budgets(client), [[12000], [12000]])
        self.assertEqual(self.stats.budget_promotions, 0)

    def test_reported_full_utilisation_without_length_stop_does_not_promote(self):
        client = ScriptedClient(
            lambda job, _attempt: valid_result(
                job, 12000 if job.id == "f0000" else 100))
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


class AdaptiveStage02BudgetTests(Stage01TestBase):
    def test_initial_success_uses_12k(self):
        client = ScriptedClient(valid_dedup_result)
        results, final_jobs, saved = self.execute(
            client, [dedup_job(0)], stage="02")
        self.assertEqual(self.budgets(client), [[12000]])
        self.assertEqual(list(results), ["d0000"])
        self.assertEqual(final_jobs[0].max_tokens, 12000)
        self.assertEqual(saved, [])

    def test_high_utilisation_success_does_not_promote(self):
        client = ScriptedClient(
            lambda job, _attempt: valid_dedup_result(
                job, 12000 if job.id == "d0000" else 100))
        self.execute(
            client, [dedup_job(0), dedup_job(1)], wave_size=1, stage="02")
        self.assertEqual(self.budgets(client), [[12000], [12000]])
        self.assertEqual(self.stats.budget_promotions, 0)

    def test_non_budget_failure_does_not_promote_untouched_work(self):
        def reply(job, _attempt):
            if job.id == "d0000":
                return llm.BatchResult('{"groups": [}', "stop", 11999, 11999)
            return valid_dedup_result(job)

        client = ScriptedClient(reply)
        self.execute(
            client, [dedup_job(0), dedup_job(1)], wave_size=1, stage="02")
        self.assertEqual(self.budgets(client), [[12000], [12000]])
        self.assertEqual(self.stats.budget_promotions, 0)

    def test_length_stop_promotes_retry_and_untouched_work(self):
        def reply(job, attempt):
            if job.id == "d0000" and attempt == 0:
                return llm.BatchResult("", "length", 12000, 12000)
            return valid_dedup_result(job)

        client = ScriptedClient(reply)
        self.execute(
            client, [dedup_job(i) for i in range(6)], wave_size=4, stage="02")
        self.assertEqual(self.ids(client), [
            ["d0000", "d0001", "d0002", "d0003"],
            ["d0000"],
            ["d0004", "d0005"],
        ])
        self.assertEqual(self.budgets(client), [
            [12000] * 4, [16000], [16000, 16000]])

    def test_max_tokens_stop_can_walk_all_tiers(self):
        client = ScriptedClient(
            lambda job, _attempt: (valid_dedup_result(job)
                                   if job.max_tokens == 32000
                                   else llm.BatchResult("", "max_tokens")))
        _results, final_jobs, _saved = self.execute(
            client, [dedup_job(0)], stage="02")
        self.assertEqual(
            self.budgets(client), [[12000], [16000], [24000], [32000]])
        self.assertEqual(final_jobs[0].max_tokens, 32000)

    def test_retry_preserves_stage02_reasoning_and_every_other_job_setting(self):
        original = dedup_job(0)
        client = ScriptedClient(
            lambda job, attempt: (llm.BatchResult("", "length")
                                  if attempt == 0 else valid_dedup_result(job)))
        self.execute(client, [original], stage="02")
        retried = client.calls[1][0]
        for field in dataclasses.fields(llm.Job):
            if field.name == "max_tokens":
                self.assertEqual(getattr(retried, field.name), 16000)
            else:
                self.assertEqual(getattr(retried, field.name),
                                 getattr(original, field.name), field.name)

    def test_32k_exhaustion_is_terminal_after_four_attempts(self):
        client = ScriptedClient(
            lambda _job, _attempt: llm.BatchResult("", "max_tokens"))
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(stream):
                with self.assertRaises(cli.BatchOutputError):
                    cli._run_stage02_adaptive(
                        client, "corpus", "preamble", [dedup_job(0)], tmp,
                        wave_size=1)
        self.assertEqual(
            self.budgets(client), [[12000], [16000], [24000], [32000]])
        self.assertEqual(len(client.calls), 4)
        self.assertIn("STAGE 02 FAILED", stream.getvalue())


class Stage01CliUxTests(Stage01TestBase):
    def test_progress_count_updates_across_rolling_waves(self):
        client = ScriptedClient(lambda job, _attempt: valid_result(job))
        self.execute(client, [filter_job(i) for i in range(6)], wave_size=4)
        self.assertIn("4/6 batches (67%)", self.output)
        self.assertIn("6/6 batches (100%)", self.output)
        self.assertIn("Current wave: f0000–f0003", self.output)
        self.assertNotIn("Stage 01 wave at", self.output)

    def test_percentage_calculation(self):
        self.assertEqual(cli._adaptive_percentage(20, 83), 24)
        self.assertEqual(cli._adaptive_percentage(83, 83), 100)
        self.assertEqual(cli._adaptive_percentage(0, 83), 0)

    def test_exhaustion_promotion_message_names_retry_and_new_floor(self):
        client = ScriptedClient(
            lambda job, attempt: (llm.BatchResult("", "length", 12000, 12000)
                                  if job.id == "f0000" and attempt == 0
                                  else valid_result(job)))
        self.execute(client, [filter_job(0), filter_job(1)], wave_size=1)
        self.assertIn("BUDGET EXHAUSTED", self.output)
        self.assertIn("f0000 hit 12k before completing the response", self.output)
        self.assertIn("f0000  12k → 16k", self.output)
        self.assertIn("New floor for untouched batches: 16k", self.output)

    def test_budget_retry_counter_counts_launched_retries(self):
        client = ScriptedClient(
            lambda job, attempt: (llm.BatchResult("", "length")
                                  if attempt == 0 else valid_result(job)))
        self.execute(client, [filter_job(0)])
        self.assertEqual(self.stats.budget_retries, 1)
        self.assertIn("Budget retries: 1", self.output)

    def test_final_summary_reports_counters_and_available_distributions(self):
        stats = cli.AdaptiveStageStats(
            stage="01", total=3, completed=3, first_pass_successes=2, budget_retries=1,
            json_repairs=1, budget_promotions=1, final_ceiling=16000,
            completion_tokens=[100, 200, 900], reasoning_tokens=[10, 20, 30])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            cli._print_adaptive_summary(stats)
        output = stream.getvalue()
        self.assertIn("Stage 01 complete", output)
        self.assertIn("Batches:              3/3", output)
        self.assertIn("First-pass successes: 2", output)
        self.assertIn("Budget retries:        1", output)
        self.assertIn("JSON repairs:          1", output)
        self.assertIn("Final ceiling:         16k", output)
        self.assertIn("Completion tokens:", output)
        self.assertIn("median: 200", output)
        self.assertIn("p95:    900", output)
        self.assertIn("Reasoning tokens:", output)

    def test_missing_usage_metadata_is_readable_and_omitted_from_summary(self):
        def reply(job, _attempt):
            result = valid_result(job)
            return llm.BatchResult(result.text, "stop")

        client = ScriptedClient(reply)
        self.execute(client, [filter_job(0)])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            cli._print_adaptive_summary(self.stats)
        summary = stream.getvalue()
        self.assertIn("success  usage metadata unavailable", self.output)
        self.assertNotIn("Completion tokens:", summary)
        self.assertNotIn("Reasoning tokens:", summary)

    def test_32k_terminal_failure_prints_loud_actionable_message(self):
        client = ScriptedClient(lambda _job, _attempt: llm.BatchResult("", "length"))
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(stream):
                with self.assertRaises(cli.BatchOutputError):
                    cli._run_stage01_adaptive(
                        client, "corpus", "preamble", [filter_job(0)], tmp,
                        wave_size=1)
            diagnostics = tmp
        output = stream.getvalue()
        self.assertIn("STAGE 01 FAILED", output)
        self.assertIn("f0000 exhausted the maximum 32k ceiling", output)
        self.assertIn("No Stage 01 output was written.", output)
        self.assertIn(f"Diagnostics saved to: {diagnostics}", output)

    def test_debug_mode_adds_scheduler_and_reasoning_details(self):
        client = ScriptedClient(lambda job, _attempt: valid_result(job))
        self.execute(client, [filter_job(0)], debug=True)
        self.assertIn("Scheduler: 1-request rolling waves", self.output)
        self.assertIn("stop_reason='stop'; reasoning=10; answer=90", self.output)


if __name__ == "__main__":
    unittest.main()
