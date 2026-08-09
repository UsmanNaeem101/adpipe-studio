# Scalable segmentation pipeline

`segment` is a persisted 03A → 03B → 03E → 03C → 04 → 05 → 06 pipeline. Raw VOC
never has to fit in one model context.

## Discovery flow

1. **03A harvest** reads Core evidence in deterministic token-sized chunks. OpenRouter
   keeps four requests in flight; budget exhaustion promotes the rolling floor through
   12k, 16k, 24k, and 32k. Context never enters 03A. Supporting is excluded by default.
   A chunk is a search space, not an assignment unit: candidates cite only their
   genuine supporting IDs, evidence may remain unassigned, and semantic validation
   rejects generic task labels or invalid evidence claims before 03B can run.
   Each request schema enumerates exactly that chunk's evidence IDs. The same
   constraint is checked locally after decoding and again before aggregation.
2. Code aggregates exact candidate keys, evidence IDs, aliases, cue terms, chunk IDs,
   and result provenance. It does not make semantic merges.
3. **03B consolidation** reads that compact catalogue globally, merges semantic
   aliases, and emits discovery statuses—not validation decisions.
4. **03E expansion** classifies all evidence in chunks against the canonical catalogue.
   Code unions evidence and computes tier/thread/subreddit counts and representative
   evidence IDs.
5. **03C novelty** audits Core evidence not strongly covered by the catalogue. Only a
   recurring proposal (two or more evidence IDs) can enter one final 03B cycle. There
   is no novelty loop.
6. **04 validation** receives compact candidate cards, code-computed metrics, and a
   bounded verbatim representative set. It retains its validate/merge/split/reject/
   needs-more-research role.
7. **05 assignment** still gives each evidence item one primary validated segment or
   an explicit unassigned status.
8. **06 build** joins assignments to audit provenance in code and writes the evidence
   tier into every segment file.

## Artifacts and lineage

Artifacts live under `research/segments/discovery/`:

```text
03_monolithic_candidate_segments.json       preserved old output, when present
03a_chunk_manifest.json
03a_chunk_candidates/03a_*.json
03a_candidate_catalogue.json
03b_initial_consolidated_candidates.json
03e_chunk_matches/03e_*.json
03c_chunk_results/03c_*.json
03c_novelty_results.json
03b_consolidated_candidates.json
03_candidate_evidence.json
discovered_segments.json
03_run_summary.json
```

Chunk artifacts include their exact evidence IDs and an input fingerprint. A final
candidate records merged provisional keys and originating 03A chunks, so lineage can
be followed back through `audit_voc.jsonl` to original URLs and Stage 01/02 decisions.

`03_run_summary.json` records chunk/candidate counts, per-substep actual provider token
usage, reasoning tokens when reported, cost, wall time, representative coverage, and
best-effort comparison data from a preserved monolithic output/audit log.

## Running and resuming

```bash
./adpipe -p PROJECT segment
./adpipe -p PROJECT segment --from 03a
./adpipe -p PROJECT segment --from 03b
./adpipe -p PROJECT segment --from 03c
./adpipe -p PROJECT segment --from 04
./adpipe -p PROJECT segment --from 05
./adpipe -p PROJECT segment --from 06
```

`--rediscover` remains an alias for `--from 03a`; `--reassign` reruns Stage 05.
Completed chunks are reused when their input fingerprint matches. A changed skill,
candidate catalogue, evidence text, schema, semantic-contract version, or chunk
membership invalidates only the affected artifact and its dependent steps. New chunk
artifacts record routed-provider and token-split telemetry when it is reported; this
does not pin provider routing.

The first persisted 03A contract has a narrow compatibility migration. Its exact
legacy fingerprints are recognized and revalidated locally: valid chunks are migrated
without a model call, semantic labels are preserved, and code assigns immutable
`<chunk_id>_c<ordinal>` candidate IDs from saved row order. Impossible out-of-chunk
evidence IDs are removed locally when valid in-chunk support remains. Only structural
contract violations—or candidates left with no valid evidence—enter structured repair.

## Per-project settings

Optional `project.json` settings live under `segmentation`:

```json
{
  "segmentation": {
    "03a_chunk_tokens": 12000,
    "03a_max_tokens": 12000,
    "03a_effort": "high",
    "03a_include_supporting": false,
    "03b_max_tokens": 64000,
    "03b_effort": "high",
    "03e_chunk_tokens": 8000,
    "03e_max_tokens": 12000,
    "03e_effort": "high",
    "03c_chunk_tokens": 8000,
    "03c_max_tokens": 12000,
    "03c_effort": "high",
    "04_effort": "high",
    "05_chunk_tokens": 8000,
    "05_effort": "high"
  }
}
```

Unset effort values inherit the selected client's reasoning policy. Stage-specific
values override it without changing unrelated stages.

Stage 03B uses a separate `64,000 → 128,000` output-ceiling ladder because it emits
the complete canonical candidate catalogue in one response. The setting above controls
its starting ceiling; only an actual `length`/`max_tokens` stop advances it. This does
not change 03A's 12,000-token rolling-wave ceiling or any stage's reasoning policy.

Stage 04 uses its own fixed `64,000 → 128,000 → 256,000` output-ceiling
ladder. It starts at 64,000 and advances only after an actual
`length`/`max_tokens` stop. The CLI reports completion, reasoning, and answer-token
usage for every attempt. At 256,000, another budget stop is a handled terminal
failure with diagnostics; no partial validation catalogue is accepted.

Stage 05 uses a separate `16,000 → 24,000 → 32,000` ladder. With native
Anthropic it submits at most 10 structured requests per Message Batch and keeps
at least 65 seconds between submissions, retaining Batch pricing and the shared
cached segment-definition prefix. Anthropic grammar-compilation capacity errors
retry only the affected requests, at most twice, in later paced batches. Every
valid chunk is checkpointed under
`research/segments/assignments/05_chunk_assignments/`; interrupted runs reuse
those artifacts and do not rerun Stage 04. Legacy paid successes are migrated
from exact-matching model audit logs without another model call.

Each 03B request receives a dynamic schema whose `source_candidate_ids` enum contains
the exact code-owned IDs in the current 03A catalogue. Semantic keys, slugs, names, and
definitions remain open for 03B to reinterpret; code assigns canonical `segment_id`
values after parsing. Local post-validation enforces machine lineage if a provider
ignores the schema. An otherwise complete response with invalid lineage is saved to
diagnostics and gets one bounded full-catalogue reviewer pass—03A is not rerun.
If an older run was audited but crashed before persisting its 03B artifact, the resume
path reuses it only when its embedded catalogue exactly fingerprints the current one.
The reviewer must return the same complete collection in the same order. Code accepts
only `source_candidate_ids` corrections, rejects partial/catalogue-shrinking responses,
and revalidates the full collection before writing. Stage 04 and Stage 05 likewise join
by `segment_id`; semantic slugs are used only for human-readable labels and filenames.
