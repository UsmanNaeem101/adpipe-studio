#!/usr/bin/env python3
"""Download a project's working corpora, and remove them once they are spent.

Stages 01--06 read each other's output, so none of it can go while segmentation
is still running. Stage 06 is the end of that chain: it writes one evidence file
per audience, and every stage after it -- extract, picc, concepts, brief -- reads
those and nothing earlier. At that point the corpora are the largest thing in the
project and the least used.

Largest is not an exaggeration. On a real 37-audience project the raw dump is
16.1MB, its cleaned twin 15.9MB, the deduplicated corpus another 15.9MB. The
evidence files they produce total under 6MB. So the material that matters is a
fraction of what is stored, and the rest is the scaffolding it was built from.

Two operations, deliberately separate:

  bundle  everything, as a zip, so it is on your disk before anything is removed
  remove  the corpora only, and only once stage 06 has actually produced output

`remove` is never automatic. The segmentation rerun flags -- --reassign,
--rediscover, --from -- all read these files, so removing them trades the ability
to adjust segmentation for the space back. That is a judgement about a particular
project on a particular day, not something a stage should decide on finishing.

Standard library only.
"""

from __future__ import annotations

import io
import os
import zipfile

import paths
import store

# Written by stages 01-06, read by nothing after them. Named one by one rather
# than matched by size: a large file is not the same thing as a spent one, and
# the segmentation state beside these (candidate_segments, validated_segments,
# segment_assignments) is what a partial rerun needs.
SPENT_CORPORA = (
    "filtered_voc.jsonl",
    "rejected_voc.jsonl",
    "retained_voc.jsonl",
    "deduplicated_voc.jsonl",
    "production_voc.jsonl",
    "audit_voc.jsonl",
    "duplicate_groups.jsonl",
)

# Whole directories that hold nothing but inputs.
SPENT_TREES = (
    ("voc", "raw"),      # the dump as it arrived
    ("imports",),        # the zip an import was read from
)


def _research(project_dir, *parts):
    return paths.research(project_dir, *parts)


def stage06_output(project_dir):
    """The evidence files stage 06 writes. Empty means it has not run."""
    evidence = paths.evidence(project_dir)
    if not store.exists(evidence):
        return []
    return sorted(name for name in store.names_in(evidence) if name.endswith(".txt"))


def _size(key):
    try:
        raw = store.read_bytes(key)
        return len(raw) if raw else 0
    except Exception:
        return 0


def spent_files(project_dir):
    """Every file that stage 06 has made redundant, with its size."""
    found = []

    voc = paths.voc(project_dir)
    for name in SPENT_CORPORA:
        key = os.path.join(voc, name)
        if store.exists(key):
            found.append((key, _size(key)))

    for parts in SPENT_TREES:
        base = paths.voc(project_dir, *parts[1:]) if parts[0] == "voc" else _research(project_dir, *parts)
        if not store.exists(base):
            continue
        for key in store.list_keys(base):
            full = key if os.path.isabs(str(key)) else os.path.join(base, key)
            if store.exists(full):
                found.append((full, _size(full)))

    # One entry per file even if a name matched twice.
    unique = {}
    for key, size in found:
        unique.setdefault(store.normalise(key), (key, size))
    return sorted(unique.values())


def bundle(project_dir, project_name):
    """The whole project as a zip, so nothing is removed before it is held.

    Everything, not only the spent corpora: a backup taken before deleting is
    the wrong moment to be selective about what it contains.
    """
    base = paths.project_dir(project_dir)
    buffer = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for key in store.list_keys(base):
            full = key if os.path.isabs(str(key)) else os.path.join(base, key)
            try:
                data = store.read_bytes(full)
            except Exception:
                continue
            if data is None:
                continue
            name = os.path.relpath(str(full), str(base)).replace(os.sep, "/")
            archive.writestr("%s/%s" % (project_name, name), data)
            written += 1
    return "%s.zip" % project_name, buffer.getvalue(), written


def remove(project_dir):
    """Delete the spent corpora. Refuses while stage 06 has produced nothing.

    Returns (removed, freed_bytes). Raises ValueError when it is too early,
    because the alternative is a project whose research cannot be rebuilt and
    whose evidence was never written.
    """
    if not stage06_output(project_dir):
        raise ValueError(
            "stage 06 has produced no evidence files — these corpora are still "
            "the only copy of this project's research. Run segmentation through "
            "stage 06, or import audience files, before removing anything.")

    removed, freed = [], 0
    for key, size in spent_files(project_dir):
        store.delete(key)
        removed.append(key)
        freed += size
    return removed, freed
