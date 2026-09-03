"""What a request has to pass before any route sees it, and what /run refuses.

The service has no login of its own. On a laptop the loopback bind is the
door; on a deployment the shared secret is, and DEPLOY.md has said so since
the first one — but nothing read the header, so the private network was open.
These tests drive the handler the way the proxy does, so the wiring is what is
covered: a route that forgets the guard is the failure this exists to catch.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import paths  # noqa: E402
import remix  # noqa: E402
import store  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Recorder(app.Handler):
    def __init__(self, path, body=None, headers=None, raw=None):
        self.path = path
        self._body = raw if raw is not None else json.dumps(body or {}).encode()
        self.headers = {"Content-Length": str(len(self._body))}
        self.headers.update(headers or {})
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()
        self.sent = {}
        self.status = None

    def _send(self, code, body, ctype="application/json", download=None):
        self.sent = {"code": code, "body": body, "ctype": ctype,
                     "download": download}

    # Streaming routes write headers themselves; record rather than socket.
    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.sent["body"])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.original = (app.ROOT, paths.ROOT)
        app.ROOT = paths.ROOT = self.root
        store.use(store.LocalStore(self.root))
        self.dir = os.path.join(self.root, "projects", "demo")
        write(os.path.join(self.dir, "project.json"), '{"name":"demo"}')
        app._running.clear()

    def tearDown(self):
        app.ROOT, paths.ROOT = self.original
        store.use(None)
        app._running.clear()


class SharedSecretTests(Fixture):
    def test_without_the_variable_nothing_is_asked(self):
        with mock.patch.dict(os.environ, {app.SHARED_SECRET_ENV: ""}):
            handler = Recorder("/storage")
            handler.do_GET()
        self.assertEqual(handler.sent["code"], 200)

    def test_a_missing_header_is_refused_before_routing(self):
        with mock.patch.dict(os.environ, {app.SHARED_SECRET_ENV: "s3cret"}):
            handler = Recorder("/storage")
            handler.do_GET()
        self.assertEqual(handler.sent["code"], 401)

    def test_a_wrong_header_is_refused_on_every_method(self):
        with mock.patch.dict(os.environ, {app.SHARED_SECRET_ENV: "s3cret"}):
            get = Recorder("/storage", headers={app.TOKEN_HEADER: "s3cre"})
            get.do_GET()
            post = Recorder("/project/cleanup", {"name": "demo"},
                            headers={app.TOKEN_HEADER: "nope"})
            post.do_POST()
        self.assertEqual(get.sent["code"], 401)
        self.assertEqual(post.sent["code"], 401)
        # Refused before the route ran: no body was consumed.
        self.assertEqual(post.rfile.tell(), 0)

    def test_the_matching_header_admits(self):
        with mock.patch.dict(os.environ, {app.SHARED_SECRET_ENV: "s3cret"}):
            handler = Recorder("/storage", headers={app.TOKEN_HEADER: "s3cret"})
            handler.do_GET()
        self.assertEqual(handler.sent["code"], 200)


class BodyCapTests(Fixture):
    def test_a_body_over_the_cap_is_413_before_it_is_read(self):
        handler = Recorder("/project/cleanup", {"name": "demo"})
        handler.headers["Content-Length"] = str(app.MAX_BODY_BYTES + 1)
        handler.do_POST()
        self.assertEqual(handler.sent["code"], 413)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_a_body_at_the_cap_is_read(self):
        with mock.patch.object(app, "MAX_BODY_BYTES", 64):
            handler = Recorder("/project/cleanup", {"name": "demo"})
            handler.do_POST()
        self.assertEqual(handler.sent["code"], 200)


class RunValidationTests(Fixture):
    def run_request(self, body):
        handler = Recorder("/run", body)
        handler.do_POST()
        return handler

    def test_an_unknown_project_is_refused_not_joined(self):
        for name in ("nope", "../demo", "", "Demo; rm -rf"):
            handler = self.run_request({"stage": "qa", "project": name, "segment": "x"})
            self.assertEqual(handler.sent["code"], 400, name)
            self.assertEqual(handler.json()["error"], "unknown project")

    def test_an_option_that_reads_as_a_flag_is_refused(self):
        for bad in ("--force", "-p", "a\nb", "x" * 300):
            handler = self.run_request(
                {"stage": "qa", "project": "demo", "segment": "seg", "model": bad})
            self.assertEqual(handler.sent["code"], 400, bad)
            self.assertIn("model", handler.json()["error"])

    def test_the_segment_slug_is_an_option_too(self):
        handler = self.run_request({"stage": "qa", "project": "demo", "segment": "--yes"})
        self.assertEqual(handler.sent["code"], 400)

    def test_a_valid_request_builds_the_argv(self):
        with mock.patch.object(subprocess, "Popen") as popen:
            popen.return_value.stdout = iter(["hello\n"])
            popen.return_value.returncode = 0
            popen.return_value.poll.return_value = 0
            self.run_request({"stage": "qa", "project": "demo", "segment": "01_desk",
                              "model": "anthropic/claude-3.5", "force": True})
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[2:], ["-p", "demo", "qa", "--force",
                                   "--model", "anthropic/claude-3.5", "01_desk"])

    def test_a_second_run_on_the_same_project_is_refused(self):
        app._running["demo"] = None
        handler = self.run_request({"stage": "qa", "project": "demo", "segment": "seg"})
        self.assertEqual(handler.sent["code"], 409)

    def test_the_child_is_stopped_when_the_connection_drops(self):
        class Wfile:
            def write(self, _):
                raise BrokenPipeError()

            def flush(self):
                pass

        with mock.patch.object(subprocess, "Popen") as popen:
            proc = popen.return_value
            proc.stdout = iter(["line\n"])
            proc.poll.return_value = None
            handler = Recorder("/run", {"stage": "qa", "project": "demo", "segment": "seg"})
            handler.wfile = Wfile()
            handler.do_POST()
        proc.terminate.assert_called_once()
        proc.wait.assert_called()
        self.assertNotIn("demo", app._running)


class ImportSourceTests(Fixture):
    def test_a_file_inside_the_project_resolves(self):
        zip_path = os.path.join(self.dir, "research", "imports", "export.zip")
        write(zip_path, "PK")
        self.assertEqual(app.import_source("demo", zip_path),
                         os.path.realpath(zip_path))

    def test_a_relative_path_is_confined_the_same_way(self):
        zip_path = os.path.join(self.dir, "research", "imports", "export.zip")
        write(zip_path, "PK")
        self.assertEqual(app.import_source("demo", os.path.relpath(zip_path, self.root)),
                         os.path.realpath(zip_path))

    def test_paths_outside_the_project_are_refused_even_when_they_exist(self):
        write(os.path.join(self.root, "projects", ".credentials.json"), "{}")
        write(os.path.join(self.root, "projects", "other", "research", "imports", "x.zip"), "PK")
        for bad in (os.path.join(self.root, "projects", ".credentials.json"),
                    os.path.join(self.root, "projects", "other", "research", "imports", "x.zip"),
                    "/proc/self/environ",
                    os.path.join(self.dir, "..", "other", "research", "imports", "x.zip")):
            with self.assertRaises(remix.RemixError, msg=bad):
                app.import_source("demo", bad)

    def test_the_run_route_answers_the_refusal_as_text(self):
        write(os.path.join(self.root, "projects", ".credentials.json"), "{}")
        handler = Recorder("/run", {"stage": "import", "project": "demo",
                                    "source": os.path.join(self.root, "projects",
                                                           ".credentials.json")})
        with mock.patch.object(subprocess, "Popen") as popen:
            handler.do_POST()
        popen.assert_not_called()
        self.assertEqual(handler.sent["code"], 200)
        self.assertIn("inside this project", handler.sent["body"])


if __name__ == "__main__":
    unittest.main()
