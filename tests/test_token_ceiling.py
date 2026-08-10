"""A recovery ladder must not escalate past what the model accepts.

Stage 04's ladder tops out at 256,000 output tokens and Stage 03B's and 07's at
128,000. No current model accepts 256,000, and 128,000 is above several. Nothing
checked a tier against the model's limit, so the top rung of a recovery ladder
was a request the provider rejects — turning a recoverable budget stop into a
failed one, which is precisely what the ladder exists to prevent. On OpenRouter
the rejection was worse than a stage failure: an unrecognised 400 called
sys.exit, killing the run mid-recovery.

The clamp cuts one way only. Lowering a ceiling the route would have honoured is
its own truncation bug, so an unknown model is left alone.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm
import openrouter


class ModelCeilingTests(unittest.TestCase):
    def ceiling(self, model):
        client = object.__new__(llm.Client)
        client.model = model
        return client.max_output_tokens()

    def test_published_ceilings_are_known(self):
        self.assertEqual(self.ceiling("claude-opus-5"), 128000)
        self.assertEqual(self.ceiling("claude-sonnet-5"), 128000)
        self.assertEqual(self.ceiling("claude-haiku-4-5"), 64000)

    def test_the_default_model_has_a_known_ceiling(self):
        """The pipeline's own default must not fall through to 'unknown'."""
        self.assertIsNotNone(self.ceiling(llm.MODEL))

    def test_an_unrecognised_model_reports_no_ceiling(self):
        self.assertIsNone(self.ceiling("some/model-released-next-year"))

    def test_openrouter_reports_only_what_the_project_declared(self):
        client = object.__new__(openrouter.Client)
        client.max_output = None
        self.assertIsNone(client.max_output_tokens())
        client.max_output = 16384
        self.assertEqual(client.max_output_tokens(), 16384)

    def test_a_client_without_the_method_is_treated_as_unknown(self):
        """Small test doubles predate this method; they must not break a stage."""
        self.assertIsNone(cli._model_ceiling(object()))

    def test_a_failing_ceiling_lookup_never_fails_the_stage(self):
        class Hostile:
            def max_output_tokens(self):
                raise RuntimeError("boom")

        self.assertIsNone(cli._model_ceiling(Hostile()))


