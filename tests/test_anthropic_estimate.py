"""Native Anthropic counting and shared-prefix cache contracts."""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import llm
from llm import Job


class FakeMessages:
    def __init__(self):
        self.count_calls = []
        self.inference_calls = []

    def count_tokens(self, **params):
        system = params.get("system")
        if isinstance(system, str):
            if not system.strip():
                raise AssertionError("empty string system reached Anthropic")
        elif system is not None:
            for block in system:
                if (isinstance(block, dict) and block.get("type") == "text"
                        and not str(block.get("text") or "").strip()):
                    raise AssertionError("empty system text block reached Anthropic")
        self.count_calls.append(params)
        # The fake only supplies deterministic native-counter behaviour. Tests
        # assert that production code used count_tokens, not a local estimator.
        chars = 0
        if isinstance(system, str):
            chars += len(system)
        elif system:
            chars += sum(len(block.get("text", "")) for block in system
                         if isinstance(block, dict))
        chars += sum(len(message.get("content", ""))
                     for message in params.get("messages", []))
        chars += len(json.dumps(params.get("output_config", {}), sort_keys=True))
        return SimpleNamespace(input_tokens=max(chars // 4, 1))

    def create(self, **params):
        self.inference_calls.append(params)
        raise AssertionError("estimation must never call messages.create")


def client_for(model="claude-haiku-4-5"):
    client = llm.Client.__new__(llm.Client)
    client.model = model
    client.effort = "high"
    client.verbose = False
    client.spent = {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0}
    client.client = SimpleNamespace(messages=FakeMessages())
    return client


class AnthropicEstimateTests(unittest.TestCase):
    def test_count_omits_empty_or_absent_system(self):
        client = client_for()

        for system in (None, "", "   ", [],
                       [{"type": "text", "text": ""}]):
            with self.subTest(system=system):
                client.count(system, "real user prompt")
                self.assertNotIn("system",
                                 client.client.messages.count_calls[-1])

    def test_count_preserves_string_system_prompt(self):
        client = client_for()
        client.count("real system prompt", "real user prompt")
        self.assertEqual(client.client.messages.count_calls[-1]["system"],
                         "real system prompt")

    def test_count_preserves_structured_system_blocks(self):
        client = client_for()
        system = [
            {"type": "text", "text": "operating instructions"},
            {"type": "text", "text": "candidate catalogue"},
        ]
        client.count(system, "real user prompt")
        self.assertEqual(client.client.messages.count_calls[-1]["system"], system)

    def test_count_preserves_cached_system_blocks(self):
        client = client_for()
        cached = [{
            "type": "text", "text": "stable candidate catalogue",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]
        client.count(cached, "real user prompt")
        self.assertEqual(client.client.messages.count_calls[-1]["system"], cached)

    def test_haiku_aliases_keep_schema_and_omit_unsupported_effort(self):
        schema = {"type": "object", "properties": {}}
        for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                client = client_for(model)
                client.count("system", "prompt", schema=schema, effort="high")
                params = client.client.messages.count_calls[-1]
                self.assertEqual(params["thinking"], {"type": "adaptive"})
                self.assertNotIn("effort", params["output_config"])
                self.assertEqual(params["output_config"]["format"]["schema"],
                                 schema)

    def test_system_builder_caches_last_nonempty_block_only(self):
        client = client_for()
        system = client._system("candidate catalogue", "stage preamble")
        self.assertEqual([block["text"] for block in system],
                         ["stage preamble", "candidate catalogue"])
        self.assertNotIn("cache_control", system[0])
        self.assertEqual(system[1]["cache_control"],
                         {"type": "ephemeral", "ttl": "1h"})

        preamble_only = client._system("", "stage preamble")
        self.assertEqual(preamble_only[0]["cache_control"]["ttl"], "1h")
        self.assertIsNone(client._system("", ""))

    def test_prewarm_uses_same_cached_prefix_and_skips_absent_system(self):
        class WarmMessages(FakeMessages):
            def create(self, **params):
                self.inference_calls.append(params)
                usage = SimpleNamespace(
                    input_tokens=0, output_tokens=1,
                    cache_creation_input_tokens=42_000,
                    cache_read_input_tokens=0)
                return SimpleNamespace(usage=usage)

        client = client_for()
        client.client.messages = WarmMessages()
        audit = SimpleNamespace(response=lambda *_args, **_kwargs: None,
                                error=lambda *_args, **_kwargs: None)
        with mock.patch.object(llm.auditlog, "start", return_value=audit):
            client.prewarm("candidate catalogue", "stage preamble")

        self.assertEqual(len(client.client.messages.inference_calls), 1)
        params = client.client.messages.inference_calls[0]
        self.assertEqual(params["max_tokens"], 1)
        self.assertEqual(params["system"],
                         client._system("candidate catalogue", "stage preamble"))
        self.assertEqual(params["system"][-1]["cache_control"]["ttl"], "1h")

        with mock.patch.object(llm.auditlog, "start", return_value=audit):
            client.prewarm("", "")
        self.assertEqual(len(client.client.messages.inference_calls), 1)

    def test_stage03e_native_estimate_with_real_catalogue_scale_and_aliases(self):
        # The live 208-candidate catalogue is about 169k characters / 42k tokens.
        catalogue = json.dumps([{
            "segment_id": f"seg_{index:04d}",
            "name": "Audience " + ("n" * 40),
            "definition": "d" * 200,
            "commercial_distinction": "c" * 150,
            "inclusion_criteria": ["i" * 40, "j" * 40],
            "exclusion_criteria": ["x" * 40],
        } for index in range(208)], indent=2)
        # Pad to the measured live request size without changing the test's
        # semantic role: a stable, non-empty candidate-catalogue prefix.
        catalogue += " " * max(169_000 - len(catalogue), 0)
        jobs = [Job(
            id=f"03e_{index:04d}", prompt="evidence batch " + ("v" * 31_000),
            max_tokens=12_000,
            schema={"type": "object", "properties": {}},
            expected_ids=(index,)) for index in range(53)]

        for model in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
            with self.subTest(model=model):
                client = client_for(model)
                estimate = client.estimate(catalogue, "Stage 03 preamble", jobs,
                                           batched=True)

                self.assertEqual(estimate.jobs, 53)
                self.assertGreater(estimate.cached_tokens, 40_000)
                self.assertLess(estimate.cached_tokens, 45_000)
                self.assertGreater(estimate.uncached_in, 0)
                self.assertEqual(client.client.messages.inference_calls, [])
                self.assertEqual(len(client.client.messages.count_calls), 55)
                self.assertTrue(all(
                    call["model"] == model
                    for call in client.client.messages.count_calls))
                cached_calls = [
                    call for call in client.client.messages.count_calls
                    if "system" in call]
                self.assertTrue(cached_calls)
                self.assertTrue(all(
                    call["system"][-1]["cache_control"]["ttl"] == "1h"
                    for call in cached_calls))


if __name__ == "__main__":
    unittest.main()
