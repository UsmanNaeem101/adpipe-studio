"""Nothing writes a project's state with open().

The behavioural cover lives in test_store_backed_project.py, which runs the real
functions against a Supabase made of dictionaries and asserts that the disk stays
empty. This is the cheaper half: it names the shape, so a call site added next
month is caught where it is written rather than where it is missed.

The shape is specific. A path built from `paths.something(...)`, from ROOT, or
from the string "projects" is project state — a corpus, an extraction, a product
sheet, a project's own configuration — and every one of those has to go through
the store or it lands on a container filesystem the next deploy discards. A path
built from a scratch directory, from an argument someone typed, or from the
credential store is not, and those keep using open() because that is what they
are.

Following it needs the assignment, not just the call. Almost none of these
functions open a path expression; they build `dest` three lines earlier and open
`dest`, so a scan of open() lines alone sees nothing — which is exactly what the
first version of this file did, passing happily while create_project put a
project on a disk. So the taint is tracked through assignment, per function, with
module constants inherited.

Both directions matter. A write that misses the store loses work on the next
deploy; a read that misses it cannot find work written correctly, which is how
`read_extractions` came to list twenty extractions through the store and then
read none of them.
"""

import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE = os.path.join(ROOT, "pipeline")

# The store is the thing being tested, and the credential store is deliberately
# a 0600 file on disk — a secret does not belong in a shared table or bucket.
SKIP_MODULES = {"store.py", "credentials.py"}

# Functions whose filesystem use is the point rather than an oversight.
ALLOWED = {
    ("app.py", "_dims_cache"),       # a model-dimensions cache under .cache/
    ("app.py", "_dims_save"),        # rebuilt on demand, never anyone's work
    ("app.py", "convert_reference"), # drives macOS `sips`, which needs real paths
    ("app.py", "thumbnail"),         # writes a derived thumbnail beside the cache
    ("cli.py", "cmd_archive"),       # writes the zip to the machine you asked from
    ("cli.py", "cmd_import"),        # reads a zip or folder someone named
    ("render.py", "main"),           # the page Chromium screenshots must be a file
    ("render.py", "resolve_asset"),  # ditto: a plate written out for Chrome to read
}

TAINTED_NAMES = {"ROOT"}
TAINTED_STRINGS = {"projects"}


def _taints(node):
    """Does this expression name project state?"""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            if child.value.id == "paths":
                return True
        if isinstance(child, ast.Name) and child.id in TAINTED_NAMES:
            return True
        if isinstance(child, ast.Constant) and child.value in TAINTED_STRINGS:
            return True
    return False


def _tainted_in(node, names):
    if _taints(node):
        return True
    return any(isinstance(child, ast.Name) and child.id in names
               for child in ast.walk(node))


def offences():
    found = []
    for filename in sorted(os.listdir(PIPELINE)):
        if not filename.endswith(".py") or filename in SKIP_MODULES:
            continue
        with open(os.path.join(PIPELINE, filename), encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename)

        # Module constants first: SKILLS, REFS and friends are built from ROOT
        # and then used inside functions that never mention it.
        module_names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and _taints(node.value):
                module_names.update(t.id for t in node.targets
                                    if isinstance(t, ast.Name))

        for function in [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if (filename, function.name) in ALLOWED:
                continue
            names = set(module_names)
            for node in ast.walk(function):
                if isinstance(node, ast.Assign) and _tainted_in(node.value, names):
                    names.update(t.id for t in node.targets
                                 if isinstance(t, ast.Name))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "open" and node.args
                        and _tainted_in(node.args[0], names)):
                    found.append("%s:%d — in %s(), open(...) on project state"
                                 % (filename, node.lineno, function.name))
    return sorted(set(found))


class StoreBypassTests(unittest.TestCase):
    def test_no_module_opens_project_state_directly(self):
        problems = offences()
        self.assertEqual(
            problems, [],
            "These build a path out of paths.*/ROOT/'projects' and then open() "
            "it. On a Supabase-backed deployment that writes to a container disk "
            "the next deploy discards, or reads from one that never held the "
            "file. Use store.read_text/write_text/read_json/write_json:\n  "
            + "\n  ".join(problems))

    def test_it_follows_the_path_through_an_assignment(self):
        """The shape that actually occurs, and that a line-by-line scan misses.

        This is create_project as it was written — the ROOT is three lines above
        the open(), so nothing on the open() line says what is being written.
        """
        module = ast.parse(
            "def create_project(name):\n"
            "    dest = os.path.join(ROOT, 'projects', name)\n"
            "    json.dump(cfg, open(os.path.join(dest, 'facts.json'), 'w'))\n")
        function = module.body[0]
        names = set()
        hits = 0
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and _tainted_in(node.value, names):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open" and node.args
                    and _tainted_in(node.args[0], names)):
                hits += 1
        self.assertEqual(hits, 1)

    def test_it_leaves_a_scratch_file_alone(self):
        # render.py writes the page it is about to screenshot to a temp dir and
        # hands Chromium a real path, because Chromium cannot read a table.
        module = ast.parse(
            "def shoot(scratch_dir):\n"
            "    tmp = os.path.join(scratch_dir, 'page.html')\n"
            "    open(tmp, 'w').write(html)\n")
        function = module.body[0]
        names = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and _tainted_in(node.value, names):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open" and node.args):
                self.assertFalse(_tainted_in(node.args[0], names))

    def test_the_allowlist_names_functions_that_exist(self):
        # An allowlist entry for a renamed function silently stops covering it.
        for filename, function in ALLOWED:
            with open(os.path.join(PIPELINE, filename), encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename)
            names = {n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertIn(function, names, "%s has no %s()" % (filename, function))


if __name__ == "__main__":
    unittest.main()
