"""Deterministic post-ingest production/audit VOC export."""

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


class VocRefinementTests(unittest.TestCase):
    @staticmethod
    def bytes_at(paths):
        values = []
        for path in paths:
            with open(path, "rb") as fh:
                values.append(fh.read())
        return values

    def test_standard_reddit_url_fields(self):
        url = ("https://www.reddit.com/r/ClusterHeadaches/comments/"
               "1m1n7e5/neckshoulder_pain/")
        self.assertEqual(
            cli.reddit_source_fields(url), ("ClusterHeadaches", "1m1n7e5"))

    def test_old_reddit_url_fields(self):
        url = ("https://old.reddit.com/r/ShoulderPain/comments/"
               "abc123/an_old_thread/")
        self.assertEqual(
            cli.reddit_source_fields(url), ("ShoulderPain", "abc123"))

    def test_missing_malformed_and_non_reddit_urls_are_null(self):
        for url in (None, "", "not a URL", "http://[broken",
                    "https://example.com/r/x/comments/abc/y",
                    "https://reddit.com/user/someone"):
            with self.subTest(url=url):
                self.assertEqual(cli.reddit_source_fields(url), (None, None))

    def fixture(self, root):
        voc = os.path.join(root, "research", "voc")
        os.makedirs(voc)
        rows = [
            {
                "id": 272,
                "text": "I&#39;ve had subsequent pain in my neck…",
                "url": ("https://www.reddit.com/r/ClusterHeadaches/comments/"
                        "1m1n7e5/neckshoulder_pain/"),
                "title": "Neck/shoulder pain",
                "evidence_id": 272,
                "decision": "retain",
                "retention_reasons": ["first_person_experience", "specific_problem"],
                "rejection_reasons": [],
            },
            {
                "id": 9,
                "text": "Keep <this> text &amp; its entities exactly.",
                "url": "https://example.com/post/9",
                "title": "External source",
                "evidence_id": 9,
                "decision": "retain",
                "retention_reasons": ["specific_problem"],
                "rejection_reasons": [],
                "duplicate_of": None,
            },
        ]
        groups = [{
            "canonical_id": 272,
            "duplicate_ids": [272, 411],
            "duplicate_type": "cross_post_duplicate",
            "rationale": "Same experience reposted",
        }]
        cli._write_jsonl(os.path.join(voc, "deduplicated_voc.jsonl"), rows)
        cli._write_jsonl(os.path.join(voc, "duplicate_groups.jsonl"), groups)
        return voc, rows, groups

    def export(self, root):
        voc, rows, groups = self.fixture(root)
        summary = cli.refine_voc({"_dir": root}, announce=False)
        production = cli._read_jsonl(os.path.join(voc, cli.PRODUCTION_VOC_FILE))
        audit = cli._read_jsonl(os.path.join(voc, cli.AUDIT_VOC_FILE))
        return voc, rows, groups, summary, production, audit

    def test_production_schema_is_exact_and_stage01_fields_cannot_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            _voc, _rows, _groups, _summary, production, _audit = self.export(tmp)
        self.assertEqual(
            list(production[0]), ["id", "text", "thread_id", "subreddit"])
        self.assertEqual(set(production[1]),
                         {"id", "text", "subreddit", "thread_id"})
        for forbidden in ("evidence_id", "url", "title", "decision",
                          "retention_reasons", "rejection_reasons"):
            self.assertNotIn(forbidden, production[0])

    def test_audit_retains_stage01_and_stage02_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _voc, _rows, groups, _summary, _production, audit = self.export(tmp)
        self.assertEqual(audit[0]["decision"], "retain")
        self.assertEqual(
            audit[0]["retention_reasons"],
            ["first_person_experience", "specific_problem"])
        self.assertEqual(audit[0]["dedup_groups"], groups)
        self.assertIn("duplicate_of", audit[1])
        self.assertNotIn("evidence_id", audit[0])

    def test_text_count_and_order_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            _voc, rows, _groups, summary, production, audit = self.export(tmp)
        self.assertEqual([row["id"] for row in production], [272, 9])
        self.assertEqual([row["text"] for row in production],
                         [row["text"] for row in rows])
        self.assertEqual([row["text"] for row in audit],
                         [row["text"] for row in rows])
        self.assertEqual(
            (summary["input"], summary["production"], summary["audit"]),
            (2, 2, 2))
        self.assertEqual(production[0]["subreddit"], "ClusterHeadaches")
        self.assertEqual(production[0]["thread_id"], "1m1n7e5")
        self.assertIsNone(production[1]["subreddit"])
        self.assertIsNone(production[1]["thread_id"])

    def test_rerun_is_byte_for_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            voc, *_rest = self.export(tmp)
            paths = [os.path.join(voc, cli.PRODUCTION_VOC_FILE),
                     os.path.join(voc, cli.AUDIT_VOC_FILE)]
            before = self.bytes_at(paths)
            cli.refine_voc({"_dir": tmp}, announce=False)
            after = self.bytes_at(paths)
        self.assertEqual(after, before)

    def test_manual_command_never_constructs_a_model_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fixture(tmp)
            with mock.patch.object(
                    cli, "client", side_effect=AssertionError("model called")) as model:
                cli.cmd_refine_voc({"_dir": tmp}, SimpleNamespace())
            self.assertFalse(model.called)
            self.assertTrue(os.path.exists(os.path.join(
                tmp, "research", "voc", cli.PRODUCTION_VOC_FILE)))

    def test_downstream_payloads_use_only_fields_each_stage_needs(self):
        production = [{
            "id": 1, "text": "exact text", "subreddit": "Pain",
            "thread_id": "abc",
        }]
        self.assertEqual(cli._corpus_text(production), "[1] exact text")
        validation = cli._validation_corpus_text(production)
        self.assertIn("[1] [t:abc] [r:Pain] exact text", validation)
        self.assertIn("exact text", validation)
        self.assertNotIn("url=", validation)
        self.assertNotIn("retention_reasons", validation)

        assignment = cli._stage05_evidence_text(production)
        self.assertEqual(assignment, "[1] exact text")
        self.assertNotIn("abc", assignment)
        self.assertNotIn("Pain", assignment)

    def test_stage03_rich_and_production_inputs_format_identically(self):
        rich = [{
            "id": 1, "text": "unchanged", "url": "https://example.com",
            "title": "title", "decision": "retain",
            "retention_reasons": ["specific_problem"],
        }]
        lean = [{
            "id": 1, "text": "unchanged", "thread_id": None,
            "subreddit": None,
        }]
        before = cli._stage03_corpus_text(rich)
        after = cli._stage03_corpus_text(lean)
        self.assertEqual(before, after)
        self.assertEqual(before, "[1] unchanged")
        for forbidden in ("url", "title", "decision", "retention_reasons"):
            self.assertNotIn(forbidden, after)

    def test_prompt_metrics_measure_the_actual_stage_formatters(self):
        rows = [{"id": 1, "text": "text", "thread_id": "abc",
                 "subreddit": "Pain"}]
        metrics = cli.voc_prompt_view_metrics(rows)
        self.assertEqual(metrics["stage03_chars"],
                         len(cli._stage03_corpus_text(rows)))
        self.assertEqual(metrics["stage04_chars"],
                         len(cli._validation_corpus_text(rows)))
        self.assertEqual(metrics["stage03_before_est_tokens"],
                         metrics["stage03_est_tokens"])
        self.assertEqual(metrics["stage03_reduction_pct"], 0.0)
        self.assertLess(metrics["stage04_reduction_pct"], 0)

    def test_missing_source_metadata_does_not_break_refinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            voc = os.path.join(tmp, "research", "voc")
            os.makedirs(voc)
            cli._write_jsonl(os.path.join(voc, "deduplicated_voc.jsonl"), [
                {"id": 1, "text": "source is unavailable", "decision": "retain"}
            ])
            cli.refine_voc({"_dir": tmp}, announce=False)
            production = cli._read_jsonl(os.path.join(
                voc, cli.PRODUCTION_VOC_FILE))
        self.assertEqual(production, [{
            "id": 1, "text": "source is unavailable",
            "thread_id": None, "subreddit": None,
        }])

    def test_stage06_traceability_resolves_from_audit_without_prompt_leakage(self):
        production = [{"id": 1, "text": "exact", "thread_id": "abc",
                       "subreddit": "Pain"}]
        audit = [{"id": 1, "text": "exact", "url": "https://reddit.test/x",
                  "title": "Original title", "thread_id": "abc",
                  "subreddit": "Pain", "decision": "retain"}]
        joined = cli._join_production_audit(production, audit)
        self.assertEqual(joined[1]["url"], "https://reddit.test/x")
        self.assertEqual(joined[1]["title"], "Original title")
        self.assertNotIn("url", cli._stage03_corpus_text(production))

    def test_normal_ingest_calls_the_same_refinement_service(self):
        class FakeClient:
            def estimate(self, *_args, **_kwargs):
                return type("Estimate", (), {"explain": lambda _self: "fake"})()

            def prewarm(self, *_args):
                pass

            def batch(self, _corpus, _preamble, jobs):
                replies = {}
                for request in jobs:
                    if request.schema is cli.FILTER_SCHEMA:
                        replies[request.id] = json.dumps({"records": [{
                            "evidence_id": evidence_id,
                            "decision": "retain",
                            "retention_reasons": ["specific_problem"],
                            "rejection_reasons": [],
                        } for evidence_id in request.expected_ids]})
                    elif request.schema is cli.DEDUP_SCHEMA:
                        replies[request.id] = '{"groups": []}'
                return replies

        raw = ("My shoulder hurts every night and this pillow gives me no support.\n\n"
               "I wake with neck pain because my current pillow becomes flat.")
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "raw.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "pain"}
            args = SimpleNamespace(source=source, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=FakeClient()), \
                    mock.patch.object(llm, "confirm", return_value=True), \
                    mock.patch.object(cli, "refine_voc", wraps=cli.refine_voc) as refine:
                cli.cmd_ingest(cfg, args)
            self.assertEqual(refine.call_count, 1)
            self.assertTrue(os.path.exists(os.path.join(
                tmp, "research", "voc", cli.PRODUCTION_VOC_FILE)))


if __name__ == "__main__":
    unittest.main()
