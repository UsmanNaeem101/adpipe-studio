# 07 Extract Pain Points

**Skill ID:** `07_extract_pain_points`  
**Version:** `2.3.0`  
**Status:** Canonical

## Purpose

Extract, normalise, coalesce, count, rank and audit the recurring unwanted states, limitations, frustrations, costs, fears and trade-offs experienced within one validated segment.

This skill receives one canonical segment evidence file:

```text
evidence/{segment_slug}.txt
```

and produces exactly one artefact:

```text
{segment_slug}_pain_points.md
```

Example:

```text
evidence/rotator_cuff.txt
        ↓
07_extract_pain_points
        ↓
rotator_cuff_pain_points.md
```

One document, because that is what the harness collects. The audit trail is not
dropped — it lives *inside* the Markdown. Every retained pain point carries its
supporting evidence IDs, its `observed`/`inferred` basis and its deduplicated
counts, and the processing decisions (merges, retires, exclusions) are recorded
in an Extraction Decisions section. A separate machine-readable sidecar would be
easier to parse, but nothing downstream reads one, and a promise nothing collects
on is not an audit trail. Keeping the evidence IDs in the document is what makes
the "never fabricate quotations" rule verifiable by anyone reading it.

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

Exactly one segment file, in one of two layers:

```text
research/segments/packs/{segment_slug}.txt   Layer 2 — the default
evidence/{segment_slug}.txt                  Layer 1 — with --evidence
```

The harness reads this file and injects its contents into the prompt, so the
path is context rather than something to open.

**When the input is a Layer-2 research pack** it contains this segment's
`PRIMARY EVIDENCE` followed by a `BORROWED CONTEXT` section drawn from related
segments — parent, children, siblings, neighbours — with every borrowed item
labelled `BORROWED FROM: {slug} ({relation}, {strength})`. That exists because a
narrow segment's own evidence is often too thin to characterise while directly
relevant language sits one edge away.

It changes what you count, not what you extract:

- **Every count is a count of primary evidence.** `evidence_count`, the
  frequency ranking, the confidence bands — all primary only.
- Borrowed context may inform a pain point's label, its wording, and how you
  interpret a thin primary signal.
- Where a pain point leans on borrowed context, say so and report its primary
  count separately. Never add the two.
- A pain point appearing **only** in borrowed context is not this segment's pain
  point. Record it under `Inferences retained` as borrowed context, naming the
  segment it came from, or leave it out.

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

Use only evidence from the current segment file, and count only its primary
evidence.

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

### 12. Record the processing decisions

Every material decision made in steps 6, 7 and 8 — candidates merged, a concept
split, a stable ID reused, created or retired, evidence excluded, an inference
retained, an ambiguity warning, a validation failure — is written into the
Extraction Decisions section of the output document. A merge that is not
recorded is indistinguishable from a pain point that was never extracted.

### 13. Produce the canonical output

Required:

```text
{segment_slug}_pain_points.md
```

## Canonical Markdown Output

```markdown
# Rotator Cuff Pain Points

## Extraction Metadata

- Segment ID: rotator_cuff
- Segment name: People with rotator-cuff, impingement or injury-related shoulder pain
- Source file: evidence/rotator_cuff.txt
- Evidence items reviewed: 614
- Pain points retained: 18
- Observed concepts: 16
- Inferred concepts: 2
- Extraction skill: 07_extract_pain_points
- Skill version: 2.3.0
- Ranking method: score descending, then evidence count descending, then concept ID ascending

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

## Extraction Decisions

Material processing decisions, in the order they were made.

### Merges

| Surviving ID | Retired IDs | Reason | Evidence IDs |
|---|---|---|---|
| pp_rotator_cuff_000001 | pp_rotator_cuff_000017 | Same underlying problem and practical consequence | ev_000123, ev_000184 |

### Splits

| Origin ID | Resulting IDs | Reason |
|---|---|---|
| pp_rotator_cuff_000004 | pp_rotator_cuff_000004, pp_rotator_cuff_000019 | Overhead-reach pain and behind-the-back reach pain have different practical consequences |

### Stable ID continuity

- Reused from prior run: 14
- Newly created: 4
- Retired: 1

### Excluded and rejected

| Item | Decision | Reason |
|---|---|---|
| ev_000455 | Evidence excluded | Duplicate excerpt of ev_000123; would have inflated counts |
| "Shoulder problems" | Candidate rejected | Generic umbrella label, fails the granularity test |

### Inferences retained

| Concept ID | Reason retained | Supporting evidence IDs |
|---|---|---|
| pp_rotator_cuff_000015 | Consequence stated repeatedly without being named directly | ev_000512, ev_000774 |

### Warnings

- pp_rotator_cuff_000015 and pp_rotator_cuff_000016 share 71% of supporting evidence; flagged for human review.
```

## Recording Decisions In-Document

There is no YAML sidecar and no `.jsonl` audit file. Both were dropped in 2.3.0:
the harness collects exactly one Markdown artefact per skill, so a sidecar was
specified but never written, and an audit trail that is never written is worse
than none — it reads as a guarantee while guaranteeing nothing.

Everything they carried is now in the document itself:

- the YAML mirrored the Markdown field for field, so nothing was lost with it
- the audit log's decisions became the **Extraction Decisions** section

Record decisions as tables under that section, not as prose. A merge needs the
surviving ID, the retired IDs, the reason and the evidence IDs; a rejection
needs the item and the reason. Anyone auditing the run should be able to
reconstruct why a pain point exists, why two became one, and why an item was
dropped, using only this file and the segment evidence file it names.

## Required Output Fields

### Document-level

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

### Decision-level

Recorded in the Extraction Decisions section:

- merges: surviving ID, retired IDs, reason, evidence IDs
- splits: origin ID, resulting IDs, reason
- stable ID continuity: reused, newly created, retired counts
- exclusions and rejections: item, decision, reason
- inferences retained: concept ID, reason, supporting evidence IDs
- warnings raised

## Naming Convention

Required output:

```text
{segment_slug}_pain_points.md
```

Example:

```text
rotator_cuff_pain_points.md
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
- the output filename is incorrect
- a merge, split or retirement is missing from Extraction Decisions
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
- the output uses the segment-prefixed naming convention

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
- output filename

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
- the canonical Markdown output is produced
- every merge, split, retirement and exclusion is recorded in Extraction Decisions
- no other extraction dimension is produced
