# Reference library — 221 ads

Sorted copy of `~/Desktop/Ad Templates` (the flat 221-file original). Same images,
grouped by **communication style**, which is the axis step 3 crosses against angles and
step 5 maps to a layout.

## The rule

These are **layout grammars, not creative to copy.** Deconstruct a reference into its
zones — hook zone · hero zone · proof chip · CTA — and reuse the *structure*. Never
reproduce a competitor's headline, claim, offer, or art direction. Never compare against
a named brand in Montisella copy; compare against a category.

## Categories → starting template

| Folder | n | Start from | Framework |
|---|---|---|---|
| `01_Comparison` | 42 | `split_screen_comparison` | Comparison → Advantage → CTA |
| `02_Demonstration` | 1 | `problem_mechanism_benefit` | Problem → Mechanism → Benefit |
| `03_Feature_Breakdown` | 60 | `problem_mechanism_benefit` | Claim → Proof |
| `04_Educational` | 15 | `problem_mechanism_benefit` | Problem → Mechanism → Benefit |
| `05_Story_Testimonial` | 52 | `ugc_quote_card` | Claim → Proof |
| `06_Problem_Solution` | 11 | `double_hook_proof_chip` | Pain → Promise |
| `07_Product_Hero` | 16 | `double_hook_proof_chip` | Pain → Promise |
| `08_Lifestyle` | 4 | `before_after_night` | Before → After |
| `09_Offer` | 16 | `objection_killer_tile` | Objection → Reframe |
| `10_Other` | 4 | — | uncategorised |

Feature_Breakdown (60) and Comparison (42) dominate the library, which matches where this
segment's barriers sit: burned-before buyers want the mechanism shown and the alternative
ruled out.

## Adding a layout

If a reference has a grammar none of the six templates covers, add a new file to
`pipeline/templates/`. Rules: read brand tokens only (never hard-code colour or type),
put `data-slot="<name>"` on every text container so the clipping check can measure it,
and cap each slot with a `max-height` so overflow is detectable rather than silent.
