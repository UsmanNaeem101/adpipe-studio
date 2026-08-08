"""Regression tests for the Stage 01 VOC filter failure.

The failure being pinned down here: a reasoning model spends its whole
`max_tokens` budget on reasoning and returns EMPTY content with a finish reason
of "length". The batch decoder saw an empty string, reported
`Expecting value: line 1 column 1 (char 0)`, and sent the job to a shape-repair
pass whose prompt is strictly longer than the request that had just exhausted
the budget — so the repair failed the same way, and the stage aborted with no
output.

Three things must hold to keep that from recurring:

  the stop reason survives the trip from the provider to the decode site,
  an unfinished reply re-runs the ORIGINAL request with a larger budget,
  a reply that is merely the wrong shape is still repaired rather than re-run.
"""

import dataclasses
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
import presets


SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {
        "type": "object",
        "properties": {"evidence_id": {"type": "integer"},
                       "decision": {"type": "string", "enum": ["retain", "reject"]}},
        "required": ["evidence_id", "decision"],
        "additionalProperties": False}}},
    "required": ["records"],
    "additionalProperties": False,
}


def rows(*ids):
    return json.dumps({"records": [{"evidence_id": i, "decision": "retain"}
                                   for i in ids]})


def job(jid="a", ids=(1,), max_tokens=8000):
    return llm.Job(jid, "judge these records", max_tokens=max_tokens,
                   schema=SCHEMA, expected_ids=tuple(ids))


