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


class FakeClient:
    def __init__(self, batch_text="", retry_texts=()):
        self.batch_text = batch_text
        self.retry_texts = list(retry_texts)
        self.one_calls = []

    def estimate(self, corpus, preamble, jobs, batched=False):
        return object()

    def prewarm(self, corpus, preamble):
        pass

    def batch(self, corpus, preamble, jobs):
        return {jobs[0].id: self.batch_text}

    def one(self, corpus, preamble, prompt, max_tokens=16000, schema=None,
            job_id="single", operation="pipeline_single"):
        self.one_calls.append((job_id, operation))
        return self.retry_texts.pop(0)

    def actual_usd(self):
        return 0.0


class ExtractRetryTests(unittest.TestCase):
    def args(self, force=False):
        return SimpleNamespace(
            segment="shoulder", skills="7", preset=None, force=force,
            yes=True, provider="openrouter", model="test", effort="high")

    def project(self, root):
        project = os.path.join(root, "project")
        evidence = os.path.join(project, "research", "evidence")
        os.makedirs(evidence)
        with open(os.path.join(evidence, "shoulder.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("customer evidence")
        return {"name": "test", "_dir": project}

    def run_extract(self, cfg, fake, force=False):
        with mock.patch.object(cli, "client", return_value=fake), \
                mock.patch.object(cli, "skill",
                                  return_value=("07_pain_points", "instructions")), \
                mock.patch.object(llm, "confirm", return_value=True):
            cli.cmd_extract(cfg, self.args(force=force))

    def test_empty_batch_response_recovers_on_third_immediate_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(retry_texts=["", "  ", "# Pain points\nRecovered"])
            self.run_extract(cfg, fake)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(written, "# Pain points\nRecovered")
        self.assertEqual(len(fake.one_calls), 3)
        self.assertEqual(
            [x[1] for x in fake.one_calls],
            ["extraction_empty_retry_1", "extraction_empty_retry_2",
             "extraction_empty_retry_3"])

    def test_three_empty_retries_fail_without_writing_a_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            fake = FakeClient(retry_texts=["", "\n", "  "])
            with self.assertRaisesRegex(SystemExit, "failed after 3 retries"):
                self.run_extract(cfg, fake)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            self.assertFalse(os.path.exists(dest))
        self.assertEqual(len(fake.one_calls), 3)

    def test_existing_zero_byte_file_is_not_treated_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            dest = os.path.join(
                cfg["_dir"], "research", "extractions", "shoulder", "07_pain_points.md")
            os.makedirs(os.path.dirname(dest))
            open(dest, "w", encoding="utf-8").close()
            fake = FakeClient(batch_text="# Pain points\nRebuilt")
            self.run_extract(cfg, fake, force=False)
            with open(dest, encoding="utf-8") as fh:
                written = fh.read()

        self.assertEqual(written, "# Pain points\nRebuilt")
        self.assertEqual(fake.one_calls, [])


if __name__ == "__main__":
    unittest.main()
