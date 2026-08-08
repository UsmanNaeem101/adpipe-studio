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
{"id": 272, "text": "I've had subsequent pain…", "thread_id": "1m1n7e5", "subreddit": "ClusterHeadaches"}
```

Missing or non-standard Reddit URLs produce JSON `null` for both derived fields.
No URL, title, filter decision, reason code, or deduplication metadata is sent in
the production record.

`audit_voc.jsonl` keeps `id`, `text`, URL/title provenance, the derived source
fields, Stage 01 decision metadata, and any other upstream audit fields. When a
surviving canonical record owns Stage 02 duplicate groups, the exact group objects
are attached under `dedup_groups`.

Both files contain the same number of records in the same order as the deduplicated
input. Re-running against identical inputs produces identical bytes.

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
`deduplicated_voc.jsonl`. Only project JSONL files containing `id` and `text` are
listed. The command deterministically overwrites the two derived exports.

## Downstream consumers

- Stage 03 receives only production `id` and `text`.
- Stage 04 receives the same text with compact `[t:thread] [r:subreddit]` tags so
  its source/thread diversity test has the metadata it requires.
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

Stage 03 already formatted the old rich records as `[id] text`, so its formatted
input is byte-for-byte identical after this migration. For the live corpus reported
at 408,552 provider-counted tokens:

```
before:    408,552 tokens
after:     408,552 tokens
reduction: 0.0%
```

The refinement materially reduces persisted size and prevents future metadata
leakage, but it does not solve Stage 03's 408k-context architecture. Stage 04 now
adds compact thread/subreddit tags because its existing skill explicitly evaluates
thread and source diversity; replacing those per-record tags with aggregates would
be a separate semantic redesign.
