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
import urllib.parse

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


class LargeTextTests(unittest.TestCase):
    """Where a 16MB corpus goes.

    Routing was decided by file extension alone, and .jsonl is not a binary
    suffix — so a raw VOC dump took the table path, as one PostgREST JSON body,
    which a hosted API gateway refuses long before Postgres would care. The
    biggest files in the system were the ones taking the least-exercised path.

    The row is still written, holding a marker instead of the text, so `exists`
    and `list_keys` keep answering from one place.
    """

    def setUp(self):
        self.store = StubSupabase()
        self.small = "x" * 128
        self.large = "y" * (store_module.TEXT_MAX_BYTES + 1)

    def sent(self, method, fragment):
        return [c for c in self.store.calls
                if c["method"] == method and fragment in c["path"]]

    def test_small_text_still_goes_to_the_table(self):
        self.store.write_text("projects/p/research/voc/notes.jsonl", self.small)
        self.assertTrue(self.sent("POST", "/rest/v1/adpipe_files"))
        self.assertEqual(self.sent("POST", "/rest/v1/adpipe_files")[0]["body"]["content"],
                         self.small)
        self.assertFalse(self.sent("POST", "/storage/v1/object/"))

    def test_a_small_write_costs_exactly_one_request(self):
        # urllib opens a fresh connection per call, and the audit log writes a
        # file per model request. A tidy-up round trip on each of those would be
        # paid thousands of times to catch a case that arises when a corpus
        # shrinks below half a megabyte.
        self.store.write_text("projects/p/research/voc/notes.jsonl", self.small)
        self.assertEqual(len(self.store.calls), 1)

    def test_a_corpus_goes_to_the_bucket(self):
        self.store.write_text("projects/p/research/voc/deduplicated_voc.jsonl", self.large)
        upload = self.sent("POST", "/storage/v1/object/")
        self.assertEqual(len(upload), 1)
        self.assertTrue(upload[0]["binary"])
        self.assertEqual(upload[0]["body"], self.large.encode("utf-8"))

    def test_the_row_is_still_written_and_says_what_it_stands_for(self):
        self.store.write_text("projects/p/research/voc/big.jsonl", self.large)
        row = self.sent("POST", "/rest/v1/adpipe_files")[0]["body"]["content"]
        self.assertTrue(row.startswith(store_module.OVERFLOW_PREFIX))
        self.assertEqual(json.loads(row[len(store_module.OVERFLOW_PREFIX):])["bytes"],
                         len(self.large.encode("utf-8")))

    def test_a_corpus_reads_back_as_itself(self):
        # The marker is an implementation detail of storage. Nothing above this
        # class knows the file went anywhere unusual.
        self.store.reply = (200, json.dumps(
            [{"content": store_module.OVERFLOW_PREFIX + '{"bytes":9}'}]).encode())
        original = self.store._request

        def routed(method, path, **kwargs):
            if "/storage/v1/object/" in path:
                return 200, b"recovered"
            return original(method, path, **kwargs)

        self.store._request = routed
        self.assertEqual(self.store.read_text("projects/p/research/voc/big.jsonl"),
                         "recovered")

    def test_a_row_whose_object_is_gone_raises_rather_than_returns_the_marker(self):
        # Handing the marker back as file content would put "adpipe:overflow:v1:"
        # through a JSONL parser, and the traceback would name the parser.
        self.store.reply = (200, json.dumps(
            [{"content": store_module.OVERFLOW_PREFIX + '{"bytes":9}'}]).encode())
        original = self.store._request

        def missing(method, path, **kwargs):
            if "/storage/v1/object/" in path:
                return 404, b""
            return original(method, path, **kwargs)

        self.store._request = missing
        with self.assertRaises(IOError):
            self.store.read_text("projects/p/research/voc/big.jsonl")

    def test_text_that_begins_like_a_marker_is_stored_as_one_too(self):
        # Otherwise a file could impersonate the marker and be read as a
        # pointer to a bucket object that was never uploaded. Size is not what
        # is being tested here — the ambiguity is.
        self.store.write_text("projects/p/a.md", store_module.OVERFLOW_PREFIX + "hello")
        self.assertTrue(self.sent("POST", "/storage/v1/object/"))

    def test_shrinking_back_under_the_limit_removes_the_old_object(self):
        # Otherwise the row says "small text" while the bucket still holds the
        # previous, larger version, and a later read could find either.
        self.store.write_text("projects/p/research/voc/x.jsonl", self.large)
        self.store.calls.clear()
        self.store.write_text("projects/p/research/voc/x.jsonl", self.small)
        self.assertEqual(self.sent("POST", "/rest/v1/adpipe_files")[0]["body"]["content"],
                         self.small)
        self.assertTrue(self.sent("DELETE", "/storage/v1/object/"))

    def test_deleting_takes_both_halves(self):
        # The row that would say whether this key overflowed is the one being
        # deleted, so the object has to go unconditionally.
        self.store.write_text("projects/p/research/voc/x.jsonl", self.large)
        self.store.calls.clear()
        self.store.delete("projects/p/research/voc/x.jsonl")
        self.assertTrue(self.sent("DELETE", "/rest/v1/adpipe_files"))
        self.assertTrue(self.sent("DELETE", "/storage/v1/object/"))

    def test_a_failed_upload_is_not_silent(self):
        self.store.reply = (413, b'{"message":"too large"}')
        with self.assertRaises(IOError):
            self.store.write_text("projects/p/research/voc/big.jsonl", self.large)


