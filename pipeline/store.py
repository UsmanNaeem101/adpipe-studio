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


def normalise(key):
    """
    One spelling per file.

    Keys are built by joining path fragments, so the same file can arrive as
    `a//b`, `./a/b` or `a/b/`. A store that treats those as three files loses
    writes in ways that are very hard to see, so they are collapsed here rather
    than trusted to every caller.
    """
    parts = []
    for part in str(key).replace("\\", "/").split("/"):
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

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _path(self, key):
        path = os.path.join(self.root, normalise(key))
        # normalise() already refuses to climb, but a store is exactly the wrong
        # place to take that on trust.
        if not os.path.abspath(path).startswith(self.root):
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
        base = self._path(prefix) if prefix else self.root
        if not os.path.isdir(base):
            return []
        found = []
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, self.root).replace(os.sep, "/"))
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
        key = normalise(key)
        if is_binary_key(key):
            status, _ = self._request("GET", self._object_path(key))
            return status == 200
        status, payload = self._rest("GET", "?key=eq.%s&select=key" % urllib.parse.quote(key, safe=""))
        return status == 200 and payload.strip() not in (b"[]", b"")

    def read_text(self, key):
        key = normalise(key)
        if is_binary_key(key):
            raw = self.read_bytes(key)
            return None if raw is None else raw.decode("utf-8")
        status, payload = self._rest(
            "GET", "?key=eq.%s&select=content" % urllib.parse.quote(key, safe="")
        )
        if status != 200:
            return None
        rows = json.loads(payload or b"[]")
        return rows[0]["content"] if rows else None

    def read_bytes(self, key):
        key = normalise(key)
        if not is_binary_key(key):
            text = self.read_text(key)
            return None if text is None else text.encode("utf-8")
        status, payload = self._request("GET", self._object_path(key))
        return payload if status == 200 else None

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

    root = (environ.get("ADPIPE_DATA_ROOT") or "").strip()
    if not root:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return LocalStore(root)


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
