"""Note: this file arrived with three further test classes covering an
in-client budget retry, a `reasoning=` request parameter and a
`failure_reasons` dict. None of those survived the merge with the adaptive
recovery ladder in cli.py, which reaches the same outcome with cost accounting
and diagnostics the caller can see. Their equivalents live in
tests/test_batch_recovery.py. What remains here is the shared rule itself.

Regression tests for the stage-01 VOC filter failure.

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
