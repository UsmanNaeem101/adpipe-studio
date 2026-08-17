#!/usr/bin/env python3
"""
Where AdPipe keeps things.

Until now the answer was "on this disk", which was right for an app you start by
double-clicking and wrong for one that runs on a server: a container's filesystem
is wiped on every deploy, so a project's research, its briefs and its finished ads
would last exactly until the next push.

This is the seam. Every read and write goes through a Store, keyed by the same
relative paths `paths.py` already produces -- `projects/montisella/research/voc.jsonl`
-- so the vocabulary does not change, only where the bytes end up.

Two backends:

  local     the filesystem, exactly as before. Still the default, so nothing
            changes for a local run and the 561 existing tests stay honest.
  supabase  Postgres for text, a Storage bucket for binary. What the deployed
            service uses.

Standard library only, like the rest of this repo -- Supabase is reachable over
plain HTTPS (PostgREST and the Storage API), so no client library is needed and
none is added.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request

# Text lives in a table; bytes live in a bucket. Both are created by the
# migration that ships with this change.
TABLE = "adpipe_files"
BUCKET = "adpipe-media"

# What counts as binary. Everything else is stored as text, which keeps a JSON
# file readable in the Supabase table editor instead of being an opaque blob.
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mp3", ".zip", ".pdf")


def is_binary_key(key):
    return key.lower().endswith(BINARY_SUFFIXES)


def data_root():
    """
    The directory keys are relative to.

    Read from `paths.ROOT` on every call rather than captured once, because the
    test suite swaps it for a temp directory per test — 561 tests depend on that
    working, and a root frozen at import time would quietly write every one of
    them into the repo.
    """
    try:
        import paths  # local import: paths imports nothing from here

        return os.path.abspath(paths.ROOT)
    except Exception:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalise(key, root=None):
    """
    One spelling per file.

    Keys are built by joining path fragments, so the same file can arrive as
    `a//b`, `./a/b` or `a/b/`. A store that treats those as three files loses
    writes in ways that are very hard to see, so they are collapsed here rather
    than trusted to every caller.

    An absolute path under the data root is accepted and reduced to a key. That
    is what lets a call site keep saying `paths.voc(project, "raw.jsonl")` and
    change only `open(...)` to `store.read_text(...)`, instead of every path
    expression in the pipeline having to be rewritten at the same time.
    """
    text = str(key).replace("\\", "/")
    base = (root if root is not None else data_root()).replace("\\", "/").rstrip("/")
    if base and (text == base or text.startswith(base + "/")):
        text = text[len(base):]

    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


class LocalStore:
    """The filesystem, unchanged. Keys are paths under `root`."""

    kind = "local"

    def __init__(self, root=None):
        # None means "whatever paths.ROOT is now" — the test suite moves it.
        self._root = os.path.abspath(root) if root else None

    @property
    def root(self):
        return self._root or data_root()

    def _path(self, key):
        """
        Where this key lives on disk.

        An absolute path is used as given. Not every caller writes under the
        data root — the audit log can be pointed anywhere, and did that long
        before this store existed — and rewriting those into the root would
        silently move a user's logs. Relative keys are joined to the root and
        may not climb out of it.
        """
        text = str(key)
        if os.path.isabs(text):
            return text

        root = self.root
        path = os.path.join(root, normalise(text, root))
        # normalise() already refuses to climb, but a store is exactly the wrong
        # place to take that on trust.
        if not os.path.abspath(path).startswith(root):
            raise ValueError("key escapes the store root: %r" % key)
        return path

    def exists(self, key):
        return os.path.exists(self._path(key))

    def read_bytes(self, key):
        try:
            with open(self._path(key), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def read_text(self, key):
        raw = self.read_bytes(key)
        return None if raw is None else raw.decode("utf-8")

    def write_bytes(self, key, data):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    def write_text(self, key, text):
        self.write_bytes(key, text.encode("utf-8"))

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def delete_prefix(self, prefix):
        path = self._path(prefix)
        if os.path.isdir(path):
            shutil.rmtree(path)

    def list_keys(self, prefix=""):
        """
        Every file under a prefix, in the same shape the prefix was given in.

        Absolute in, absolute out. A caller that asked with an absolute path is
        holding absolute paths, and handing it keys relative to the data root
        makes the two impossible to line up — the more so because callers can
        ask about a directory outside the root entirely.
        """
        base = self._path(prefix) if prefix else self.root
        if not os.path.isdir(base):
            return []
        absolute = bool(prefix) and os.path.isabs(str(prefix))
        relative_to = base if absolute else self.root

        found = []
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                rest = os.path.relpath(full, relative_to).replace(os.sep, "/")
                found.append(str(prefix).rstrip("/") + "/" + rest if absolute else rest)
        return sorted(found)


class SupabaseStore:
    """
    Postgres for text, Storage for bytes.

    Text goes in a table because most of what this pipeline writes is JSON and
    markdown that someone will want to read, query or diff -- material that is
    useless as an opaque object in a bucket. Images and video go to the bucket,
    where bytes belong.
    """

    kind = "supabase"

    def __init__(self, url, service_key, table=TABLE, bucket=BUCKET):
        self.url = url.rstrip("/")
        self.key = service_key
        self.table = table
        self.bucket = bucket
        # The image underneath.
        #
        # Templates, the reference ads, the skills markdown and brand.json all
        # ship inside the container and are read-only; a project's state lives
        # up here. So a read that misses falls through to the disk, and a write
        # never does — which is what stops "is this file there?" having a
        # different answer depending on which layer somebody was thinking of.
        self.underlay = LocalStore()

    # -- plumbing ---------------------------------------------------------

    def _request(self, method, path, *, body=None, headers=None, binary=False):
        request = urllib.request.Request(self.url + path, method=method)
        request.add_header("apikey", self.key)
        request.add_header("Authorization", "Bearer " + self.key)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if body is not None and not binary:
            request.add_header("Content-Type", "application/json")
            body = json.dumps(body).encode("utf-8")
        try:
            with urllib.request.urlopen(request, body, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def _rest(self, method, query="", body=None, headers=None):
        return self._request(method, "/rest/v1/%s%s" % (self.table, query), body=body, headers=headers)

    # -- reads ------------------------------------------------------------

    def exists(self, key):
        """
        Is there anything here?

        A prefix counts, because callers ask this about directories as often as
        about files — `if os.path.isdir(d): listdir(d)` is the shape all over
        the pipeline, and a store has no directories, only keys with a common
        beginning.
        """
        tidy = normalise(key)
        if is_binary_key(tidy):
            status, _ = self._request("GET", self._object_path(tidy))
            if status == 200:
                return True
        else:
            status, payload = self._rest(
                "GET", "?key=eq.%s&select=key" % urllib.parse.quote(tidy, safe=""))
            if status == 200 and payload.strip() not in (b"[]", b""):
                return True
            if self.list_keys(tidy):
                return True
        return self.underlay.exists(key)

    def read_text(self, key):
        key = normalise(key)
        if is_binary_key(key):
            raw = self.read_bytes(key)
            return None if raw is None else raw.decode("utf-8")
        status, payload = self._rest(
            "GET", "?key=eq.%s&select=content" % urllib.parse.quote(key, safe="")
        )
        if status != 200:
            return self.underlay.read_text(key)
        rows = json.loads(payload or b"[]")
        return rows[0]["content"] if rows else self.underlay.read_text(key)

    def read_bytes(self, key):
        key = normalise(key)
        if not is_binary_key(key):
            text = self.read_text(key)
            return None if text is None else text.encode("utf-8")
        status, payload = self._request("GET", self._object_path(key))
        return payload if status == 200 else self.underlay.read_bytes(key)

    # -- writes -----------------------------------------------------------

    def write_text(self, key, text):
        key = normalise(key)
        if is_binary_key(key):
            return self.write_bytes(key, text.encode("utf-8"))
        status, payload = self._rest(
            "POST",
            "?on_conflict=key",
            body={"key": key, "project": key.split("/")[1] if key.startswith("projects/") else None,
                  "content": text},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status >= 300:
            raise IOError("could not write %s: %s %s" % (key, status, payload[:200]))

    def write_bytes(self, key, data):
        key = normalise(key)
        if not is_binary_key(key):
            return self.write_text(key, data.decode("utf-8"))
        status, payload = self._request(
            "POST",
            self._object_path(key),
            body=data,
            binary=True,
            # Overwrite rather than fail: a re-render of the same ad is a
            # replacement, not a second file.
            headers={"Content-Type": "application/octet-stream", "x-upsert": "true"},
        )
        if status >= 300:
            raise IOError("could not upload %s: %s %s" % (key, status, payload[:200]))

    # -- removal and listing ----------------------------------------------

    def delete(self, key):
        key = normalise(key)
        if is_binary_key(key):
            self._request("DELETE", self._object_path(key))
            return
        self._rest("DELETE", "?key=eq.%s" % urllib.parse.quote(key, safe=""))

    def delete_prefix(self, prefix):
        prefix = normalise(prefix)
        for key in self.list_keys(prefix):
            self.delete(key)

    def list_keys(self, prefix=""):
        prefix = normalise(prefix)
        pattern = urllib.parse.quote((prefix + "/%") if prefix else "%", safe="")
        status, payload = self._rest("GET", "?key=like.%s&select=key&order=key" % pattern)
        keys = [row["key"] for row in json.loads(payload or b"[]")] if status == 200 else []

        # The bucket is listed separately: binary keys are not rows in the table.
        status, payload = self._request(
            "POST",
            "/storage/v1/object/list/%s" % self.bucket,
            body={"prefix": prefix, "limit": 1000},
        )
        if status == 200:
            for entry in json.loads(payload or b"[]"):
                name = entry.get("name")
                if name:
                    keys.append("%s/%s" % (prefix, name) if prefix else name)
        return sorted(set(keys))

    def _object_path(self, key):
        return "/storage/v1/object/%s/%s" % (self.bucket, urllib.parse.quote(key))


def build_store(environ=None):
    """
    The store this process should use.

    Supabase when it is configured, the filesystem otherwise — so a local run
    and the whole test suite behave exactly as they did, and the deployed
    service keeps nothing on a disk that is about to be thrown away.
    """
    environ = os.environ if environ is None else environ
    url = (environ.get("SUPABASE_URL") or "").strip()
    key = (environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if url and key:
        return SupabaseStore(url, key)

    # No root of its own unless one is named: LocalStore then follows
    # `paths.ROOT`, which is what the test suite moves per test and what the
    # rest of the pipeline already treats as "where things are". Pinning a root
    # here would quietly resolve every relative key against the repo instead.
    root = (environ.get("ADPIPE_DATA_ROOT") or "").strip()
    return LocalStore(root or None)


_store = None


def store():
    """The process-wide store, built once."""
    global _store
    if _store is None:
        _store = build_store()
    return _store


def use(replacement):
    """Swap the store — for tests, and for a CLI told to work somewhere else."""
    global _store
    _store = replacement
    return _store


# ── What call sites use ──────────────────────────────────────────────────
#
# `store.read_text(paths.voc(project, "raw.jsonl"))` — the path expression is
# unchanged, only the opening of it.


def read_text(key):
    return store().read_text(key)


def read_bytes(key):
    return store().read_bytes(key)


def write_text(key, text):
    return store().write_text(key, text)


def write_bytes(key, data):
    return store().write_bytes(key, data)


def exists(key):
    return store().exists(key)


def delete(key):
    return store().delete(key)


def delete_prefix(prefix):
    return store().delete_prefix(prefix)


def list_keys(prefix=""):
    return store().list_keys(prefix)


def read_json(key, default=None):
    """Read and parse, tolerating both absence and rubbish.

    Every caller of this in the pipeline wants the same thing: the object if it
    is there and parses, the default otherwise. A half-written file from an
    interrupted run should not take down the next stage.
    """
    raw = read_text(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def write_json(key, value, indent=2):
    write_text(key, json.dumps(value, indent=indent, ensure_ascii=False) + "\n")


def names_in(prefix):
    """
    The immediate children of a prefix, like os.listdir once did.

    Deliberately matched against the prefix as given rather than a normalised
    one: list_keys answers in the shape it was asked in, and normalising only
    one side is how a listing comes back empty for a directory that is plainly
    full.
    """
    given = str(prefix).replace("\\", "/").rstrip("/")
    tidy = normalise(given)
    names = set()
    for key in list_keys(prefix):
        text = str(key).replace("\\", "/")
        rest = None
        for head in (given, tidy):
            if head and text.startswith(head + "/"):
                rest = text[len(head) + 1:]
                break
        if rest is None:
            rest = text
        if rest:
            names.add(rest.split("/")[0])
    return sorted(names)


@contextlib.contextmanager
def open_key(key, mode="r", encoding="utf-8", errors=None, newline=None):
    """
    `open()` against the store.

    A file object rather than a set of read/write helpers, because that is what
    lets a call site change one token and keep its body: `json.load(fh)`,
    iterating lines, several `fh.write(...)` calls in a row all still work. The
    alternative — rewriting two hundred bodies by hand — is where this migration
    would have introduced its bugs.

    Writes buffer and land once, on a clean exit. An exception inside the block
    leaves the previous value alone, which is what the temp-file-and-rename
    dance used to buy and is now simply how it works. `encoding`, `errors` and
    `newline` are accepted and ignored: the store speaks UTF-8 and bytes.
    """
    binary = "b" in mode
    writing = any(flag in mode for flag in ("w", "a", "x", "+"))

    if not writing:
        data = read_bytes(key) if binary else read_text(key)
        if data is None:
            raise FileNotFoundError(key)
        yield io.BytesIO(data) if binary else io.StringIO(data)
        return

    buffer = io.BytesIO() if binary else io.StringIO()
    if "a" in mode:
        existing = read_bytes(key) if binary else read_text(key)
        if existing:
            buffer.write(existing)
    yield buffer
    value = buffer.getvalue()
    write_bytes(key, value) if binary else write_text(key, value)


def dirs_in(prefix):
    """
    The children of a prefix that have something under them.

    `os.listdir(d)` filtered by `os.path.isdir` was how this pipeline found its
    projects, its segments and its stages. A store has no directories, so the
    equivalent question is which names are the start of a longer key — and it
    has to be asked deliberately, because "does this exist" answers yes for a
    stray file and would list it as a project.
    """
    given = str(prefix).replace("\\", "/").rstrip("/")
    tidy = normalise(given)
    names = set()
    for key in list_keys(prefix):
        text = str(key).replace("\\", "/")
        rest = None
        for head in (given, tidy):
            if head and text.startswith(head + "/"):
                rest = text[len(head) + 1:]
                break
        if rest is None:
            rest = text
        # A name only counts if the key continues past it.
        if "/" in rest:
            names.add(rest.split("/")[0])
    return sorted(names)
