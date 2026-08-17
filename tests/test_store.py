"""The storage seam.

Everything AdPipe knows will eventually be read and written through this, so the
properties worth pinning are the ones a filesystem gave us for free and a remote
store does not: one spelling per key, no climbing out of the store, a missing
file reads as None rather than raising, and text and bytes survive the round trip
unchanged.

The Supabase backend is exercised against a stub transport. That is deliberate:
these tests must run in half a second with no network, and what is worth checking
is the requests it builds — the table it writes text to, the bucket it uploads
bytes to, and that it upserts rather than duplicating.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import store as store_module  # noqa: E402


class NormaliseTests(unittest.TestCase):
    def test_one_spelling_per_key(self):
        for messy in ["a//b", "./a/b", "a/b/", "a/./b", "a\\b"]:
            self.assertEqual(store_module.normalise(messy), "a/b", messy)

    def test_cannot_climb_out(self):
        self.assertEqual(store_module.normalise("../../etc/passwd"), "etc/passwd")
        self.assertEqual(store_module.normalise("a/../b"), "b")

    def test_binary_is_decided_by_suffix(self):
        self.assertTrue(store_module.is_binary_key("projects/x/assets/ad.PNG"))
        self.assertFalse(store_module.is_binary_key("projects/x/research/voc.jsonl"))


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = store_module.LocalStore(self.dir)

    def test_text_round_trip(self):
        self.store.write_text("projects/p/research/voc.jsonl", "line one\n")
        self.assertEqual(self.store.read_text("projects/p/research/voc.jsonl"), "line one\n")

    def test_bytes_round_trip(self):
        blob = bytes(range(256))
        self.store.write_bytes("projects/p/assets/ad.png", blob)
        self.assertEqual(self.store.read_bytes("projects/p/assets/ad.png"), blob)

    def test_missing_reads_as_none(self):
        # Callers branch on this constantly; raising would mean touching all of them.
        self.assertIsNone(self.store.read_text("nothing/here.json"))
        self.assertIsNone(self.store.read_bytes("nothing/here.png"))
        self.assertFalse(self.store.exists("nothing/here.json"))

    def test_write_creates_the_tree(self):
        self.store.write_text("a/deep/and/new/path.md", "#")
        self.assertTrue(self.store.exists("a/deep/and/new/path.md"))

    def test_listing_is_relative_and_sorted(self):
        self.store.write_text("projects/p/b.json", "{}")
        self.store.write_text("projects/p/a.json", "{}")
        self.store.write_text("projects/other/c.json", "{}")
        self.assertEqual(
            self.store.list_keys("projects/p"), ["projects/p/a.json", "projects/p/b.json"]
        )

    def test_delete_and_delete_prefix(self):
        self.store.write_text("projects/p/a.json", "{}")
        self.store.write_text("projects/p/sub/b.json", "{}")
        self.store.delete("projects/p/a.json")
        self.assertFalse(self.store.exists("projects/p/a.json"))
        self.store.delete_prefix("projects/p")
        self.assertEqual(self.store.list_keys("projects/p"), [])

    def test_deleting_what_is_not_there_is_quiet(self):
        self.store.delete("never/existed.json")
        self.store.delete_prefix("never/existed")

    def test_an_absolute_path_is_used_as_given(self):
        """Not every caller writes under the data root.

        The audit log can be pointed anywhere and did that long before this
        store existed. Rewriting an absolute path into the root would silently
        move a user's logs somewhere they would never look.
        """
        elsewhere = tempfile.mkdtemp()
        target = os.path.join(elsewhere, "day", "call", "request.json")
        self.store.write_text(target, "{}")
        self.assertTrue(os.path.exists(target))
        self.assertEqual(self.store.read_text(target), "{}")

    def test_a_path_under_the_root_is_the_same_file_either_way(self):
        # This is what lets a call site keep `paths.voc(project, "raw.jsonl")`
        # and change only the opening of it.
        self.store.write_text("projects/p/a.json", "one")
        absolute = os.path.join(self.dir, "projects", "p", "a.json")
        self.assertEqual(self.store.read_text(absolute), "one")

    def test_a_key_cannot_escape_the_root(self):
        outside = os.path.join(self.dir, "..", "escaped.txt")
        self.store.write_text("../escaped.txt", "no")
        self.assertFalse(os.path.exists(os.path.abspath(outside)))
        self.assertTrue(self.store.exists("escaped.txt"))


class StubSupabase(store_module.SupabaseStore):
    """Records requests instead of making them."""

    def __init__(self):
        super().__init__("https://project.supabase.co", "service-key")
        self.calls = []
        self.reply = (200, b"[]")

    def _request(self, method, path, *, body=None, headers=None, binary=False):
        self.calls.append({"method": method, "path": path, "body": body,
                           "headers": headers or {}, "binary": binary})
        return self.reply


class SupabaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = StubSupabase()

    def test_text_goes_to_the_table_and_upserts(self):
        self.store.write_text("projects/montisella/brief.md", "# brief")
        call = self.store.calls[-1]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/rest/v1/adpipe_files", call["path"])
        self.assertEqual(call["body"]["content"], "# brief")
        # Re-rendering a stage rewrites its output; a second row would mean the
        # next read gets whichever Postgres felt like.
        self.assertIn("merge-duplicates", call["headers"]["Prefer"])

    def test_the_project_is_recorded_alongside_the_key(self):
        self.store.write_text("projects/montisella/brief.md", "x")
        self.assertEqual(self.store.calls[-1]["body"]["project"], "montisella")
        self.store.write_text("references/readme.md", "x")
        self.assertIsNone(self.store.calls[-1]["body"]["project"])

    def test_bytes_go_to_the_bucket_and_overwrite(self):
        self.store.write_bytes("projects/p/assets/ad.png", b"\x89PNG")
        call = self.store.calls[-1]
        self.assertIn("/storage/v1/object/adpipe-media/projects/p/assets/ad.png", call["path"])
        self.assertTrue(call["binary"])
        self.assertEqual(call["headers"]["x-upsert"], "true")

    def test_reading_text_unwraps_the_row(self):
        self.store.reply = (200, json.dumps([{"content": "hello"}]).encode())
        self.assertEqual(self.store.read_text("projects/p/a.md"), "hello")

    def test_a_missing_row_reads_as_none(self):
        self.store.reply = (200, b"[]")
        self.assertIsNone(self.store.read_text("projects/p/a.md"))

    def test_a_failed_write_is_not_silent(self):
        # Losing a stage's output quietly is the worst thing this class could do.
        self.store.reply = (401, b'{"message":"nope"}')
        with self.assertRaises(IOError):
            self.store.write_text("projects/p/a.md", "x")
        with self.assertRaises(IOError):
            self.store.write_bytes("projects/p/a.png", b"x")

    def test_keys_are_normalised_before_they_are_sent(self):
        self.store.write_text("projects//p/./a.md", "x")
        self.assertEqual(self.store.calls[-1]["body"]["key"], "projects/p/a.md")


class BuildStoreTests(unittest.TestCase):
    def test_supabase_when_configured(self):
        built = store_module.build_store(
            {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}
        )
        self.assertEqual(built.kind, "supabase")

    def test_the_filesystem_otherwise(self):
        # A local run and the rest of this suite must behave exactly as before.
        built = store_module.build_store({})
        self.assertEqual(built.kind, "local")

    def test_the_local_store_follows_paths_root(self):
        """No root of its own unless one is named.

        The rest of the pipeline treats `paths.ROOT` as where things are, and
        the test suite moves it per test. A store that pinned a root at first
        use would resolve every relative key against the repository instead —
        which looked, from the outside, like products vanishing the moment they
        were created.
        """
        import paths

        built = store_module.build_store({})
        with tempfile.TemporaryDirectory() as moved:
            original = paths.ROOT
            paths.ROOT = moved
            try:
                self.assertEqual(built.root, os.path.abspath(moved))
                built.write_text("projects/p/a.json", "{}")
                self.assertTrue(os.path.exists(os.path.join(moved, "projects", "p", "a.json")))
            finally:
                paths.ROOT = original

    def test_a_named_root_wins(self):
        with tempfile.TemporaryDirectory() as named:
            built = store_module.build_store({"ADPIPE_DATA_ROOT": named})
            self.assertEqual(built.root, os.path.abspath(named))

    def test_half_a_configuration_is_not_supabase(self):
        # A URL with no key would fail on every call; better to stay local and
        # be obviously local than to fail obscurely on the first write.
        self.assertEqual(store_module.build_store({"SUPABASE_URL": "https://x"}).kind, "local")
        self.assertEqual(store_module.build_store({"SUPABASE_SERVICE_ROLE_KEY": "k"}).kind, "local")


if __name__ == "__main__":
    unittest.main()
