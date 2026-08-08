"""Tests for stage 01's output-budget sizing, reasoning control, and vocabulary.

The stage failed because `max_tokens` caps reasoning AND the answer together,
and nothing bounded the reasoning half: the budget was picked round (8000), the
reason codes were unconstrained, and OpenRouter accepted an `effort` setting it
then never sent. These pin all three.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm
import openrouter
import profile_filter


class BudgetSizingTests(unittest.TestCase):
    """The budget must come from the schema, not from a round number."""

    def test_budget_reserves_the_answer_before_reasoning(self):
        # 60 records x 40 tokens = 2400 of answer, plus the reasoning reserve.
        sized = cli._record_max_tokens(60, 40)
        self.assertGreaterEqual(sized, 60 * 40 + cli.REASONING_RESERVE)

    def test_budget_scales_with_batch_size(self):
        self.assertLess(cli._record_max_tokens(20, 40),
                        cli._record_max_tokens(60, 40))

    def test_budget_leaves_a_margin_above_the_bare_requirement(self):
        bare = 60 * 40 + cli.REASONING_RESERVE
        self.assertGreater(cli._record_max_tokens(60, 40), bare)

    def test_stage_01_asks_for_a_sized_budget_and_low_effort(self):
        """The failing run's config, end to end through job construction."""
        jobs = self.build_filter_jobs(records=120, chunk=60)
        self.assertEqual(len(jobs), 2)
        for j in jobs:
            self.assertEqual(j.effort, "low")
            self.assertEqual(j.max_tokens, cli._record_max_tokens(60, 40))
            # The whole point: smaller than the budget that failed, because the
            # fix is bounding reasoning rather than buying it more room.
            self.assertLess(j.max_tokens, 8000)

    @staticmethod
    def build_filter_jobs(records, chunk):
        """Run cmd_ingest far enough to capture the skill-01 jobs it builds."""
        captured = {}

        class Client:
            def estimate(self, *_a, **_k):
                return type("E", (), {"explain": lambda _s: ""})()

            def prewarm(self, *_a):
                pass

            def batch(self, _corpus, _preamble, jobs):
                captured.setdefault("jobs", jobs)
                raise SystemExit("stop after capture")

        from types import SimpleNamespace
        text = ("My shoulder aches every single night and the pillow goes flat "
                "before morning arrives at all")
        raw = "\n\n".join(f"{text} number {i}" for i in range(records))
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "raw.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "shoulders"}
            args = SimpleNamespace(source=src, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=Client()), \
                    mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch.object(cli, "CHUNK_OVERRIDE", chunk, create=True):
                try:
                    cli.cmd_ingest(cfg, args)
                except SystemExit:
                    pass
        return captured.get("jobs", [])


class ReasonVocabularyTests(unittest.TestCase):
    """The schema must enforce skill 01's closed list, and not drift from it."""

    def skill_codes(self, label):
        with open(os.path.join(ROOT, "skills", "01_filter_voc.md"),
                  encoding="utf-8") as fh:
            section = fh.read().split("## Reason codes")[1].split("\n##")[0]
        block = section.split(f"**{label}:**")[1].split("**")[0]
        return re.findall(r"`([a-z_]+)`", block)

    def test_schema_matches_the_skill_file(self):
        """The skill is the spec; cli.py mirrors it. Fail loudly on drift."""
        self.assertEqual(cli.RETENTION_REASONS, self.skill_codes("Retention"))
        self.assertEqual(cli.REJECTION_REASONS, self.skill_codes("Rejection"))

    def test_schema_constrains_reasons_to_the_vocabulary(self):
        item = cli.FILTER_SCHEMA["properties"]["records"]["items"]
        self.assertEqual(
            item["properties"]["retention_reasons"]["items"]["enum"],
            cli.RETENTION_REASONS)
        self.assertEqual(
            item["properties"]["rejection_reasons"]["items"]["enum"],
            cli.REJECTION_REASONS)

    def test_prose_reasons_are_now_rejected(self):
        """Free-form reasons were the old behaviour; they must not validate."""
        bad = {"records": [{"evidence_id": 1, "decision": "retain",
                            "retention_reasons": ["they describe a real problem"],
                            "rejection_reasons": []}]}
        self.assertIsNotNone(cli._schema_issue(bad, cli.FILTER_SCHEMA))

    def test_code_reasons_validate(self):
        good = {"records": [{"evidence_id": 1, "decision": "retain",
                             "retention_reasons": ["first_person_experience",
                                                   "specific_problem"],
                             "rejection_reasons": []}]}
        self.assertIsNone(cli._schema_issue(good, cli.FILTER_SCHEMA))

    def test_a_prose_reason_routes_to_repair_not_rerun(self):
        """An enum violation has text — it's a shape problem, so repair it."""
        self.assertFalse(llm.BatchResult('{"records":[...]}', "stop").retryable)


