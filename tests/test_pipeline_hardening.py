"""The failures a pipeline run only shows after the model has been paid.

Each of these was a real gap: a write that crashed after the spend, a stage
that wrote a fragment and never redid it, a merge that trusted an id the model
made up, a ceiling that never reached the wire. The tests run the real
functions against fakes and assert on what was written and what was sent.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import llm  # noqa: E402
import openrouter  # noqa: E402
import paths  # noqa: E402
import presets  # noqa: E402
import store as store_module  # noqa: E402
from test_store_backed_project import DictSupabase  # noqa: E402


# ── Atomic writes go through the store ───────────────────────────────────

class AtomicWriteTests(unittest.TestCase):
    """The JSON helpers wrote a `.tmp` through the store and renamed it on a
    disk that, under Supabase, had never seen it."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.original = paths.ROOT
        paths.ROOT = self.data
        self.supabase = DictSupabase(self.data)
        store_module.use(self.supabase)

    def tearDown(self):
        paths.ROOT = self.original
        store_module.use(None)

    def on_disk(self):
        return [name for _b, _d, files in os.walk(self.data) for name in files]

    def test_json_atomic_lands_in_the_store_and_nowhere_else(self):
        key = os.path.join(self.data, "projects", "p", "research", "x.json")
        cli._json_atomic(key, {"a": 1})
        self.assertEqual(json.loads(self.supabase.rows["projects/p/research/x.json"]),
                         {"a": 1})
        self.assertEqual(self.on_disk(), [])
        self.assertEqual(cli._load_json(key), {"a": 1})

    def test_jsonl_atomic_lands_in_the_store_and_nowhere_else(self):
        key = os.path.join(self.data, "projects", "p", "research", "voc", "r.jsonl")
        cli._write_jsonl_atomic(key, [{"id": 1}, {"id": 2}])
        self.assertEqual(self.supabase.rows["projects/p/research/voc/r.jsonl"],
                         '{"id": 1}\n{"id": 2}\n')
        self.assertEqual(self.on_disk(), [])
        self.assertEqual(cli._read_jsonl(key), [{"id": 1}, {"id": 2}])


# ── Synthesis through the result path ────────────────────────────────────

class SynthClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def estimate(self, *_a, **_k):
        return object()

    def one_result(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
                   job_id="single", operation="pipeline_single", effort=None,
                   reasoning_max_tokens=None):
        self.calls.append((operation, max_tokens))
        text, stop = self.replies.pop(0)
        return llm.BatchResult(text=text, stop_reason=stop)

    def one(self, *_a, **_k):
        raise AssertionError("synth must keep the stop reason")

    def actual_usd(self):
        return 0.0


class SynthTruncationTests(unittest.TestCase):
    def run_synth(self, tmp, fake):
        os.makedirs(os.path.join(tmp, "research", "evidence"))
        with open(os.path.join(tmp, "research", "evidence", "s.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("evidence")
        cfg = {"name": "t", "_dir": tmp}
        args = SimpleNamespace(segment="s", force=False, yes=True)
        dest = os.path.join(tmp, "output", "s", "01_picc_card.md")
        with mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(llm, "confirm", return_value=True):
            cli.synth(cfg, args, "picc", "prompt", dest, 1000)
        return dest

    def test_a_clean_answer_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self.run_synth(tmp, SynthClient([("# card", "end_turn")]))
            with open(dest, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "# card")

    def test_a_truncated_answer_is_retried_wider_then_fails_without_writing(self):
        fake = SynthClient([("# ca", "max_tokens"), ("# card but", "max_tokens")])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "Nothing was written"):
                self.run_synth(tmp, fake)
            self.assertFalse(os.path.exists(
                os.path.join(tmp, "output", "s", "01_picc_card.md")))
        self.assertEqual([c[1] for c in fake.calls],
                         [1000, 1000 * cli.BUDGET_RETRY_FACTOR])

    def test_an_empty_answer_recovers_on_the_retry(self):
        fake = SynthClient([("", "end_turn"), ("# card", "end_turn")])
        with tempfile.TemporaryDirectory() as tmp:
            dest = self.run_synth(tmp, fake)
            with open(dest, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "# card")

    def test_a_refusal_fails_at_once(self):
        fake = SynthClient([("", "refusal")])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "blocked"):
                self.run_synth(tmp, fake)
        self.assertEqual(len(fake.calls), 1)


# ── Dedup validation and chain resolution ────────────────────────────────

