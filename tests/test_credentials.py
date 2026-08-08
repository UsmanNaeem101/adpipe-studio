"""Shared Studio/CLI credential resolution and secret-handling regressions."""

import json
import os
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app
import credentials
import llm
import openrouter


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "private", "credentials.json")

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, **values):
        return credentials.update(values, path=self.path, environ={})

    def test_environment_overrides_stored_value(self):
        self.save(openai="stored-openai", anthropic="stored-anthropic",
                  openrouter="stored-openrouter")
        cases = (
            ("openai", "OPENAI_API_KEY"),
            ("anthropic", "ANTHROPIC_API_KEY"),
            ("openrouter", "OPENROUTER_API_KEY"),
        )
        for provider, variable in cases:
            with self.subTest(provider=provider):
                value = credentials.resolve(
                    provider, path=self.path,
                    environ={variable: f"environment-{provider}"})
                self.assertEqual(value, f"environment-{provider}")

    def test_stored_value_is_cli_fallback(self):
        self.save(anthropic="stored-anthropic")
        self.assertEqual(
            credentials.resolve("anthropic", path=self.path, environ={}),
            "stored-anthropic")

    def test_missing_credential_is_empty(self):
        self.assertEqual(
            credentials.resolve("openrouter", path=self.path, environ={}), "")

    def test_provider_specific_lookup_does_not_cross_keys(self):
        self.save(openai="openai-secret", anthropic="anthropic-secret",
                  openrouter="openrouter-secret")
        self.assertEqual(credentials.resolve(
            "openai", path=self.path, environ={}), "openai-secret")
        self.assertEqual(credentials.resolve(
            "anthropic", path=self.path, environ={}), "anthropic-secret")
        self.assertEqual(credentials.resolve(
            "openrouter", path=self.path, environ={}), "openrouter-secret")

    def test_store_is_private_and_status_is_redacted(self):
        secret = "sk-should-never-be-in-an-api-response"
        state = self.save(openrouter=secret)
        self.assertEqual(state, {
            "openai": False, "anthropic": False, "openrouter": True})
        self.assertNotIn(secret, json.dumps(state))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)

    def test_explicit_clear_removes_only_requested_provider(self):
        self.save(anthropic="a", openrouter="o")
        state = credentials.update(
            {}, clear=["openrouter"], path=self.path, environ={})
        self.assertEqual(state, {
            "openai": False, "anthropic": True, "openrouter": False})


class ProviderIntegrationTests(unittest.TestCase):
    def test_openrouter_client_uses_shared_resolver(self):
        with mock.patch.object(credentials, "resolve",
                               return_value="stored-openrouter") as resolve:
            client = openrouter.Client(model="test", verbose=False)
        resolve.assert_called_once_with("openrouter")
        self.assertEqual(client.key, "stored-openrouter")

    def test_openrouter_missing_key_keeps_helpful_failure(self):
        with mock.patch.object(credentials, "resolve", return_value=""):
            with self.assertRaises(SystemExit) as caught:
                openrouter.Client(model="test", verbose=False)
        self.assertIn("No OpenRouter key", str(caught.exception))
        self.assertIn("Settings tab", str(caught.exception))

    def test_anthropic_client_uses_shared_resolver(self):
        seen = []

        class FakeClient:
            auth_token = None

            def __init__(self, api_key=None):
                self.api_key = api_key
                seen.append(api_key)

        sdk = types.SimpleNamespace(Anthropic=FakeClient)
        with mock.patch.dict(sys.modules, {"anthropic": sdk}), \
                mock.patch.object(credentials, "resolve",
                                  return_value="stored-anthropic") as resolve:
            client = llm.Client(model="test", verbose=False)
        resolve.assert_called_once_with("anthropic")
        self.assertEqual(seen, ["stored-anthropic"])
        self.assertEqual(client.client.api_key, "stored-anthropic")


class StudioMigrationTests(unittest.TestCase):
    def test_settings_migrates_legacy_browser_keys_without_persisting_new_ones(self):
        self.assertIn("localStorage.getItem('adpipe_keys')", app.PAGE)
        self.assertIn("localStorage.removeItem('adpipe_keys')", app.PAGE)
        self.assertNotIn("localStorage.setItem('adpipe_keys'", app.PAGE)
        self.assertIn("Migrated your browser-saved keys", app.PAGE)

    def test_settings_exposes_status_not_saved_values(self):
        self.assertIn("keys are never returned to the browser", app.PAGE)
        self.assertNotIn("Remember in this browser", app.PAGE)


if __name__ == "__main__":
    unittest.main()