class Recorder:
    """Stands in for the recovery callbacks and remembers how it was called."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.rerun_calls = []
        self.repair_calls = []

    def rerun(self, failures, factor):
        self.rerun_calls.append(
            [(j.id, j.max_tokens, factor, reason) for j, _r, reason in failures])
        return {j.id: self.replies.get(("rerun", j.id), "") for j, _r, _x in failures}

    def repair(self, failures):
        self.repair_calls.append([(j.id, reason) for j, _raw, reason in failures])
        return {j.id: self.replies.get(("repair", j.id), "") for j, _raw, _x in failures}


class FailureClassificationTests(unittest.TestCase):
    """A BatchResult must be able to say which recovery it needs."""

    def test_output_budget_stop_is_retryable(self):
        for stop in ("length", "max_tokens"):
            result = llm.BatchResult("", stop)
            self.assertTrue(result.out_of_budget, stop)
            self.assertTrue(result.retryable, stop)

    def test_truncated_but_budget_stopped_reply_is_retryable(self):
        # Text came back, but the provider says it was cut off. More budget is
        # the fix; repairing at the same budget just truncates again.
        self.assertTrue(llm.BatchResult('{"records":[{"evi', "length").retryable)

    def test_empty_reply_with_no_stop_reason_is_retryable(self):
        self.assertTrue(llm.BatchResult("", None).retryable)
        self.assertTrue(llm.BatchResult("   ", None).retryable)

    def test_transport_failure_is_retryable(self):
        self.assertTrue(llm.BatchResult("", "request_error:URLError").retryable)
        self.assertTrue(llm.BatchResult("", "batch_errored").retryable)

    def test_refusal_is_never_retryable(self):
        # Re-asking only spends money to be refused again.
        self.assertFalse(llm.BatchResult("", "refusal").retryable)

    def test_wrong_shape_reply_is_not_retryable(self):
        # Text is present and complete — cheaper to repair than to regenerate.
        self.assertFalse(llm.BatchResult('{"nope":1}', "end_turn").retryable)


class ParserSharingTests(unittest.TestCase):
    """cli._json_object must be the hardened presets parser, not a second one."""

    def test_fenced_json(self):
        self.assertEqual(cli._json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_around_object(self):
        self.assertEqual(cli._json_object('Here you go: {"a": 1} — done'), {"a": 1})

    def test_brace_inside_string_does_not_end_object_early(self):
        self.assertEqual(
            cli._json_object('{"note": "use {curly} braces", "b": 2}'),
            {"note": "use {curly} braces", "b": 2})

    def test_trailing_comma_is_repaired(self):
        self.assertEqual(cli._json_object('{"a": [1, 2,],}'), {"a": [1, 2]})

    def test_truncated_json_is_reported_as_truncated(self):
        # Previously: "Expecting property name enclosed in double quotes" — an
        # error that points at a comma instead of at the real cause.
        with self.assertRaises(ValueError) as ctx:
            cli._json_object('{"records": [{"evidence_id": 1,')
        self.assertIn("truncated", str(ctx.exception))

    def test_empty_response_still_raises(self):
        with self.assertRaises(ValueError):
            cli._json_object("")


class BatchRecoveryTests(unittest.TestCase):
    def run_rows(self, results, jobs, rec, tmp=None, key="records"):
        if tmp is None:
            with tempfile.TemporaryDirectory() as d:
                return cli._batch_rows(results, jobs, key, d,
                                       repair=rec.repair, rerun=rec.rerun)
        return cli._batch_rows(results, jobs, key, tmp,
                               repair=rec.repair, rerun=rec.rerun)

    # ---- the normal path must stay untouched -----------------------------

    def test_successful_normal_path_calls_no_recovery(self):
        rec = Recorder()
        out = self.run_rows({"a": rows(1, 2)}, [job("a", (1, 2))], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1, 2])
        self.assertEqual(rec.rerun_calls, [])
        self.assertEqual(rec.repair_calls, [])

    def test_fenced_json_needs_no_recovery(self):
        rec = Recorder()
        out = self.run_rows({"a": f"```json\n{rows(1)}\n```"}, [job("a")], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1])
        self.assertEqual(rec.rerun_calls, [])
        self.assertEqual(rec.repair_calls, [])

    # ---- the reported failure --------------------------------------------

    def test_empty_budget_stopped_reply_reruns_original_at_larger_budget(self):
        """The exact Stage 01 failure: empty content, finish_reason 'length'."""
        rec = Recorder({("rerun", "a"): rows(1)})
        out = self.run_rows(
            {"a": llm.BatchResult("", "length")}, [job("a", (1,), 8000)], rec)

        self.assertEqual([r["evidence_id"] for r in out], [1])
        # Re-run happened, at 3x, and repair was never needed.
        self.assertEqual(len(rec.rerun_calls), 1)
        jid, max_tokens, factor, reason = rec.rerun_calls[0][0]
        self.assertEqual((jid, max_tokens, factor), ("a", 8000, 3))
        self.assertEqual(rec.repair_calls, [])
        # The reason names the budget, not a phantom JSON syntax error.
        self.assertIn("output token budget", reason)
        self.assertNotIn("Expecting value", reason)

    def test_rerun_job_carries_the_multiplied_budget_and_original_prompt(self):
        captured = {}

        class FakeClient:
            def batch(self, _corpus, _preamble, jobs):
                captured["jobs"] = jobs
                return {j.id: rows(1) for j in jobs}

        original = job("a", (1,), max_tokens=8000)
        replies = cli._rerun_batch(
            FakeClient(), "corpus", "preamble",
            [(original, llm.BatchResult("", "length"), "budget")], 3)

        sent = captured["jobs"][0]
        self.assertEqual(sent.max_tokens, 24000)
        # The ORIGINAL request is re-sent — not a longer repair prompt built on
        # top of it, which is what made the old recovery fail the same way.
        self.assertEqual(sent.prompt, original.prompt)
        self.assertIs(sent.schema, original.schema)
        self.assertEqual(sent.expected_ids, original.expected_ids)
        self.assertEqual(replies["a"], rows(1))

    def test_empty_response_reports_emptiness_not_a_json_syntax_error(self):
        rec = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                self.run_rows({"a": llm.BatchResult("", None)}, [job("a")], rec, tmp)
        message = str(ctx.exception)
        self.assertIn("empty response", message)
        self.assertNotIn("Expecting value: line 1 column 1", message)

    def test_truncated_json_reruns_before_repairing(self):
        rec = Recorder({("rerun", "a"): rows(1)})
        out = self.run_rows(
            {"a": llm.BatchResult('{"records":[{"evidence_id":1,', "length")},
            [job("a")], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1])
        self.assertEqual(len(rec.rerun_calls), 1)
        self.assertEqual(rec.repair_calls, [])

    def test_rerun_failure_falls_through_to_repair(self):
        """Both recoveries get a turn, in the order that makes sense."""
        rec = Recorder({("rerun", "a"): "still no JSON at all",
                        ("repair", "a"): rows(1)})
        out = self.run_rows({"a": llm.BatchResult("", "length")}, [job("a")], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1])
        self.assertEqual(len(rec.rerun_calls), 1)
        self.assertEqual(len(rec.repair_calls), 1)
        # The repair sees the whole history, not just the last step.
        self.assertIn("output token budget", rec.repair_calls[0][0][1])

    # ---- shape failures must NOT waste a re-run --------------------------

    def test_malformed_json_is_repaired_not_rerun(self):
        rec = Recorder({("repair", "a"): rows(1)})
        out = self.run_rows(
            {"a": llm.BatchResult("I judged them but forgot the JSON.", "end_turn")},
            [job("a")], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1])
        self.assertEqual(rec.rerun_calls, [])
        self.assertEqual(len(rec.repair_calls), 1)

    def test_schema_invalid_json_is_repaired_not_rerun(self):
        rec = Recorder({("repair", "a"): rows(1)})
        bad = json.dumps({"records": [{"evidence_id": 1, "decision": "maybe"}]})
        out = self.run_rows({"a": llm.BatchResult(bad, "end_turn")}, [job("a")], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1])
        self.assertEqual(rec.rerun_calls, [])
        self.assertIn("not an allowed value", rec.repair_calls[0][0][1])

    def test_incomplete_coverage_is_repaired_not_rerun(self):
        rec = Recorder({("repair", "a"): rows(1, 2)})
        out = self.run_rows({"a": llm.BatchResult(rows(1), "end_turn")},
                            [job("a", (1, 2))], rec)
        self.assertEqual([r["evidence_id"] for r in out], [1, 2])
        self.assertEqual(rec.rerun_calls, [])
        self.assertIn("1 missing", rec.repair_calls[0][0][1])

    def test_refusal_is_neither_rerun_nor_repaired(self):
        rec = Recorder()
        with self.assertRaises(cli.BatchOutputError) as ctx:
            self.run_rows({"a": llm.BatchResult("", "refusal")}, [job("a")], rec)
        self.assertEqual(rec.rerun_calls, [])
        self.assertEqual(rec.repair_calls, [])
        self.assertIn("refused", str(ctx.exception))

    # ---- nothing is hidden ------------------------------------------------

    def test_every_attempt_is_saved_with_its_stop_reason(self):
        rec = Recorder({("rerun", "a"): "", ("repair", "a"): ""})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError):
                self.run_rows({"a": llm.BatchResult("", "length")}, [job("a")],
                              rec, tmp)
            saved = sorted(os.listdir(tmp))
            with open(os.path.join(tmp, "a.original.txt"), encoding="utf-8") as fh:
                original = fh.read()

        # No .repaired.txt: the re-run also came back empty, and an empty reply
        # has no shape to repair — spending a repair request on "" is the
        # original bug wearing a different hat.
        self.assertEqual(saved, ["a.original.txt", "a.rerun.txt"])
        # The metadata whose absence made this failure unreadable on disk.
        self.assertIn("STOP REASON: length", original)
        self.assertIn("MAX TOKENS: 8000", original)
        self.assertIn("RESPONSE CHARS: 0", original)

    def test_error_names_the_dominant_failure_mode(self):
        rec = Recorder({("rerun", jid): "" for jid in ("a", "b")})
        jobs = [job("a"), job("b")]
        with self.assertRaises(cli.BatchOutputError) as ctx:
            self.run_rows({"a": llm.BatchResult("", "length"),
                           "b": llm.BatchResult("", "length")}, jobs, rec)
        message = str(ctx.exception)
        self.assertIn("2 output budget exhausted", message)
        self.assertIn("2/2 batch response(s)", message)

    def test_partial_failure_still_aborts_the_stage(self):
        """A batch that half-worked must not write a half-corpus."""
        rec = Recorder({("rerun", "b"): "", ("repair", "b"): ""})
        with self.assertRaises(cli.BatchOutputError):
            self.run_rows({"a": rows(1), "b": llm.BatchResult("", "length")},
                          [job("a"), job("b", (2,))], rec)


class ProviderStopReasonTests(unittest.TestCase):
    """The stop reason has to actually leave the provider layer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()
        self.client = object.__new__(openrouter.Client)
        self.client.model = "deepseek/deepseek-v4-flash"
        self.client.key = "test-key"
        self.client.verbose = False
        self.client.effort = "low"
        self.client.spent = {"in": 0, "out": 0, "reasoning": 0, "cache_write": 0, "cache_read": 0}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def reply(self, payload):
        class Fake:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_a):
                return False

            def read(self_inner):
                return json.dumps(payload).encode()

        return Fake()

    def test_openrouter_surfaces_length_finish_reason(self):
        """A reasoning model that burned its budget: empty content, 'length'."""
        payload = {"choices": [{"message": {"content": "", "reasoning": "thinking…"},
                                "finish_reason": "length"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 8000}}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(payload)):
            result = self.client._post([{"role": "user", "content": "x"}], 8000)

        self.assertIsInstance(result, llm.BatchResult)
        self.assertEqual(result.stop_reason, "length")
        self.assertEqual(result.text, "")
        self.assertTrue(result.retryable)

    def test_openrouter_surfaces_normal_finish_reason(self):
        payload = {"choices": [{"message": {"content": rows(1)},
                                "finish_reason": "stop"}],
                   "usage": {}}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(payload)):
            result = self.client._post([{"role": "user", "content": "x"}], 100)
        self.assertEqual(result.stop_reason, "stop")
        self.assertFalse(result.retryable)

    def test_openrouter_tolerates_a_reply_with_no_choices(self):
        # Previously an IndexError, which aborted the whole batch instead of
        # producing one recoverable empty response.
        with mock.patch("urllib.request.urlopen",
                        return_value=self.reply({"choices": [], "usage": {}})):
            result = self.client._post([{"role": "user", "content": "x"}], 100)
        self.assertEqual(result.text, "")
        self.assertTrue(result.retryable)

    def test_openrouter_one_still_returns_plain_text(self):
        payload = {"choices": [{"message": {"content": rows(1)},
                                "finish_reason": "stop"}], "usage": {}}
        with mock.patch("urllib.request.urlopen", return_value=self.reply(payload)):
            text = self.client.one("corpus", "preamble", "prompt", 100)
        self.assertEqual(text, rows(1))

    def test_openrouter_batch_records_a_failed_request_by_job_id(self):
        self.client.verbose = False
        with mock.patch.object(
                self.client, "_post",
                side_effect=urllib.error.URLError("boom")):
            out = self.client.batch("corpus", "preamble", [job("a")])
        # The job is present and marked retryable rather than silently missing.
        self.assertIn("a", out)
        self.assertTrue(out["a"].retryable)

    def test_schema_fallback_still_returns_a_batch_result(self):
        unsupported = urllib.error.HTTPError(
            "https://example.test", 404, "not found", {},
            io.BytesIO(b'{"error":"No endpoints support requested parameters: '
                       b'response_format"}'))
        ok = {"choices": [{"message": {"content": rows(1)}, "finish_reason": "stop"}],
              "usage": {}}
        with mock.patch("urllib.request.urlopen",
                        side_effect=[unsupported, self.reply(ok)]):
            result = self.client._post(
                [{"role": "user", "content": "x"}], 100, SCHEMA)
        self.assertIsInstance(result, llm.BatchResult)
        self.assertEqual(result.text, rows(1))


