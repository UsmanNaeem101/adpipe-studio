#!/usr/bin/env python3
"""Every project setting, declared once, so the UI and the validator agree.

These settings were always real and always doing work -- floors were removing
segments from runs, scope was excluding whole communities -- but they lived in a
JSON file the app never mentioned. A setting nobody can find is a setting nobody
tunes, which makes it a hard-coded constant with extra steps.

So the schema below is the single description of what is editable: the UI renders
its fields from it, and the writer validates against it. A second copy in the
frontend would drift, and the first thing to drift would be the bounds -- which
is how a typo becomes a corpus filtered to nothing.

Bounds are deliberately generous. The job here is to stop a value that would
break a run, not to enforce anyone's taste: a floor of 1 is a bad idea and a
legitimate experiment, and this module has no business refusing it.

Standard library only.
"""

from __future__ import annotations

import json
import os
import store

# kind:
#   text     one line of free text        int    whole number, with bounds
#   regex    free text, compiled to check  float  decimal, with bounds
#   choice   one of `options`              list   newline-separated strings
#   longtext several lines
FIELDS = [
    # ---------------------------------------------------------------- audience
    {"path": ["audience", "population_scope"], "kind": "choice",
     "options": ["all", "mainstream"], "default": "all",
     "group": "Audience", "label": "Population scope",
     "help": "'all' researches everyone who mentions the topic. 'mainstream' "
             "excludes communities organised around a diagnosis and tells "
             "skills 01 and 04 to treat a named condition as out of scope — "
             "narrower voices, broader market. Nothing is deleted either way."},
    {"path": ["audience", "excluded_subreddits"], "kind": "list", "default": [],
     "group": "Audience", "label": "Excluded subreddits",
     "help": "One per line, without r/. These are held out of the production "
             "corpus and stay in the audit file."},
    {"path": ["audience", "included_subreddits"], "kind": "list", "default": [],
     "group": "Audience", "label": "Included subreddits",
     "help": "One per line. When set, only these are researched."},

    # ---------------------------------------------------- corroboration (01/refine)
    {"path": ["audience", "min_signals_first_person"], "kind": "int",
     "min": 1, "max": 8, "default": 2,
     "group": "Evidence quality", "label": "Signals needed — first-hand account",
     "help": "A first-person account is the strongest evidence in the corpus, so "
             "it needs fewer signals to show it is about something. Comments "
             "below this are held back, not deleted."},
    {"path": ["audience", "min_signals_report"], "kind": "int",
     "min": 1, "max": 8, "default": 3,
     "group": "Evidence quality", "label": "Signals needed — everything else",
     "help": "Observation, advice and commentary about somebody else are reports, "
             "and a report needs more independent signals before it is worth a "
             "segment's attention. On one run, comments kept on a single signal "
             "were usable 1.2% of the time; four or more, 49%."},

    # ------------------------------------------------------------- segment floors
    {"path": ["segmentation", "min_segment_threads"], "kind": "int",
     "min": 1, "max": 50, "default": 4,
     "group": "Audience floors", "label": "Root — separate conversations",
     "help": "Sixty comments from four threads is four people and their "
             "repliers. Below this an audience is demoted, not deleted."},
    {"path": ["segmentation", "min_segment_evidence"], "kind": "int",
     "min": 1, "max": 200, "default": 8,
     "group": "Audience floors", "label": "Root — comments"},
    {"path": ["segmentation", "min_child_threads"], "kind": "int",
     "min": 1, "max": 50, "default": 2,
     "group": "Audience floors", "label": "Child — separate conversations",
     "help": "A child of an existing audience clears a lower bar, because its "
             "parent already proved the audience exists."},
    {"path": ["segmentation", "min_child_evidence"], "kind": "int",
     "min": 1, "max": 200, "default": 3,
     "group": "Audience floors", "label": "Child — comments"},
    {"path": ["segmentation", "min_distinctive_terms"], "kind": "int",
     "min": 0, "max": 20, "default": 1,
     "group": "Audience floors", "label": "Child — new words required",
     "help": "A child must contribute recurring language its parent does not "
             "already use, or it is a relabelling and gets folded back in. "
             "Set to 0 to switch this test off."},
    {"path": ["segmentation", "distinctive_min_items"], "kind": "int",
     "min": 1, "max": 20, "default": 2,
     "group": "Audience floors", "label": "Child — times a word must recur",
     "help": "One person's word is a turn of phrase; two people's word is the "
             "beginning of an angle."},
    {"path": ["segmentation", "parent_top_terms"], "kind": "int",
     "min": 10, "max": 500, "default": 50,
     "group": "Audience floors", "label": "Parent vocabulary compared against",
     "help": "How much of the parent's language counts as 'already said'."},

    # ------------------------------------------------------------- research packs
    {"path": ["segmentation", "pack_context_budget_tokens"], "kind": "int",
     "min": 0, "max": 100000, "default": 8000,
     "group": "Research packs", "label": "Borrowed context budget (tokens)",
     "help": "How much evidence a segment may borrow from related segments. "
             "Borrowed items are never counted — they are language, not size. "
             "Set to 0 to turn borrowing off."},

    # -------------------------------------------------------------------- filter
    {"path": ["filter", "topic"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "Topic pattern",
     "help": "Decides whether an item is about this market at all. "
             "Case-insensitive regex. Empty matches nothing."},
    {"path": ["filter", "pain"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "Pain pattern"},
    {"path": ["filter", "first_person"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "First-person pattern"},
    {"path": ["filter", "solution"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "Solution pattern"},
    {"path": ["filter", "emotion"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "Emotion pattern"},
    {"path": ["filter", "product"], "kind": "regex", "default": "",
     "group": "Ingest filter", "label": "Product pattern"},
    {"path": ["filter", "min_words"], "kind": "int", "min": 0, "max": 200,
     "default": 12, "group": "Ingest filter", "label": "Minimum words"},
    {"path": ["filter", "min_score"], "kind": "int", "min": 0, "max": 20,
     "default": 2, "group": "Ingest filter", "label": "Minimum relevance score"},

    # --------------------------------------------------------------------- model
    {"path": ["model", "id"], "kind": "text", "default": "claude-opus-5",
     "group": "Model", "label": "Model id"},
    {"path": ["model", "effort"], "kind": "choice",
     "options": ["low", "medium", "high", "xhigh", "max"], "default": "high",
     "group": "Model", "label": "Reasoning effort"},
    {"path": ["model", "provider"], "kind": "choice",
     "options": ["anthropic", "openrouter", "deepseek"], "default": "anthropic",
     "group": "Model", "label": "Provider"},
    {"path": ["model", "extraction_max_tokens"], "kind": "int",
     "min": 2000, "max": 200000, "default": 16000, "group": "Model",
     "label": "Output budget per extraction",
     "help": "Room for one skill's answer. Raising it costs nothing on its own — "
             "output is billed per token actually written — and a budget too "
             "small is how an extraction stops mid-list."},
    {"path": ["model", "reasoning_share"], "kind": "int",
     "min": 0, "max": 90, "default": 40, "group": "Model",
     "label": "Most of that budget reasoning may take (%)",
     "help": "Reasoning is billed from the same allowance as the answer, so a "
             "model left unbounded can spend all of it thinking and write "
             "nothing. 0 leaves the model's own default in charge."},

    # ------------------------------------------------------------------ creative
    {"path": ["creative", "awareness"], "kind": "choice",
     "options": ["unaware", "problem-aware", "solution-aware",
                 "product-aware", "most-aware"],
     "default": "problem-aware", "group": "Creative", "label": "Awareness stage"},
    {"path": ["creative", "traffic"], "kind": "choice",
     "options": ["cold", "warm", "retargeting"], "default": "cold",
     "group": "Creative", "label": "Traffic temperature"},
    {"path": ["creative", "concepts_per_run"], "kind": "int", "min": 1, "max": 40,
     "default": 10, "group": "Creative", "label": "Concepts per run"},
    {"path": ["creative", "briefs_per_run"], "kind": "int", "min": 1, "max": 20,
     "default": 4, "group": "Creative", "label": "Briefs per run"},

    # ---------------------------------------------------------------- compliance
    {"path": ["compliance", "profile"], "kind": "choice",
     "options": ["health_adjacent", "general"], "default": "health_adjacent",
     "group": "Compliance", "label": "Ruleset",
     "help": "'health_adjacent' is the strict Meta set. Use it for anything "
             "wellness-adjacent."},
    {"path": ["compliance", "platform"], "kind": "choice",
     "options": ["meta", "tiktok", "google"], "default": "meta",
     "group": "Compliance", "label": "Platform"},
    {"path": ["compliance", "notes"], "kind": "longtext", "default": "",
     "group": "Compliance", "label": "Notes"},
]


class SettingsError(ValueError):
    """A submitted value would break a run."""


def _get(cfg, path):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise SettingsError(f"{'.'.join(path)} sits under a non-object")
    node[path[-1]] = value


def schema():
    """The fields, as the browser needs them."""
    return [{**field, "key": ".".join(field["path"])} for field in FIELDS]


def current(cfg):
    """Every field's present value, falling back to its default."""
    out = {}
    for field in FIELDS:
        value = _get(cfg, field["path"])
        out[".".join(field["path"])] = (
            field["default"] if value is None else value)
    return out


def coerce(field, value):
    """One submitted value, checked and converted, or SettingsError."""
    label = field.get("label") or ".".join(field["path"])
    kind = field["kind"]
    if kind == "int":
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise SettingsError(f"{label}: '{value}' is not a whole number")
        low, high = field.get("min"), field.get("max")
        if low is not None and number < low:
            raise SettingsError(f"{label}: must be at least {low}")
        if high is not None and number > high:
            raise SettingsError(f"{label}: must be at most {high}")
        return number
    if kind == "float":
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            raise SettingsError(f"{label}: '{value}' is not a number")
        low, high = field.get("min"), field.get("max")
        if low is not None and number < low:
            raise SettingsError(f"{label}: must be at least {low}")
        if high is not None and number > high:
            raise SettingsError(f"{label}: must be at most {high}")
        return number
    if kind == "choice":
        text = str(value).strip()
        if text not in field["options"]:
            raise SettingsError(
                f"{label}: '{text}' is not one of "
                f"{', '.join(field['options'])}")
        return text
    if kind == "list":
        if isinstance(value, list):
            items = value
        else:
            items = str(value or "").replace(",", "\n").split("\n")
        cleaned = []
        for item in items:
            item = str(item).strip().lstrip("/").removeprefix("r/").strip("/")
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned
    if kind == "regex":
        import re
        text = str(value or "")
        if text:
            try:
                re.compile(text, re.I)
            except re.error as error:
                # A broken pattern silently matches nothing, which looks exactly
                # like a corpus with no relevant comments in it.
                raise SettingsError(f"{label}: invalid regex — {error}")
        return text
    return str(value if value is not None else "")


def apply(cfg, submitted):
    """Validate every submitted field, then return an updated copy of the config.

    All-or-nothing: one bad value writes none of them, so a run can never start
    against a half-applied config.
    """
    by_key = {".".join(field["path"]): field for field in FIELDS}
    unknown = sorted(set(submitted) - set(by_key))
    if unknown:
        raise SettingsError(f"unknown setting(s): {', '.join(unknown)}")
    checked = {key: coerce(by_key[key], value) for key, value in submitted.items()}
    updated = json.loads(json.dumps(cfg))
    for key, value in checked.items():
        _set(updated, by_key[key]["path"], value)
    return updated


def write(path, cfg):
    """Replace project.json atomically, so a failed write cannot truncate it."""
    # One store write replaces the temp-file-and-rename: a store write does not
    # half-happen, so there is no truncated file to protect against.
    store.write_text(path, json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return path
