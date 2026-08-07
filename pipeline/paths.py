#!/usr/bin/env python3
"""
Where things live inside a project.

    projects/<project>/
      project.json
      research/          what the market said
        voc/             raw and filtered voice-of-customer
        evidence/        one .txt per validated segment
        extractions/     per-segment skill outputs
      products/          what you sell
        <product>/       truth, segments, strategy
      assets/            what gets made
        <segment>/       picc card, concepts, briefs, plates, renders

Three things a project holds: the research, the products sold into it, and the
assets produced from both. Everything used to sit loose at the project root,
which stopped reading as a structure once a project could hold several products.

Every resolver falls back to the old location when the new one is absent, so a
project works before it has been migrated and during a half-finished move. That
tolerance is deliberate: the alternative is a flag day where a stale checkout
silently reads an empty directory instead of erroring.

Standard library only.
"""

from __future__ import annotations

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# old name at project root -> new home
MOVES = [
    ("voc", os.path.join("research", "voc")),
    ("evidence", os.path.join("research", "evidence")),
    ("extractions", os.path.join("research", "extractions")),
    ("output", "assets"),
]

RESEARCH = "research"
PRODUCTS = "products"
ASSETS = "assets"


def project_dir(project):
    """Accepts a project name or an already-resolved project directory.

    Callers hold one or the other depending on where they sit — the CLI carries
    cfg["_dir"], the web app carries the name — and requiring a single form
    meant every call site had to convert, which is how a KeyError on cfg["name"]
    got into the ingest path.
    """
    if os.path.isabs(project):
        return project
    return os.path.join(ROOT, "projects", project)


def _resolve(project, new_rel, legacy_rel):
    """The new path if it exists, else the legacy one if that does, else new.

    Returning the new path when neither exists means writers create the modern
    layout without needing to know about the old one.
    """
    base = project_dir(project)
    new = os.path.join(base, new_rel)
    if os.path.exists(new):
        return new
    legacy = os.path.join(base, legacy_rel)
    if os.path.exists(legacy):
        return legacy
    return new


def voc(project, *parts):
    return os.path.join(_resolve(project, os.path.join(RESEARCH, "voc"), "voc"), *parts)


def evidence(project, *parts):
    return os.path.join(
        _resolve(project, os.path.join(RESEARCH, "evidence"), "evidence"), *parts)


def extractions(project, *parts):
    return os.path.join(
        _resolve(project, os.path.join(RESEARCH, "extractions"), "extractions"), *parts)


def assets(project, *parts):
    return os.path.join(_resolve(project, ASSETS, "output"), *parts)


def products(project, *parts):
    return os.path.join(project_dir(project), PRODUCTS, *parts)


def research(project, *parts):
    return os.path.join(project_dir(project), RESEARCH, *parts)


def scaffold(project):
    """Create the three-part skeleton for a new project."""
    base = project_dir(project)
    for d in (os.path.join(RESEARCH, "voc"), os.path.join(RESEARCH, "evidence"),
              os.path.join(RESEARCH, "extractions"), PRODUCTS, ASSETS):
        os.makedirs(os.path.join(base, d), exist_ok=True)


def needs_migration(project):
    base = project_dir(project)
    return [old for old, _ in MOVES if os.path.isdir(os.path.join(base, old))]


def migrate(project):
    """Move a flat project into research / products / assets. Idempotent.

    Merges rather than replaces when both sides exist, so a project that was
    half-moved by hand does not lose the half already in place.
    """
    base = project_dir(project)
    moved = []
    for old, new in MOVES:
        src = os.path.join(base, old)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(base, new)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            os.rename(src, dst)
        else:
            for name in os.listdir(src):
                s, d = os.path.join(src, name), os.path.join(dst, name)
                if not os.path.exists(d):
                    shutil.move(s, d)
            if not os.listdir(src):
                os.rmdir(src)
        moved.append(f"{old} -> {new}")
    if moved:
        scaffold(project)
    return moved


def migrate_all():
    root = os.path.join(ROOT, "projects")
    if not os.path.isdir(root):
        return {}
    out = {}
    for p in sorted(os.listdir(root)):
        if not os.path.isdir(os.path.join(root, p)) or p.startswith((".", "_")):
            continue
        m = migrate(p)
        if m:
            out[p] = m
    return out


def main():
    import sys
    if "--migrate" in sys.argv:
        done = migrate_all()
        if not done:
            print("  nothing to migrate — every project is already laid out")
        for p, moves in done.items():
            print(f"  {p}:")
            for m in moves:
                print(f"    {m}")
        return
    root = os.path.join(ROOT, "projects")
    for p in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if os.path.isdir(os.path.join(root, p)):
            pend = needs_migration(p)
            print(f"  {p:<28} {'needs migration: ' + ', '.join(pend) if pend else 'ok'}")


if __name__ == "__main__":
    main()
