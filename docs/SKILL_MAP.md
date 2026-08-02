# Where every skill file runs

All 27 prompts from
`Business/Dropshipping/Documents/SDK v3 manual/markdown/` are copied to `skills/`
in this project and wired to a stage. Verified against the code, not from memory —
re-run the audit any time:

```bash
.venv/bin/python -c "
import re,os; src=open('pipeline/cli.py').read()
n=set(int(m) for m in re.findall(r'skill\((\d+)\)',src)) | set(range(7,27))
print(sorted(set(range(1,28))-n) or 'all 27 wired')"
```

The copies are byte-identical to your originals. If you edit a prompt in the SDK
folder, re-copy it — nothing syncs automatically.

---

## The map

| Skill | Stage | Called from | Reads | Writes |
|---|---|---|---|---|
| **01** filter_voc | `ingest` | `cmd_ingest()` | raw dump | `voc/retained_voc.jsonl` · `rejected_voc.jsonl` |
| **02** deduplicate_voc | `ingest` | `cmd_ingest()` | retained | `voc/deduplicated_voc.jsonl` · `duplicate_groups.jsonl` |
| **03** discover_segments | `segment` | `cmd_segment()` | deduplicated corpus | `voc/candidate_segments.json` |
| **04** validate_segments | `segment` | `cmd_segment()` | candidates | `voc/validated_segments.json` |
| **05** assign_primary_segment | `segment` | `cmd_segment()` | validated + corpus | `voc/segment_assignments.jsonl` |
| **06** build_segment_evidence_files | `segment` | `build_evidence_files()` | assignments | `evidence/<slug>.txt` + audit set |
| **07–26** the 20 extractors | `extract` | `cmd_extract()` via `EXTRACTORS` | one evidence file | `extractions/<segment>/<skill>.md` |
| **27** rank_buying_barriers | `picc` | `cmd_picc()` | extractions 07–26 | `output/<segment>/01_picc_card.md` |

Skills 07–26 are driven by `EXTRACTORS = list(range(7, 27))` — adding a new
extractor file to `skills/` and widening that range is all it takes to add a
dimension.

---

## Which run on the model, and why

**25 of 27 are sent to the model.** Two are not, and here's the honest reasoning:

- **06 build_segment_evidence_files** is wholly mechanical — join by evidence ID,
  group, sort deterministically, emit, audit. It has no judgement in it, and its own
  spec demands byte-identical output across reruns, which code guarantees and a model
  does not. It's implemented in `build_evidence_files()`, and the skill file is copied
  to `voc/06_build_contract.md` on every run so the spec travels with the artefacts.
- **01/02 under `--rules-only`** — an escape hatch that runs only the deterministic
  pre-pass (interface chrome, byte-identical captures, minimum length) and skips the
  model. Cheap, but it is *not* running skills 01 and 02. The output says so.

---

## Two bugs this audit found

**Skill 05 was being faked.** The old implementation scored assignments by counting
substring hits of segment "cues". Skill 05's own rubric scores
`incidental_keyword → 0` — *"a passing mention, on its own, earns nothing"* — and
lists as a failed run: *"assignment rested on keyword overlap alone."* That was
exactly the implementation. It now sends every item to the model, scored by cue
**type** (explicit self-identification 5, dominant context 5, segment-specific
problem 4, constraint 3, failed solution 3, incidental keyword 0), enforces the
**≥6 score and ≥2 margin** thresholds, and records `unassigned_*` with a reason as a
valid outcome rather than forcing a fit.

**Skills 01, 02 and 04 weren't running at all.** `ingest` was pure regex from
`project.json`, which is a fraction of skill 01 and one of skill 02's four duplicate
types. Skill 04's text was pasted into the discovery prompt but the output schema had
nowhere to put a decision, so validation was silently discarded. Both now run
properly, with the five-decision status set and a written rationale per candidate.

The stage that said **"Free — pure code"** in the UI was calling the model. Fixed —
`ingest` and `segment` are both marked as costing credit now.

---

## Contract files

The workflow analysis in `reference_docs/voc_workflow_analysis.md` specifies file
contracts between stages. They now exist:

```
raw dump
  → voc/retained_voc.jsonl          (01)
  → voc/deduplicated_voc.jsonl      (02)   also written as filtered_voc.jsonl
  → voc/candidate_segments.json     (03)
  → voc/validated_segments.json     (04)
  → voc/segment_assignments.jsonl   (05)
  → evidence/<slug>.txt             (06)   + unassigned_evidence.md,
                                             segment_evidence_manifest.yaml,
                                             assignment_conflicts.jsonl,
                                             missing_evidence.jsonl
  → extractions/<segment>/*.md      (07–26)
  → output/<segment>/01_picc_card.md (27)
```

Each stage skips work already on disk, so a re-run resumes rather than repeating.
`--rediscover` redoes 03/04, `--reassign` redoes 05, `--force` redoes extractions.

---

## What this costs

`ingest` and `segment` are now genuinely expensive — they judge every comment
individually rather than pattern-matching. On ~1,700 records that's ~28 filter
chunks + ~21 dedup chunks + ~29 assignment chunks, all batched at 50% with the
corpus cached. Every stage prints an estimate and waits for approval before
spending.

If you want a cheap first pass to sanity-check a new dump, use
`ingest --rules-only` (free), then run the real thing once the input looks right.
