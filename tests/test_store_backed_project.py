"""A whole project's life with no disk under it.

Every one of these passed before as a unit and the deployment still lost work,
because the store was never the thing being asked. `store.write_text` was
correct; `open(dest, "w")` beside it was also correct, and only the second one
ran. The difference is invisible on a laptop, where the underlay and the data
root are the same directory and a file written either way reads back either way.

So this runs the real functions against a Supabase that is only a dictionary,
and then asserts on the dictionary: what is in it, and — the part no unit test
was asking — that nothing landed anywhere else. A file that appears on the
temporary root bypassed the store, and on a container that root is discarded on
the next deploy.

The chain it walks is the one somebody actually walks: create a project, import
the stage-06 export, extract against it, save a product, render an ad, then
delete it.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import importer  # noqa: E402
import paths  # noqa: E402
import products  # noqa: E402
import store as store_module  # noqa: E402


class DictSupabase(store_module.SupabaseStore):
    """Supabase as two dictionaries: the table and the bucket.

    Faithful only where this suite depends on it — upsert by key, `eq.` and
    `like.` filters, and object put/get/list/delete. Enough that anything
    reaching for the filesystem instead shows up as a file on disk.
    """

    def __init__(self, underlay_root):
        super().__init__("https://dict.supabase.co", "service-key")
        self.rows = {}
        self.objects = {}
        self.underlay = store_module.LocalStore(underlay_root)

    def _request(self, method, path, *, body=None, headers=None, binary=False):
        if path.startswith("/rest/v1/"):
            return self._table(method, path, body)
        if path.startswith("/storage/v1/object/list/"):
            prefix = (body or {}).get("prefix", "")
            names = [{"name": k[len(prefix) + 1:] if prefix else k}
                     for k in self.objects
                     if not prefix or k.startswith(prefix + "/")]
            return 200, json.dumps(names).encode()
        if path.startswith("/storage/v1/object/"):
            key = urllib.parse.unquote(
                path[len("/storage/v1/object/") + len(self.bucket) + 1:])
            if method == "POST":
                self.objects[key] = body
                return 200, b"{}"
            if method == "GET":
                return (200, self.objects[key]) if key in self.objects else (404, b"")
            if method == "DELETE":
                self.objects.pop(key, None)
                return 200, b"{}"
        raise AssertionError("unhandled %s %s" % (method, path))

    def _table(self, method, path, body):
        params = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
        clause = params.get("key", [""])[0]
        if method == "POST":
            self.rows[body["key"]] = body["content"]
            return 201, b""
        if method == "GET":
            if clause.startswith("eq."):
                key = clause[3:]
                if key not in self.rows:
                    return 200, b"[]"
                return 200, json.dumps([{"key": key, "content": self.rows[key]}]).encode()
            head = clause[5:].rstrip("%") if clause.startswith("like.") else ""
            return 200, json.dumps(
                [{"key": k, "content": self.rows[k]}
                 for k in sorted(self.rows) if k.startswith(head)]).encode()
        if method == "DELETE":
            if clause.startswith("eq."):
                self.rows.pop(clause[3:], None)
            return 200, b""
        raise AssertionError("unhandled table %s" % method)


def audience_markdown(name, count):
    """The shape a stage-06 export actually has: fenced original text."""
    lines = ["# %s" % name, "", "## Comments", ""]
    for i in range(count):
        lines += ["### Comment %d" % (i + 1), "",
                  "- **Source:** reddit r/desks", "",
                  "**Original text**", "", "```text",
                  "my back aches after %d hours at this desk" % (i + 1),
                  "```", ""]
    return "\n".join(lines)


def export_zip(*audiences):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, (name, count) in enumerate(audiences, 1):
            archive.writestr("%02d_%s.md" % (index, name.lower().replace(" ", "_")),
                             audience_markdown(name, count))
    return buffer.getvalue()


class StoreBackedProject(unittest.TestCase):
    def setUp(self):
        # An empty data root. Anything that lands here went round the store.
        self.data = tempfile.mkdtemp()
        self.original = (paths.ROOT, app.ROOT, getattr(products, "ROOT", None))
        paths.ROOT = app.ROOT = self.data
        if hasattr(products, "ROOT"):
            products.ROOT = self.data
        self.supabase = DictSupabase(self.data)
        store_module.use(self.supabase)

    def tearDown(self):
        paths.ROOT, app.ROOT = self.original[0], self.original[1]
        if self.original[2] is not None:
            products.ROOT = self.original[2]
        store_module.use(None)

    # -- helpers ----------------------------------------------------------

    def on_disk(self):
        return sorted(
            os.path.relpath(os.path.join(base, name), self.data).replace(os.sep, "/")
            for base, _dirs, files in os.walk(self.data) for name in files)

    def redeploy(self):
        """What a container does between deploys: the disk goes, the store stays."""
        for entry in os.listdir(self.data):
            target = os.path.join(self.data, entry)
            if os.path.isdir(target):
                __import__("shutil").rmtree(target)
            else:
                os.remove(target)

    def project(self, name="probe"):
        return os.path.join(self.data, "projects", name)


class CreationTests(StoreBackedProject):
    def test_a_new_project_is_in_the_store_not_on_the_disk(self):
        app.create_project("probe", "lumbar cushions", "desk workers")
        self.assertIn("projects/probe/project.json", self.supabase.rows)
        self.assertIn("projects/probe/facts.json", self.supabase.rows)
        self.assertEqual(self.on_disk(), [])

    def test_it_is_in_its_own_list_immediately(self):
        # projects() asks the store. Written with open(), a project created on a
        # deployment did not appear in the list it had just been created from.
        app.create_project("probe", "lumbar cushions", "desk workers")
        self.assertEqual(app.projects(), ["probe"])

    def test_it_survives_a_redeploy(self):
        app.create_project("probe", "lumbar cushions", "desk workers")
        self.redeploy()
        self.assertEqual(app.projects(), ["probe"])
        cfg = store_module.read_json(os.path.join(self.project(), "project.json"))
        self.assertEqual(cfg["name"], "probe")
        self.assertEqual(cfg["market"], "desk workers")

    def test_the_compliance_profile_comes_back_too(self):
        # Read with open() this returned "" after a deploy, and an empty
        # compliance profile is not a safe default — it is no rules at all.
        app.create_project("probe", "lumbar cushions", "desk workers")
        self.redeploy()
        self.assertEqual(app.compliance_notes("probe"),
                         (store_module.read_json(
                             os.path.join(self.project(), "project.json"))
                          .get("compliance", {}).get("notes", "")))

    def test_creating_it_twice_is_refused(self):
        app.create_project("probe", "lumbar cushions", "desk workers")
        with self.assertRaises(ValueError):
            app.create_project("probe", "other", "other")


class ImportAndExtractTests(StoreBackedProject):
    def setUp(self):
        super().setUp()
        app.create_project("probe", "lumbar cushions", "desk workers")
        rows = importer.plan(importer.audience_members(
            export_zip(("Desk Workers", 25), ("New Parents", 10))))
        importer.write(self.project(), rows, "export.zip")

    def test_the_evidence_lands_in_the_store(self):
        keys = [k for k in self.supabase.rows if "/evidence/" in k]
        self.assertEqual(sorted(keys), [
            "projects/probe/research/evidence/01_desk_workers.txt",
            "projects/probe/research/evidence/02_new_parents.txt",
            "projects/probe/research/evidence/_provenance.json",
        ])
        self.assertEqual(self.on_disk(), [])

    def test_every_verbatim_arrives(self):
        # The whole point of importing a segmentation someone already paid for.
        text = self.supabase.rows[
            "projects/probe/research/evidence/01_desk_workers.txt"]
        self.assertEqual(text.count("my back aches after"), 25)

    def test_the_app_lists_the_imported_segments(self):
        self.redeploy()
        self.assertEqual(app.segments("probe"), ["01_desk_workers", "02_new_parents"])

    def test_provenance_survives_and_still_says_imported(self):
        # Without this an imported file is indistinguishable from one this
        # pipeline produced, and every ad built on it inherits that silently.
        self.redeploy()
        outputs = app.project_outputs("probe")
        self.assertEqual(
            outputs["provenance"]["01_desk_workers"]["origin"], "imported")

    def test_the_skills_are_readable_even_though_they_only_ship_in_the_image(self):
        # list_keys did not fall through to the underlay while every reader did,
        # so the skills listed as absent — and skill() exits when it cannot find
        # one. Extract, picc, concepts and brief all refused to start.
        import cli
        name, body = cli.skill(7)
        self.assertTrue(name.startswith("07_"))
        self.assertGreater(len(body), 100)

    def test_extractions_round_trip_for_the_next_stage(self):
        import cli
        out = paths.extractions(self.project(), "01_desk_workers")
        store_module.write_text(os.path.join(out, "07_pain.md"), "# Pain\n\nsore\n")
        store_module.write_text(os.path.join(out, "08_moments.md"), "# Moments\n\n3pm\n")
        self.redeploy()
        cfg = {"_dir": self.project(), "name": "probe"}
        merged = cli.read_extractions(cfg, "01_desk_workers")
        # Listed through the store and read through it: mixed, this found both
        # names and then read neither, returning two empty sections.
        self.assertIn("sore", merged)
        self.assertIn("3pm", merged)


class UploadThenImportTests(StoreBackedProject):
    """The browser's route from a ChatGPT export to evidence files.

    Upload parses the zip and shows a plan; Run imports it. Those are two
    requests, and the second has to find what the first wrote — which stopped
    being true the moment the upload started going to the store while the guard
    still asked the filesystem. The browser showed a correct plan for a zip it
    had just read, and pressing Run answered "nothing uploaded to import yet".
    """

    def setUp(self):
        super().setUp()
        app.create_project("probe", "lumbar cushions", "desk workers")
        self.zip_bytes = export_zip(("Desk Workers", 12), ("New Parents", 8))

    def upload(self, filename="stage06.zip"):
        captured = {}

        class Recorder(app.Handler):
            def __init__(self):
                pass

            def _send(self, code, body, ctype="application/json", download=None):
                captured.update(json.loads(body))

        Recorder()._upload({"project": "probe", "kind": "import",
                            "filename": filename,
                            "data": __import__("base64").b64encode(
                                self.zip_bytes).decode()})
        return captured

    def test_the_upload_lands_in_the_store(self):
        reply = self.upload()
        self.assertTrue(reply.get("ok"), reply)
        self.assertTrue(store_module.exists(reply["path"]))
        self.assertEqual(self.on_disk(), [])

    def test_the_plan_is_shown_before_anything_is_written(self):
        reply = self.upload()
        self.assertEqual([row["slug"] for row in reply["plan"]],
                         ["01_desk_workers", "02_new_parents"])
        # Nothing written yet — the plan is what the browser confirms against.
        self.assertEqual([k for k in self.supabase.rows if "/evidence/" in k], [])

    def test_a_zip_survives_being_stored(self):
        # Routed as text it would be decoded as UTF-8, which a zip does not
        # survive — and the failure is a UnicodeDecodeError from inside the
        # store rather than anything naming the upload.
        reply = self.upload()
        self.assertEqual(store_module.read_bytes(reply["path"]), self.zip_bytes)

    def test_a_zip_with_no_extension_is_still_kept_as_one(self):
        reply = self.upload(filename="stage06export")
        self.assertTrue(reply.get("ok"), reply)
        self.assertTrue(reply["path"].endswith(".zip"))
        self.assertEqual(store_module.read_bytes(reply["path"]), self.zip_bytes)

    def test_the_run_route_finds_what_the_upload_wrote(self):
        import subprocess
        from unittest import mock

        reply = self.upload()
        captured = {}

        class Recorder(app.Handler):
            """Enough of an HTTP handler to get past the guard and no further."""

            def __init__(self):
                self.wfile = io.BytesIO()

            def _send(self, code, body, ctype="application/json", download=None):
                captured["refused"] = body

            def send_response(self, *a, **k):
                captured["streaming"] = True

            def send_header(self, *a, **k):
                pass

            def end_headers(self):
                pass

        with mock.patch.object(subprocess, "Popen",
                               side_effect=RuntimeError("would have spawned")):
            Recorder()._run({"project": "probe", "stage": "import",
                             "source": reply["path"], "approve": True})
        # It began streaming rather than answering with the refusal, which is
        # what says the guard found the uploaded zip. The runner it then tried
        # to start is stubbed; getting that far is the whole assertion.
        self.assertTrue(captured.get("streaming"))
        self.assertNotIn("Nothing uploaded", str(captured.get("refused", "")))

    def test_the_run_route_still_refuses_when_nothing_was_uploaded(self):
        captured = {}

        class Recorder(app.Handler):
            def __init__(self):
                self.wfile = io.BytesIO()

            def _send(self, code, body, ctype="application/json", download=None):
                captured["refused"] = body

            def send_response(self, *a, **k):
                captured["streaming"] = True

        Recorder()._run({"project": "probe", "stage": "import",
                         "source": os.path.join(self.project(), "research",
                                                "imports", "never-uploaded.zip")})
        self.assertIn("Nothing uploaded", str(captured.get("refused")))
        self.assertFalse(captured.get("streaming"))

    def test_the_command_imports_it_out_of_the_store(self):
        import cli

        reply = self.upload()

        class Args:
            source = reply["path"]
            yes = True

        cli.cmd_import({"_dir": self.project(), "name": "probe"}, Args())
        self.assertEqual(
            sorted(k for k in self.supabase.rows if "/evidence/" in k),
            ["projects/probe/research/evidence/01_desk_workers.txt",
             "projects/probe/research/evidence/02_new_parents.txt",
             "projects/probe/research/evidence/_provenance.json"])
        self.assertEqual(self.on_disk(), [])


class ProductAndRenderTests(StoreBackedProject):
    def setUp(self):
        super().setUp()
        app.create_project("probe", "lumbar cushions", "desk workers")

    def test_a_product_and_its_segment_sheet_persist(self):
        products.save("probe", "cushion", products.blank_product())
        segment = products.blank_segment()
        segment["identity"]["name"] = products.cell("Desk Workers", "user_approved")
        products.save_segments("probe", "cushion",
                               [{"slug": "01_desk_workers", "doc": segment}])
        self.redeploy()
        self.assertEqual(products.list_products("probe"), ["cushion"])
        self.assertEqual(len(products.load_segments("probe", "cushion")), 1)

    def test_a_render_goes_to_the_bucket(self):
        key = os.path.join(paths.assets(self.project()), "renders", "ad_01.png")
        store_module.write_bytes(key, b"\x89PNG" + b"0" * 400)
        self.assertIn("projects/probe/assets/renders/ad_01.png", self.supabase.objects)
        self.redeploy()
        self.assertTrue(store_module.read_bytes(key).startswith(b"\x89PNG"))

    def test_a_corpus_too_large_for_a_row_still_reads_back(self):
        # 16MB of VOC is what made this necessary. Scaled down here; the
        # threshold is what is being crossed, not the size.
        key = os.path.join(paths.voc(self.project(), "raw"), "dump.txt")
        corpus = "line of voc\n" * 60_000
        store_module.write_text(key, corpus)
        self.assertIn("projects/probe/research/voc/raw/dump.txt", self.supabase.objects)
        self.redeploy()
        self.assertEqual(store_module.read_text(key), corpus)


class SummaryAndDeleteTests(StoreBackedProject):
    def setUp(self):
        super().setUp()
        app.create_project("probe", "lumbar cushions", "desk workers")
        importer.write(self.project(), importer.plan(importer.audience_members(
            export_zip(("Desk Workers", 5)))), "export.zip")
        store_module.write_text(
            os.path.join(paths.extractions(self.project(), "01_desk_workers"),
                         "07_pain.md"), "# Pain\n")
        store_module.write_bytes(
            os.path.join(paths.assets(self.project()), "renders", "a.png"), b"\x89PNG")

    def test_the_summary_counts_what_is_in_the_store(self):
        # This fills the row in Settings and the delete confirmation. Counted
        # with os.walk it reported nought of everything, which reads as an empty
        # project — the worst possible thing for a confirmation to say.
        summary = app.project_summary("probe")
        self.assertEqual(summary["evidence"], 1)
        self.assertEqual(summary["extractions"], 1)
        self.assertEqual(summary["renders"], 1)
        self.assertEqual(summary["segments"], ["01_desk_workers"])
        self.assertGreater(summary["bytes"], 0)

    def test_delete_takes_the_keys_with_it(self):
        # os.rename moved the empty scaffolding and left every row where it was,
        # so the project came back the moment the list refreshed.
        app.delete_project("probe")
        self.assertEqual(app.projects(), [])
        self.assertEqual(
            [k for k in self.supabase.rows if k.startswith("projects/probe/")], [])

    def test_delete_archives_rather_than_erases(self):
        destination = app.delete_project("probe")
        self.assertTrue(destination.startswith(os.path.join("projects", "_deleted")))
        archived = [k for k in self.supabase.rows if "_deleted/probe_" in k]
        self.assertTrue(any(k.endswith("/research/evidence/01_desk_workers.txt")
                            for k in archived), archived)

    def test_a_deleted_project_stays_deleted_across_a_redeploy(self):
        app.delete_project("probe")
        self.redeploy()
        self.assertEqual(app.projects(), [])


if __name__ == "__main__":
    unittest.main()
