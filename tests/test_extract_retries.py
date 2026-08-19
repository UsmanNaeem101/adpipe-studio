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


class FakeClient:
    """The client as cmd_extract uses it.

    Retries go through `one_result` rather than `one`, because the recovery
    needs the stop reason: a retry that comes back with text but stopped at the
    budget again is still truncated, and treating it as recovered is how a
    half-written extraction reaches the next stage looking finished.

    `retry_texts` entries may be a plain string (a clean completion) or a
    (text, stop_reason) pair.
    """

    def __init__(self, batch_text="", retry_texts=(), batch_stop=None):
        self.batch_text = batch_text
        self.retry_texts = list(retry_texts)
        self.batch_stop = batch_stop
        self.one_calls = []

    def estimate(self, corpus, preamble, jobs, batched=False):
        return object()

    def prewarm(self, corpus, preamble):
        pass

    def batch(self, corpus, preamble, jobs):
        if self.batch_stop is None:
            return {jobs[0].id: self.batch_text}
        return {jobs[0].id: llm.BatchResult(
            id=jobs[0].id, text=self.batch_text, stop_reason=self.batch_stop)}

    def one_result(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
                   job_id="single", operation="pipeline_single", effort=None,
                   reasoning_max_tokens=None):
        self.one_calls.append((job_id, operation, max_tokens))
        nxt = self.retry_texts.pop(0)
        text, stop = nxt if isinstance(nxt, tuple) else (nxt, "end_turn")
        return llm.BatchResult(text=text, stop_reason=stop)

    def actual_usd(self):
        return 0.0


class ExtractRetryTests(unittest.TestCase):
    def args(self, force=False):
        return SimpleNamespace(
            segment="shoulder", skills="7", preset=None, force=force,
            yes=True, provider="openrouter", model="test", effort="high")

    def project(self, root):
        project = os.path.join(root, "project")
        evidence = os.path.join(project, "research", "evidence")
        os.makedirs(evidence)
        with open(os.path.join(evidence, "shoulder.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("customer evidence")
        return {"name": "test", "_dir": project}

    def run_extract(self, cfg, fake, force=False):
        with mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "skill",
                                  return_value=("07_pain_points", "instructions")), \
                mock.patch.object(llm, "confirm", return_value=True):
            cli.cmd_extract(cfg, self.args(force=force))

    def test_empty_batch_response_recovers_on_third_immediate_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(retry_texts=["", "  ", "# Pain points\nRecovered"])
            self.run_extract(cfg, fake)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(written, "# Pain points\nRecovered")
        self.assertEqual(len(fake.one_calls), 3)
        self.assertEqual(
            [x[1] for x in fake.one_calls],
            ["extraction_short_retry_1", "extraction_short_retry_2",
             "extraction_short_retry_3"])

    def test_three_empty_retries_fail_without_writing_a_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(retry_texts=["", "\n", "  "])
            with self.assertRaisesRegex(SystemExit, "failed after 3 retries"):
                self.run_extract(cfg, fake)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            self.assertFalse(os.path.exists(dest))
        self.assertEqual(len(fake.one_calls), 3)

    def test_batch_result_from_the_provider_is_unwrapped(self):
        """The Markdown stage consumes c.batch() directly, so it has to handle
        the same BatchResult the JSON stages get."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(
                batch_text=llm.BatchResult("# Pain points\nFrom batch", "stop"))
            self.run_extract(cfg, fake)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(written, "# Pain points\nFrom batch")
        self.assertEqual(fake.one_calls, [])

    def test_budget_starved_extraction_retries_with_more_room(self):
        """Empty + finish_reason 'length' must not be retried at the same cap."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(batch_text=llm.BatchResult("", "length"),
                              retry_texts=["# Pain points\nRecovered"])
            self.run_extract(cfg, fake)

        # Jobs are created at 16000; the budget-aware retry asks for 3x.
        self.assertEqual([call[2] for call in fake.one_calls], [48000])

    def test_existing_zero_byte_file_is_not_treated_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            os.makedirs(os.path.dirname(dest))
            open(dest, "w", encoding="utf-8").close()
            fake = FakeClient(batch_text="# Pain points\nRebuilt")
            self.run_extract(cfg, fake, force=False)
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(written, "# Pain points\nRebuilt")
        self.assertEqual(fake.one_calls, [])


if __name__ == "__main__":
    unittest.main()


