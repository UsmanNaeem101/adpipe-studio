"""Failure-aware recovery for structured stages that use one model call."""

import contextlib
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
import segmentation


SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
    "required": ["items"],
    "additionalProperties": False,
}


class ScriptedSingleClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def one_result(self, corpus, preamble, prompt, max_tokens=16000,
                   schema=None, **settings):
        self.calls.append({
            "corpus": corpus, "preamble": preamble, "prompt": prompt,
            "max_tokens": max_tokens, "schema": schema, **settings,
        })
        return self.replies.pop(0)


def job(max_tokens=16000):
    return llm.Job(
        id="03_discover", prompt="discover segments", max_tokens=max_tokens,
        schema=SCHEMA, effort="high", reasoning_max_tokens=7000)


def provisional_catalogue():
    return [{
        "candidate_id": "03a_0000_c000",
        "candidate_key": "validation_gaslit", "evidence_ids": [1, 2],
        "chunk_ids": ["03a_0000"], "aliases": ["Validation seekers"],
    }]


def consolidated_payload(source_id):
    return {"candidates": [{
        "slug": "new_canonical_slug",
        "name": "Validation-seeking patients",
        "definition": "People dismissed by clinicians",
        "commercial_distinction": "Recognition-led messaging",
        "inclusion_criteria": ["dismissed symptoms"],
        "exclusion_criteria": ["ordinary uncertainty"],
        "source_candidate_ids": [source_id], "merged_aliases": [],
        "discovery_status": "strong_candidate",
    }]}


def consolidation_job():
    return llm.Job(
        id="03b_consolidate", prompt="consolidate this catalogue",
        max_tokens=64000,
        schema=segmentation.consolidate_schema(["03a_0000_c000"]),
        effort="high")


