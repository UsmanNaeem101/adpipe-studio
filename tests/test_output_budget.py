"""Regression tests for the stage-01 VOC filter failure.

The observed run: 83 batches, 82 initial responses "malformed or missing", the
repair pass fixing only 7, and a parser error of
`Expecting value: line 1 column 1 (char 0)` — which is what json.loads("")
says. The responses were not malformed; they were EMPTY, because deepseek spent
the whole `max_tokens` allowance on reasoning and stopped before writing JSON.

These tests pin the four outcomes the pipeline must now distinguish:

  A  empty content + length/max_tokens  -> re-run the ORIGINAL request, bigger budget
  B  non-empty but malformed/truncated  -> tolerant extraction / shape repair
  C  valid JSON, wrong `records` shape  -> shape repair
  D  empty content for any other reason -> report the provider's reason as-is
"""

import io
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
import modeloutput
import openrouter


SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {"type": "object"}}},
    "required": ["records"],
    "additionalProperties": False,
}


class Reply:
    """A canned OpenRouter chat-completions response."""

    def __init__(self, content, finish_reason="stop", completion_tokens=8000):
        self.payload = {
            "choices": [{"message": {"content": content},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 12000,
                      "completion_tokens": completion_tokens},
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def make_client():
    c = object.__new__(openrouter.Client)
    c.model = "deepseek/deepseek-v4-flash"
    c.key = "test-key"
    c.verbose = False
    c.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    c.failures = {}
    return c


class ModelOutputRuleTests(unittest.TestCase):
    """The detection rule itself, which now lives in exactly one module."""

    def test_empty_plus_length_is_budget_exhaustion(self):
        self.assertTrue(modeloutput.is_budget_exhaustion("", "length"))
        self.assertTrue(modeloutput.is_budget_exhaustion("   \n ", "max_tokens"))

    def test_non_empty_is_never_budget_exhaustion(self):
        # Truncated JSON is case B — it has content, so it goes to repair, not
        # to a budget retry.
        self.assertFalse(modeloutput.is_budget_exhaustion('{"records":[{"ev',
                                                          "length"))

    def test_empty_without_length_is_not_budget_exhaustion(self):
        self.assertFalse(modeloutput.is_budget_exhaustion("", "content_filter"))
        self.assertFalse(modeloutput.is_budget_exhaustion("", "stop"))

    def test_budget_message_names_the_budget_and_denies_json_blame(self):
        msg = modeloutput.budget_message(8000, "length", "f0007")
        self.assertIn("8000", msg)
        self.assertIn("f0007", msg)
        self.assertIn("not malformed JSON", msg)

    def test_empty_reason_reports_the_provider_reason(self):
        self.assertIn("content_filter", modeloutput.empty_reason("content_filter"))
        self.assertIn("provider", modeloutput.empty_reason("content_filter"))


class OpenRouterBudgetTests(unittest.TestCase):
    def setUp(self):
        self.audit_tmp = tempfile.TemporaryDirectory()
        self.audit_env = mock.patch.dict(
            os.environ, {"ADPIPE_LOG_DIR": self.audit_tmp.name})
        self.audit_env.start()

    def tearDown(self):
        self.audit_env.stop()
        self.audit_tmp.cleanup()

    # ---- A: empty + length -------------------------------------------------

    def test_empty_length_reply_retries_original_request_with_bigger_budget(self):
        """The exact stage-01 failure: retry the ORIGINAL classification request."""
        client = make_client()
        replies = [Reply("", finish_reason="length"),
                   Reply('{"records":[{"evidence_id":1,"decision":"retain"}]}')]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as call:
            out = client._post([{"role": "user", "content": "classify"}], 8000,
                               SCHEMA, job_id="f0007")

        self.assertEqual(json.loads(out)["records"][0]["evidence_id"], 1)
        self.assertEqual(call.call_count, 2)
        first, second = (json.loads(c.args[0].data) for c in call.call_args_list)
        # Same request, larger allowance — not a repair prompt.
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(first["max_tokens"], 8000)
        self.assertEqual(second["max_tokens"], 8000 * modeloutput.RETRY_MULTIPLIER)

    def test_budget_retry_happens_only_once(self):
        client = make_client()
        replies = [Reply("", finish_reason="length"),
                   Reply("", finish_reason="length")]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as call:
            with self.assertRaises(modeloutput.OutputBudgetExhausted) as ctx:
                client._post([{"role": "user", "content": "classify"}], 8000,
                             SCHEMA, job_id="f0007")
        self.assertEqual(call.call_count, 2)
        self.assertIn("output token budget", str(ctx.exception))

    # ---- D: empty for another reason ---------------------------------------

    def test_empty_reply_without_length_is_not_called_a_json_error(self):
        client = make_client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=[Reply("", finish_reason="content_filter")]) as call:
            with self.assertRaises(openrouter.EmptyResponse) as ctx:
                client._post([{"role": "user", "content": "classify"}], 8000,
                             SCHEMA, job_id="f0007")
        self.assertEqual(call.call_count, 1)  # no pointless budget retry
        self.assertIn("content_filter", str(ctx.exception))

    # ---- normal path -------------------------------------------------------

    def test_successful_reply_passes_straight_through(self):
        client = make_client()
        body = '{"records":[{"evidence_id":1,"decision":"retain"}]}'
        with mock.patch("urllib.request.urlopen", side_effect=[Reply(body)]) as call:
            out = client._post([{"role": "user", "content": "c"}], 8000, SCHEMA)
        self.assertEqual(out, body)
        self.assertEqual(call.call_count, 1)

    # ---- reasoning control -------------------------------------------------

    def test_reasoning_control_is_sent_when_a_job_asks_for_it(self):
        client = make_client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=[Reply('{"records":[]}')]) as call:
            client._post([{"role": "user", "content": "c"}], 8000, SCHEMA,
                         reasoning={"enabled": False})
        self.assertEqual(json.loads(call.call_args.args[0].data)["reasoning"],
                         {"enabled": False})

    def test_no_reasoning_key_when_the_job_does_not_ask(self):
        client = make_client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=[Reply('{"records":[]}')]) as call:
            client._post([{"role": "user", "content": "c"}], 8000, SCHEMA)
        self.assertNotIn("reasoning", json.loads(call.call_args.args[0].data))

    def test_batch_forwards_each_jobs_reasoning_and_records_failures(self):
        client = make_client()
        seen = {}

        def fake_post(_messages, _max_tokens, schema=None, retries=3, job_id=None,
                      operation="completion", reasoning=None, budget_retry=True):
            seen[job_id] = reasoning
            if job_id == "b":
                raise openrouter.EmptyResponse("empty, finish_reason='error'")
            return '{"records":[]}'

        client._post = fake_post
        jobs = [llm.Job("a", "one", schema=SCHEMA, reasoning={"enabled": False}),
                llm.Job("b", "two", schema=SCHEMA)]
        out = client.batch("corpus", "preamble", jobs)

        self.assertEqual(set(out), {"a"})          # failed job is not faked out
        self.assertEqual(seen["a"], {"enabled": False})
        self.assertIn("finish_reason='error'", client.failures["b"])


