# Where every skill file runs

The original 27 numbered skills are in `skills/` and wired to stages. Stage 03 is
now an orchestration contract with 03A/03B/03E/03C production subskills for scalable
discovery. Verified against the code, not from memory — re-run the audit any time:

```bash
.venv/bin/python -c "
import re,os; src=open('pipeline/cli.py').read()
n=set(int(m) for m in re.findall(r'skill\((\d+)\)',src)) | set(range(7,27))
print(sorted(set(range(1,28))-n) or 'all 27 wired')"
```

The segmentation contracts intentionally differ from the old monolithic Skill 03.
Nothing syncs automatically with an external SDK folder.

---

## The map

| Skill | Stage | Called from | Reads | Writes |
|---|---|---|---|---|
| **01** filter_voc | `ingest` | `cmd_ingest()` | raw dump | `voc/retained_voc.jsonl` · `rejected_voc.jsonl` |
| **02** deduplicate_voc | `ingest` | `cmd_ingest()` | retained | `voc/deduplicated_voc.jsonl` · `duplicate_groups.jsonl` |
| deterministic refinement | `ingest` / `refine-voc` | `refine_voc()` | deduplicated + duplicate groups | `voc/production_voc.jsonl` · `audit_voc.jsonl` |
| **03A** harvest candidates | `segment` | `cmd_segment()` | token-sized Core chunks | `research/segments/discovery/03a_chunk_candidates/` |
| **03B** consolidate candidates | `segment` | `cmd_segment()` | compact 03A catalogue | `03b_consolidated_candidates.json` |
| **03E** expand evidence | `segment` | `cmd_segment()` | candidate cards + evidence chunks | `03_candidate_evidence.json` |
| **03C** novelty audit | `segment` | `cmd_segment()` | unexplained Core chunks | `03c_novelty_results.json` · `discovered_segments.json` |
| **04** validate_segments | `segment` | `cmd_segment()` | compact cards + metrics + representatives | `voc/validated_segments.json` |
| **05** assign_primary_segment | `segment` | `cmd_segment()` | validated + corpus | `voc/segment_assignments.jsonl` |
| **06** build_segment_evidence_files | `segment` | `build_evidence_files()` | assignments | `evidence/<slug>.txt` + audit set |
| commercial **07** coalescing | `segment` | `_run_commercial_layers()` | validated research cards + Stage-05 metrics | `commercial/07_*.json` |
| commercial **08A/08B** VOC synthesis | `segment` | `_run_commercial_layers()` | canonical evidence unions | `commercial/synthesis/cseg_*.json` |
| commercial **09** research pack | `segment` | `render_research_pack()` | Stage-07/08 artifacts | `segments/final/` |
| **07–26** the 20 extractors | `extract` | `cmd_extract()` via `EXTRACTORS` | one evidence file | `extractions/<segment>/<skill>.md` |
| **27** rank_buying_barriers | `picc` | `cmd_picc()` | extractions 07–26 | `output/<segment>/01_picc_card.md` |

Skills 07–26 are driven by `EXTRACTORS = list(range(7, 27))` — adding a new
extractor file to `skills/` and widening that range is all it takes to add a
dimension.

---

## Which run on the model, and why

Skills 01–05 and 07–27 contain model judgement; Stage 03 now invokes four bounded
model subskills. Stage 06 remains code. Two special cases do not call a model:

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
  → voc/deduplicated_voc.jsonl      (02)   legacy copy: filtered_voc.jsonl
  → voc/production_voc.jsonl               deterministic lean export
  → voc/audit_voc.jsonl                    deterministic rich audit
  → research/segments/discovery/    (03A/03B/03E/03C intermediates)
  → voc/candidate_segments.json     (03 compatibility copy of discovered_segments)
  → voc/validated_segments.json     (04)
  → voc/segment_assignments.jsonl   (05)
  → evidence/<slug>.txt             (06)   + unassigned_evidence.md,
                                             segment_evidence_manifest.yaml,
                                             assignment_conflicts.jsonl,
                                             missing_evidence.jsonl
  → research/segments/commercial/  (commercial 07/08 catalogue + synthesis)
  → research/segments/final/       (commercial 09 human research pack)
  → extractions/<segment>/*.md      (07–26)
  → output/<segment>/01_picc_card.md (27)
```

Each segmentation chunk has an input fingerprint and skips completed work on rerun.
Use `segment --from 03a|03b|03c|04|05|06|07|08|09` to rerun that substep and its downstream
steps; `--rediscover` remains an alias for restarting at 03A and `--reassign` redoes
05. `--force` redoes extractions.

---

## What this costs

`ingest` and `segment` are now genuinely expensive — they judge every comment
individually rather than pattern-matching. On ~1,700 records that's ~28 filter
chunks + ~21 dedup chunks + ~29 assignment chunks, all batched at 50% with the
corpus cached. Every stage prints an estimate and waits for approval before
spending.

If you want a cheap first pass to sanity-check a new dump, use
`ingest --rules-only` (free), then run the real thing once the input looks right.
