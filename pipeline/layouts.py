"""The layout vocabulary the concepts stage picks from.

What survives of the old compositor. Ads are no longer built by screenshotting
HTML, but a concept still has to say what shape it is — a hook over a product
shot is a different brief from a before-and-after — and the template files are
where those shapes and their text slots are already written down.

So they are read for their names rather than rendered. Nothing here opens a
browser, and nothing here writes.
"""

import os
import re

import paths  # noqa: F401  (kept so ROOT moves with the rest of the pipeline)
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTORY = os.path.join(ROOT, "pipeline", "templates")

# Slots that describe the frame rather than the copy. A concept chooses words;
# it does not choose a logo or supply an avatar, so offering these as slots only
# invited the model to fill them.
NOT_COPY = ("logo_text",)
NOT_COPY_SUBSTRINGS = ("image", "avatar")


def slots(path):
    """Every {{...}} name a template references, minus internals."""
    source = store.read_text(path) or ""
    names = set(re.findall(r"\{\{[{#^]?\s*([\w.]+)\s*\}?\}\}", source))
    return sorted(name for name in names if not name.startswith("__"))


def copy_slots(path):
    return [name for name in slots(path)
            if name not in NOT_COPY
            and not any(part in name for part in NOT_COPY_SUBSTRINGS)]


def catalogue(directory=None):
    """One line per layout, as the concepts prompt wants it.

    A leading underscore marks a partial — `_base.css` is shared styling, not a
    layout somebody can choose.
    """
    directory = directory or DIRECTORY
    lines = []
    for name in store.names_in(directory):
        if not name.endswith(".html") or name.startswith("_"):
            continue
        lines.append("    %s — slots: %s"
                     % (name[:-5], ", ".join(copy_slots(os.path.join(directory, name)))))
    return "\n".join(lines)
