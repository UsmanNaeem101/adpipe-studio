"""Where is my work going, asked without redeploying to read a log.

The startup banner answers this once per process, by writing a boot record. That
is the wrong shape for a page: it cannot be asked twice without inflating the
count, and by the time somebody wants to know they are mid-session and not
looking at a deploy log.

The value is in the failure modes. A missing table, an anon key pasted where the
service_role key belongs, and an unreachable host all look identical from inside
the app — an empty project list — and every one of them is a setting somebody
can fix in a minute once told which. So each answer names its own remedy.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import paths  # noqa: E402
import store as store_module  # noqa: E402


class Answering(store_module.SupabaseStore):
    def __init__(self, reply):
        super().__init__("https://abcdefgh.supabase.co", "service-key")
        self.reply = reply
        self.asked = []

    def _request(self, method, path, *, body=None, headers=None, binary=False):
        self.asked.append((method, path))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.original = (paths.ROOT, app.ROOT)
        paths.ROOT = app.ROOT = self.data

    def tearDown(self):
        paths.ROOT, app.ROOT = self.original
        store_module.use(None)

    def health(self, reply):
        store_module.use(Answering(reply))
        return store_module.health()

    def test_a_working_store_says_so_and_names_the_project(self):
        found = self.health((200, b"[]"))
        self.assertTrue(found["ok"])
        self.assertEqual(found["where"], "abcdefgh")

    def test_the_anon_key_is_named_because_it_is_the_usual_mistake(self):
        found = self.health((401, b'{"message":"invalid jwt"}'))
        self.assertFalse(found["ok"])
        self.assertIn("service_role", found["detail"])

    def test_a_missing_table_points_at_the_migration(self):
        found = self.health((404, b'{"message":"relation does not exist"}'))
        self.assertFalse(found["ok"])
        self.assertIn("adpipe_files", found["detail"])
        self.assertIn("migration", found["detail"])

    def test_an_unreachable_host_is_reported_rather_than_thrown(self):
        # This runs inside a request. Raising would take the Settings tab down
        # at the moment it is being used to diagnose the outage.
        found = self.health(OSError("name or service not known"))
        self.assertFalse(found["ok"])
        self.assertIn("name or service not known", found["detail"])

    def test_an_unexpected_status_carries_what_supabase_said(self):
        found = self.health((500, b'{"message":"upstream boom"}'))
        self.assertFalse(found["ok"])
        self.assertIn("upstream boom", found["detail"])

    def test_no_supabase_at_all_says_the_disk_will_be_discarded(self):
        store_module.use(store_module.LocalStore(self.data))
        found = store_module.health()
        self.assertEqual(found["kind"], "local")
        self.assertIn("discards", found["detail"])

    def test_asking_writes_nothing(self):
        # The banner's boot record is a write. This is asked on every visit to
        # the Settings tab, and a counter that climbs with page loads is worse
        # than no counter.
        answering = Answering((200, b"[]"))
        store_module.use(answering)
        store_module.health()
        self.assertEqual([method for method, _ in answering.asked], ["GET"])

    def test_it_does_not_pull_rows_back_to_answer(self):
        # A project can hold a 16MB corpus. This must stay one cheap request.
        answering = Answering((200, b"[]"))
        store_module.use(answering)
        store_module.health()
        self.assertIn("limit=1", answering.asked[0][1])


class VariableNameTests(HealthTests):
    """The two variables, and the ways they are spelled wrong.

    This cost an afternoon. The deployment had SUPABASE_SERVICE_ROLE_KEY and
    NEXT_PUBLIC_SUPABASE_URL — the second being what Topic Atlas calls it,
    because Next.js needs the prefix to expose a value to the browser, and the
    two apps share one Supabase project. AdPipe read SUPABASE_URL, found
    nothing, and ran on the container's disk exactly as if nothing had been
    configured at all. Every write succeeded. Every deploy discarded them.
    """

    def test_the_next_js_name_is_accepted_for_the_url(self):
        built = store_module.build_store(
            {"NEXT_PUBLIC_SUPABASE_URL": "https://x.supabase.co",
             "SUPABASE_SERVICE_ROLE_KEY": "k"})
        self.assertEqual(built.kind, "supabase")

    def test_the_plain_name_still_wins(self):
        url, _ = store_module.supabase_settings(
            {"SUPABASE_URL": "https://plain.supabase.co",
             "NEXT_PUBLIC_SUPABASE_URL": "https://prefixed.supabase.co"})
        self.assertEqual(url, "https://plain.supabase.co")

    def test_there_is_no_such_fallback_for_the_key(self):
        # A service-role key under a NEXT_PUBLIC_ name is a key published to
        # every visitor. Accepting one here would make that mistake work.
        _, key = store_module.supabase_settings(
            {"NEXT_PUBLIC_SUPABASE_SERVICE_ROLE_KEY": "leaked"})
        self.assertEqual(key, "")

    def test_a_key_with_no_url_says_which_one_is_missing(self):
        store_module.use(store_module.LocalStore(self.data))
        found = store_module.health(environ={"SUPABASE_SERVICE_ROLE_KEY": "k"})
        self.assertEqual(found["kind"], "local")
        self.assertIn("SUPABASE_URL is not", found["detail"])

    def test_a_url_with_no_key_says_which_one_is_missing(self):
        store_module.use(store_module.LocalStore(self.data))
        found = store_module.health(
            environ={"SUPABASE_URL": "https://x.supabase.co"})
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY is not", found["detail"])

    def test_neither_set_is_not_reported_as_a_mistake(self):
        # A laptop is meant to run on the disk. Only a half-configuration is a
        # mistake worth naming.
        store_module.use(store_module.LocalStore(self.data))
        found = store_module.health(environ={})
        self.assertNotIn("is not", found["detail"])
        self.assertIn("discards", found["detail"])


class EndpointTests(HealthTests):
    def test_the_route_answers_with_the_report_and_a_count(self):
        store_module.use(Answering((200, b"[]")))
        captured = {}

        class Recorder(app.Handler):
            def __init__(self):
                self.path = "/storage"

            def _send(self, code, body, ctype="application/json", download=None):
                captured.update(json.loads(body))

        Recorder().do_GET()
        self.assertTrue(captured["ok"])
        self.assertEqual(captured["kind"], "supabase")
        self.assertIn("projects", captured)


if __name__ == "__main__":
    unittest.main()