class SingleStructuredRecoveryTests(unittest.TestCase):
    def run_job(self, client, request=None):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(stream):
                value = cli._run_single_structured(
                    client, "corpus", "preamble", request or job(), tmp)
            saved = sorted(os.listdir(tmp))
        return value, stream.getvalue(), saved

    def test_empty_length_is_retried_as_budget_exhaustion_not_parsed(self):
        client = ScriptedSingleClient([
            llm.BatchResult("", "length", 16000, 16000),
            llm.BatchResult(json.dumps({"items": [1]}), "stop", 5000, 6000),
        ])
        value, output, saved = self.run_job(client)
        self.assertEqual(value, {"items": [1]})
        self.assertEqual([c["max_tokens"] for c in client.calls], [16000, 24000])
        self.assertIn("OUTPUT BUDGET EXHAUSTED", output)
        self.assertIn("16k → 24k", output)
        self.assertNotIn("Expecting value", output)
        self.assertEqual(saved, ["03_discover.budget_16000.txt"])

    def test_budget_retry_preserves_reasoning_and_all_other_settings(self):
        client = ScriptedSingleClient([
            llm.BatchResult("", "length"),
            llm.BatchResult('{"items":[]}', "stop"),
        ])
        self.run_job(client)
        first, retry = client.calls
        for name in ("corpus", "preamble", "prompt", "schema", "job_id",
                     "effort", "reasoning_max_tokens"):
            self.assertEqual(retry[name], first[name], name)
        self.assertEqual((first["max_tokens"], retry["max_tokens"]),
                         (16000, 24000))

    def test_03b_uses_64k_then_128k_for_a_large_legitimate_answer(self):
        client = ScriptedSingleClient([
            llm.BatchResult("partial catalogue", "max_tokens", 1273, 64000),
            llm.BatchResult('{"items":[1]}', "stop", 1400, 65000),
        ])
        stream = io.StringIO()
        request = job(64000)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(stream):
            value = cli._run_single_structured(
                client, "corpus", "preamble", request, tmp,
                token_tiers=cli.STAGE03B_TOKEN_TIERS)
        self.assertEqual(value, {"items": [1]})
        self.assertEqual([call["max_tokens"] for call in client.calls],
                         [64000, 128000])
        self.assertIn("64k → 128k", stream.getvalue())
        self.assertIn("62,727 answer", stream.getvalue())
        for name in ("prompt", "schema", "effort", "reasoning_max_tokens"):
            self.assertEqual(client.calls[0][name], client.calls[1][name], name)

    def test_03b_128k_ceiling_is_terminal(self):
        client = ScriptedSingleClient([
            llm.BatchResult("partial", "length", 1000, 128000),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError):
                cli._run_single_structured(
                    client, "corpus", "preamble", job(128000), tmp,
                    token_tiers=cli.STAGE03B_TOKEN_TIERS)
        self.assertEqual([call["max_tokens"] for call in client.calls], [128000])

    def test_single_call_tiers_are_stage_specific(self):
        self.assertEqual(cli._single_call_tiers(64000, cli.STAGE03B_TOKEN_TIERS),
                         (64000, 128000))
        self.assertEqual(cli._single_call_tiers(16000), (16000, 24000, 32000))

    def test_03b_invalid_lineage_repairs_the_completed_response(self):
        completed = llm.BatchResult(json.dumps(
            consolidated_payload("03a_9999_c999")), "stop")
        client = ScriptedSingleClient([
            llm.BatchResult(json.dumps(
                consolidated_payload("03a_0000_c000")), "stop"),
        ])
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(stream):
            final = cli._run_03b_consolidation(
                client, "skill", "preamble", consolidation_job(),
                provisional_catalogue(), tmp, initial_result=completed)
            saved = sorted(os.listdir(tmp))
        self.assertEqual(final[0]["slug"], "new_canonical_slug")
        self.assertEqual(final[0]["segment_id"], "seg_001")
        self.assertEqual(final[0]["source_candidate_ids"],
                         ["03a_0000_c000"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["operation"], "single_structured_repair")
        self.assertIn("PREVIOUS RESPONSE", client.calls[0]["prompt"])
        self.assertIn("03B CONTRACT ERROR", stream.getvalue())
        self.assertIn("Stage 03A will not rerun", stream.getvalue())
        self.assertEqual(saved, ["03b_consolidate.contract.txt"])

    def test_03b_invalid_lineage_fails_cleanly_after_one_repair(self):
        bad = json.dumps(consolidated_payload("03a_9999_c999"))
        client = ScriptedSingleClient([llm.BatchResult(bad, "stop")])
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(stream):
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._run_03b_consolidation(
                    client, "skill", "preamble", consolidation_job(),
                    provisional_catalogue(), tmp,
                    initial_result=llm.BatchResult(bad, "stop"))
        self.assertEqual(len(client.calls), 1)
        self.assertIn("after one repair", str(ctx.exception))
        self.assertIn("03B CONTRACT REPAIR FAILED", stream.getvalue())
        self.assertNotIsInstance(ctx.exception, ValueError)

    def test_03b_finds_only_completed_audit_for_the_exact_catalogue(self):
        catalogue = provisional_catalogue()
        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "logs", "model", "2026-08-08", "request")
            os.makedirs(audit)
            request = {
                "job_id": "03b_consolidate", "operation": "single_structured",
                "request": {"messages": [{"content": "system"}, {"content":
                    "Consolidate.\nCATALOGUE:\n" + json.dumps(catalogue)}]},
            }
            response = {
                "text": json.dumps(consolidated_payload("03a_9999_c999")),
                "metadata": {"finish_reason": "stop", "usage": {
                    "completion_tokens": 54336,
                    "completion_tokens_details": {"reasoning_tokens": 3629}}},
                "provider_response": {"provider": "test-route"},
            }
            with open(os.path.join(audit, "request.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(request, fh)
            with open(os.path.join(audit, "response.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(response, fh)

            recovered = cli._03b_audit_recovery(
                tmp, "03b_consolidate", catalogue)
            mismatch = cli._03b_audit_recovery(
                tmp, "03b_consolidate", provisional_catalogue() + [{
                    "candidate_id": "03a_0001_c000",
                    "candidate_key": "other", "evidence_ids": [3],
                    "chunk_ids": ["03a_0001"], "aliases": ["Other"]}])

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered[0].completion_tokens, 54336)
        self.assertEqual(recovered[0].reasoning_tokens, 3629)
        self.assertIsNone(mismatch)

    def test_empty_refusal_or_filter_fails_without_retry(self):
        for stop in llm.NO_RETRY_STOP_REASONS:
            with self.subTest(stop=stop):
                client = ScriptedSingleClient([llm.BatchResult("", stop)])
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(cli.BatchOutputError) as ctx:
                        cli._run_single_structured(
                            client, "corpus", "preamble", job(), tmp)
                self.assertEqual(len(client.calls), 1)
                self.assertIn("provider/refusal failure", str(ctx.exception))
                self.assertNotIn("Expecting value", str(ctx.exception))

    def test_unexplained_empty_response_is_provider_failure(self):
        client = ScriptedSingleClient([llm.BatchResult("", "stop")])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._run_single_structured(
                    client, "corpus", "preamble", job(), tmp)
        self.assertIn("provider returned an empty response", str(ctx.exception))
        self.assertEqual(len(client.calls), 1)

    def test_nonempty_malformed_json_gets_one_shape_repair(self):
        client = ScriptedSingleClient([
            llm.BatchResult('{"items": [1,}', "stop"),
            llm.BatchResult('{"items": [1]}', "stop"),
        ])
        value, output, saved = self.run_job(client)
        self.assertEqual(value, {"items": [1]})
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([c["max_tokens"] for c in client.calls], [16000, 16000])
        self.assertIn("RECOVERY TASK", client.calls[1]["prompt"])
        self.assertIn("shape repair", output)
        self.assertEqual(saved, ["03_discover.malformed.txt"])

    def test_schema_invalid_json_also_gets_shape_repair(self):
        client = ScriptedSingleClient([
            llm.BatchResult('{"wrong": []}', "stop"),
            llm.BatchResult('{"items": []}', "stop"),
        ])
        value, _output, _saved = self.run_job(client)
        self.assertEqual(value, {"items": []})
        self.assertEqual(len(client.calls), 2)

    def test_valid_json_continues_without_recovery(self):
        client = ScriptedSingleClient([
            llm.BatchResult('{"items": [1, 2]}', "stop", 100, 200),
        ])
        value, output, saved = self.run_job(client)
        self.assertEqual(value, {"items": [1, 2]})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(output, "")
        self.assertEqual(saved, [])

    def test_32k_budget_stop_is_terminal_and_never_loops(self):
        client = ScriptedSingleClient([
            llm.BatchResult("", "length", 32000, 32000),
        ])
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(stream):
                with self.assertRaises(cli.BatchOutputError) as ctx:
                    cli._run_single_structured(
                        client, "corpus", "preamble", job(32000), tmp)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("output token budget", str(ctx.exception))
        self.assertIn("STRUCTURED CALL FAILED", stream.getvalue())
        self.assertNotIn("JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
