# VOC refinement/export

`refine-voc` is deterministic local pipeline code. It does not call a model and
is not an LLM skill.

## Input contract

The exporter reads `research/voc/deduplicated_voc.jsonl` and, when present,
`research/voc/duplicate_groups.jsonl`. Input ordering and each decoded `text`
value are preserved exactly. `evidence_id` may exist as redundant Stage 01 output
metadata; when present it must equal `id` and is not copied to either canonical
record schema.

## Outputs

`production_voc.jsonl` is the canonical downstream model input. Its fields are
exactly:

```json
{"id": 272, "text": "I've had subsequent pain…", "thread_id": "1m1n7e5", "subreddit": "ClusterHeadaches", "tier": "core"}
```

Missing or non-standard Reddit URLs produce JSON `null` for both derived fields.
No URL, title, filter decision, reason code, or deduplication metadata is sent in
the production record. `tier` is the deterministic evidence-strength mapping from
Stage 01's closed retention-reason vocabulary: `core`, `supporting`, or `context`.

`audit_voc.jsonl` keeps `id`, `text`, URL/title provenance, the derived source
fields, Stage 01 decision metadata, the canonical tier, and any other upstream audit
fields. Rejected source rows remain auditable with a null tier but never enter
production. When a
surviving canonical record owns Stage 02 duplicate groups, the exact group objects
are attached under `dedup_groups`.

Audit holds every input row; production holds the subset that clears the corpus
policies below. A row held out carries `production_excluded` on its audit record,
naming the rule that held it. Re-running against identical inputs produces identical
bytes.

## Corpus policies

Both are applied here rather than at ingest, because this stage is deterministic and
free. Retuning either one is a re-run of `refine-voc` — no model call, no cost — and
nothing is deleted, so every held-back record is recoverable from the audit file.

**Corroboration** (always on). A first-person account needs one other retention
reason; anything else needs three. Held-back rows are marked
`single_uncorroborated_signal` or `uncorroborated_second_hand_report`. Rows that
predate Stage 01's reason vocabulary — a lean production file with a persisted tier
and no reason codes — are passed through untouched rather than judged on absent
evidence.

**Population scope** (`audience.population_scope` in `project.json`). `all` is the
default and excludes nobody. `mainstream` drops communities organised around a
diagnosis, marking them `out_of_scope_community`, and tells skills 01 and 04 to treat
a named condition as out of scope. Projects extend the built-in list with
`audience.excluded_subreddits`; `audience.included_subreddits` overrules both the
built-in list and the project's own exclusions, so a project whose audience genuinely
lives in one of those communities can say so.

This is a reach-versus-intensity trade, not a quality filter: people managing a
chronic condition are often the most motivated buyers of a support product, and
excluding them narrows who the research can speak for.

## Evidence tiers

- `core`: first-person lived experience plus a concrete problem, context, solution,
  product/competitor experience, outcome, decision signal, workaround, comparison,
  or emotional signal.
- `supporting`: substantive evidence without that direct-lived combination, including
  third-person observations, beliefs, explanations, comparisons, and specific
  problem/solution context.
- `context`: remaining retained signal whose reason codes do not establish enough
  specificity to drive discovery, such as terminology or a weakly specified personal
  remark.

The mapping is pure code in `pipeline/segmentation.py`; it makes no model call.

## URL derivation

For a standard Reddit path `/r/<subreddit>/comments/<thread_id>/...`, the exporter
copies the two named path components. `reddit.com` and its normal subdomains,
including `www.reddit.com` and `old.reddit.com`, are supported. Other hosts and
non-standard paths are not guessed.

## Commands

Automatic ingest runs refinement after Stage 02 succeeds. It can also be rerun
without filtering, deduplication, provider setup, or model calls:

```bash
./adpipe -p PROJECT refine-voc
./adpipe -p PROJECT refine-voc --source /path/to/alternate_voc.jsonl
```

Studio exposes the same choice on the Pipeline page and recommends
`deduplicated_voc.jsonl`. Only project JSONL files containing `id`, `text`, and
Stage 01 retention reasons or an existing canonical tier are listed. A legacy lean
file without either cannot be tiered reliably and fails clearly instead of silently
being labelled Context. The command deterministically overwrites the two derived
exports.

## Downstream consumers

- Stage 03A receives deterministic chunks of Core `id`, tier, and text only.
- Stage 03 evidence expansion classifies Core, Supporting, and Context in bounded
  chunks against the compact canonical catalogue.
- Stage 04 receives compact candidate cards, deterministic metrics, and bounded
  representative evidence rather than the raw corpus.
- Stage 05 receives only production `id` and `text`.
- Stage 06 joins `audit_voc.jsonl` locally for URL/title provenance and never sends
  those audit-only fields through the model stages.
- Stages 07 onward read the Stage 06 evidence files, not VOC JSONL directly.

`filtered_voc.jsonl` remains as a legacy rich artefact during the initial migration
but is no longer the default segmentation contract.

## Prompt-size measurement

The exporter reports character counts and the same four-characters-per-token
planning estimate used by the OpenRouter client for the exact Stage 03 and Stage 04
formatted strings. This is explicitly an estimate because tokenization is model and
route specific; the request audit remains the source of actual provider token usage.

The exporter reports the old monolithic Stage 03 view, the total Core evidence view
that will be divided into token-sized 03A chunks, and the Stage 04 raw corpus now
avoided. Candidate-dependent Stage 04 packet size is measured during `segment`, not
guessed during refinement.