class IngestStage01Tests(unittest.TestCase):
    """End to end: the run that aborted must now produce the filtered corpus."""

    RAW = ("I have a very specific shoulder problem that wakes me every single night.\n\n"
           "My current pillow becomes flat and uncomfortable before the morning arrives.")

    def ingest(self, client):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "raw.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(self.RAW)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "shoulders"}
            args = SimpleNamespace(source=source, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=client), \
                    mock.patch.object(llm, "confirm", return_value=True):
                cli.cmd_ingest(cfg, args)
            voc = os.path.join(tmp, "research", "voc")
            with open(os.path.join(voc, "filtered_voc.jsonl"), encoding="utf-8") as fh:
                out = [json.loads(line) for line in fh]
            # Read the diagnostics before the temp dir goes away.
            saved, failures_dir = {}, os.path.join(voc, "_model_failures", "01_filter")
            for name in sorted(os.listdir(failures_dir)):
                with open(os.path.join(failures_dir, name), encoding="utf-8") as fh:
                    saved[name] = fh.read()
            return out, saved

    def test_stage_completes_when_every_first_response_is_budget_starved(self):
        """A 12k exhaustion retries at 16k and then completes the stage."""

        class BudgetStarvedClient:
            """Returns nothing until the request has room to think AND answer."""

            def __init__(self):
                self.seen = []

            def estimate(self, *_a, **_k):
                return type("E", (), {"explain": lambda _s: "fake"})()

            def prewarm(self, *_a):
                pass

            def batch(self, _corpus, _preamble, jobs):
                result = {}
                for j in jobs:
                    self.seen.append((j.id, j.max_tokens))
                    if j.max_tokens == 12000:
                        result[j.id] = llm.BatchResult("", "length")
                    elif j.schema is cli.FILTER_SCHEMA:
                        ids = [int(x) for x in __import__("re").findall(
                            r"^\[(\d+)\]", j.prompt, __import__("re").M)]
                        result[j.id] = llm.BatchResult(json.dumps({"records": [
                            {"evidence_id": i, "decision": "retain",
                             "retention_reasons": ["specific_problem"],
                             "rejection_reasons": []} for i in ids]}), "stop")
                    else:
                        result[j.id] = llm.BatchResult('{"groups":[]}', "stop")
                return result

        client = BudgetStarvedClient()
        rows_out, saved = self.ingest(client)

        self.assertEqual(len(rows_out), 2)
        self.assertTrue(all(r["decision"] == "retain" for r in rows_out))
        filter_budgets = [mt for jid, mt in client.seen if jid.startswith("f")]
        self.assertEqual(filter_budgets, [12000, 16000])
        # The exhausted attempt is saved with its exact tier; the successful
        # retry proceeds directly to validation and is not mislabeled a repair.
        self.assertEqual(sorted(saved), ["f0000.budget_12000.txt"])
        self.assertIn("STOP REASON: length", saved["f0000.budget_12000.txt"])
        self.assertIn("output token budget", saved["f0000.budget_12000.txt"])

    def test_stage_still_aborts_when_recovery_cannot_fix_it(self):
        """No silent partial corpus — a stage that cannot recover must fail."""

        class DeadClient:
            def estimate(self, *_a, **_k):
                return type("E", (), {"explain": lambda _s: "fake"})()

            def prewarm(self, *_a):
                pass

            def batch(self, _corpus, _preamble, jobs):
                return {j.id: llm.BatchResult("", "length") for j in jobs}

        with self.assertRaises(cli.BatchOutputError) as ctx:
            self.ingest(DeadClient())
        message = str(ctx.exception)
        self.assertIn("hard ceiling exhausted", message)
        self.assertIn("32k", message)
        self.assertIn("No stage output was written", message)


