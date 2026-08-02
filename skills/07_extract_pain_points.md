# 07 Extract Pain Points

**Skill ID:** `07_extract_pain_points`  
**Version:** `2.2.0`  
**Status:** Canonical

## Purpose

Extract, normalise, coalesce, count, rank and audit the recurring unwanted states, limitations, frustrations, costs, fears and trade-offs experienced within one validated segment.

This skill receives one canonical segment evidence file:

```text
{segment_slug}_evidence.md
```

and produces:

```text
{segment_slug}_pain_points.md
{segment_slug}_pain_points.yaml
{segment_slug}_pain_points_audit.jsonl
```

Example:

```text
rotator_cuff_evidence.md
        ↓
07_extract_pain_points
        ↓
rotator_cuff_pain_points.md
rotator_cuff_pain_points.yaml
rotator_cuff_pain_points_audit.jsonl
```

## Single Responsibility

This skill extracts pain points only.

It must not extract:

- pain moments
- desired outcomes
- emotional states
- psychological drivers
- beliefs
- limiting beliefs
- assumed solutions
- failed solutions
- objections
- buying triggers
- buying criteria
- mechanisms
- proof requirements
- product mentions
- competitor mentions
- offers
- final segment reports

Representative quotations may be retained only as evidence for a pain point.

## Core Definition

A pain point is a recurring unwanted state, difficulty, limitation, symptom burden, functional impairment, frustration, cost, fear, risk or trade-off experienced by the segment.

A valid pain point answers:

> What is difficult, painful, limiting, disruptive, frustrating, costly or risky for this segment?

Examples:

- Pain when lifting the arm overhead
- Unable to sleep on the affected shoulder
- Loss of strength during pressing movements
- Pain returning after temporary relief
- Difficulty dressing or reaching behind the back
- Fear of worsening the injury
- Confusion caused by conflicting diagnoses

## Input Contract

### Required input

Exactly one file produced by:

```text
06_build_segment_evidence_files
```

Filename:

```text
{segment_slug}_evidence.md
```

### Required segment metadata

- `segment_id`
- `segment_name`
- `segment_slug`
- `segment_definition`
- `validation_status`
- `evidence_count`

### Required evidence fields

- `evidence_id`
- `source_type`
- `text`

### Recommended evidence fields

- `title`
- `url`
- `thread_id`
- `author_id`
- `created_at`
- `assignment_score`
- `winning_margin`
- `primary_cues`
- `assignment_rationale`

## Evidence Policy

Use only evidence from the current segment evidence file.

Every retained pain point must:

- link to one or more valid `evidence_id` values
- preserve traceability to source text
- distinguish observed evidence from any optional inference
- use unique deduplicated evidence items for counts

The skill must not:

- invent missing customer evidence
- import pain points from another segment
- use external sources
- infer diagnoses
- convert a feature or treatment into a pain point without evidence
- present a paraphrase as a verbatim quotation
- mix separate research dimensions

## Observed Evidence Versus Inference

### Observed

A pain point is `observed` when it is directly stated or strongly and unambiguously expressed in the evidence.

Example:

> “I can’t sleep on my right side because the shoulder starts throbbing.”

Observed pain point:

```text
Unable to sleep on the affected shoulder
```

### Inferred

A pain point is `inferred` only when the evidence strongly supports the interpretation but does not state it directly.

Inference must be:

- used sparingly
- explicitly labelled
- assigned lower confidence
- kept separate from observed-only counts where practical

Example:

> “I now ask my wife to reach the top shelves for me.”

Possible inferred pain point:

```text
Loss of independence when reaching overhead
```

Do not infer when multiple interpretations are equally plausible.

## Fail-Closed Rule

When evidence is insufficient, ambiguous or contradictory:

- omit the candidate, or
- retain it as `low` confidence with an explicit ambiguity note

Never fill gaps using general knowledge.

## Classification Boundaries

### Pain point versus pain moment

```text
Pain point:
Unable to sleep comfortably

Pain moment:
Waking at 3 a.m. after rolling onto the injured shoulder
```

Pain moments belong to:

```text
08_extract_pain_moments
```

### Pain point versus desired outcome

```text
Pain point:
Cannot lift the arm overhead without pain

Desired outcome:
Raise the arm normally again
```

### Pain point versus emotional state

```text
Pain point:
Repeated treatments provide only temporary relief

Emotional state:
Frustrated and hopeless
```

### Pain point versus belief

```text
Pain point:
The shoulder hurts during exercise

Belief:
Rest is the only way to avoid further damage
```

### Pain point versus failed solution

```text
Pain point:
Persistent night pain

Failed solution:
Tried multiple pillows without improvement
```

### Pain point versus product or treatment mention