class TruncatedExtractionTests(ExtractRetryTests):
    """A non-empty answer that stopped at the budget.

    The failure this covers happened in production, on the very first
    extraction somebody ran: 16,000 completion tokens, 2,545 of them reasoning,
    13,455 of answer, and `1/1 written` with exit 0. The file stops
    mid-sentence and every stage after it reads that as the finished article.

    An empty extraction is obvious. This one is not, which makes it worse.
    """

    def extraction(self, cfg):
        return os.path.join(cfg["_dir"], "research", "extractions", "shoulder",
                            "07_pain_points.md")

    def test_a_cut_off_answer_is_retried_with_more_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(
                batch_text=llm.BatchResult("# Pain points\n- one\n- two", "length"),
                retry_texts=["# Pain points\n- one\n- two\n- three\n- four"])
            self.run_extract(cfg, fake)
            with open(self.extraction(cfg), encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual([call[2] for call in fake.one_calls], [48000])
        self.assertIn("- four", written)
        self.assertNotIn(cli.TRUNCATED_MARKER, written)

    def test_a_partial_that_never_completes_says_so_in_the_file(self):
        # Three retries, all still stopping at the budget. The work is kept —
        # throwing away real extraction because it is incomplete helps nobody —
        # but the file has to admit what it is, because the next stage reads
        # the file and did not see the terminal.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(
                batch_text=llm.BatchResult("# Pain points\n- one", "length"),
                retry_texts=[("# Pain points\n- one\n- two", "length")] * 3)
            self.run_extract(cfg, fake)
            with open(self.extraction(cfg), encoding="utf-8") as fh:
                written = fh.read()

        self.assertIn(cli.TRUNCATED_MARKER, written)
        # The longest attempt survives, not the last or the first.
        self.assertIn("- two", written)

    def test_the_longest_attempt_wins_not_the_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(
                batch_text=llm.BatchResult("# Pain points\n- one", "length"),
                retry_texts=[("# Pain points\n- one\n- two\n- three", "length"),
                             ("# Pain points\n- one", "length"),
                             ("# Pain points\n- one", "length")])
            self.run_extract(cfg, fake)
            with open(self.extraction(cfg), encoding="utf-8") as fh:
                written = fh.read()

        self.assertIn("- three", written)

    def test_a_complete_answer_is_left_entirely_alone(self):
        # The common case must not pay for any of this: no retry, no marker.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(
                batch_text=llm.BatchResult("# Pain points\n- one", "stop"))
            self.run_extract(cfg, fake)
            with open(self.extraction(cfg), encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(fake.one_calls, [])
        self.assertEqual(written, "# Pain points\n- one")


class OutputBudgetTests(unittest.TestCase):
    """Reasoning and answer come out of one allowance.

    A real run reported 16,000 completion tokens of which 16,000 were reasoning
    and 0 were answer. The recovery raised the total to 48,000 — and, because it
    passed no ceiling, bought the model three times as much room to think in and
    still no obligation to write. Bounding the reasoning half is the fix; a
    bigger number on its own is not.
    """

    def test_the_default_leaves_most_of_the_budget_for_the_answer(self):
        total, cap = cli.extraction_budget({})
        self.assertEqual(total, cli.EXTRACTION_MAX_TOKENS)
        self.assertLess(cap, total / 2)

    def test_a_project_can_raise_the_budget(self):
        # Raising it costs nothing by itself: output is billed per token
        # actually written, and a budget too small is how an answer stops
        # mid-list.
        total, _ = cli.extraction_budget({"model": {"extraction_max_tokens": 40000}})
        self.assertEqual(total, 40000)

    def test_a_share_of_zero_hands_the_decision_back_to_the_model(self):
        # Not "no reasoning" — no ceiling. Some models need their own default.
        _, cap = cli.extraction_budget({"model": {"reasoning_share": 0}})
        self.assertIsNone(cap)

    def test_the_share_is_of_the_configured_budget_not_the_default(self):
        total, cap = cli.extraction_budget(
            {"model": {"extraction_max_tokens": 32000, "reasoning_share": 25}})
        self.assertEqual((total, cap), (32000, 8000))


class ReasoningCeilingTravelsTests(ExtractRetryTests):
    """The ceiling has to reach the request, on the first try and the retry."""

    class Recording(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.jobs_seen = []

        def batch(self, corpus, preamble, jobs):
            self.jobs_seen.extend(jobs)
            return super().batch(corpus, preamble, jobs)

        def one_result(self, corpus, preamble, prompt, max_tokens=16000,
                       schema=None, job_id="single", operation="pipeline_single",
                       effort=None, reasoning_max_tokens=None):
            self.one_calls.append((job_id, operation, max_tokens,
                                   reasoning_max_tokens))
            nxt = self.retry_texts.pop(0)
            text, stop = nxt if isinstance(nxt, tuple) else (nxt, "end_turn")
            return llm.BatchResult(text=text, stop_reason=stop)

    def test_the_first_request_carries_a_reasoning_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = self.Recording(
                batch_text=llm.BatchResult("# Pain points\n- one", "stop"))
            self.run_extract(cfg, fake)

        job = fake.jobs_seen[0]
        self.assertEqual(job.max_tokens, cli.EXTRACTION_MAX_TOKENS)
        self.assertTrue(job.reasoning_max_tokens)
        self.assertLess(job.reasoning_max_tokens, job.max_tokens)

    def test_the_retry_raises_the_total_and_keeps_the_ceiling(self):
        # The specific failure: 3x the room and no bound is 3x the thinking.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = self.Recording(batch_text=llm.BatchResult("", "length"),
                                  retry_texts=["# Pain points\nRecovered"])
            self.run_extract(cfg, fake)

        _job, _op, max_tokens, ceiling = fake.one_calls[0]
        self.assertEqual(max_tokens, cli.EXTRACTION_MAX_TOKENS * cli.BUDGET_RETRY_FACTOR)
        self.assertTrue(ceiling, "the retry dropped the reasoning ceiling")
        # Scaled with the budget, so the answer keeps the same share of a
        # bigger allowance rather than a shrinking one.
        self.assertEqual(ceiling, cli.extraction_budget({})[1] * cli.BUDGET_RETRY_FACTOR)
        self.assertLess(ceiling, max_tokens)
