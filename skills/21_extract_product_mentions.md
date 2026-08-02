# Extract Product Mentions

You are an expert qualitative researcher analysing voice-of-customer (VOC)
comments for one validated customer segment. Your job is to identify the specific
**products, categories, accessories, and treatments** people name — and, crucially,
*their relationship to each one*. This is the solution landscape as customers
actually reference it: what they use, tried, rejected, or recommend.

Work only from the segment definition and the comments; use no outside knowledge.

## What a product mention is

```
product mention = a named product, category, accessory, or treatment
                + the customer's relationship to it (owned / considered /
                  recommended / rejected / failed)
                + sentiment and, where present, brand/model specificity
```

The relationship is the signal. "I use X," "someone told me to try X," and "X did
nothing for me" are three completely different data points — flattening them all to
"X mentioned" throws away most of the value.

## The two rules that make or break this skill

**1. Capture the relationship — don't flatten to a bare mention.** Tag every
mention with how the customer relates to it, because the relationship is what makes
it useful downstream.

```
Owned / using   currently has it                 "I've slept on a [brand] contour for a year"
Considered      looking at it, not bought        "been eyeing the [brand] one"
Recommended     they tell others to try it       "honestly just get a buckwheat pillow"
Rejected        considered and passed            "looked at memory foam but no — sleeps hot"
Failed          tried it, it didn't work         "had the [brand], went flat in a month"  (→ links to failed solution 14)
```

**2. Don't infer ownership from a recommendation, and don't treat a generic
activity as a product.** "My physio said try a cervical pillow" is a recommendation,
not ownership. "Stretching helps" is an activity, not a product. Require the actual
object and the actual relationship.

## Evidence discipline

- **Preserve brand and model specificity** where present ("[brand] Original", not
  just "memory foam pillow") — specificity is what makes a mention actionable, and
  a named competing brand also feeds competitor mentions (22).
- **Don't merge distinct product types when positioning differs.** A £15 wedge and a
  £90 cervical contour are not one "pillow" category; different price/positioning →
  separate concepts.
- **Normalise spelling variants and aliases** onto one concept, but keep the surface
  forms.
- **Record sentiment** (positive / negative / neutral / mixed) as expressed, not
  inferred.
- Keep single-comment mentions at low confidence; don't invent an ownership,
  outcome, or use case the comment doesn't state.

## Merging and splitting

**Merge** spelling variants and aliases of the same product ("tempurpedic" /
"tempur pedic" / "tempur" → one concept). **Split** on distinct product types or
distinct positioning even within a category. Preserve brand/model detail through
any merge.

## Output

Return the retained product mentions, most prevalent first. For each:

- **Name** — product / category, with brand and model where present
- **Category** — the product type
- **Relationship** — owned/using, considered, recommended, rejected, or failed
- **Sentiment** — positive, negative, neutral, or mixed
- **Use case / outcome** — the context and the result, where stated
- **Prevalence** — roughly how many different people mention it
- **Representative quotes** — 2–4 verbatim quotes from different people
- **Related failed solution / competitor** — linked concept, if any
- **Basis** — stated or implied
- **Extraction confidence** — high / medium / low

Include a mention only if it names an actual product/category (not a generic
activity) and its relationship is preserved without inferring ownership from a
recommendation.

## Quick reference

```
❌ "stretching helps" (activity, not a product)   ✅ Buckwheat pillow — recommended, positive
❌ "physio said try one" → logged as owned          ✅ Cervical pillow — recommended (not owned)
❌ merge a £15 wedge and a £90 contour into         ✅ keep them as distinct products (positioning differs)
   "pillows"
```