```text
Mention:
Cortisone injection

Pain point:
Pain returned after the injection wore off
```

### Pain point versus cause or diagnosis

```text
Diagnosis:
Rotator-cuff tear

Pain point:
Weakness when lifting the arm
```

A diagnosis may explain the pain point, but is not itself the pain point.

## Invalid Generic Labels

Do not retain topic labels such as:

- pain
- sleep
- surgery
- posture
- exercise
- ageing
- injury
- shoulder problems
- discomfort

Extract the concrete experienced problem.

## Workflow

### 1. Read the complete evidence file

Review every evidence item before finalising the output.

Do not stop when an apparently sufficient list has been found.

### 2. Extract atomic candidate pains

Each candidate should describe one primary unwanted state.

Good:

```text
Cannot sleep on the affected side
```

Bad:

```text
Cannot sleep, exercise, work or lift the arm
```

Split multi-problem evidence into separate candidates.

### 3. Preserve meaningful specificity

Evidence:

> “Every time I reach into the back seat, I get a sharp pain at the front of my shoulder.”

Good candidate:

```text
Sharp shoulder pain when reaching behind
```

Too broad:

```text
Shoulder pain
```

### 4. Normalise wording

Normalise superficial wording differences without deleting useful meaning.

Potentially equivalent:

- cannot sleep on that side
- side sleeping hurts
- lying on the injured shoulder is unbearable

Canonical concept:

```text
Unable to sleep on the affected shoulder
```

Potentially distinct:

- difficulty falling asleep
- repeated waking
- pain on waking

Do not merge these automatically.

### 5. Assign a stable concept ID

Every retained pain point must have a stable `concept_id`.

Format:

```text
pp_{segment_slug}_{six_digit_number}
```

Example:

```text
pp_rotator_cuff_000001
```

### Stable ID rules

A concept ID must remain unchanged across reruns when the underlying canonical concept remains materially the same.

Generate or preserve IDs using this order:

1. exact match to a prior canonical concept
2. approved alias match
3. semantic identity match above the configured threshold
4. otherwise create a new ID

Do not reuse an old ID for a materially different concept.

When concepts merge:

- preserve the surviving canonical ID
- record retired IDs as aliases
- write the merge to the audit file

When a concept splits:

- preserve the old ID for the dominant continuing concept where defensible
- create new IDs for genuinely new concepts
- record the split in the audit file

### 6. Coalesce synonymous candidates

Merge candidates when they represent the same underlying customer problem.

A merged concept must preserve:

- all supporting evidence IDs
- all approved aliases
- unique thread count
- unique author count where available
- representative expressions
- provenance of merges

### 7. Prevent over-coalescing

Do not create vague umbrella concepts.

Bad:

```text
Shoulder pain affects daily life
```

Better:

- Pain when reaching overhead
- Difficulty dressing
- Unable to sleep on the affected side
- Weakness during lifting
- Pain while driving

Keep concepts separate when they imply materially different:

- messages
- hooks
- creative scenes
- desired outcomes
- product requirements
- proof requirements
- landing-page sections

### 8. Count unique evidence

Primary count:

```text
unique supporting evidence items
```

Rules:

- count each `evidence_id` once per pain point
- repeated excerpts from the same evidence item do not increase counts
- preserve unique thread count separately
- preserve unique author count where available
- duplicate source material must not inflate frequency

One evidence item may support multiple pain points when it genuinely expresses multiple distinct problems.

This does not violate the one-primary-segment rule. Segment assignment controls corpus membership, not the number of valid concepts an item may support within its assigned segment.

### 9. Score pain points

Default score range:

```text
0–100
```

Default weights:

| Dimension | Weight |
|---|---:|
| Evidence volume | 25% |
| Thread diversity | 20% |
| Functional impact | 20% |
| Intensity | 15% |
| Commercial distinctiveness | 10% |
| Segment specificity | 10% |

The score ranks pain points within the segment.

It is not a medical severity score.

### 10. Assign confidence

Allowed values:

- `high`
- `medium`
- `low`

#### High

- repeated across independent evidence items
- supported by multiple threads
- clearly expressed
- low ambiguity

#### Medium

- coherent but less widespread
- supported by several items or one unusually detailed item
- some ambiguity remains

#### Low

- sparse evidence
- heavily implied
- overlaps another concept
- depends partly on inference

### 11. Select representative quotations

Select no more than three short quotations per pain point.

Each quotation must:

- come from the input evidence
- preserve the original meaning
- cite its `evidence_id`
- not expose unnecessary personal details
- never present rewritten wording as verbatim

### 12. Produce all canonical outputs

Required:

```text
{segment_slug}_pain_points.md
{segment_slug}_pain_points.yaml
{segment_slug}_pain_points_audit.jsonl
```

## Canonical Markdown Output

```markdown
# Rotator Cuff Pain Points

## Extraction Metadata

- Segment ID: rotator_cuff
- Segment name: People with rotator-cuff, impingement or injury-related shoulder pain
- Source file: rotator_cuff_evidence.md
- Evidence items reviewed: 614
- Pain points retained: 18
- Observed concepts: 16
- Inferred concepts: 2
- Extraction skill: 07_extract_pain_points
- Skill version: 2.2.0

## Pain Point Summary

| Rank | Concept ID | Pain point | Evidence | Threads | Score | Confidence | Basis |
|---:|---|---|---:|---:|---:|---|---|
| 1 | pp_rotator_cuff_000001 | Unable to sleep on the affected shoulder | 94 | 51 | 92 | High | Observed |
| 2 | pp_rotator_cuff_000002 | Pain when lifting the arm overhead | 83 | 47 | 89 | High | Observed |

## Pain Points

### 1. Unable to sleep on the affected shoulder

**Concept ID**

`pp_rotator_cuff_000001`

**Basis**

Observed

**Canonical statement**

People cannot lie on the affected shoulder without triggering pain, forcing them to change position or avoid their preferred sleeping side.

**Why it matters**

It disrupts sleep and makes the problem feel unavoidable because it continues during rest.

**Evidence**

- Supporting evidence items: 94
- Unique threads: 51
- Unique authors: 72
- Score: 92/100
- Confidence: High

**Aliases and common expressions**

- Cannot sleep on that side
- Side sleeping hurts
- Shoulder throbs when lying on it
- Forced to sleep on the back

**Representative quotations**

> “I haven’t been able to sleep on my right side for months.”  
> — `ev_000123`

> “The pain wakes me whenever I roll onto that shoulder.”  
> — `ev_000891`

**Supporting evidence IDs**

```text
ev_000123
ev_000184
ev_000227
```

**Boundary note**

Includes inability to tolerate pressure on the affected shoulder. Excludes general insomnia without a shoulder-related cause.
```

## Canonical YAML Output

```yaml
skill:
  id: 07_extract_pain_points
  version: 2.2.0

segment:
  segment_id: rotator_cuff
  segment_slug: rotator_cuff
  source_file: rotator_cuff_evidence.md

extraction:
  evidence_items_reviewed: 614
  retained_concepts: 18
  observed_concepts: 16
  inferred_concepts: 2

pain_points:
  - concept_id: pp_rotator_cuff_000001
    rank: 1
    canonical_name: Unable to sleep on the affected shoulder
    canonical_statement: >
      People cannot lie on the affected shoulder without triggering pain,
      forcing them to change position or avoid their preferred sleeping side.
    basis: observed
    aliases:
      - Cannot sleep on that side
      - Side sleeping hurts
    supporting_evidence_count: 94
    unique_thread_count: 51
    unique_author_count: 72
    score: 92
    confidence: high
    evidence_ids:
      - ev_000123
      - ev_000184
    boundary_note: >
      Includes inability to tolerate pressure on the affected shoulder.
      Excludes general insomnia without a shoulder-related cause.
```

## Audit Output

Filename:

```text
{segment_slug}_pain_points_audit.jsonl
```

The audit must record material processing decisions, including:

- candidate accepted
- candidate rejected
- candidates merged
- concept split
- stable ID reused
- new stable ID created
- old ID retired
- evidence excluded
- inference retained
- ambiguity warning
- validation failure

Example:

```json
{"event":"concept_merge","surviving_concept_id":"pp_rotator_cuff_000001","retired_concept_ids":["pp_rotator_cuff_000017"],"reason":"Same underlying problem and practical consequence","evidence_ids":["ev_000123","ev_000184"]}
```

## Required Output Fields

### File-level

- segment ID
- segment name
- segment slug
- source evidence filename
- evidence items reviewed
- retained concept count
- observed concept count
- inferred concept count
- skill ID
- skill version
- ranking method

### Pain-point level

- stable `concept_id`
- rank
- canonical name
- canonical statement
- basis: `observed` or `inferred`
- why it matters
- supporting evidence count
- unique thread count
- unique author count where available
- score
- confidence
- aliases
- representative quotations
- supporting evidence IDs
- boundary note

## Naming Convention

Required outputs:

```text
{segment_slug}_pain_points.md
{segment_slug}_pain_points.yaml
{segment_slug}_pain_points_audit.jsonl
```

Examples:

```text
rotator_cuff_pain_points.md
rotator_cuff_pain_points.yaml
rotator_cuff_pain_points_audit.jsonl
```

