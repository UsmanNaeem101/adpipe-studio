"""Exporting a stage's model logs as one archive.

The Outputs tab lists one row per log FILE, which is unusable at the scale a
failing stage produces: an 83-chunk ingest with a recovery pass leaves several
hundred rows, and the ones worth reading are scattered through the middle. These
cover picking a whole stage out by its own recorded label and handing it over in
one file.

The fixtures are written by auditlog itself rather than hand-rolled, so the
export is tested against the layout the pipeline actually produces.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app
import auditlog


class LogExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(m, "ROOT", self.tmp.name)
                        for m in (app, auditlog)]
        for p in self.patches:
            p.start()
        os.makedirs(os.path.join(self.tmp.name, "projects", "shoulder"))
        with open(os.path.join(self.tmp.name, "projects", "shoulder",
                               "project.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "shoulder"}, fh)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def log(self, stage, job_id, project="shoulder", response=True):
        """Write a real audit record, the way the pipeline writes them."""
        with auditlog.scope(project=project, stage=stage):
            audit = auditlog.start("openrouter", "deepseek/v4", "pipeline_batch_job",
                                   {"max_tokens": 5060}, job_id=job_id)
            if response:
                audit.response({"usage": {"completion_tokens": 5060}},
                               text="", finish_reason="length")
            return audit

    # ------------------------------------------------------ discovery
    def test_runs_are_grouped_by_the_stage_that_made_them(self):
        self.log("ingest", "f0000")
        self.log("ingest", "f0001")
        self.log("extract", "chunk000")
        stages = {s["stage"]: s for s in app.log_stages("shoulder")}
        self.assertEqual(stages["ingest"]["runs"], 2)
        self.assertEqual(stages["extract"]["runs"], 1)

    def test_stage_summary_counts_files_and_bytes(self):
        self.log("ingest", "f0000")
        stage = app.log_stages("shoulder")[0]
        # request.json + response.json for the one run.
        self.assertEqual(stage["files"], 2)
        self.assertGreater(stage["bytes"], 0)
        self.assertIn("modified_at", stage)

    def test_a_run_with_no_recorded_stage_is_still_offered(self):
        """Unattributed logs are still evidence — losing them silently is worse
        than showing them under a name that says what is known about them."""
        # A call made inside the project but outside any stage — the preset
        # picker is one, and it lands in the project's log directory all the same.
        with auditlog.scope(project="shoulder"):
            auditlog.start("openrouter", "m", "preset_pick", {}, job_id="x")
        self.assertEqual([s["stage"] for s in app.log_stages("shoulder")],
                         ["unattributed"])

    def test_a_stage_key_inside_the_prompt_cannot_relabel_the_run(self):
        """Stage 01's own system prompt opens "You are the filtering stage…", and
        prompts routinely carry JSON. A log filed under a stage that never ran it
        is worse than a slow read: it goes missing from the export you search."""
        with auditlog.scope(project="shoulder", stage="ingest"):
            auditlog.start("openrouter", "m", "pipeline_batch_job",
                           {"messages": [{"role": "user", "content":
                                          'emit {"stage": "render"} exactly'}]},
                           job_id="f0000")
        self.assertEqual([s["stage"] for s in app.log_stages("shoulder")],
                         ["ingest"])

    def test_a_run_whose_context_is_beyond_the_prefix_is_still_read(self):
        """The prefix is an optimisation, not the contract — a fat context block
        falls back to a full parse rather than being called unattributed."""
        # segment before stage on purpose: auditlog preserves insertion order, so
        # this pushes "stage" past the 4096-byte prefix and forces the full parse.
        with auditlog.scope(project="shoulder", segment="s" * 5000,
                            stage="ingest"):
            auditlog.start("openrouter", "m", "pipeline_batch_job", {},
                           job_id="f0000")
        self.assertEqual([s["stage"] for s in app.log_stages("shoulder")],
                         ["ingest"])

    def test_no_logs_is_an_empty_list_not_an_error(self):
        self.assertEqual(app.log_stages("shoulder"), [])

    # --------------------------------------------------------- export
    def export(self, stage=""):
        name, blob = app.export_logs("shoulder", stage)
        return name, zipfile.ZipFile(io.BytesIO(blob))

    def test_export_contains_only_the_requested_stage(self):
        self.log("ingest", "f0000")
        self.log("extract", "chunk000")
        _name, zf = self.export("ingest")
        payload = [n for n in zf.namelist() if n != "manifest.json"]
        self.assertTrue(payload)
        self.assertTrue(all("f0000" in n for n in payload),
                        f"extract logs leaked into the ingest export: {payload}")

    def test_export_carries_every_file_of_each_run(self):
        audit = self.log("ingest", "f0000")
        audit.event("http_error", "429 slow down", attempt=1)
        _name, zf = self.export("ingest")
        names = [os.path.basename(n) for n in zf.namelist() if n != "manifest.json"]
        self.assertCountEqual(names, ["request.json", "response.json", "events.jsonl"])

    def test_exported_files_keep_their_contents(self):
        self.log("ingest", "f0000")
        _name, zf = self.export("ingest")
        req = next(n for n in zf.namelist() if n.endswith("request.json"))
        self.assertEqual(json.loads(zf.read(req))["request"]["max_tokens"], 5060)

    def test_manifest_describes_every_request(self):
        """The archive has to be navigable without unpacking it — several hundred
        directories named by timestamp are not a browsable index."""
        self.log("ingest", "f0000")
        self.log("ingest", "f0001")
        _name, zf = self.export("ingest")
        m = json.loads(zf.read("manifest.json"))
        self.assertEqual(m["stage"], "ingest")
        self.assertEqual(m["runs"], 2)
        self.assertCountEqual([r["job_id"] for r in m["requests"]],
                              ["f0000", "f0001"])
        self.assertTrue(all(r["model"] == "deepseek/v4" for r in m["requests"]))

    def test_empty_stage_exports_everything(self):
        self.log("ingest", "f0000")
        self.log("extract", "chunk000")
        _name, zf = self.export("")
        m = json.loads(zf.read("manifest.json"))
        self.assertEqual(m["runs"], 2)
        self.assertEqual(m["stage"], "(all stages)")

    def test_filename_names_the_project_and_stage(self):
        self.log("ingest", "f0000")
        name, _zf = self.export("ingest")
        self.assertTrue(name.startswith("shoulder-ingest-logs-"), name)
        self.assertTrue(name.endswith(".zip"), name)

    def test_export_of_a_stage_with_no_logs_is_refused_by_name(self):
        self.log("ingest", "f0000")
        with self.assertRaises(ValueError) as caught:
            app.export_logs("shoulder", "render")
        self.assertIn("render", str(caught.exception))

    def test_export_of_an_unknown_project_is_refused(self):
        with self.assertRaises(ValueError):
            app.export_logs("not_a_project", "ingest")

    def test_export_scales_to_a_failing_stage(self):
        """The case that motivated this: 83 chunks plus a recovery pass."""
        for i in range(83):
            self.log("ingest", f"f{i:04d}")
        _name, zf = self.export("ingest")
        self.assertEqual(json.loads(zf.read("manifest.json"))["runs"], 83)
        self.assertEqual(
            len([n for n in zf.namelist() if n.endswith("request.json")]), 83)

    # ------------------------------------------------------------- UI
    def test_page_offers_the_export_control(self):
        for token in ("logstage", "logexport", "/logs/export?project="):
            self.assertIn(token, app.PAGE)


if __name__ == "__main__":
    unittest.main()
