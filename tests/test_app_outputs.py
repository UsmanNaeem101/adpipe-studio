import datetime
import io
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
        voc = os.path.join(project, "research", "voc")
        os.makedirs(voc)
        for name in (
                "production_voc.jsonl", "audit_voc.jsonl",
                "filtered_voc.jsonl", "deduplicated_voc.jsonl",
                "retained_voc.jsonl", "rejected_voc.jsonl",
                "duplicate_groups.jsonl", "candidate_segments.json"):
            with open(os.path.join(voc, name), "w", encoding="utf-8") as fh:
                if name.endswith(".jsonl") and name != "duplicate_groups.jsonl":
                    fh.write('{"id":"e1","text":"customer voice"}\n')
                else:
                    fh.write("{}\n")
        stamp = 1_700_000_000
        os.utime(os.path.join(voc, "production_voc.jsonl"), (stamp, stamp))
        return voc, stamp

    def test_segment_picker_lists_only_final_ingest_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            _voc, stamp = self.make_project(tmp)
            with mock.patch.object(app, "ROOT", tmp):
                files = app.segment_voc_files("shoulder")

        self.assertEqual([f["name"] for f in files], ["production_voc.jsonl"])
        self.assertEqual(files[0]["label"], "Final · production_voc.jsonl")
        self.assertEqual(files[0]["mtime"], stamp)
        datetime.datetime.fromisoformat(files[0]["modified_at"])

    def test_refine_picker_lists_record_jsonl_and_recommends_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_project(tmp)
            with mock.patch.object(app, "ROOT", tmp):
                files = app.refine_voc_files("shoulder")

        self.assertEqual(files[0]["name"], "deduplicated_voc.jsonl")
        self.assertTrue(files[0]["recommended"])
        self.assertIn("Recommended", files[0]["label"])
        self.assertNotIn("duplicate_groups.jsonl", [row["name"] for row in files])

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
                         ["production_voc.jsonl"])
        self.assertCountEqual(
            [os.path.basename(x["path"]) for x in additional],
            ["audit_voc.jsonl", "filtered_voc.jsonl",
             "retained_voc.jsonl", "rejected_voc.jsonl",
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

    def test_pipeline_lists_refine_voc_as_a_free_stage(self):
        refine = [stage for stage in app.STAGES if stage[0] == "refine-voc"]

        self.assertEqual(len(refine), 1)
        self.assertFalse(refine[0][2])
        self.assertFalse(refine[0][3])
        self.assertLess(
            [stage[0] for stage in app.STAGES].index("refine-voc"),
            [stage[0] for stage in app.STAGES].index("segment"))

    def test_refine_voc_runs_without_requiring_a_segment(self):
        handler = object.__new__(app.Handler)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        proc = mock.Mock(stdout=[], returncode=0)

        with mock.patch.object(app.subprocess, "Popen", return_value=proc) as popen:
            handler._run({
                "stage": "refine-voc",
                "project": "shoulder",
                "force": True,
                "provider": "openrouter",
                "model": "example/model",
                "refine_source": "/tmp/chosen.jsonl",
            })

        self.assertFalse(popen.called)
        self.assertIn("not a refinable file", handler.wfile.getvalue().decode())

    def test_refine_voc_passes_selected_project_file_to_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            voc, _stamp = self.make_project(tmp)
            chosen = os.path.join(voc, "deduplicated_voc.jsonl")
            handler = object.__new__(app.Handler)
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            handler.wfile = io.BytesIO()
            proc = mock.Mock(stdout=[], returncode=0)

            with mock.patch.object(app, "ROOT", tmp), \
                    mock.patch.object(app.subprocess, "Popen", return_value=proc) as popen:
                handler._run({"stage": "refine-voc", "project": "shoulder",
                              "refine_source": chosen})

        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--source") + 1], os.path.realpath(chosen))
        self.assertNotIn("No segment selected", handler.wfile.getvalue().decode())


if __name__ == "__main__":
    unittest.main()