class BatchRowsReportingTests(unittest.TestCase):
    """cli._batch_rows must describe what actually happened, per case."""

    def test_missing_response_reports_provider_reason_not_json_error(self):
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA)]
        reasons = {"f0007": "the model returned empty content with "
                            "finish_reason='error'"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._batch_rows({}, jobs, "records", tmp,
                                failure_reasons=reasons)
        message = str(ctx.exception)
        self.assertIn("finish_reason='error'", message)
        self.assertNotIn("Expecting value", message)

    def test_empty_string_response_is_not_reported_as_a_syntax_error(self):
        """The old behaviour handed "" to json.loads and blamed JSON syntax."""
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA)]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._batch_rows({"f0007": ""}, jobs, "records", tmp)
        self.assertNotIn("Expecting value", str(ctx.exception))
        self.assertIn("empty response", str(ctx.exception))

    def test_truncated_json_still_reaches_the_repair_path(self):
        """Case B is unchanged: there is content, so repair is the right move."""
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA,
                        expected_ids=(1,))]
        calls = []

        def repair(failures):
            calls.append([(j.id, reason) for j, _raw, reason in failures])
            return {"f0007": '{"records":[{"evidence_id":1,"decision":"retain"}]}'}

        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"f0007": '{"records":[{"evidence_id":1,"deci'},
                jobs, "records", tmp, repair=repair)
        self.assertEqual(rows, [{"evidence_id": 1, "decision": "retain"}])
        self.assertEqual(len(calls), 1)

    def test_fenced_json_is_accepted_without_a_repair_round(self):
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA, expected_ids=(1,))]
        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"f0007": '```json\n{"records":[{"evidence_id":1,'
                          '"decision":"retain"}]}\n```'},
                jobs, "records", tmp,
                repair=lambda f: self.fail("repair should not run"))
        self.assertEqual(rows, [{"evidence_id": 1, "decision": "retain"}])

    def test_schema_valid_response_passes(self):
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA, expected_ids=(1, 2))]
        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"f0007": '{"records":[{"evidence_id":1,"decision":"retain"},'
                          '{"evidence_id":2,"decision":"reject"}]}'},
                jobs, "records", tmp)
        self.assertEqual([r["evidence_id"] for r in rows], [1, 2])

    def test_schema_invalid_response_is_reported_as_a_shape_problem(self):
        # Valid JSON, wrong shape: `records` is an object, not a list (case C).
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA)]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._batch_rows({"f0007": '{"records":{"evidence_id":1}}'},
                                jobs, "records", tmp)
        self.assertIn("must be array", str(ctx.exception))

    def test_incomplete_coverage_is_still_fail_closed(self):
        """No partial corpus: a batch that judged 1 of 2 records fails the stage."""
        jobs = [llm.Job("f0007", "prompt", schema=SCHEMA, expected_ids=(1, 2))]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._batch_rows(
                    {"f0007": '{"records":[{"evidence_id":1,"decision":"retain"}]}'},
                    jobs, "records", tmp)
        self.assertIn("coverage", str(ctx.exception))
        self.assertIn("No stage output was written", str(ctx.exception))


