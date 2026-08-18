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
import datetime
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

# The largest text this will put in a table row.
#
# Postgres would hold far more, but the row goes through PostgREST as one JSON
# request body, and a hosted API gateway refuses a large one long before
# Postgres would care. The pipeline routinely writes past this: a raw VOC dump
# is 16MB, its deduplicated twin nearly as big. Those went to the table because
# routing was decided by file extension alone, and .jsonl is not a binary
# suffix — so the one path never exercised beyond a one-line fixture was the one
# the biggest files in the system took.
#
# It is also a quota question. The database is metered in hundreds of megabytes
# and the object store in gigabytes; a corpus belongs in the second.
TEXT_MAX_BYTES = int(os.environ.get("ADPIPE_TEXT_MAX_BYTES") or 512 * 1024)

# What stands in the table when the text itself went to the bucket. The row is
# kept so `exists` and `list_keys` still answer from one place.
OVERFLOW_PREFIX = "adpipe:overflow:v1:"


def is_binary_key(key):
    return key.lower().endswith(BINARY_SUFFIXES)


# What the image never ships, and so must never be answered from it.
#
# The underlay exists for read-only material that arrives inside the container:
# the skills, the ad templates, the reference ads. A project is not that. It is
# written by whoever is using the app, and on a deployment the only durable
# place for it is the store.
#
# Merging the two under here made a project held on the container's own disk —
# an import that ran before Supabase was configured, or any write that still
# bypasses the store — indistinguishable from one safely in Postgres. The app
# listed its segments, the person read that as "it worked", and the next deploy
# took it. Answering "not there" is the honest answer and it arrives before the
# money is spent rather than after.
PROJECT_PREFIX = "projects/"


