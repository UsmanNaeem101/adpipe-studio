"""The browser's half of taking a copy and taking the space back.

The CLI has these already, and the reason to test the endpoints separately is
that they are the surface where the two can drift: the guard that decides when
removal is safe belongs to one place, and a route that answered the question
itself would be free to answer it differently.

So what is checked here is not that removal works — test_archive.py does that —
but that the endpoint refuses on the same condition, and that a project name
arriving from a browser is looked up rather than joined onto a path.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import paths  # noqa: E402
import store  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class RouteFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.original = (app.ROOT, paths.ROOT)
        app.ROOT = paths.ROOT = self.root
        store.use(store.LocalStore(self.root))

        self.dir = os.path.join(self.root, "projects", "demo")
        write(os.path.join(self.dir, "project.json"), '{"name":"demo"}')
        voc = os.path.join(self.dir, "research", "voc")
        write(os.path.join(voc, "deduplicated_voc.jsonl"), "d" * 4096)
        write(os.path.join(voc, "filtered_voc.jsonl"), "f" * 2048)
        write(os.path.join(voc, "validated_segments.json"), "{}")

    def tearDown(self):
        app.ROOT, paths.ROOT = self.original
        store.use(None)

    def finish_stage_06(self):
        write(paths.evidence(self.dir, "01_desk_workers.txt"), "verbatim\n")


class StorageReportTests(RouteFixture):
    def test_it_reports_what_is_spent_and_whether_it_may_go(self):
        report = app.project_storage("demo")
        self.assertFalse(report["ready"])
        self.assertEqual(report["evidence"], [])
        self.assertEqual(report["bytes"], 4096 + 2048)
        self.assertEqual([f["path"] for f in report["files"]],
                         ["research/voc/deduplicated_voc.jsonl",
                          "research/voc/filtered_voc.jsonl"])

    def test_largest_first(self):
        # The list is read to decide whether the space is worth the trade, so
        # the file that answers that is the one to put at the top.
        sizes = [f["bytes"] for f in app.project_storage("demo")["files"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_stage_06_output_flips_ready(self):
        self.finish_stage_06()
        report = app.project_storage("demo")
        self.assertTrue(report["ready"])
        self.assertEqual(report["evidence"], ["01_desk_workers.txt"])

    def test_an_unknown_project_is_refused_not_walked(self):
        # The name comes from a query string. Joining it onto ROOT and asking
        # the filesystem would make "../../etc" a question worth answering.
        for name in ("nope", "../../etc", ""):
            with self.assertRaises(ValueError):
                app.project_storage(name)


class CleanupEndpointTests(RouteFixture):
    def test_it_refuses_on_the_same_condition_as_the_command(self):
        with self.assertRaises(ValueError) as caught:
            app.project_cleanup("demo")
        self.assertIn("stage 06", str(caught.exception))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "research", "voc", "deduplicated_voc.jsonl")))

    def test_it_removes_once_stage_06_has_run(self):
        self.finish_stage_06()
        result = app.project_cleanup("demo")
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["freed"], 4096 + 2048)
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "research", "voc", "deduplicated_voc.jsonl")))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "research", "voc", "validated_segments.json")))

    def test_an_unknown_project_is_refused(self):
        with self.assertRaises(ValueError):
            app.project_cleanup("../demo")


class ArchiveEndpointTests(RouteFixture):
    def test_it_returns_a_named_zip_of_the_whole_project(self):
        filename, blob, count = app.project_archive("demo")
        self.assertEqual(filename, "demo.zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(count, len(names))
        self.assertIn("demo/project.json", names)
        self.assertIn("demo/research/voc/deduplicated_voc.jsonl", names)

    def test_it_is_available_before_removal_is(self):
        # This is the copy someone takes *because* they are about to be told
        # that removal cannot be undone.
        _, blob, count = app.project_archive("demo")
        self.assertGreater(count, 0)
        self.assertTrue(blob.startswith(b"PK"))

    def test_an_unknown_project_is_refused(self):
        with self.assertRaises(ValueError):
            app.project_archive("nope")


class RoutingTests(RouteFixture):
    """Driven through the handler, so the wiring is covered and not just the
    functions behind it."""

    class Recorder(app.Handler):
        def __init__(self, path, body=None):
            self.path = path
            self._body = json.dumps(body or {}).encode()
            self.headers = {"Content-Length": str(len(self._body))}
            self.rfile = io.BytesIO(self._body)
            self.sent = {}

        def _send(self, code, body, ctype="application/json", download=None):
            self.sent = {"code": code, "body": body, "ctype": ctype,
                         "download": download}

        def json(self):
            return json.loads(self.sent["body"])

    def test_the_storage_route_answers(self):
        handler = self.Recorder("/project/storage?project=demo")
        handler.do_GET()
        self.assertEqual(handler.json()["bytes"], 4096 + 2048)

    def test_the_archive_route_offers_a_download(self):
        handler = self.Recorder("/project/archive?project=demo")
        handler.do_GET()
        self.assertEqual(handler.sent["ctype"], "application/zip")
        self.assertEqual(handler.sent["download"], "demo.zip")
        self.assertTrue(handler.sent["body"].startswith(b"PK"))

    def test_the_archive_route_404s_on_an_unknown_project(self):
        handler = self.Recorder("/project/archive?project=nope")
        handler.do_GET()
        self.assertEqual(handler.sent["code"], 404)

    def test_the_cleanup_route_returns_the_refusal_as_a_message(self):
        # Not a 500. The browser shows this text to the person who clicked, and
        # "stage 06 has produced no evidence files" is the whole answer.
        handler = self.Recorder("/project/cleanup", {"name": "demo"})
        handler.do_POST()
        self.assertEqual(handler.sent["code"], 200)
        self.assertIn("stage 06", handler.json()["error"])

    def test_the_cleanup_route_removes_once_it_may(self):
        self.finish_stage_06()
        handler = self.Recorder("/project/cleanup", {"name": "demo"})
        handler.do_POST()
        self.assertEqual(handler.json()["removed"], 2)


if __name__ == "__main__":
    unittest.main()
