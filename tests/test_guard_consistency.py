"""A guard must be asked of whatever answers the next line.

This exists because of a crash on the first deployment that ever got far enough
to run. Moving the pipeline onto the store meant replacing `os.path.isdir(d)`
with `store.exists(d)` in ninety-odd places, and the store answers a broader
question: `isdir` says no for a file, `exists` says yes. So this:

    for cat in os.listdir(REFS):
        d = os.path.join(REFS, cat)
        if store.exists(d):
            for f in os.listdir(d):

stopped skipping `references/README.md`, called `os.listdir` on a file, and the
app died at startup with NotADirectoryError before serving a single request.

The rule is simple enough to check: if the lines after a guard use the
filesystem directly, the guard has to be the filesystem too. Mixing them is not
a style question — it is a promise the two APIs do not both keep.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(ROOT, "pipeline")

# How far past the guard to look. Long enough to catch the real shape, short
# enough not to blame an unrelated listing further down the function.
WINDOW = 6


def offences():
    found = []
    for name in sorted(os.listdir(PIPELINE)):
        if not name.endswith(".py"):
            continue
        lines = open(os.path.join(PIPELINE, name), encoding="utf-8").read().split("\n")
        for index, line in enumerate(lines):
            match = re.search(r"store\.exists\(([A-Za-z_][\w\.\[\]\"']*)\)", line)
            if not match:
                continue
            target = match.group(1)
            window = "\n".join(lines[index:index + WINDOW])
            if re.search(r"os\.(listdir|walk)\(%s\)" % re.escape(target), window):
                found.append("%s:%d — store.exists(%s) then os.listdir/os.walk(%s)"
                             % (name, index + 1, target, target))
    return found


class GuardConsistencyTests(unittest.TestCase):
    def test_a_store_guard_is_never_followed_by_a_filesystem_listing(self):
        problems = offences()
        self.assertEqual(
            problems, [],
            "A store.exists() guard passes for a file; os.listdir() on a file "
            "raises NotADirectoryError. Use os.path.isdir() when the next line "
            "reads the filesystem:\n  " + "\n  ".join(problems))

    def test_the_check_can_actually_see_the_shape_it_forbids(self):
        # A guard that cannot fail is worse than no guard, so prove the matcher
        # against the exact code that crashed in production.
        import tempfile

        crashed = (
            "for cat in sorted(os.listdir(REFS)):\n"
            "    d = os.path.join(REFS, cat)\n"
            "    if store.exists(d):\n"
            "        files = sorted(f for f in os.listdir(d))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(crashed)
            lines = crashed.split("\n")
            hit = False
            for index, line in enumerate(lines):
                match = re.search(r"store\.exists\(([A-Za-z_][\w\.\[\]\"']*)\)", line)
                if match and re.search(
                        r"os\.(listdir|walk)\(%s\)" % re.escape(match.group(1)),
                        "\n".join(lines[index:index + WINDOW])):
                    hit = True
            self.assertTrue(hit, "the matcher no longer recognises the crash")


if __name__ == "__main__":
    unittest.main()