class SharedBudgetVocabularyTests(unittest.TestCase):
    """The single-call and batched paths must agree on what a budget stop is."""

    def test_presets_uses_the_shared_stop_reason_set(self):
        self.assertIs(presets.BUDGET_STOP_REASONS, llm.BUDGET_STOP_REASONS)

    def test_single_call_path_still_detects_the_same_condition(self):
        for stop in llm.BUDGET_STOP_REASONS:
            with self.assertRaises(presets.OutputBudgetError):
                with mock.patch.object(
                        presets, "_post_json",
                        return_value={"choices": [{"message": {"content": ""},
                                                   "finish_reason": stop}]}):
                    presets._post_text("openrouter", {"openrouter": "k"}, "m",
                                       "sys", "prompt", 100, 30, "label")


class RecoveryCarriesReasoningSettingsTests(unittest.TestCase):
    """A recovery request must be the SAME request, only roomier.

    Both recovery paths rebuilt their Job without `effort` or
    `reasoning_max_tokens`, so every retry silently fell back to the client
    default. A stage configured for `low` with a 2,000-token reasoning ceiling
    re-ran at `reasoning: {"effort": "high"}` with no ceiling at all — the run
    that produced this test escalated reasoning on 77 requests that had failed
    *because of* reasoning, and spent the widened budget thinking again.
    """

    def failing_job(self, **over):
        kw = dict(id="f0000", prompt="classify these", max_tokens=5060,
                  schema=cli.FILTER_SCHEMA, expected_ids=(1,),
                  effort="low", reasoning_max_tokens=2000)
        kw.update(over)
        return llm.Job(**kw)

    def capture(self, fn):
        seen = {}

        class Client:
            def batch(_self, _corpus, _preamble, jobs):
                seen["jobs"] = jobs
                return {j.id: llm.BatchResult('{"records":[]}', "stop") for j in jobs}

        fn(Client())
        return seen["jobs"]

    def test_rerun_keeps_the_stage_reasoning_settings(self):
        job = self.failing_job()
        result = llm.BatchResult("", "length")
        sent = self.capture(lambda c: cli._rerun_batch(
            c, "corpus", "preamble", [(job, result, "budget")], 3))
        self.assertEqual(sent[0].effort, "low")
        self.assertEqual(sent[0].reasoning_max_tokens, 2000)

    def test_rerun_widens_only_the_total_budget(self):
        job = self.failing_job()
        sent = self.capture(lambda c: cli._rerun_batch(
            c, "corpus", "preamble",
            [(job, llm.BatchResult("", "length"), "budget")], 3))
        self.assertEqual(sent[0].max_tokens, 5060 * 3)

    def test_rerun_sizes_from_the_observed_spend_when_it_is_larger(self):
        """A reply that stopped at its ceiling proves the ceiling was too low,
        never by how much — so believe the meter over the arithmetic."""
        job = self.failing_job(max_tokens=5060)
        result = llm.BatchResult("", "length", reasoning_tokens=9000,
                                 completion_tokens=9000)
        sent = self.capture(lambda c: cli._rerun_batch(
            c, "corpus", "preamble", [(job, result, "budget")], 3))
        self.assertEqual(sent[0].max_tokens, 27000)

    def test_recovery_carries_every_job_field_it_does_not_change(self):
        """The guard against this bug returning in a new field's costume.

        The rebuild listed fields by hand, so a field it forgot was silently
        dropped rather than flagged. Recovery now derives the job with
        dataclasses.replace: anything added to Job travels by default, and this
        fails if some site goes back to naming fields one at a time.
        """
        job = self.failing_job()
        changed = {"max_tokens", "prompt"}
        for fn, args in (
                (cli._rerun_batch, ([(job, llm.BatchResult("", "length"), "b")], 3)),
                (cli._repair_batch, ([(job, "half json", "malformed")], "records"))):
            sent = self.capture(
                lambda c, f=fn, a=args: f(c, "corpus", "preamble", *a))[0]
            for field in dataclasses.fields(llm.Job):
                if field.name not in changed:
                    self.assertEqual(
                        getattr(sent, field.name), getattr(job, field.name),
                        f"{fn.__name__} dropped Job.{field.name}")

    def test_repair_keeps_the_stage_reasoning_settings(self):
        job = self.failing_job()
        sent = self.capture(lambda c: cli._repair_batch(
            c, "corpus", "preamble", [(job, "half a json", "malformed")],
            "records"))
        self.assertEqual(sent[0].effort, "low")
        self.assertEqual(sent[0].reasoning_max_tokens, 2000)

    def test_json_repair_does_not_increase_the_output_ceiling(self):
        job = self.failing_job(max_tokens=12000)
        sent = self.capture(lambda c: cli._repair_batch(
            c, "corpus", "preamble", [(job, "half a json", "malformed")],
            "records"))
        self.assertEqual(sent[0].max_tokens, 12000)


