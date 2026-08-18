"""Taking a copy, and then taking the space back.

The corpora stages 01--06 build are the largest thing in a project and, once
stage 06 has written its evidence files, the least read. Removing them is worth
doing and is also the one destructive operation here that cannot be undone from
inside the app — the source is a scrape that would have to be paid for again.

So two properties matter more than the rest, and both are about refusing:

  * `remove` will not run before stage 06 has produced output, whatever it is
    asked. Its guard is the difference between reclaiming scaffolding and
    deleting the only copy of the research.
  * it takes the corpora and nothing else. The segmentation state sitting beside
    them — candidates, validated, assignments — is what `--reassign` and
    `--rediscover` read, and a project without it can only be re-segmented from
    scratch.

`bundle` is the counterweight: whole project, everything in it, so the copy
exists before the question of removal comes up at all.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import archive  # noqa: E402
import paths  # noqa: E402
import store  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class ProjectFixture(unittest.TestCase):
    """A project part-way through: corpora written, stage 06 not yet run."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.original_root = paths.ROOT
        paths.ROOT = self.root
        store.use(store.LocalStore(self.root))
        self.dir = os.path.join(self.root, "projects", "demo")

        write(os.path.join(self.dir, "project.json"), '{"name":"demo"}')
        voc = os.path.join(self.dir, "research", "voc")
        for name in ("filtered_voc.jsonl", "deduplicated_voc.jsonl",
                     "production_voc.jsonl"):
            write(os.path.join(voc, name), '{"text":"%s"}\n' % name * 200)
        write(os.path.join(voc, "raw", "dump.txt"), "x" * 5000)
        write(os.path.join(self.dir, "research", "imports", "export.md"), "# Import\n")

        # Segmentation state — beside the corpora, and not spent with them.
        for name in ("candidate_segments.json", "validated_segments.json",
                     "segment_assignments.jsonl"):
            write(os.path.join(voc, name), "{}")

    def tearDown(self):
        paths.ROOT = self.original_root
        store.use(None)

    def finish_stage_06(self):
        write(paths.evidence(self.dir, "01_desk_workers.txt"), "verbatim one\n")
        write(paths.evidence(self.dir, "02_drivers.txt"), "verbatim two\n")

    def relative(self, keys):
        return sorted(os.path.relpath(k, self.dir).replace(os.sep, "/") for k in keys)


class GuardTests(ProjectFixture):
    def test_it_refuses_before_stage_06_has_written_anything(self):
        # The corpora are the only copy of the research at this point. Removing
        # them here is not reclaiming scaffolding, it is deleting the project.
        self.assertEqual(archive.stage06_output(self.dir), [])
        with self.assertRaises(ValueError) as caught:
            archive.remove(self.dir)
        self.assertIn("stage 06", str(caught.exception))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, "research", "voc", "deduplicated_voc.jsonl")))

    def test_an_empty_evidence_directory_is_not_stage_06_having_run(self):
        # scaffold() creates research/evidence/ for every new project, so its
        # existence says nothing. Only files in it do.
        os.makedirs(paths.evidence(self.dir), exist_ok=True)
        self.assertEqual(archive.stage06_output(self.dir), [])
        with self.assertRaises(ValueError):
            archive.remove(self.dir)

    def test_evidence_files_open_the_gate(self):
        self.finish_stage_06()
        self.assertEqual(archive.stage06_output(self.dir),
                         ["01_desk_workers.txt", "02_drivers.txt"])
        removed, freed = archive.remove(self.dir)
        self.assertTrue(removed)
        self.assertGreater(freed, 0)


class SelectionTests(ProjectFixture):
    def test_it_names_the_corpora_and_the_input_trees(self):
        found = self.relative(key for key, _ in archive.spent_files(self.dir))
        self.assertEqual(found, [
            "research/imports/export.md",
            "research/voc/deduplicated_voc.jsonl",
            "research/voc/filtered_voc.jsonl",
            "research/voc/production_voc.jsonl",
            "research/voc/raw/dump.txt",
        ])

    def test_segmentation_state_is_never_spent(self):
        # These are what --reassign, --rediscover and --from read. Losing them
        # turns "adjust the segmentation" into "ingest it all again", which is
        # the cost this whole operation exists to avoid paying twice.
        self.finish_stage_06()
        archive.remove(self.dir)
        voc = os.path.join(self.dir, "research", "voc")
        for name in ("candidate_segments.json", "validated_segments.json",
                     "segment_assignments.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(voc, name)), name)

    def test_the_evidence_it_waited_for_survives_it(self):
        self.finish_stage_06()
        archive.remove(self.dir)
        self.assertEqual(archive.stage06_output(self.dir),
                         ["01_desk_workers.txt", "02_drivers.txt"])

    def test_a_project_config_is_not_a_corpus(self):
        self.finish_stage_06()
        archive.remove(self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "project.json")))

    def test_freed_matches_what_was_actually_there(self):
        expected = sum(size for _, size in archive.spent_files(self.dir))
        self.finish_stage_06()
        _, freed = archive.remove(self.dir)
        self.assertEqual(freed, expected)

    def test_running_it_twice_finds_nothing_the_second_time(self):
        self.finish_stage_06()
        archive.remove(self.dir)
        self.assertEqual(archive.spent_files(self.dir), [])
        removed, freed = archive.remove(self.dir)
        self.assertEqual((removed, freed), ([], 0))


class BundleTests(ProjectFixture):
    def test_it_holds_the_whole_project_not_only_what_is_spent(self):
        # A backup taken immediately before deleting is the wrong moment to be
        # selective about what it contains.
        self.finish_stage_06()
        name, blob, count = archive.bundle(self.dir, "demo")
        self.assertEqual(name, "demo.zip")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = sorted(zf.namelist())
        self.assertEqual(count, len(names))
        self.assertIn("demo/project.json", names)
        self.assertIn("demo/research/voc/deduplicated_voc.jsonl", names)
        self.assertIn("demo/research/voc/candidate_segments.json", names)
        self.assertIn("demo/research/evidence/01_desk_workers.txt", names)

    def test_contents_survive_the_round_trip(self):
        self.finish_stage_06()
        _, blob, _ = archive.bundle(self.dir, "demo")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertEqual(
                zf.read("demo/research/evidence/01_desk_workers.txt").decode(),
                "verbatim one\n")

    def test_every_path_sits_under_the_project_name(self):
        # Unzipping in a downloads folder should produce one directory, not
        # scatter research/ and products/ across whatever is already there.
        _, blob, _ = archive.bundle(self.dir, "demo")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                self.assertTrue(name.startswith("demo/"), name)
                self.assertNotIn("..", name)

    def test_it_can_be_taken_before_stage_06_has_run(self):
        # The guard belongs to removal. Copying is safe at any point, and the
        # moment someone most wants a copy is before they are allowed to delete.
        _, blob, count = archive.bundle(self.dir, "demo")
        self.assertGreater(count, 0)
        self.assertTrue(blob.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