Generic filenames such as `pain_points.md` are prohibited.

## Coalescing Rules

Merge only when:

- the underlying problem is the same
- the practical consequence is substantially the same
- separation does not materially improve commercial actionability
- the resulting label remains specific

Keep separate when:

- situations differ meaningfully
- functional consequences differ
- messaging implications differ
- desired outcomes differ
- product requirements differ
- one concept is a symptom and another a consequence
- one is physical and another emotional
- merging creates a vague umbrella

Shared keywords alone are never sufficient grounds for merging.

## Granularity Test

Before retaining a pain point, ask:

1. Is it more specific than a generic topic?
2. Is it broad enough to recur?
3. Is it narrow enough to support distinct messaging?
4. Does it describe an actual unwanted customer experience?
5. Can it be traced to evidence IDs?
6. Does it remain distinct from other extraction dimensions?

Target level:

```text
Too broad:
Shoulder pain

Useful:
Pain when lifting the arm overhead

Potentially too narrow:
Sharp pain at exactly 73 degrees during one specific repetition
```

Very specific subthemes may be retained where evidence volume and commercial relevance justify them.

## Advice, Products and Treatments

Advice and product discussions may contain valid pain evidence.

Evidence:

> “A wedge pillow helped because lying flat made the shoulder ache all night.”

Extract:

```text
Shoulder pain worsens when lying flat
```

Do not extract:

```text
Needs a wedge pillow
```

Evidence:

> “Physical therapy helped mobility, but I still get pain at night.”

Extract:

```text
Night pain persists despite improved mobility
```

The treatment failure belongs to the failed-solutions skill.

## Emotional Language

Emotional language may signal intensity, but the underlying problem must remain concrete.

Evidence:

> “It’s maddening that I still can’t put on a jacket without help.”

Pain point:

```text
Difficulty dressing independently
```

Emotional state:

```text
Frustrated
```

Only the pain point belongs in this output.

## Medical Language

Preserve reported diagnoses and customer terminology without independently diagnosing.

Allowed:

```text
People reporting diagnosed rotator-cuff tears frequently describe weakness when lifting.
```

Not allowed:

```text
Weakness proves the person has a rotator-cuff tear.
```

## Exclusions

Exclude:

- generic agreement such as “same”
- unsupported assumptions
- moderator or platform text
- standalone recommendations without experienced problems
- abstract medical information without personal experience
- desired outcomes
- standalone emotional states
- beliefs
- failed solutions
- isolated keywords
- evidence outside the assigned segment
- external knowledge
- duplicate concepts
- concepts with insufficient evidence that cannot be safely marked low-confidence

## Validation Rules

Fail the run when:

- input is not a canonical segment evidence file
- segment slug cannot be determined
- output filenames are incorrect
- a retained concept has no stable `concept_id`
- a retained concept has no supporting evidence ID
- a cited evidence ID is absent from the input
- duplicate evidence inflates counts
- external evidence is introduced
- source text is fabricated
- a paraphrase is presented as verbatim
- content from another segment is introduced
- another extraction output is used as source evidence
- dimensions are mixed
- a final segment report is generated

Warn when:

- more than 30% of concepts are low confidence
- inferred concepts exceed the configured maximum
- one concept contains more than 40% of all evidence
- two concepts share more than 70% of supporting evidence
- one concept is supported by a single evidence item
- most support comes from one thread
- many concepts are vague umbrellas
- a previously stable concept changes materially
- an ID merge or split requires human review

## Audit Checklist

### Source integrity

- every evidence ID exists
- every quotation preserves source meaning
- no external evidence was used
- the complete input file was reviewed

### Concept integrity

- every concept has a stable ID
- synonymous concepts were merged
- actionable distinctions were preserved
- aliases were retained
- merged and retired IDs were audited
- inferred concepts are labelled

### Counting integrity

- counts use unique evidence IDs
- thread counts use unique thread IDs
- duplicate excerpts do not inflate counts
- evidence from other segments is absent

### Naming integrity

- segment slug is unchanged
- all outputs use the segment-prefixed naming convention

## Determinism

Given identical:

- segment evidence
- configuration
- prior concept registry
- scoring weights
- skill version

the skill must produce identical:

- accepted concepts
- canonical labels
- stable IDs
- merge decisions
- counts
- scores
- rankings
- output filenames

## Completion Criteria

The skill is complete when:

- the entire segment evidence file has been reviewed
- every retained pain point is evidence-grounded
- observed and inferred concepts are separated
- every concept has a stable ID
- synonyms are coalesced
- meaningful distinctions remain separate
- every concept cites evidence IDs
- counts are deduplicated
- ranking is deterministic
- all three canonical outputs are produced
- no other extraction dimension is produced