class FailureModeReportingTests(unittest.TestCase):
    """Truncated-mid-answer and nothing-written-at-all must not read the same.

    The 77/83 run is the case: one reply stopped before writing anything (the
    only shape that printed), and the rest stopped partway through the JSON,
    which printed nothing at all. Both landed in one undifferentiated count, so
    the run looked silent and then collapsed.
    """

    def test_truncated_and_empty_budget_stops_are_different_modes(self):
        self.assertEqual(cli._failure_mode(llm.BatchResult("", "length")),
                         "hit the output budget before writing anything")
        self.assertEqual(
            cli._failure_mode(llm.BatchResult('{"records":[{"evi', "length")),
            "hit the output budget partway through the answer")

    def test_other_modes_keep_their_own_names(self):
        for result, mode in (
                (llm.BatchResult("", "refusal"), "blocked by the provider"),
                (llm.BatchResult("", "request_error:URLError"),
                 "the request never completed"),
                (llm.BatchResult("", "stop"), "returned an empty reply"),
                (llm.BatchResult('{"nope":1}', "stop"), "returned the wrong shape")):
            self.assertEqual(cli._failure_mode(result), mode)

    def report(self, failures, total=83):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cli._report_failure_modes(failures, total, "records")
        return buf.getvalue()

    def test_report_counts_each_mode_separately(self):
        j = llm.Job("f0000", "p", max_tokens=5060, reasoning_max_tokens=2000)
        failures = ([(j, llm.BatchResult("", "length", 5060, 5060), "x")]
                    + [(j, llm.BatchResult('{"records":[{"e', "length", 3000, 5060),
                        "x") for _ in range(76)])
        out = self.report(failures)
        self.assertIn("77/83", out)
        self.assertIn("76  hit the output budget partway through the answer", out)
        self.assertIn("1  hit the output budget before writing anything", out)

    def test_report_flags_reasoning_that_overran_the_ceiling_it_asked_for(self):
        """The question the last run could not answer about itself."""
        j = llm.Job("f0000", "p", max_tokens=5060, reasoning_max_tokens=2000)
        out = self.report(
            [(j, llm.BatchResult('{"rec', "length", 3400, 5060), "x")] * 5)
        self.assertIn("median 3,400 reasoning tokens", out)
        self.assertIn("OVER the ceiling", out)

    def test_report_does_not_cry_overrun_when_reasoning_obeyed_the_ceiling(self):
        j = llm.Job("f0000", "p", max_tokens=5060, reasoning_max_tokens=2000)
        out = self.report(
            [(j, llm.BatchResult('{"rec', "length", 1200, 5060), "x")] * 5)
        self.assertNotIn("OVER the ceiling", out)

    def test_report_omits_token_detail_when_usage_is_unreported(self):
        j = llm.Job("f0000", "p", max_tokens=5060)
        out = self.report([(j, llm.BatchResult("", "length"), "x")])
        self.assertNotIn("reasoning tokens", out)


