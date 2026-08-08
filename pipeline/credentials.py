"""Canonical local API credential resolution for AdPipe.

Precedence is explicit provider environment variable, then the private user-level
credential file.  The store lives outside projects and the repository, is created
with user-only permissions, and this module never returns secrets from status or
update operations.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid


PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
STORE_OVERRIDE_ENV = "ADPIPE_CREDENTIALS_FILE"


class CredentialStoreError(RuntimeError):
    pass


def store_path(environ=None):
    env = os.environ if environ is None else environ
    override = str(env.get(STORE_OVERRIDE_ENV) or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/AdPipe/credentials.json")
    config = str(env.get("XDG_CONFIG_HOME") or "").strip()
    base = os.path.expanduser(config) if config else os.path.expanduser("~/.config")
    return os.path.join(base, "adpipe", "credentials.json")


def _clean(values):
    return {provider: str(values.get(provider) or "").strip()
            for provider in PROVIDER_ENV
            if str(values.get(provider) or "").strip()}


def load_stored(path=None, environ=None):
    target = path or store_path(environ)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags)
        with os.fdopen(fd, encoding="utf-8") as fh:
            mode = stat.S_IMODE(os.fstat(fh.fileno()).st_mode)
            if mode & 0o077:
                os.fchmod(fh.fileno(), 0o600)
            value = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as error:
        raise CredentialStoreError(
            f"Could not read the local AdPipe credential store at {target}") from error
    if not isinstance(value, dict):
        raise CredentialStoreError(
            f"The local AdPipe credential store at {target} is not a JSON object")
    return _clean(value)


def _write_stored(values, path=None, environ=None):
    target = path or store_path(environ)
    parent = os.path.dirname(target)
    temporary = target + f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_clean(values), fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise CredentialStoreError(
            f"Could not write the local AdPipe credential store at {target}") from error


def resolve(provider, *, environ=None, path=None):
    name = str(provider or "").lower()
    if name not in PROVIDER_ENV:
        raise ValueError(f"unknown credential provider {provider!r}")
    env = os.environ if environ is None else environ
    explicit = str(env.get(PROVIDER_ENV[name]) or "").strip()
    if explicit:
        return explicit
    return load_stored(path, env).get(name, "")


def resolve_all(*, environ=None, path=None):
    env = os.environ if environ is None else environ
    stored = load_stored(path, env)
    return {
        provider: (str(env.get(variable) or "").strip()
                   or stored.get(provider, ""))
        for provider, variable in PROVIDER_ENV.items()
    }


def status(*, environ=None, path=None):
    """Return presence booleans only; this is safe for the browser/API."""
    return {provider: bool(value) for provider, value in
            resolve_all(environ=environ, path=path).items()}


def update(values=None, *, clear=(), environ=None, path=None):
    """Persist nonempty submitted values and explicitly clear requested providers."""
    current = load_stored(path, environ)
    for provider in clear:
        if provider in PROVIDER_ENV:
            current.pop(provider, None)
    for provider, value in (values or {}).items():
        if provider not in PROVIDER_ENV:
            continue
        cleaned = str(value or "").strip()
        if cleaned:
            current[provider] = cleaned
    _write_stored(current, path, environ)
    return status(environ=environ, path=path)
