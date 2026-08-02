# PICC Card — {{segment}}

**ONE segment, ONE awareness stage.** Every field is pulled from the evidence file via
its mapped skill. No field is filled from memory, from the product sheet, or from
another segment.

> **The rule that governs this card:** research dimensions are **SELECTORS, not copy**.
> Nine of these fields never appear as words in the ad — they decide how the visible
> parts get built. Only five things become visible copy: headline, support line, proof
> element, CTA, visual idea. If a dimension is showing up as literal words in the
> headline, put it back as a selector.

---

## Card

| Field | Skill | Value |
|---|---|---|
| Segment | 03/05 | |
| Avatar | 06 | |
| Awareness stage | — | problem-aware *(default for cold Meta static)* |
| Traffic temperature | — | cold |
| Pain | 07 | |
| Pain moment | 08 | |
| Emotional state | 10 | |
| Limiting belief | 13 | |
| Assumed solution | 15 | |
| Solution doubt | 18 | |
| Mechanism reframe | 19 | |
| **Primary buying barrier** | **27** | |
| Driver | 11 / 16 | |
| Bias | — | |
| Primary angle | — | |
| Communication style | — | |
| Representative VOC phrase | 24 | *(verbatim — must appear in the evidence file exactly)* |
| Proof | 20 | |
| Objection handled | 18 | |
| Hook direction | 25 / 26 | |
| CTA | — | |
| Destination | — | |

---

## Selector wiring — how each dimension becomes an ad

| Dimension | Selects |
|---|---|
| pain (07/08) | the hook + the visual |
| desired outcome (09) | the promise / after-state language |
| failed solution (14) | the **angle** + the contrast copy |
| objection (18) | the proof element + the reassurance line |
| emotional state (10) | the tone + the entry point |
| undercurrent (10/11) | the subtext, the "this is me" resonance |
| driver (11/16) | urgency / why-now |
| bias | proof style + ordering + presentation |
| segment | which ad exists at all |

---

## Step 2 — Five angles

An angle = which truth from the research the ad leads with. Strategic messages, not copy.
Usual families: pain-led · failed-solution · desired-outcome · mechanism · objection-busting.

| # | Family | Angle (one line) |
|---|---|---|
| A1 | | |
| A2 | | |
| A3 | | |
| A4 | | |
| A5 | | |

---

## Compliance note for this segment

This evidence file is dense with medical framing (discs, nerves, cervical spine,
forward-head-posture) because that is how sufferers talk. **None of it becomes a
Montisella claim.** Mechanism (19) and proof (20) are where the overclaim risk
concentrates — deploy the *felt* mechanism, not the medical one.

If the primary barrier can only be answered with a claim that cannot be substantiated,
**flag it** — that is a signal the angle is wrong, not a licence to overclaim.

Run `python3 pipeline/qa.py` on the concepts before anything is called done.