class PagedListingTests(unittest.TestCase):
    """A listing that stops at a limit without saying so.

    PostgREST caps an unbounded select and the bucket listing took a literal
    1000, and neither says it truncated — the caller reads the answer as "that
    is everything". A stage then runs against part of the evidence and reports
    success, which is the failure mode with no symptom.

    One real project passes both caps on its own: 37 evidence files, twenty
    extractions each, three audit files per model request.
    """

    class Pager(store_module.SupabaseStore):
        def __init__(self, total):
            super().__init__("https://p.supabase.co", "k")
            self.keys = ["projects/p/research/extractions/s/%04d.md" % i
                         for i in range(total)]
            self.objects = ["projects/p/assets/renders/%04d.png" % i
                            for i in range(total)]
            self.requests = 0

        def _request(self, method, path, *, body=None, headers=None, binary=False):
            self.requests += 1
            if path.startswith("/storage/v1/object/list/"):
                start = (body or {}).get("offset", 0)
                window = self.objects[start:start + (body or {}).get("limit", 0)]
                return 200, json.dumps(
                    [{"name": k.split("/")[-1]} for k in window]).encode()
            params = urllib.parse.parse_qs(path.split("?", 1)[1])
            start = int(params.get("offset", ["0"])[0])
            size = int(params.get("limit", ["0"])[0])
            return 200, json.dumps(
                [{"key": k} for k in self.keys[start:start + size]]).encode()

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def listing(self, total, prefix="projects/p"):
        pager = self.Pager(total)
        pager.underlay = store_module.LocalStore(self.dir)
        return pager, pager.list_keys(prefix)

    def test_everything_comes_back_past_the_first_page(self):
        _, keys = self.listing(store_module.PAGE + 1)
        self.assertEqual(len([k for k in keys if k.endswith(".md")]),
                         store_module.PAGE + 1)

    def test_and_past_several_pages(self):
        _, keys = self.listing(store_module.PAGE * 3 + 7)
        self.assertEqual(len([k for k in keys if k.endswith(".md")]),
                         store_module.PAGE * 3 + 7)

    def test_the_bucket_is_paged_too(self):
        # Binary keys are not rows, so they are listed separately — and had a
        # limit written into the request rather than inherited from a server.
        _, keys = self.listing(store_module.PAGE + 5)
        self.assertEqual(len([k for k in keys if k.endswith(".png")]),
                         store_module.PAGE + 5)

    def test_a_short_page_ends_it_rather_than_looping(self):
        pager, keys = self.listing(3)
        # One table page, one bucket page, and no speculative extra round trips.
        self.assertEqual(pager.requests, 2)

    def test_an_empty_prefix_is_not_an_infinite_loop(self):
        pager, keys = self.listing(0)
        self.assertEqual(pager.requests, 2)


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