class BudgetVisibilityTests(unittest.TestCase):
    """You cannot fix a budget you cannot see being spent.

    BatchResult carried only text and a stop reason, so a run could not tell
    "ran out of room" from "reasoned instead of answering" — the two are the
    same stop reason and completely different problems.
    """

    def test_budget_note_splits_reasoning_from_answer(self):
        note = llm.BatchResult("x", "length", 4800, 5060).budget_note
        self.assertIn("4,800 reasoning", note)
        self.assertIn("260 answer", note)

    def test_budget_note_is_empty_when_usage_is_unreported(self):
        self.assertEqual(llm.BatchResult("x", "length").budget_note, "")

    def test_saved_failure_records_what_was_asked_and_what_was_spent(self):
        job = llm.Job("f0000", "p", max_tokens=5060, effort="low",
                      reasoning_max_tokens=2000)
        result = llm.BatchResult("", "length", 5060, 5060)
        with tempfile.TemporaryDirectory() as tmp:
            cli._save_failure(tmp, job, result, "budget", "original")
            with open(os.path.join(tmp, "f0000.original.txt"),
                      encoding="utf-8") as fh:
                saved = fh.read()
        self.assertIn("REASONING ASKED: 2000", saved)
        self.assertIn("effort low", saved)
        self.assertIn("5,060 reasoning", saved)


if __name__ == "__main__":
    unittest.main()