class ReasoningControlTests(unittest.TestCase):
    """`effort` must actually reach the provider."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()
        self.client = object.__new__(openrouter.Client)
        self.client.model = "deepseek/deepseek-v4-flash"
        self.client.key = "k"
        self.client.verbose = False
        self.client.effort = "high"
        self.client.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def reply(self, payload=None):
        body = payload or {"choices": [{"message": {"content": "{}"},
                                        "finish_reason": "stop"}], "usage": {}}

        class Fake:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps(body).encode()

        return Fake()

    def sent_body(self, call):
        return json.loads(call.call_args.args[0].data)

    def test_openrouter_sends_the_reasoning_parameter(self):
        """This is the defect that emptied the budget: effort was stored and
        never transmitted, so the model reasoned at its own default."""
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 100)
        self.assertEqual(self.sent_body(call)["reasoning"], {"effort": "high"})

    def test_per_job_effort_overrides_the_client(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 100, effort="low")
        self.assertEqual(self.sent_body(call)["reasoning"], {"effort": "low"})

    def test_anthropic_effort_levels_are_mapped_not_forwarded_blindly(self):
        # xhigh/max exist in the pipeline's vocabulary but not OpenRouter's.
        for given, expected in (("xhigh", "high"), ("max", "high"),
                                ("medium", "medium")):
            with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
                self.client._post([{"role": "user", "content": "x"}], 100,
                                  effort=given)
            self.assertEqual(self.sent_body(call)["reasoning"]["effort"], expected,
                             given)

    def test_batch_passes_each_job_effort(self):
        seen = []

        def fake_post(_m, _mt, schema=None, retries=3, job_id=None,
                      operation="completion", effort=None, **_k):
            seen.append((job_id, effort))
            return llm.BatchResult("{}", "stop")

        self.client._post = fake_post
        self.client.batch("corpus", "preamble", [
            llm.Job("a", "one", effort="low"), llm.Job("b", "two")])
        self.assertCountEqual(seen, [("a", "low"), ("b", None)])

    def test_route_that_rejects_reasoning_is_retried_without_it(self):
        """Not every route accepts the parameter; losing it must not lose the
        stage — the sized budget and the audited re-run still cover us."""
        import io
        import urllib.error
        rejected = urllib.error.HTTPError(
            "https://example.test", 400, "bad", {},
            io.BytesIO(b'{"error":"unknown parameter: reasoning"}'))
        with mock.patch("urllib.request.urlopen",
                        side_effect=[rejected, self.reply()]) as call:
            result = self.client._post([{"role": "user", "content": "x"}], 100)
        retry = json.loads(call.call_args_list[1].args[0].data)
        self.assertNotIn("reasoning", retry)
        self.assertEqual(result.text, "{}")

    def test_anthropic_params_carry_per_job_effort(self):
        client = object.__new__(llm.Client)
        client.model = "claude-opus-5"
        client.effort = "high"
        self.assertEqual(
            client._params([], "p", 100, effort="low")["output_config"]["effort"],
            "low")
        self.assertEqual(
            client._params([], "p", 100)["output_config"]["effort"], "high")


class ProfilerTests(unittest.TestCase):
    """The profiler's arithmetic is the argument for the sizing — pin it."""

    def test_content_budget_is_one_verdict_per_record(self):
        one = profile_filter.verdict_tokens()
        self.assertGreater(profile_filter.content_budget(60), 60 * one - 1)

    def test_worst_case_verdict_exceeds_typical(self):
        self.assertGreater(profile_filter.verdict_tokens(worst_case=True),
                           profile_filter.verdict_tokens())

    def test_the_answer_fits_the_old_budget_at_every_swept_batch_size(self):
        """The finding that redirected the fix: 8000 was never too small for the
        ANSWER, so raising it would not have helped."""
        for chunk in (20, 40, 60, 80, 120):
            self.assertLess(profile_filter.content_budget(chunk), 8000, chunk)

    def test_failing_config_left_most_of_the_budget_to_reasoning(self):
        used = profile_filter.content_budget(60)
        self.assertLess(used / 8000, 0.35)

    def test_profiler_and_cli_agree_on_the_sized_budget(self):
        """Same formula in both places, so the tool's advice matches what the
        stage actually asks for. They differ only by the `{"records":[...]}`
        wrapper the profiler counts and the CLI absorbs into its margin."""
        tool = profile_filter.recommended_max_tokens(60, cli.REASONING_RESERVE)
        stage = cli._record_max_tokens(
            60, profile_filter.verdict_tokens(worst_case=True))
        self.assertLess(abs(tool - stage) / stage, 0.01)

    def test_the_stage_rounds_its_per_record_estimate_up(self):
        """cli uses a round 40 tok/verdict; the measured worst case must not
        exceed it, or the sizing under-reserves."""
        self.assertLessEqual(profile_filter.verdict_tokens(worst_case=True), 40)


if __name__ == "__main__":
    unittest.main()
