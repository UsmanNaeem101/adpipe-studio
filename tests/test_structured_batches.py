import json
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm
import openrouter


SCHEMA = {
    "type": "object",
    "properties": {"records": {"type": "array", "items": {"type": "object"}}},
    "required": ["records"],
    "additionalProperties": False,
}


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": '{"records":[]}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode()


class StructuredBatchTests(unittest.TestCase):
    def setUp(self):
        self.audit_tmp = tempfile.TemporaryDirectory()
        self.audit_env = mock.patch.dict(
            os.environ, {"ADPIPE_LOG_DIR": self.audit_tmp.name})
        self.audit_env.start()

    def tearDown(self):
        self.audit_env.stop()
        self.audit_tmp.cleanup()

    def test_anthropic_params_include_job_schema(self):
        client = object.__new__(llm.Client)
        client.model = "test-model"
        client.effort = "high"
        params = client._params([], "prompt", 100, SCHEMA)
        self.assertEqual(
            params["output_config"]["format"],
            {"type": "json_schema", "schema": SCHEMA},
        )

    def test_openrouter_structured_request_requires_supported_route(self):
        client = object.__new__(openrouter.Client)
        client.model = "test/model"
        client.key = "test-key"
        client.effort = "high"
        client.spent = {"in": 0, "out": 0, "reasoning": 0, "cache_write": 0, "cache_read": 0}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as call:
            client._post([{"role": "user", "content": "x"}], 100, SCHEMA)
        request = call.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["provider"]["require_parameters"])
        logged = {}
        for base, _dirs, files in os.walk(self.audit_tmp.name):
            for name in files:
                if name in ("request.json", "response.json"):
                    with open(os.path.join(base, name), encoding="utf-8") as fh:
                        logged[name] = json.load(fh)
        self.assertEqual(logged["request.json"]["request"]["messages"][0]["content"],
                         "x")
        self.assertEqual(logged["response.json"]["text"], '{"records":[]}')
        self.assertEqual(
            logged["response.json"]["provider_response"]["usage"]["prompt_tokens"], 1)

    def test_openrouter_batch_passes_each_job_schema(self):
        client = object.__new__(openrouter.Client)
        client.verbose = False
        client.effort = "high"
        seen = []

        def fake_post(_messages, _max_tokens, schema=None, retries=3,
                      job_id=None, operation="completion", **_kwargs):
            seen.append(schema)
            return '{"records":[]}'

        client._post = fake_post
        jobs = [llm.Job("a", "one", schema=SCHEMA), llm.Job("b", "two")]
        result = client.batch("corpus", "preamble", jobs)
        self.assertEqual(set(result), {"a", "b"})
        self.assertCountEqual(seen, [SCHEMA, None])

    def test_openrouter_falls_back_when_route_cannot_enforce_schema(self):
        client = object.__new__(openrouter.Client)
        client.model = "test/model"
        client.key = "test-key"
        client.verbose = False
        client.effort = "high"
        client.spent = {"in": 0, "out": 0, "reasoning": 0, "cache_write": 0, "cache_read": 0}
        unsupported = urllib.error.HTTPError(
            "https://example.test", 404, "not found", {},
            io.BytesIO(b'{"error":"No endpoints support requested parameters: '
                       b'response_format"}'))
        with mock.patch("urllib.request.urlopen",
                        side_effect=[unsupported, FakeResponse()]) as call:
            result = client._post([{"role": "user", "content": "x"}], 100, SCHEMA)
        fallback_body = json.loads(call.call_args_list[1].args[0].data)
        self.assertEqual(result.text, '{"records":[]}')
        self.assertNotIn("response_format", fallback_body)
        self.assertIn('"records"', fallback_body["messages"][-1]["content"])

    def test_batch_rows_accepts_fenced_json(self):
        jobs = [llm.Job("a", "prompt", schema=SCHEMA)]
        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"a": '```json\n{"records":[{"evidence_id":1}]}\n```'},
                jobs,
                "records",
                tmp,
            )
        self.assertEqual(rows, [{"evidence_id": 1}])

    def test_batch_rows_fails_and_saves_raw_response(self):
        jobs = [llm.Job("a", "prompt", schema=SCHEMA), llm.Job("b", "prompt")]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError):
                cli._batch_rows({"a": "not json"}, jobs, "records", tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "a.original.txt")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "b.original.txt")))

    def test_batch_rows_repairs_and_preserves_both_payloads(self):
        jobs = [llm.Job("a", "prompt", schema=cli.FILTER_SCHEMA,
                        expected_ids=(1,))]
        repaired = json.dumps({"records": [{
            "evidence_id": 1,
            "decision": "retain",
            "retention_reasons": ["specific_problem"],
            "rejection_reasons": [],
        }]})
        seen = []

        def repair(failures):
            seen.extend(failures)
            return {"a": repaired}

        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"a": ('{"records":[{"evidence_id":1,"decision":"retain",'
                       '"rejection_reasons":[]}]}')}, jobs, "records", tmp,
                repair=repair)
            with open(os.path.join(tmp, "a.original.txt"), encoding="utf-8") as fh:
                original_audit = fh.read()
            with open(os.path.join(tmp, "a.repaired.txt"), encoding="utf-8") as fh:
                repaired_audit = fh.read()

        self.assertIn("retention_reasons", seen[0][2])
        self.assertIn('"evidence_id":1', original_audit)
        # Saved payloads now carry a diagnostic header — the stop reason and
        # response size are what distinguish an empty reply from a prose one.
        self.assertIn("STOP REASON:", repaired_audit)
        self.assertTrue(repaired_audit.endswith(repaired))
        self.assertEqual(rows[0]["decision"], "retain")

    def test_batch_rows_repairs_incomplete_chunk_coverage(self):
        jobs = [llm.Job("a", "prompt", schema=SCHEMA, expected_ids=(1, 2))]
        original = '{"records":[{"evidence_id":1}]}'
        fixed = '{"records":[{"evidence_id":1},{"evidence_id":2}]}'
        reasons = []

        def repair(failures):
            reasons.append(failures[0][2])
            return {"a": fixed}

        with tempfile.TemporaryDirectory() as tmp:
            rows = cli._batch_rows(
                {"a": original}, jobs, "records", tmp, repair=repair)
        self.assertIn("1 missing", reasons[0])
        self.assertEqual([row["evidence_id"] for row in rows], [1, 2])

    def test_record_stage_requires_complete_unique_coverage(self):
        with self.assertRaises(cli.BatchOutputError):
            cli._require_exact_ids(
                [{"evidence_id": 1}, {"evidence_id": 1}], {1, 2}, "filter")

    def test_ingest_uses_schemas_and_writes_only_judged_records(self):
        class FakeEstimate:
            def explain(self):
                return "fake"

        class FakeClient:
            def estimate(self, *_args, **_kwargs):
                return FakeEstimate()

            def prewarm(self, *_args):
                pass

            def batch(self, _corpus, _preamble, jobs):
                result = {}
                for job in jobs:
                    if job.schema is cli.FILTER_SCHEMA:
                        ids = [int(x) for x in __import__("re").findall(
                            r"^\[(\d+)\]", job.prompt, __import__("re").M)]
                        result[job.id] = json.dumps({"records": [
                            {"evidence_id": i, "decision": "retain",
                             "retention_reasons": ["specific_problem"],
                             "rejection_reasons": []}
                            for i in ids]})
                    elif job.schema is cli.DEDUP_SCHEMA:
                        result[job.id] = '{"groups":[]}'
                    else:
                        raise AssertionError("ingest job had no structured schema")
                return result

        raw = (
            "I have a very specific shoulder problem that wakes me every single night.\n\n"
            "My current pillow becomes flat and uncomfortable before the morning arrives."
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "raw.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "shoulders"}
            args = SimpleNamespace(source=source, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=FakeClient()), \
                    mock.patch.object(llm, "confirm", return_value=True):
                cli.cmd_ingest(cfg, args)
            with open(os.path.join(tmp, "research", "voc", "filtered_voc.jsonl"),
                      encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["decision"] == "retain" for r in rows))

    def test_ingest_automatically_repairs_bad_filter_output(self):
        class FakeEstimate:
            def explain(self):
                return "fake"

        class FakeClient:
            def __init__(self):
                self.batch_calls = 0

            def estimate(self, *_args, **_kwargs):
                return FakeEstimate()

            def prewarm(self, *_args):
                pass

            def batch(self, _corpus, _preamble, jobs):
                self.batch_calls += 1
                result = {}
                for job in jobs:
                    if job.schema is cli.FILTER_SCHEMA and not job.prompt.startswith(
                            "RECOVERY TASK"):
                        result[job.id] = "I judged them, but forgot to emit JSON."
                    elif job.schema is cli.FILTER_SCHEMA:
                        result[job.id] = json.dumps({"records": [
                            {"evidence_id": i, "decision": "retain",
                             "retention_reasons": ["specific_problem"],
                             "rejection_reasons": []}
                            for i in job.expected_ids]})
                    elif job.schema is cli.DEDUP_SCHEMA:
                        result[job.id] = '{"groups":[]}'
                return result

        raw = (
            "I have a very specific shoulder problem that wakes me every single night.\n\n"
            "My current pillow becomes flat and uncomfortable before the morning arrives."
        )
        fake_client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "raw.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "shoulders"}
            args = SimpleNamespace(source=source, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=fake_client), \
                    mock.patch.object(llm, "confirm", return_value=True):
                cli.cmd_ingest(cfg, args)
            audit_dir = os.path.join(tmp, "research", "voc", "_model_failures", "01_filter")
            self.assertTrue(os.path.exists(os.path.join(
                audit_dir, "f0000.original.txt")))
            self.assertTrue(os.path.exists(os.path.join(
                audit_dir, "f0000.repaired.txt")))
            with open(os.path.join(tmp, "research", "voc", "filtered_voc.jsonl"),
                      encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]

        self.assertEqual(fake_client.batch_calls, 3)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