def is_project_state(key):
    # The bare prefix counts. "projects" is how the listing that builds the
    # project list is asked, and matching only "projects/..." let that one
    # listing — the one that answers "is my work here?" — still merge the disk.
    tidy = normalise(key)
    return tidy == PROJECT_PREFIX.rstrip("/") or tidy.startswith(PROJECT_PREFIX)


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

    def stat(self, key):
        try:
            found = os.stat(self._path(key))
        except OSError:
            return None
        return {"size": found.st_size, "mtime": found.st_mtime}


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
        # Keys this process has sent to the bucket as overflow text. See
        # write_text: it is what lets the common small write stay one request.
        self._overflowed = set()

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
        given, tidy = key, normalise(key)
        if is_binary_key(tidy):
            status, _ = self._request("GET", self._object_path(tidy))
            if status == 200:
                return True
        else:
            status, payload = self._rest(
                "GET", "?key=eq.%s&select=key" % urllib.parse.quote(tidy, safe=""))
            if status == 200 and payload.strip() not in (b"[]", b""):
                return True
            if self.list_keys(given):
                return True
        return False if is_project_state(given) else self.underlay.exists(given)

    def read_text(self, key):
        # The key as asked is kept for the underlay. Normalising strips the data
        # root, and the image is not always underneath it — hand a relative key
        # to a LocalStore rooted elsewhere and it resolves the path a second
        # time, against the wrong base. The skills read as missing, and skill()
        # exits rather than returning None.
        given, key = key, normalise(key)
        if is_binary_key(key):
            raw = self.read_bytes(given)
            return None if raw is None else raw.decode("utf-8")
        status, payload = self._rest(
            "GET", "?key=eq.%s&select=content" % urllib.parse.quote(key, safe="")
        )
        if status != 200:
            return self._beneath(given, "read_text")
        rows = json.loads(payload or b"[]")
        if not rows:
            return self._beneath(given, "read_text")
        content = rows[0]["content"]
        if isinstance(content, str) and content.startswith(OVERFLOW_PREFIX):
            status, payload = self._request("GET", self._object_path(key))
            if status == 200:
                return payload.decode("utf-8")
            # The row says the bytes exist and the bucket disagrees. Say so
            # rather than returning the marker as if it were the file.
            raise IOError("%s is recorded as stored but its object is missing" % key)
        return content

    def read_bytes(self, key):
        given, key = key, normalise(key)
        if not is_binary_key(key):
            text = self.read_text(given)
            return None if text is None else text.encode("utf-8")
        status, payload = self._request("GET", self._object_path(key))
        return payload if status == 200 else self._beneath(given, "read_bytes")

    # -- writes -----------------------------------------------------------

    def _put_row(self, key, content):
        status, payload = self._rest(
            "POST",
            "?on_conflict=key",
            body={"key": key, "project": key.split("/")[1] if key.startswith("projects/") else None,
                  "content": content},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status >= 300:
            raise IOError("could not write %s: %s %s" % (key, status, payload[:200]))

    def _put_object(self, key, data):
        status, payload = self._request(
            "POST",
            self._object_path(key),
            body=data,
            binary=True,
            headers={"Content-Type": "application/octet-stream", "x-upsert": "true"},
        )
        if status >= 300:
            raise IOError("could not upload %s: %s %s" % (key, status, payload[:200]))

    def write_text(self, key, text):
        key = normalise(key)
        if is_binary_key(key):
            return self.write_bytes(key, text.encode("utf-8"))

        raw = text.encode("utf-8")
        # The prefix test is not paranoia about size: it removes the only way a
        # real file could be mistaken for a marker, by sending any text that
        # begins like one to the bucket as well.
        if len(raw) > TEXT_MAX_BYTES or text.startswith(OVERFLOW_PREFIX):
            self._put_object(key, raw)
            self._put_row(key, OVERFLOW_PREFIX + json.dumps({"bytes": len(raw)}))
            self._overflowed.add(key)
            return

        self._put_row(key, text)
        # A file that shrank back under the limit leaves its old, larger self in
        # the bucket. No read will ever return it — the row decides, and the row
        # now holds the text — so this is space, not correctness, and it is
        # reclaimed either here or by delete(), which takes both halves.
        #
        # Conditional because urllib opens a fresh connection per call and the
        # small write is the hot path: the audit log writes one file per model
        # request, and paying a second round trip on each of those to tidy up
        # after a case that only arises when a corpus shrinks is the wrong trade.
        if key in self._overflowed:
            self._request("DELETE", self._object_path(key))
            self._overflowed.discard(key)

    def write_bytes(self, key, data):
        key = normalise(key)
        if not is_binary_key(key):
            return self.write_text(key, data.decode("utf-8"))
        # Overwrite rather than fail: a re-render of the same ad is a
        # replacement, not a second file.
        self._put_object(key, data)

    # -- removal and listing ----------------------------------------------

    def delete(self, key):
        key = normalise(key)
        if is_binary_key(key):
            self._request("DELETE", self._object_path(key))
            return
        self._rest("DELETE", "?key=eq.%s" % urllib.parse.quote(key, safe=""))
        # Unconditional, because the row that would have said whether this key
        # overflowed is the one just deleted.
        self._request("DELETE", self._object_path(key))
        self._overflowed.discard(key)

    def delete_prefix(self, prefix):
        prefix = normalise(prefix)
        for key in self.list_keys(prefix):
            self.delete(key)

    def list_keys(self, prefix=""):
        """
        Every key under a prefix, in the same shape the prefix was given in, and
        including what is only in the image.

        Both halves of that were wrong, and the second was fatal. `exists` and
        every reader fall through to the underlay, but this did not — so on a
        store-backed deployment the skills, the ad templates and the reference
        ads all listed as empty while existing. `skill()` exits when it cannot
        find its markdown, which meant extract, picc, concepts and brief refused
        to start on the one configuration they were added for.

        Absolute in, absolute out, for the same reason LocalStore does it:
        callers hold the absolute paths `paths.py` hands them, and some ask
        about directories outside the data root entirely.
        """
        given = str(prefix).replace("\\", "/").rstrip("/")
        tidy = normalise(given)
        absolute = bool(given) and os.path.isabs(given)
        pattern = urllib.parse.quote((tidy + "/%") if tidy else "%", safe="")
        status, payload = self._rest("GET", "?key=like.%s&select=key&order=key" % pattern)
        stored = [row["key"] for row in json.loads(payload or b"[]")] if status == 200 else []

        # The bucket is listed separately: binary keys are not rows in the table.
        status, payload = self._request(
            "POST",
            "/storage/v1/object/list/%s" % self.bucket,
            body={"prefix": tidy, "limit": 1000},
        )
        if status == 200:
            for entry in json.loads(payload or b"[]"):
                name = entry.get("name")
                if name:
                    stored.append("%s/%s" % (tidy, name) if tidy else name)

        keys = set()
        for key in stored:
            if absolute:
                rest = key[len(tidy) + 1:] if tidy and key.startswith(tidy + "/") else key
                keys.add(given + "/" + rest)
            else:
                keys.add(key)
        if not is_project_state(given):
            keys.update(self.underlay.list_keys(prefix))
        return sorted(keys)

    def stat(self, key):
        """How big, and when — without downloading a corpus to find out.

        The Outputs tab lists a dozen artefacts and shows a size and a date for
        each. os.stat answered that on a laptop and raised FileNotFoundError on
        a project held in Postgres, which took the whole tab down with it.

        The row carries `updated_at`, so the date is free. Size is the awkward
        half — PostgREST cannot compute octet_length in a select — but the files
        where that would matter are exactly the ones written to the bucket, and
        their marker records the byte count. So a corpus costs a marker, and
        anything read in full is under the overflow limit by definition.
        """
        given, tidy = key, normalise(key)
        if is_binary_key(tidy):
            status, payload = self._request("GET", self._object_path(tidy))
            if status == 200:
                return {"size": len(payload), "mtime": 0}
            return self._beneath(given, "stat")

        status, payload = self._rest(
            "GET",
            "?key=eq.%s&select=content,updated_at" % urllib.parse.quote(tidy, safe=""))
        rows = json.loads(payload or b"[]") if status == 200 else []
        if not rows:
            return self._beneath(given, "stat")

        content = rows[0].get("content") or ""
        if isinstance(content, str) and content.startswith(OVERFLOW_PREFIX):
            try:
                size = int(json.loads(content[len(OVERFLOW_PREFIX):])["bytes"])
            except (ValueError, KeyError, TypeError):
                size = 0
        else:
            size = len(content.encode("utf-8"))
        return {"size": size, "mtime": _epoch(rows[0].get("updated_at"))}

    def health(self):
        """One request, and it distinguishes the ways this is usually wrong.

        A missing table, a rejected key and an unreachable host all look the
        same from inside the app — an empty project list — and all three are
        settings somebody can fix in a minute once told which.
        """
        host = self.url.split("//")[-1].split(".")[0]
        try:
            status, payload = self._rest("GET", "?select=key&limit=1")
        except Exception as error:
            return {"kind": "supabase", "ok": False, "where": host,
                    "detail": "cannot reach Supabase: %s" % error}
        if status == 200:
            return {"kind": "supabase", "ok": True, "where": host,
                    "detail": "projects are held in Supabase; no volume needed"}
        if status in (401, 403):
            return {"kind": "supabase", "ok": False, "where": host,
                    "detail": "Supabase rejected the key — SUPABASE_SERVICE_ROLE_KEY "
                              "must be the service_role key, not the anon key"}
        if status in (404, 400):
            return {"kind": "supabase", "ok": False, "where": host,
                    "detail": "the %s table is not there — run the migration in "
                              "supabase/migrations/" % self.table}
        return {"kind": "supabase", "ok": False, "where": host,
                "detail": "Supabase answered %s: %s" % (status, payload[:120].decode(
                    "utf-8", "replace"))}

    def _beneath(self, key, method):
        """The image, unless the key is a project's own state. See is_project_state."""
        if is_project_state(key):
            return None
        return getattr(self.underlay, method)(key)

    def _object_path(self, key):
        return "/storage/v1/object/%s/%s" % (self.bucket, urllib.parse.quote(key))


def health(store_object=None):
    """Is this store actually usable, and can it say why not?

    Read-only on purpose. The startup banner answers the same question by
    writing a boot record, which is right once per process and wrong for
    anything a page can ask — and "where is my work going?" is exactly the
    question somebody wants to ask without redeploying to read a log.
    """
    target = store_object or store()
    if target.kind == "local":
        root = target.root
        return {"kind": "local", "ok": os.access(root, os.W_OK),
                "where": root,
                "detail": "the filesystem — a container discards this on deploy"}
    return target.health()


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


def _epoch(stamp):
    """A Postgres timestamptz as seconds, or 0.

    Listings sort on this, so an unreadable date has to sort as oldest rather
    than stop the sort.
    """
    if not stamp:
        return 0
    text = str(stamp).replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0


def stat(key):
    """Size and modification time, or None when there is no such key."""
    return store().stat(key)


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