class ListingShapeTests(unittest.TestCase):
    """Absolute in, absolute out.

    Callers hold absolute paths, because that is what `paths.py` returns, and
    some of them ask about directories outside the data root entirely. Answering
    with keys relative to the root made the two impossible to line up, and the
    symptom was a listing coming back empty for a directory that was plainly
    full — which looked like the pipeline losing its own extractions.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = store_module.LocalStore(self.dir)

    def test_an_absolute_prefix_answers_absolutely(self):
        self.store.write_text("projects/p/extractions/s/07_pain.md", "x")
        absolute = os.path.join(self.dir, "projects", "p", "extractions", "s")
        keys = self.store.list_keys(absolute)
        self.assertEqual(keys, [absolute + "/07_pain.md"])

    def test_a_relative_prefix_answers_relatively(self):
        self.store.write_text("projects/p/a.json", "{}")
        self.assertEqual(self.store.list_keys("projects/p"), ["projects/p/a.json"])

    def test_names_in_works_for_either_shape(self):
        self.store.write_text("projects/p/one/a.md", "x")
        self.store.write_text("projects/p/two/b.md", "x")
        store_module.use(self.store)
        try:
            self.assertEqual(store_module.names_in("projects/p"), ["one", "two"])
            self.assertEqual(
                store_module.names_in(os.path.join(self.dir, "projects", "p")),
                ["one", "two"])
        finally:
            store_module.use(None)

    def test_a_directory_outside_the_root_can_still_be_listed(self):
        outside = tempfile.mkdtemp()
        os.makedirs(os.path.join(outside, "day"), exist_ok=True)
        with open(os.path.join(outside, "day", "events.jsonl"), "w") as fh:
            fh.write("{}\n")
        store_module.use(self.store)
        try:
            self.assertEqual(store_module.names_in(outside), ["day"])
        finally:
            store_module.use(None)


class DirectoryShapeTests(unittest.TestCase):
    """"Directories under here" has no direct equivalent in a store.

    `os.listdir(d)` filtered by `os.path.isdir` was how this pipeline found its
    projects, its segments and its stages. Replacing that with "does this
    exist" answers yes for a stray file, and a stray file listed as a project
    is the kind of wrong that only shows up in front of someone.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = store_module.LocalStore(self.dir)
        store_module.use(self.store)

    def tearDown(self):
        store_module.use(None)

    def test_only_names_with_something_under_them_count(self):
        self.store.write_text("projects/montisella/product.json", "{}")
        self.store.write_text("projects/lumbar/product.json", "{}")
        self.store.write_text("projects/stray-note.txt", "not a project")
        self.assertEqual(store_module.dirs_in("projects"), ["lumbar", "montisella"])
        # names_in is the unfiltered question, and still answers it.
        self.assertIn("stray-note.txt", store_module.names_in("projects"))

    def test_it_works_from_an_absolute_prefix_too(self):
        self.store.write_text("projects/p/extractions/seg/07.md", "x")
        absolute = os.path.join(self.dir, "projects", "p", "extractions")
        self.assertEqual(store_module.dirs_in(absolute), ["seg"])

    def test_nothing_there_is_an_empty_list(self):
        self.assertEqual(store_module.dirs_in("projects"), [])


class UnderlayTests(unittest.TestCase):
    """The image underneath.

    Templates, reference ads and the skills markdown ship inside the container
    and are read-only. A read that misses the store falls through to them; a
    write never does, so nothing can overwrite what shipped.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = StubSupabase()
        self.store.underlay = store_module.LocalStore(self.dir)

    def test_a_read_falls_through_to_the_image(self):
        self.store.underlay.write_text("pipeline/templates/a.html", "<div>")
        self.store.reply = (200, b"[]")  # not in the store
        self.assertEqual(self.store.read_text("pipeline/templates/a.html"), "<div>")

    def test_existence_falls_through_too(self):
        self.store.underlay.write_text("references/ads/one.png", "x")
        self.store.reply = (404, b"")
        self.assertTrue(self.store.exists("references/ads/one.png"))

    def test_a_write_never_goes_to_the_image(self):
        self.store.reply = (201, b"")
        self.store.write_text("projects/p/brief.md", "mine")
        self.assertIsNone(self.store.underlay.read_text("projects/p/brief.md"))