class AnthropicBudgetTests(unittest.TestCase):
    """The same rule applies on the Anthropic path — it had the same hole."""

    def setUp(self):
        self.audit_tmp = tempfile.TemporaryDirectory()
        self.audit_env = mock.patch.dict(
            os.environ, {"ADPIPE_LOG_DIR": self.audit_tmp.name})
        self.audit_env.start()
        # batch() imports these lazily; the SDK is not a test dependency. Both are
        # plain typed dicts upstream, so dict stands in for them exactly.
        self.sdk_modules = mock.patch.dict(sys.modules, {
            "anthropic": mock.Mock(),
            "anthropic.types": mock.Mock(),
            "anthropic.types.message_create_params": mock.Mock(
                MessageCreateParamsNonStreaming=dict),
            "anthropic.types.messages": mock.Mock(),
            "anthropic.types.messages.batch_create_params": mock.Mock(
                Request=lambda **kw: kw),
        })
        self.sdk_modules.start()

    def tearDown(self):
        self.sdk_modules.stop()
        self.audit_env.stop()
        self.audit_tmp.cleanup()

    def _client(self, pages):
        """A real llm.Client with only the SDK surface faked.

        `pages` is a list of {custom_id: (text, stop_reason)} — one per batch
        submission, so a retry submission reads the next page.
        """
        client = object.__new__(llm.Client)
        client.model = "claude-opus-5"
        client.effort = "high"
        client.verbose = False
        client.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
        submissions = []

        # Plain namespaces, not Mocks: the audit logger serialises the provider
        # response, and a Mock answers every attribute with another Mock, so it
        # never bottoms out.
        usage = SimpleNamespace(input_tokens=12000, output_tokens=8000,
                                cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)

        def results(_batch_id):
            page = pages[len(submissions) - 1]
            for cid, (text, stop) in page.items():
                block = SimpleNamespace(type="text", text=text)
                yield SimpleNamespace(
                    custom_id=cid,
                    result=SimpleNamespace(
                        type="succeeded",
                        message=SimpleNamespace(content=[block], usage=usage,
                                                stop_reason=stop)))

        def create(requests):
            submissions.append([r["custom_id"] for r in requests])
            return SimpleNamespace(id=f"batch_{len(submissions)}")

        sdk = mock.Mock()
        sdk.messages.batches.create.side_effect = create
        sdk.messages.batches.retrieve.return_value = SimpleNamespace(
            processing_status="ended")
        sdk.messages.batches.results.side_effect = results
        client.client = sdk
        self.submissions = submissions
        self.sdk = sdk
        return client

    def test_empty_max_tokens_message_is_retried_with_a_bigger_budget(self):
        client = self._client([
            {"f0007": ("", "max_tokens")},                 # spent it all thinking
            {"f0007": ('{"records":[]}', "end_turn")},      # retry succeeds
        ])
        out = client.batch("corpus", "preamble",
                           [llm.Job("f0007", "prompt", max_tokens=8000)])

        self.assertEqual(out, {"f0007": '{"records":[]}'})
        self.assertEqual(len(self.submissions), 2)
        retried = self.sdk.messages.batches.create.call_args_list[1]
        self.assertEqual(
            retried.kwargs["requests"][0]["params"]["max_tokens"],
            8000 * modeloutput.RETRY_MULTIPLIER)

    def test_budget_retry_is_not_repeated_forever(self):
        client = self._client([
            {"f0007": ("", "max_tokens")},
            {"f0007": ("", "max_tokens")},
        ])
        out = client.batch("corpus", "preamble",
                           [llm.Job("f0007", "prompt", max_tokens=8000)])
        self.assertEqual(out, {})               # fail-closed, not faked out
        self.assertEqual(len(self.submissions), 2)

    def test_empty_reply_without_a_length_stop_is_reported_not_retried(self):
        client = self._client([{"f0007": ("", "end_turn")}])
        out = client.batch("corpus", "preamble",
                           [llm.Job("f0007", "prompt", max_tokens=8000)])
        self.assertEqual(out, {})
        self.assertEqual(len(self.submissions), 1)
        self.assertIn("end_turn", client.failures["f0007"])

    def test_normal_reply_is_untouched(self):
        client = self._client([{"f0007": ('{"records":[]}', "end_turn")}])
        out = client.batch("corpus", "preamble",
                           [llm.Job("f0007", "prompt", max_tokens=8000)])
        self.assertEqual(out, {"f0007": '{"records":[]}'})
        self.assertEqual(len(self.submissions), 1)


if __name__ == "__main__":
    unittest.main()
