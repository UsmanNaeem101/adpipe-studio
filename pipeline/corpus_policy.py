#!/usr/bin/env python3
"""
Which voices are allowed to become research evidence.

Two policies live here, both answering the same question at different points in
the funnel: *is this person part of the audience we are researching?*

**Corroboration.** Skill 01 is told to lean toward keeping borderline items and
let a downstream skill decide. On the montisella corpus that produced an 86%
retention rate against a 28% Stage-05 usable rate — the "downstream skill" was
Stage 05, which had to throw away three fifths of what it was handed. Measuring
the retained set against what Stage 05 could actually use is unambiguous:

    retention reasons   items   assigned   yield
    exactly 1             170          2    1.2%
    exactly 2           1,272        133   10.5%
    exactly 3           1,611        462   28.7%
    4 or more           1,123        551   49.1%

    third-person without first-person   772   53   6.9%

A lone signal is not evidence. The gate below requires either a first-person
account carrying at least one other signal, or three independent signals — which
on that corpus cut 826 items while losing 29 of the 1,187 Stage 05 could use.

**Population scope.** A segmentation is only as broad as the communities it was
scraped from. A corpus drawn substantially from diagnosed-condition subreddits
produces condition-shaped segments — accurately, which is the problem: they are
real audiences but small ones, and they crowd out the mainstream buyer a large
TAM depends on. `mainstream` scope excludes those communities at the door and
tells Stages 01 and 04 to treat a diagnosis as a disqualifier rather than a
distinguishing trait.

This is a deliberate narrowing, not a quality filter. People managing a chronic
condition are often the *most* motivated buyers of a support product; excluding
them trades intensity for reach. Scope `all` keeps everyone.

Both policies are configured per project and both are reversible: nothing is
deleted, only held out of the production corpus, so flipping the setting and
re-running `refine-voc` restores the full set without another model call.

Standard library only.
"""

from __future__ import annotations

# --------------------------------------------------------------- corroboration

FIRST_PERSON = "first_person_experience"

# Cut codes are recorded on the audit row so a held-back item can be explained
# without re-deriving the rule that held it back.
SINGLE_SIGNAL = "single_uncorroborated_signal"
UNCORROBORATED_REPORT = "uncorroborated_second_hand_report"


def corroborated(reasons):
    """True when a retained Stage 01 record has enough signal to research on.

    Two ways to clear the bar, because they fail differently. A first-person
    account is the strongest evidence the corpus holds, so it needs only one
    other signal to show it is about something. Everything else — observation,
    advice, commentary about a third party — is a report, and a report needs
    three independent signals before it is worth a segment's attention.
    """
    reasons = set(reasons or [])
    if FIRST_PERSON in reasons:
        return len(reasons) >= 2
    return len(reasons) >= 3


def corroboration_cut(record):
    """The code explaining why this record is held out, or None if it stays.

    A record with no `retention_reasons` key at all predates Stage 01's reason
    vocabulary — lean production files carry a persisted tier and nothing else.
    Those are passed through untouched. Judging corroboration on absent evidence
    would not be a strict reading of the rule, it would be deleting every
    legacy corpus in the project directory on the next refine-voc.
    """
    if "retention_reasons" not in record:
        return None
    reasons = set(record.get("retention_reasons") or [])
    if corroborated(reasons):
        return None
    return SINGLE_SIGNAL if len(reasons) <= 1 else UNCORROBORATED_REPORT


# -------------------------------------------------------------- population scope

SCOPE_ALL = "all"
SCOPE_MAINSTREAM = "mainstream"
SCOPES = (SCOPE_ALL, SCOPE_MAINSTREAM)

OUT_OF_SCOPE_SUBREDDIT = "out_of_scope_community"

# Communities organised around a diagnosis. Membership is the diagnosis, so
# every voice in them is a clinical voice — no per-comment judgement can recover
# a mainstream buyer from a subreddit whose entry condition is a disease.
#
# Lower-cased for matching. Projects extend or override this: a brace brand
# selling into scoliosis would move r/scoliosis to `included_subreddits` rather
# than edit this list.
CLINICAL_SUBREDDITS = frozenset({
    "ehlersdanlos", "eds", "hypermobility", "hypermobilityehlers",
    "endometriosis", "endo", "adenomyosis", "pcos",
    "fibromyalgia", "chronicpain", "chronicillness", "chronicfatigue", "cfs",
    "cfsme", "mecfs", "longcovid", "covidlonghaulers",
    "pots", "dysautonomia", "mcas", "maskmcas",
    "multiplesclerosis", "rheumatoid", "rheumatoidarthritis", "lupus",
    "ankylosingspondylitis", "psoriaticarthritis", "arthritis",
    "crohnsdisease", "ulcerativecolitis", "ibs", "interstitialcystitis",
    "scoliosis", "kyphosis", "spondylolisthesis", "stenosis",
    "costochondritis", "thoracicoutlet", "cubitaltunnel", "carpaltunnel",
    "clusterheadaches", "migraine", "occipitalneuralgia", "trigeminalneuralgia",
    "clotsurvivors", "cancer", "breastcancer", "strokesurvivors",
    "parkinsons", "cerebralpalsy", "musculardystrophy", "spinalcordinjuries",
    "disability", "cripplingalcoholism",
})