class DedupValidationTests(unittest.TestCase):
    def setUp(self):
        self.validate = cli._dedup_row_validator({"d0000": {1, 2, 3, 4}})
        self.job = llm.Job(id="d0000", prompt="")

    def group(self, canonical, duplicates):
        return {"canonical_id": canonical, "duplicate_ids": duplicates,
                "duplicate_type": "exact_duplicate", "rationale": "same"}

    def test_a_group_within_the_chunk_passes(self):
        self.validate(self.job, [self.group(1, [2, 3])])

    def test_an_id_the_chunk_never_showed_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not in this chunk"):
            self.validate(self.job, [self.group(1, [2, 99])])
        with self.assertRaisesRegex(ValueError, "not in this chunk"):
            self.validate(self.job, [self.group(99, [1])])

    def test_a_canonical_among_its_own_duplicates_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "own duplicate"):
            self.validate(self.job, [self.group(1, [1, 2])])

    def test_an_empty_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no duplicates"):
            self.validate(self.job, [self.group(1, [])])


class DedupResolutionTests(unittest.TestCase):
    def group(self, canonical, duplicates, kind="exact_duplicate"):
        return {"canonical_id": canonical, "duplicate_ids": duplicates,
                "duplicate_type": kind, "rationale": f"{canonical}<-{duplicates}"}

    def dropped(self, groups):
        return sorted(i for g in groups for i in g["duplicate_ids"])

    def test_an_a_b_b_a_pair_keeps_one_record(self):
        """Taken literally the pair deletes both — the case the skill warns about."""
        resolved = cli.resolve_duplicate_groups(
            [self.group(7, [12]), self.group(12, [7])])
        self.assertEqual(resolved[0]["canonical_id"], 7)
        self.assertEqual(self.dropped(resolved), [12])

    def test_a_chain_collapses_onto_the_lowest_id(self):
        resolved = cli.resolve_duplicate_groups(
            [self.group(5, [9]), self.group(9, [3]), self.group(20, [21])])
        self.assertEqual([(g["canonical_id"], g["duplicate_ids"]) for g in resolved],
                         [(3, [5, 9]), (20, [21])])

    def test_the_canonical_is_never_also_dropped(self):
        resolved = cli.resolve_duplicate_groups(
            [self.group(2, [4]), self.group(3, [2])])
        self.assertEqual(self.dropped(resolved), [3, 4])
        self.assertNotIn(2, self.dropped(resolved))

    def test_an_empty_answer_is_an_empty_answer(self):
        self.assertEqual(cli.resolve_duplicate_groups([]), [])


# ── The reasoning cap reaches the wire ───────────────────────────────────

class AnthropicReasoningCapTests(unittest.TestCase):
    def client_for(self, model):
        client = llm.Client.__new__(llm.Client)
        client.model, client.effort, client.verbose = model, "high", False
        return client

    def test_a_budget_model_receives_the_cap_as_a_thinking_budget(self):
        for model in ("claude-sonnet-4-5", "claude-opus-4-6", "claude-haiku-4-5"):
            with self.subTest(model=model):
                params = self.client_for(model)._params(
                    None, "p", 10_000, reasoning_max_tokens=3_000)
                self.assertEqual(params["thinking"],
                                 {"type": "enabled", "budget_tokens": 3_000})

    def test_a_model_that_rejects_budgets_stays_adaptive(self):
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-7"):
            with self.subTest(model=model):
                params = self.client_for(model)._params(
                    None, "p", 10_000, reasoning_max_tokens=3_000)
                self.assertEqual(params["thinking"], {"type": "adaptive"})
                self.assertEqual(params["output_config"]["effort"], "high")

    def test_the_cap_that_cannot_be_enforced_is_said_once(self):
        client = self.client_for("claude-opus-5")
        client.verbose = True
        with mock.patch("builtins.print") as out:
            client._params(None, "p", 10_000, reasoning_max_tokens=3_000)
            client._params(None, "p", 10_000, reasoning_max_tokens=3_000)
        notes = [c for c in out.call_args_list if "advisory" in str(c)]
        self.assertEqual(len(notes), 1)

    def test_no_cap_means_no_note_and_no_budget(self):
        params = self.client_for("claude-opus-4-6")._params(None, "p", 10_000)
        self.assertEqual(params["thinking"], {"type": "adaptive"})


class RouteCapRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()
        self.client = object.__new__(openrouter.Client)
        self.client.model = "deepseek/deepseek-v4-flash"
        self.client.key, self.client.verbose, self.client.effort = "k", False, "low"
        self.client.max_output = None
        self.client.spent = {"in": 0, "out": 0, "reasoning": 0,
                             "cache_write": 0, "cache_read": 0}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_the_reasoning_budget_shrinks_with_the_route_cap(self):
        error = urllib.error.HTTPError(
            "https://example.test", 400, "bad request", {},
            io.BytesIO(json.dumps({"error": {"message":
                "max_tokens is too large: 32000. Maximum is 8000."}}).encode()))

        class Ok:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"},
                                 "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}}).encode()

        with mock.patch("urllib.request.urlopen", side_effect=[error, Ok()]) as call:
            self.client._post([{"role": "user", "content": "x"}], 32_000,
                              reasoning_max_tokens=16_000)
        retried = json.loads(call.call_args_list[1].args[0].data)
        self.assertEqual(retried["max_tokens"], 8_000)
        self.assertEqual(retried["reasoning"], {"max_tokens": 4_000})


# ── Presets retry ────────────────────────────────────────────────────────

class PresetRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def http(code):
        return urllib.error.HTTPError("https://openrouter.ai/x", code, "err", {},
                                      io.BytesIO(b'{"error":"busy"}'))

    @staticmethod
    def ok():
        class Ok:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()
        return Ok()

    def test_a_rate_limit_is_retried_with_backoff(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self.http(429), self.http(503), self.ok()]), \
                mock.patch.object(presets.time, "sleep") as sleep:
            payload = presets._post_json(presets.OPENROUTER_URL, {}, {"model": "m"})
        self.assertEqual(payload["choices"][0]["message"]["content"], "{}")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_a_key_rejection_is_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http(401)) as call, \
                mock.patch.object(presets.time, "sleep") as sleep:
            with self.assertRaisesRegex(presets.PresetError, "401"):
                presets._post_json(presets.OPENROUTER_URL, {}, {"model": "m"})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_the_last_attempt_fails_honestly(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http(429)) as call, \
                mock.patch.object(presets.time, "sleep"):
            with self.assertRaisesRegex(presets.PresetError, "429"):
                presets._post_json(presets.OPENROUTER_URL, {}, {"model": "m"})
        self.assertEqual(call.call_count, 3)


# ── Evidence ids survive a re-ingest ─────────────────────────────────────

class EvidenceIdTests(unittest.TestCase):
    """Ids were positions, so a change to the pre-pass renumbered every
    citation in every extraction."""

    LINES = [
        "My shoulder hurts every night and this pillow gives me no support at all",
        "I wake with neck pain because my current pillow becomes flat by morning",
        "Third person here with the same tension that never quite switches off",
    ]

    def ingest(self, tmp, lines):
        source = os.path.join(tmp, "raw.txt")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(lines))
        cfg = {"_dir": tmp, "filter": {"min_words": 8}}
        args = SimpleNamespace(source=source, rules_only=True, yes=True)
        with mock.patch("builtins.print") as out:
            cli.cmd_ingest(cfg, args)
        rows = cli._read_jsonl(os.path.join(tmp, "research", "voc", "filtered_voc.jsonl"))
        return {r["text"]: r["id"] for r in rows}, " ".join(str(c) for c in out.call_args_list)

    def test_first_ingest_numbers_from_one_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids, _ = self.ingest(tmp, self.LINES)
        self.assertEqual([ids[t] for t in self.LINES], [1, 2, 3])

    def test_a_record_keeps_its_id_when_an_earlier_one_disappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.ingest(tmp, self.LINES)
            ids, said = self.ingest(tmp, [self.LINES[1], self.LINES[2],
                                          "A brand new comment about a sore neck "
                                          "after sleeping on the sofa again"])
        self.assertEqual(ids[self.LINES[1]], 2)
        self.assertEqual(ids[self.LINES[2]], 3)
        # New records take unused ids; a retired id is never reassigned.
        self.assertEqual(max(ids.values()), 4)
        self.assertIn("WARNING", said)
        self.assertIn("ids 1", said)

    def test_an_unchanged_ingest_says_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.ingest(tmp, self.LINES)
            _, said = self.ingest(tmp, self.LINES)
        self.assertNotIn("WARNING", said)

    def test_prepass_drops_are_written_with_their_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.ingest(tmp, self.LINES + ["too short", "log in or sign up in seconds "
                                           "to join the conversation on reddit"])
            dropped = cli._read_jsonl(
                os.path.join(tmp, "research", "voc", cli.PREPASS_DROPPED_FILE))
        self.assertEqual(sorted(d["reason"] for d in dropped),
                         ["interface_chrome", "too_short"])


if __name__ == "__main__":
    unittest.main()
