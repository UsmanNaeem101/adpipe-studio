"""Will the work put here still be here after the next deploy?

A container's filesystem is discarded on every deploy. Mount a volume and work
persists; forget to, or mount it at the wrong path, and everything looks perfect
until a deploy takes it — after which the app comes back up showing the bundled
example project and nothing else, which is indistinguishable from a healthy first
boot.

That ambiguity is the bug. A day's work vanished and nothing in the app had an
opinion about it; the logs from before and after the loss are identical. So the
app now answers the question out loud at every boot, and these tests hold it to
every state it has to tell apart — including the one the first attempt got wrong,
where a wipe reported cheerfully as "first boot".
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402


class StorageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.marker = os.path.join(self.projects, ".storage.json")
        self.image = os.path.join(self.projects, ".in-the-image")
        self._real_projects = app.projects
        app.projects = lambda: sorted(
            name for name in os.listdir(self.projects)
            if os.path.isdir(os.path.join(self.projects, name))
            and not name.startswith((".", "_")))

    def tearDown(self):
        app.projects = self._real_projects
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self, names, *, in_image):
        shutil.rmtree(self.projects, ignore_errors=True)
        os.makedirs(self.projects, exist_ok=True)
        for name in names:
            os.makedirs(os.path.join(self.projects, name), exist_ok=True)
        if in_image:
            with open(self.image, "w", encoding="utf-8") as fh:
                fh.write("shipped")

    def boot(self):
        return app.storage_report(marker=self.marker, image_marker=self.image)

    def test_no_volume_is_reported_as_ephemeral_immediately(self):
        # The definite answer, available on the first boot without waiting for a
        # deploy to demonstrate the loss.
        self.state(["montisella"], in_image=True)
        line, ok = self.boot()
        self.assertFalse(ok)
        self.assertIn("EPHEMERAL", line)

    def test_a_mounted_volume_is_unknown_until_it_has_survived_once(self):
        # Missing record proves nothing on its own — a first boot and a wipe look
        # the same — so it must never read as reassurance.
        self.state(["alpha"], in_image=False)
        line, _ = self.boot()
        self.assertIn("UNKNOWN", line)
        self.assertNotIn("persistent", line)

    def test_surviving_a_restart_is_reported_as_persistent(self):
        self.state(["alpha", "beta"], in_image=False)
        self.boot()
        line, ok = self.boot()
        self.assertTrue(ok)
        self.assertIn("persistent", line)
        self.assertIn("boot 2", line)

    def test_a_project_disappearing_is_named(self):
        self.state(["alpha", "beta"], in_image=False)
        self.boot()
        self.boot()
        shutil.rmtree(os.path.join(self.projects, "beta"))
        line, ok = self.boot()
        self.assertFalse(ok)
        self.assertIn("LOST", line)
        self.assertIn("beta", line)

    def test_a_wipe_is_never_mistaken_for_a_first_boot(self):
        # The failure the first version of this shipped with: after a wipe the
        # record is gone, so it reported "FIRST BOOT" and ok=True — the exact
        # reassurance that let a day's work disappear unremarked. The image
        # marker reappearing is what makes this answerable.
        self.state(["alpha"], in_image=False)
        self.boot()
        self.boot()
        self.state(["montisella"], in_image=True)
        line, ok = self.boot()
        self.assertFalse(ok)
        self.assertIn("EPHEMERAL", line)

    def test_an_unwritable_directory_says_so_rather_than_crashing(self):
        # Total failure — nothing can be saved — and the app must still start, so
        # that it can say this. The write is made to fail directly: a missing
        # parent is not unwritable (the store creates it), and this runs as root,
        # where chmod does not stop a write either.
        self.state(["alpha"], in_image=False)
        with mock.patch.object(app.store, "write_json",
                               side_effect=OSError("read-only file system")):
            line, ok = self.boot()
        self.assertFalse(ok)
        self.assertIn("NOT WRITABLE", line)
        self.assertIn("read-only file system", line)


class ImageMarkerShipsTests(unittest.TestCase):
    def test_the_marker_is_in_the_repository(self):
        # If this file stops being shipped, "EPHEMERAL" can never be detected and
        # the whole check silently degrades to the ambiguity it replaced.
        self.assertTrue(os.path.exists(os.path.join(ROOT, "projects", ".in-the-image")))


if __name__ == "__main__":
    unittest.main()
