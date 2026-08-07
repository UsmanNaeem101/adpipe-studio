"""Tests for the tolerant JSON parser and the automatic repair pass."""

import sys
import unittest

sys.path.insert(0, "pipeline")
import presets


class JsonParsingTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(presets._extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(
            presets._extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_prose_prefix_and_suffix(self):
        self.assertEqual(
            presets._extract_json('Sure! Here: {"x": 1} hope this helps'),
            {"x": 1})

    def test_brace_inside_string_value(self):
        # A '{' or '}' inside a quoted value must not end the object early.
        self.assertEqual(
            presets._extract_json('{"note": "use {curly} braces", "b": 2}'),
            {"note": "use {curly} braces", "b": 2})

    def test_trailing_comma_repair(self):
        self.assertEqual(
            presets._extract_json('{"a": 1, "b": [1, 2,],}'), {"a": 1, "b": [1, 2]})

    def test_truncated_output_is_reported_as_truncated(self):
        text = '{"a": 1, "b": [2,'  # string never closes
        with self.assertRaises(presets.PresetError) as ctx:
            presets._extract_json(text)
        self.assertIn("truncated", str(ctx.exception))

    def test_no_json_raises(self):
        with self.assertRaises(presets.PresetError):
            presets._extract_json("the model just said no")


class ModelJsonRepairTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_repairs_truncated_first_reply(self):
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            if label == "model":
                return '{"a": 1, "b": [2,'
            return '{"a": 1, "b": [2, 3]}'
        presets._post_text = fake
        out = presets.model_json("openrouter", {"openrouter": "k"}, "m",
                                 "sys", "prompt", max_tokens=100)
        self.assertEqual(out, {"a": 1, "b": [2, 3]})
        self.assertEqual(self.calls, ["model", "model repair"])

    def test_valid_first_reply_no_repair_call(self):
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            return '{"ok": true}'
        presets._post_text = fake
        out = presets.model_json("openrouter", {"openrouter": "k"}, "m",
                                 "sys", "prompt", max_tokens=100)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(self.calls, ["model"])

    def test_repair_prompt_preserves_original_and_prior(self):
        seen = {}
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            if label == "model":
                return '{"a": [1,'  # broken
            seen["prompt"] = prompt
            return '{"a": [1, 2]}'
        presets._post_text = fake
        presets.model_json("openrouter", {"openrouter": "k"}, "m", "sys",
                           "original request body", max_tokens=100)
        self.assertIn("original request body", seen["prompt"])
        self.assertIn('{"a": [1,', seen["prompt"])  # prior output handed back
        self.assertIn("RECOVERY TASK", seen["prompt"])

    def test_http_error_propagates_without_repair(self):
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            raise presets.PresetError("the key for X was rejected (401).")
        presets._post_text = fake
        with self.assertRaises(presets.PresetError) as ctx:
            presets.model_json("openrouter", {"openrouter": "k"}, "m",
                               "sys", "prompt")
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(self.calls, ["model"])  # never attempted a repair

    def test_double_failure_raises_combined_error(self):
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            return '{"bad'
        presets._post_text = fake
        with self.assertRaises(presets.PresetError) as ctx:
            presets.model_json("openrouter", {"openrouter": "k"}, "m",
                               "sys", "prompt")
        self.assertIn("repair pass also failed", str(ctx.exception))
        self.assertEqual(self.calls, ["model", "model repair"])

    def test_token_budget_cutoff_retries_with_larger_budget(self):
        # First call raises OutputBudgetError (model consumed max_tokens on
        # reasoning, wrote nothing). The retry must use a larger budget and
        # return the parsed JSON.
        seen = {}
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            seen["max_tokens"] = max_tokens
            seen["calls"] = seen.get("calls", 0) + 1
            if seen["calls"] == 1:
                raise presets.OutputBudgetError(
                    "the model hit its output token budget (10000) during "
                    "reasoning and stopped before writing any JSON")
            return '{"a": 1, "b": 2}'
        presets._post_text = fake
        out = presets.model_json("openrouter", {"openrouter": "k"}, "m",
                                 "sys", "prompt", max_tokens=10000)
        self.assertEqual(out, {"a": 1, "b": 2})
        self.assertEqual(seen["calls"], 2)
        self.assertEqual(seen["max_tokens"], 30000)  # 3x budget

    def test_token_budget_cutoff_retry_failure_is_clear(self):
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            raise presets.OutputBudgetError("ran out of tokens")
        presets._post_text = fake
        with self.assertRaises(presets.PresetError) as ctx:
            presets.model_json("openrouter", {"openrouter": "k"}, "m",
                               "sys", "prompt", max_tokens=10000)
        self.assertIn("output token budget", str(ctx.exception))
        self.assertIn("retry also failed", str(ctx.exception))

    def test_empty_content_without_length_reason_propagates(self):
        # Empty content but a normal finish reason (not a budget cut) should not
        # be misclassified as a budget issue.
        def fake(provider, keys, model, system, prompt, max_tokens, timeout, label):
            self.calls.append(label)
            return ""  # empty, but _post_text's length-check lives upstream
        presets._post_text = fake
        with self.assertRaises(presets.PresetError):
            presets.model_json("openrouter", {"openrouter": "k"}, "m",
                               "sys", "prompt")


if __name__ == "__main__":
    unittest.main()