import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app
import cli


class AppOutputTests(unittest.TestCase):
    def make_project(self, root):
        project = os.path.join(root, "projects", "shoulder")
        voc = os.path.join(project, "voc")
        os.makedirs(voc)
        for name in (
                "filtered_voc.jsonl", "deduplicated_voc.jsonl",
                "retained_voc.jsonl", "rejected_voc.jsonl",
                "duplicate_groups.jsonl", "candidate_segments.json"):
            with open(os.path.join(voc, name), "w", encoding="utf-8") as fh:
                fh.write("{}\n")
        stamp = 1_700_000_000
        os.utime(os.path.join(voc, "filtered_voc.jsonl"), (stamp, stamp))
        return voc, stamp

    def test_segment_picker_lists_only_final_ingest_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            _voc, stamp = self.make_project(tmp)
            with mock.patch.object(app, "ROOT", tmp):
                files = app.segment_voc_files("shoulder")

        self.assertEqual([f["name"] for f in files], ["filtered_voc.jsonl"])
        self.assertEqual(files[0]["label"], "Final · filtered_voc.jsonl")
        self.assertEqual(files[0]["mtime"], stamp)
        datetime.datetime.fromisoformat(files[0]["modified_at"])

    def test_outputs_classify_ingest_final_and_additional_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_project(tmp)
            with mock.patch.object(app, "ROOT", tmp):
                output = app.project_outputs("shoulder")

        ingest = [x for x in output["stages"] if x["stage"] == "ingest"]
        segment = [x for x in output["stages"] if x["stage"] == "segment"]
        final = [x for x in ingest if x["role"] == "final"]
        additional = [x for x in ingest if x["role"] == "additional"]

        self.assertEqual([os.path.basename(x["path"]) for x in final],
                         ["filtered_voc.jsonl"])
        self.assertCountEqual(
            [os.path.basename(x["path"]) for x in additional],
            ["retained_voc.jsonl", "rejected_voc.jsonl",
             "deduplicated_voc.jsonl", "duplicate_groups.jsonl"])
        self.assertIn("candidate_segments.json",
                      [os.path.basename(x["path"]) for x in segment])
        self.assertTrue(all("modified_at" in x and "mtime" in x for x in ingest))

    def test_model_audit_files_appear_in_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_project(tmp)
            log_dir = os.path.join(
                tmp, "projects", "shoulder", "logs", "model", "2026-08-05",
                "151500_extract_abc")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "request.json"), "w",
                      encoding="utf-8") as fh:
                fh.write('{"request":{"prompt":"test"}}')
            with open(os.path.join(log_dir, "response.json"), "w",
                      encoding="utf-8") as fh:
                fh.write('{"text":"answer"}')
            with mock.patch.object(app, "ROOT", tmp):
                output = app.project_outputs("shoulder")

        logs = [x for x in output["stages"] if x["stage"] == "logs"]
        self.assertCountEqual(
            [os.path.basename(x["path"]) for x in logs],
            ["request.json", "response.json"])
        self.assertIn("'render','logs'", app.PAGE)

    def test_page_has_one_accordion_outputs_renderer(self):
        self.assertEqual(app.PAGE.count("async function loadOutputs()"), 1)
        self.assertIn("<details class=outstage", app.PAGE)
        self.assertIn("groupHtml('Final file'", app.PAGE)
        self.assertIn("groupHtml('Additional files'", app.PAGE)
        self.assertIn("Latest · ${outWhen(latest)}", app.PAGE)
        self.assertIn("if(other!==panel)other.open=false", app.PAGE)

    def test_extract_stage_exposes_exactly_three_research_depths(self):
        self.assertIn('<option value=fast selected>Fast Test', app.PAGE)
        self.assertIn('<option value=standard>Standard', app.PAGE)
        self.assertIn('<option value=deep>Deep Research', app.PAGE)
        self.assertNotIn('<option value=custom>Custom', app.PAGE)
        self.assertIn("showOpts(s.name)", app.PAGE)
        self.assertEqual(set(cli.PRESETS), {"fast", "standard", "deep"})

    def test_extract_stage_can_rerun_one_skill(self):
        self.assertIn("Individual skill rerun", app.PAGE)
        self.assertIn("fetch('/skills')", app.PAGE)
        self.assertIn("body.skills=$('#extractskill').value", app.PAGE)
        self.assertIn("body.force=true", app.PAGE)
        self.assertIn("--force", app.Handler._run.__code__.co_consts)

    def test_pipeline_exposes_force_redo_for_any_stage(self):
        self.assertIn('id=force', app.PAGE)
        self.assertIn('Force redo existing outputs', app.PAGE)
        self.assertIn("force:$('#force').checked", app.PAGE)


if __name__ == "__main__":
    unittest.main()
