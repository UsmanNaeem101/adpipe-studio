"""Durable, per-request model audit logs.

Every paid generation writes its request before network I/O, then its complete
provider response (or error/retry events) into the same timestamped directory.
API credentials are never passed to this module and therefore cannot be logged.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import hashlib
import json
import os
import re
import threading
import time
import uuid


import store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_context = contextvars.ContextVar("adpipe_audit_context", default={})


def set_context(**values):
    """Set persistent context for the current process/thread."""
    _context.set({**_context.get(), **{k: v for k, v in values.items() if v}})


@contextlib.contextmanager
def scope(**values):
    token = _context.set({**_context.get(), **{k: v for k, v in values.items() if v}})
    try:
        yield
    finally:
        _context.reset(token)


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump())
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return repr(value)


def _stamp():
    return datetime.datetime.now().astimezone()


def _safe(value):
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "request")).strip("-")
    return text[:60] or "request"


def _log_root(context):
    override = os.environ.get("ADPIPE_LOG_DIR")
    if override:
        return os.path.abspath(override)
    project = context.get("project") or os.environ.get("ADPIPE_PROJECT")
    if project and re.fullmatch(r"[a-zA-Z0-9_-]+", str(project)):
        return os.path.join(ROOT, "projects", str(project), "logs", "model")
    return os.path.join(ROOT, "logs", "model")


class Audit:
    def __init__(self, provider, model, operation, request, job_id=None, metadata=None):
        self.context = {**_context.get()}
        self.context.setdefault("project", os.environ.get("ADPIPE_PROJECT"))
        self.context.setdefault("stage", os.environ.get("ADPIPE_STAGE"))
        self.started = _stamp()
        self.started_mono = time.monotonic()
        ident = f"{self.started.strftime('%H%M%S.%f')}_{_safe(operation)}"
        if job_id:
            ident += f"_{_safe(job_id)}"
        ident += f"_{uuid.uuid4().hex[:8]}"
        self.directory = os.path.join(_log_root(self.context),
                                      self.started.strftime("%Y-%m-%d"), ident)
        self._lock = threading.Lock()
        # No directory to make: the store creates on write, and a remote one has
        # no directories at all. Uniqueness already comes from the uuid above.
        self._write("request.json", {
            "started_at": self.started.isoformat(timespec="milliseconds"),
            "provider": provider,
            "model": model,
            "operation": operation,
            "job_id": job_id,
            "context": self.context,
            "metadata": metadata or {},
            "request": request,
        })

    @property
    def relative_path(self):
        return os.path.relpath(self.directory, ROOT)

    def _write(self, name, value):
        # The write-to-temp-then-rename dance was for a half-written file being
        # read by something else. A store write is one operation, so the dance
        # would only add a stray key to clean up.
        store.write_text(os.path.join(self.directory, name),
                         json.dumps(_plain(value), ensure_ascii=False, indent=2))

    def event(self, kind, detail, **metadata):
        row = {"at": _stamp().isoformat(timespec="milliseconds"), "kind": kind,
               "detail": _plain(detail), **_plain(metadata)}
        key = os.path.join(self.directory, "events.jsonl")
        with self._lock:
            # Append by rewriting: a store has no open-for-append, and an audit
            # log for one call is a handful of lines, not a stream.
            existing = store.read_text(key) or ""
            store.write_text(key, existing + json.dumps(row, ensure_ascii=False) + "\n")

    def response(self, provider_response, text=None, **metadata):
        self._write("response.json", {
            "completed_at": _stamp().isoformat(timespec="milliseconds"),
            "elapsed_seconds": round(time.monotonic() - self.started_mono, 3),
            "text": text,
            "text_bytes": len((text or "").encode("utf-8")) if text is not None else None,
            "empty_text": text is not None and not text.strip(),
            "metadata": metadata,
            "provider_response": provider_response,
        })

    def binary_response(self, data, provider_response=None, mime_type="image/png", **metadata):
        ext = ".png" if mime_type == "image/png" else ".bin"
        name = "response" + ext
        store.write_bytes(os.path.join(self.directory, name), data)
        self.response(provider_response or {}, binary_file=name, mime_type=mime_type,
                      binary_bytes=len(data), binary_sha256=hashlib.sha256(data).hexdigest(),
                      **metadata)

    def error(self, error, **metadata):
        self.event("error", {"type": type(error).__name__, "message": str(error)},
                   **metadata)


def start(provider, model, operation, request, job_id=None, metadata=None):
    return Audit(provider, model, operation, _plain(request), job_id, _plain(metadata or {}))
