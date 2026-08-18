"""Running as a service rather than on a desk.

Four things differ on a container and each one is silent when it is wrong: the
port it listens on, the address it binds to, the browser it screenshots ads
with, and where its API keys are kept. A studio bound to loopback inside a
container is simply unreachable, with no error to read; a missing browser fails
only when someone renders; and a credential store off the mounted volume works
perfectly until the next deploy wipes it.
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import credentials  # noqa: E402
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


class StartupCredentialLineTests(unittest.TestCase):
    """The startup banner answers "are my keys here, and where do new ones go?"

    Both halves were wrong on the first container deploy — the store path came
    from a variable name nothing read — and neither is visible until somebody
    spends money and gets a 401 back.
    """

    def test_it_names_the_providers_that_have_a_key(self):
        with mock.patch.object(credentials, "status",
                               return_value={"openai": False, "anthropic": True,
                                             "openrouter": True}):
            line = app.credential_line()
        self.assertIn("anthropic", line)
        self.assertIn("openrouter", line)
        self.assertNotIn("openai", line)

    def test_it_never_prints_a_key(self):
        # status() returns presence booleans precisely so this line cannot leak,
        # and these logs are read by whoever can see the deployment.
        secret = "sk-do-not-log-me"
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": secret}):
            with mock.patch.dict(os.environ, {"ADPIPE_CREDENTIALS_FILE":
                                              "/nowhere/credentials.json"}):
                line = app.credential_line()
        self.assertNotIn(secret, line)
        self.assertIn("openrouter", line)

    def test_with_no_keys_it_says_where_a_settings_key_will_land(self):
        # The container question. If this prints a path that is not on the
        # volume, keys typed into Settings survive until the next deploy.
        with mock.patch.dict(os.environ,
                             {"ADPIPE_CREDENTIALS_FILE": "/app/projects/.credentials.json"},
                             clear=True):
            line = app.credential_line()
        self.assertIn("/app/projects/.credentials.json", line)
        self.assertIn("Settings", line)

    def test_a_variable_read_by_nothing_is_called_out(self):
        """The trap this deployment actually fell into.

        ADPIPE_CREDENTIALS_PATH sat in the variable list for weeks doing nothing.
        The store stayed at its user-level default — off the volume, wiped by
        every deploy — while the list said the question had been dealt with. A
        setting that is ignored is worse than one that is absent: absent prompts
        a search, ignored prompts nothing.
        """
        with mock.patch.dict(os.environ,
                             {"ADPIPE_CREDENTIALS_PATH": "/app/projects/x.json"},
                             clear=True):
            line = app.credential_line()
        self.assertIn("ADPIPE_CREDENTIALS_PATH", line)
        self.assertIn("read by nothing", line)
        self.assertIn("ADPIPE_CREDENTIALS_FILE", line)

    def test_it_stays_quiet_once_the_real_one_is_set(self):
        # Both together is a tidy-up, not a fault — the working one is in use.
        with mock.patch.dict(os.environ,
                             {"ADPIPE_CREDENTIALS_PATH": "/app/projects/x.json",
                              "ADPIPE_CREDENTIALS_FILE": "/app/projects/x.json"},
                             clear=True):
            line = app.credential_line()
        self.assertNotIn("read by nothing", line)

    def test_it_says_nothing_when_neither_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("read by nothing", app.credential_line())

    def test_an_unreadable_store_is_reported_rather_than_thrown(self):
        # A corrupt store must not stop the studio starting: every other way in
        # still works, and the message is how anyone finds out.
        with mock.patch.object(credentials, "status",
                               side_effect=credentials.CredentialStoreError("bad json")):
            line = app.credential_line()
        self.assertIn("bad json", line)


if __name__ == "__main__":
    unittest.main()