def _config(cfg):
    return (cfg or {}).get("audience") or {}


def scope(cfg):
    """The configured population scope, defaulting to the whole population.

    An unrecognised value is a configuration mistake worth surfacing rather than
    silently reading as `all` — a project that meant to narrow its corpus and
    typed the scope wrong would otherwise research the opposite of what it asked
    for and never be told.
    """
    value = str(_config(cfg).get("population_scope", SCOPE_ALL)).strip().lower()
    if value not in SCOPES:
        raise ValueError(
            f"audience.population_scope is {value!r}; expected one of "
            + ", ".join(repr(name) for name in SCOPES))
    return value


def normalise_subreddit(name):
    """Fold `r/Name`, `/r/name` and `name` onto one key.

    Deliberately not `lstrip("r/")`: lstrip removes any leading character in the
    set, so it silently turns `rheumatoid` into `heumatoid` and would quietly
    stop matching half this list.
    """
    text = str(name or "").strip().lower().strip("/")
    return text[2:] if text.startswith("r/") else text


def _names(cfg, key):
    return {normalise_subreddit(name) for name in _config(cfg).get(key) or ()
            if normalise_subreddit(name)}


def excluded_subreddits(cfg):
    """Communities held out of this project's corpus, lower-cased.

    `included_subreddits` wins over both the built-in clinical list and the
    project's own exclusions: it is the escape hatch for a project whose
    audience genuinely lives in one of these communities, and an escape hatch
    that can be overruled is not one.
    """
    excluded = _names(cfg, "excluded_subreddits")
    if scope(cfg) == SCOPE_MAINSTREAM:
        excluded |= CLINICAL_SUBREDDITS
    return frozenset(excluded - _names(cfg, "included_subreddits"))


def subreddit_excluded(subreddit, cfg):
    name = normalise_subreddit(subreddit)
    return bool(name) and name in excluded_subreddits(cfg)


# The comment-level half of the same policy. A subreddit list cannot catch the
# person in r/flexibility whose every post is about managing their EDS, so the
# judgement stages are told the rule as well. Kept as one string so Stage 01 and
# Stage 04 cannot drift into applying different definitions of the audience.
SCOPE_INSTRUCTION = (
    "AUDIENCE SCOPE — mainstream population.\n"
    "This research is for a broad consumer market, so a diagnosed disease or "
    "chronic medical condition puts a voice OUT of scope, however vivid the "
    "evidence is. Out of scope: anyone whose problem is presented as a "
    "consequence of a named condition (hypermobility/EDS, endometriosis, "
    "fibromyalgia, autoimmune or inflammatory disease, cancer, neurological "
    "disease, post-surgical or post-injury rehabilitation under clinical care). "
    "In scope: ordinary causes an ordinary person has — desk work, sleep "
    "position, training, pregnancy, ageing, carrying children, bad chairs, long "
    "drives, stress — including pain that is severe or long-standing, so long "
    "as it is not attributed to a diagnosis.\n"
    "Judge the voice, not the vocabulary: someone who mentions a friend's "
    "diagnosis while describing their own ordinary desk pain is in scope."
)


def scope_prompt(cfg):
    """The instruction to append for this project, or "" when scope is `all`."""
    return SCOPE_INSTRUCTION if scope(cfg) == SCOPE_MAINSTREAM else ""


# Stage 01 gets its own reason code so an out-of-scope voice is auditable as a
# scope decision rather than hiding inside `off_topic`, which would make the
# policy impossible to measure or reverse.
CLINICAL_POPULATION = "clinical_population"


# ---------------------------------------------------------------- segment floors

# Skill 04 already says a single thread is a conversation rather than an
# audience, but said it only in prose: the montisella run validated 70 segments,
# 19 of which rested on three threads or fewer and one of which — 62 items — came
# from five. Floors make the rule countable.
DEFAULT_MIN_THREADS = 4
DEFAULT_MIN_EVIDENCE = 8


def segment_floors(cfg):
    settings = (cfg or {}).get("segmentation") or {}
    return (int(settings.get("min_segment_threads", DEFAULT_MIN_THREADS)),
            int(settings.get("min_segment_evidence", DEFAULT_MIN_EVIDENCE)))


def floor_failure(candidate, min_threads, min_evidence):
    """Why this validated candidate is too thin to stand, or None.

    Counts come from the Stage 03 candidate card, which computed them in code;
    this only compares them, so a segment can never be demoted over a number the
    model asserted.
    """
    threads = int(candidate.get("unique_thread_count") or 0)
    evidence = int(candidate.get("unique_evidence_count") or 0)
    if threads < min_threads:
        return (f"rests on {threads} thread(s); {min_threads} required — "
                "too few conversations to be an audience")
    if evidence < min_evidence:
        return (f"has {evidence} evidence item(s); {min_evidence} required")
    return None


# ------------------------------------------------------------------- reporting


def summarise_exclusions(counter, limit=12):
    """Render a cut tally for the operator, largest first."""
    return [f"    {name:34} {count:,}"
            for name, count in counter.most_common(limit)]