class ClampTests(unittest.TestCase):
    def clamp(self, tiers, ceiling):
        return cli._clamp_tiers(tiers, ceiling, verbose=False)

    def test_a_ladder_within_the_ceiling_is_untouched(self):
        self.assertEqual(self.clamp((12000, 16000, 24000), 128000),
                         (12000, 16000, 24000))

    def test_tiers_above_the_ceiling_collapse_onto_it(self):
        """Dropping them instead would cost the stage its final retry."""
        self.assertEqual(self.clamp((64000, 128000, 256000), 128000),
                         (64000, 128000))

    def test_the_top_rung_is_always_reachable(self):
        for name in ("STAGE03B_TOKEN_TIERS", "STAGE04_TOKEN_TIERS",
                     "STAGE07_TOKEN_TIERS", "STAGE08B_TOKEN_TIERS",
                     "ADAPTIVE_TOKEN_TIERS", "STAGE05_TOKEN_TIERS"):
            tiers = getattr(cli, name)
            for ceiling in (64000, 128000):
                clamped = self.clamp(tiers, ceiling)
                self.assertLessEqual(max(clamped), ceiling, f"{name} @ {ceiling}")
                self.assertTrue(clamped, name)

    def test_stage_04_is_clamped_for_every_supported_model(self):
        """The concrete case: 256k is above every published ceiling."""
        for model, ceiling in llm.MAX_OUTPUT_TOKENS.items():
            clamped = self.clamp(cli.STAGE04_TOKEN_TIERS, ceiling)
            self.assertLessEqual(max(clamped), ceiling, model)

    def test_an_unknown_ceiling_leaves_the_ladder_alone(self):
        """Clamping on a guess would truncate work the route would have done."""
        self.assertEqual(self.clamp((64000, 128000, 256000), None),
                         (64000, 128000, 256000))

    def test_a_ceiling_below_every_tier_leaves_one_usable_rung(self):
        self.assertEqual(self.clamp((64000, 128000), 16000), (16000,))

    def test_an_empty_ladder_is_returned_unchanged(self):
        self.assertEqual(self.clamp((), 128000), ())

    def test_the_clamp_announces_itself(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cli._clamp_tiers((64000, 128000, 256000), 128000, stage="04")
        out = buf.getvalue()
        self.assertIn("Stage 04", out)
        self.assertIn("output limit", out)

    def test_no_announcement_when_nothing_is_clamped(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cli._clamp_tiers((12000, 16000), 128000, stage="01")
        self.assertEqual(buf.getvalue(), "")


class AdaptiveStageClampTests(unittest.TestCase):
    """The clamp has to reach the requests, not just the tier list."""

    class Client:
        def __init__(self, ceiling, starve=False):
            self.ceiling, self.seen, self.starve = ceiling, [], starve

        def max_output_tokens(self):
            return self.ceiling

        def batch(self, _corpus, _preamble, jobs):
            self.seen.extend(j.max_tokens for j in jobs)
            if self.starve:
                # Never satisfied — forces the stage up every rung of the ladder,
                # which is the only way a too-high tier is ever requested.
                return {j.id: llm.BatchResult("", "length", 1, j.max_tokens)
                        for j in jobs}
            return {j.id: llm.BatchResult('{"records":[]}', "stop", 1, 2)
                    for j in jobs}

    def run_stage(self, ceiling, tiers, start, starve=False):
        client = self.Client(ceiling, starve)
        jobs = [llm.Job(id="a0000", prompt="p", max_tokens=start,
                        schema=cli.FILTER_SCHEMA)]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.stdout", io.StringIO()):
                try:
                    cli._run_adaptive_stage(
                        client, "corpus", "pre", jobs, tmp, stage="01",
                        key="records", token_tiers=tiers)
                except cli.BatchOutputError:
                    pass   # exhausting the ladder is the point when starving
        return client.seen

    def test_requests_never_exceed_the_ceiling_while_climbing(self):
        """The failure only appears on escalation — a stage that succeeds at its
        opening tier never asks for the rung that would have been rejected."""
        # A ladder with room to climb below the ceiling and a top rung above it,
        # so the clamp is tested without removing the escalation being tested.
        sent = self.run_stage(64000, (16000, 64000, 256000), 16000, starve=True)
        self.assertGreater(len(sent), 1, "the stage never escalated")
        self.assertLessEqual(max(sent), 64000)

    def test_a_job_starting_above_the_ceiling_is_brought_down(self):
        sent = self.run_stage(64000, (64000, 128000), 128000)
        self.assertLessEqual(max(sent), 64000)

    def test_an_unknown_ceiling_changes_nothing(self):
        sent = self.run_stage(None, (64000, 128000), 64000)
        self.assertEqual(sent, [64000])


class OpenRouterOverBudgetTests(unittest.TestCase):
    """A rejected ceiling must not kill the process mid-recovery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()
        self.client = object.__new__(openrouter.Client)
        self.client.model = "deepseek/deepseek-v4-flash"
        self.client.key = "k"
        self.client.verbose = False
        self.client.effort = "low"
        self.client.max_output = None
        self.client.spent = {"in": 0, "out": 0, "reasoning": 0,
                             "cache_write": 0, "cache_read": 0}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def http_error(self, message):
        return urllib.error.HTTPError(
            "https://example.test", 400, "bad request", {},
            io.BytesIO(json.dumps({"error": {"message": message}}).encode()))

    def ok(self):
        class Fake:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"},
                                 "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5},
                }).encode()

        return Fake()

    def test_a_stated_limit_is_retried_at_that_limit(self):
        error = self.http_error(
            "max_tokens is too large: 256000. Maximum is 8192.")
        with mock.patch("urllib.request.urlopen",
                        side_effect=[error, self.ok()]) as call:
            result = self.client._post([{"role": "user", "content": "x"}], 256000)
        retried = json.loads(call.call_args_list[1].args[0].data)
        self.assertEqual(retried["max_tokens"], 8192)
        self.assertEqual(result.text, "{}")

    def test_the_rejected_value_is_never_mistaken_for_the_limit(self):
        """The error quotes what we asked for as well as the cap."""
        self.assertEqual(
            openrouter._stated_token_limit(
                "max_tokens 256000 exceeds the maximum of 8192", 256000),
            8192)

    def test_an_unstated_limit_names_the_setting_that_fixes_it(self):
        error = self.http_error("max_tokens exceeds the model's maximum.")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                self.client._post([{"role": "user", "content": "x"}], 256000)
        self.assertIn("max_output_tokens", str(caught.exception))

    def test_an_unrelated_400_is_untouched(self):
        """Only ceiling complaints take this path."""
        error = self.http_error("invalid model id")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                self.client._post([{"role": "user", "content": "x"}], 4096)
        self.assertNotIn("max_output_tokens", str(caught.exception))

    def test_common_provider_phrasings_are_recognised(self):
        for message in (
                "max_tokens is too large",
                "requested 200000 max output tokens, limit is 32000",
                "completion tokens exceed the maximum for this model",
                "max_tokens: must be at most 16384"):
            self.assertTrue(openrouter._MAX_TOKENS_ERROR.search(message), message)

    def test_a_reasoning_complaint_still_wins_its_own_path(self):
        """The reasoning fallback must not be shadowed by the new branch."""
        self.assertIsNone(
            openrouter._MAX_TOKENS_ERROR.search(
                "this route does not support the reasoning parameter"))


if __name__ == "__main__":
    unittest.main()
