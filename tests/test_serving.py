"""Running as a service rather than on a desk.

Three things differ on a container and each one is silent when it is wrong: the
port it listens on, the address it binds to, and the browser it screenshots ads
with. A studio bound to loopback inside a container is simply unreachable, with
no error to read; a missing browser fails only when someone renders.
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import render  # noqa: E402


class ChromeDiscoveryTests(unittest.TestCase):
    def test_the_chrome_variable_is_honoured(self):
        # The error message has told people to set this for a long time and
        # nothing read it. On a container it is the only way to name a browser
        # that is neither in /Applications nor first on PATH.
        with mock.patch.dict(os.environ, {"CHROME": sys.executable}):
            self.assertEqual(render.find_chrome(), sys.executable)

    def test_a_chrome_variable_pointing_nowhere_says_so(self):
        # Falling back silently would render every ad with the wrong browser,
        # or none, long after the person who set it stopped watching.
        with mock.patch.dict(os.environ, {"CHROME": "/no/such/browser"}):
            with self.assertRaises(SystemExit) as caught:
                render.find_chrome()
            self.assertIn("/no/such/browser", str(caught.exception))

    def test_it_still_finds_a_browser_without_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(render, "CHROME_CANDIDATES", []):
                with mock.patch.object(render.shutil, "which",
                                       side_effect=lambda name: "/usr/bin/chromium"
                                       if name == "chromium" else None):
                    self.assertEqual(render.find_chrome(), "/usr/bin/chromium")

    def test_renders_run_without_a_sandbox(self):
        # Chromium as root in a container refuses to start otherwise, and the
        # error it gives does not mention the sandbox.
        recorded = {}

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(render.subprocess, "run", fake_run):
            render.chrome_run("/usr/bin/chromium", "/tmp/a.html", ["--screenshot=/tmp/a.png"])
        self.assertIn("--no-sandbox", recorded["cmd"])
        self.assertIn("--headless", recorded["cmd"])


class ListenAddressTests(unittest.TestCase):
    """app.py reads these at import, so they are re-derived the same way here."""

    @staticmethod
    def resolve(environ):
        port = int(environ.get("STUDIO_PORT") or environ.get("PORT") or "8765")
        host = environ.get("STUDIO_HOST", "127.0.0.1")
        return host, port

    def test_loopback_by_default(self):
        # On a laptop this app is for the person sitting at it, and it has no
        # login of its own — binding wider would put it on the local network.
        self.assertEqual(self.resolve({}), ("127.0.0.1", 8765))

    def test_a_container_can_bind_wide(self):
        self.assertEqual(self.resolve({"STUDIO_HOST": "0.0.0.0"})[0], "0.0.0.0")

    def test_the_platform_port_is_accepted(self):
        # Railway and friends inject PORT; STUDIO_PORT keeps precedence so a
        # local override still wins.
        self.assertEqual(self.resolve({"PORT": "3000"})[1], 3000)
        self.assertEqual(self.resolve({"PORT": "3000", "STUDIO_PORT": "8765"})[1], 8765)


if __name__ == "__main__":
    unittest.main()
