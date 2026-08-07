# Production Brief — side_sleepers_night_pain

10 concepts rendered at 1080×1350 (4:5). **All 10 are shippable as they stand** —
they are typographic/colour-block ads and need no photography. Plates are an
upgrade, not a dependency. See §Plates for how to add them.

Primary barrier being worked: **selection — "I can't tell which one is right for
me before I buy it."** Every concept resolves to the same click: *which height are
you?*

---

## The four strongest

### C02 — "Too low squishes your shoulder. Too high bends your neck."
**Angle** A2 failed-solution · **Layout** `double_hook_proof_chip` · **Framework** Mistake → Fix

The strongest of the ten. It states the segment's own dilemma almost verbatim
([2]) and then does the one thing no competitor ad does — asserts there is a
correct answer and that it is *theirs*, not the brand's. Ties directly to barrier
#1.

| Zone | Copy |
|---|---|
| Eyebrow | Side sleepers |
| Hook | Too low squishes your shoulder. **Too high bends your neck.** |
| Headline | There is a height between those. |
| Subhead | It's set by your shoulder, not by how the pillow feels in the shop. |
| Proof chip | Pick before you buy |
| CTA | Find your height |

**Visual direction:** none required — the type is the ad. With a plate: a single
pillow shot side-on at eye level against a plain wall, so the *height* of the
pillow is the subject of the photograph.
**Destination:** fit guide → variant PDP.

---

### C01 — "You didn't buy bad pillows. You bought the wrong height."
**Angle** A2 failed-solution · **Layout** `split_screen_comparison` · **Framework** Comparison → Advantage → CTA

Reframes years of failed purchases as one fixable mistake, which removes the
self-blame without blaming a competitor. Compares against the *category's shopping
axis* (soft/firm), not a named brand.

| Zone | Copy |
|---|---|
| Eyebrow | If you've bought more than one |
| Hook | You didn't buy bad pillows. You bought the wrong **height**. |
| Left | *What you shopped on* — Soft or firm. Then hope. Then another one in six months. |
| Right | *What actually decides it* — How far your shoulder holds your head off the mattress. |
| CTA | Which height are you? |

---

### C05 — "Soft or firm is the wrong question."
**Angle** A4 mechanism · **Layout** `problem_mechanism_benefit` · **Framework** Problem → Mechanism → Benefit

The teaching ad. Highest overclaim risk of the set and the copy is deliberately
mechanical: it explains a *geometry* (shoulder width sets a gap), never a
physiological effect. Nothing here says the pillow does anything to the body.

| Step | Copy |
|---|---|
| 1 | **Your shoulder sets the gap** — On your side, your head sits as far off the mattress as your shoulder is wide. |
| 2 | **The pillow fills it or it doesn't** — Too little and your head drops. Too much and your neck bends the other way. |
| 3 | **Firmness is a separate question** — A soft one at the right height beats a firm one at the wrong height. |

**Proof chip:** Matched, not guessed · **CTA:** Which height are you?

---

### C09 — "Straight through, or four times over."
**Angle** A3 desired-outcome · **Layout** `before_after_night` · **Framework** Before → After

The only concept that sells the night rather than the purchase. Both states are
subjective and behavioural — what they *do* — so nothing reads as a treatment
claim. This is the one that most rewards real photography.

| Zone | Copy |
|---|---|
| Hook | Straight through, or **four times over**. |
| Before — *Wrong height* | Roll over. Rearrange it. Fold it under. Try the other side. Again. |
| After — *Right height* | Roll over. Stay asleep. |
| CTA | Find your height |

---

## The other six

| ID | Angle | Layout | Note |
|---|---|---|---|
| C03 | A1 pain-led | `double_hook_proof_chip` | "The sore shoulder was decided at midnight." Strongest pure-pain hook. |
| C04 | A1 pain-led | `before_after_night` | Morning-focused sibling of C09. Run one or the other, not both. |
| C06 | A4 mechanism | `problem_mechanism_benefit` | Durability angle. **Depends on latex-vs-foam being accurate — confirm before running.** |
| C07 | A5 objection | `objection_killer_tile` | **Weakened deliberately** — see Blocked below. |
| C08 | A5 objection | `split_screen_comparison` | Guess vs measure. Cleanest statement of the barrier. |
| C10 | A3 / VOC entry | `ugc_quote_card` | **Read the note below before running this one.** |

---

## Blocked on your input

**1. C07 has no risk reversal.** The layout is built for trial/returns/warranty
chips and I could only fill one, because trial length, returns policy and warranty
are all `NEEDS INPUT` in `facts.json`. As it stands C07 is the weakest of the ten.
Fill those three facts and it becomes one of the strongest.

**2. No concept carries a number.** No trial length, no warranty, no density, no
price, no "9 out of 10". That is deliberate — `qa.py` fails any unsubstantiated
figure, and nothing is substantiated yet. The proof chips currently say
qualitative things ("Pick before you buy"). Once you publish the variant heights
in mm, the proof chips should carry them — that is the literal answer to the
primary barrier.

**3. C06 asserts latex springs back where foam softens.** Directionally true for
the material class, but it is a durability claim about your product. Confirm it
holds for your actual SKU before spending on it.

**4. C10 uses a real person's public words.** The quote is verbatim from a public
Reddit comment, and it is the best single articulation of the problem in all 636
items. Two cautions: it is **not** a customer review — I have not put stars on it
and the attribution says so plainly — and the thread is in a health-related
subreddit I have deliberately not named, because naming it would expose the
poster's health context in a paid ad. **My recommendation: run C10 only until you
have a real customer saying the same thing, then swap it.** The quote is doing a
job a testimonial should do.

---

## Plates

Every ad renders complete without images. To upgrade, generate a plate, drop it in
`plates/`, add the filename to the concept's `image` slot in `concepts.json`, and
re-render — the templates switch out of no-plate mode automatically.

**Every prompt below produces a TEXT-FREE image.** Image models cannot spell;
all copy is composited by `render.py`. Any lettering in a plate is a defect.

| Concept | Slot | Prompt |
|---|---|---|
| C01 | `left_image` | A messy stack of two mismatched bed pillows on a plain white sheet, shot square-on at mattress level, soft overcast window light from the left, muted warm neutral tones, shallow depth of field, editorial product photography, absolutely no text or lettering anywhere |
| C01 | `right_image` | A single smooth latex pillow lying flat on a plain white sheet, shot square-on at mattress level, same soft overcast light, calm cool green-grey tones, crisp and uncluttered, editorial product photography, absolutely no text or lettering anywhere |
| C02 | `image` | A single ergonomic latex pillow photographed side-on at eye level against a plain warm off-white wall, the pillow's height clearly readable as a silhouette, soft directional daylight, minimal editorial still life, no text, no words, no labels |
| C03 | `image` | Close overhead view of an empty rumpled bed at dawn, the impression of a head still visible in the pillow, low blue morning light through a window, quiet and still, muted desaturated colour, cinematic, no people, no text or lettering |
| C04 | `before_image` | Dark bedroom before sunrise, a person's silhouette sitting on the edge of the bed with shoulders hunched, seen from behind, deep shadow, single cool window light, moody cinematic photography, no text |
| C04 | `after_image` | Same bedroom in warm early daylight, bed empty and neatly thrown back, curtains open, calm and bright, warm green-neutral palette, cinematic photography, no people, no text |
| C05 | `image` | Studio macro of a latex pillow cross-section on a seamless pale background, open-cell structure and airflow channels clearly visible, crisp even lighting, technical product photography, no text, no annotations, no callouts, no lettering |
| C06 | `image` | Two pillow cross-sections side by side on a seamless pale background, one visibly compressed and sagging, one holding a clean even shape, crisp even studio lighting, technical product photography, no text, no labels, no arrows |
| C07 | `image` | A latex pillow still in its unopened packaging resting on a bed, natural window light, calm neutral palette, honest unstyled product photography, no text, no branding, no logos, no lettering |
| C08 | `left_image` | Several different pillows piled untidily in a bedroom corner, warm dim light, slightly cluttered domestic scene, muted tones, documentary photography, no text |
| C08 | `right_image` | One latex pillow placed precisely centred on a made bed, clean symmetrical composition, calm cool daylight, minimal and ordered, editorial photography, no text |
| C09 | `before_image` | Night-time bedroom seen from above, bedding twisted and disordered, pillow folded in half under a sleeper, deep shadow with a sliver of moonlight, cinematic, no faces, no text |
| C09 | `after_image` | Night-time bedroom seen from above, bedding smooth and undisturbed, a single pillow holding its shape, soft moonlight, calm and still, cinematic, no faces, no text |
| C10 | `image` | Soft-focus bedroom background at dusk, out-of-focus bed and warm lamp light, very low contrast so overlaid text stays readable, muted neutral palette, no people, no text or lettering |

`render.py` crops plates with `object-fit: cover`, so generate **portrait**
(1024×1536 is the right shape for 4:5) and don't worry about exact dimensions.

---

## QA — this batch

| Check | Status |
|---|---|
| Headline readable at thumbnail | ✅ all 10, clipping check passes with 0 warnings |
| Correct product | ✅ latex ergonomic pillow throughout |
| No hallucinated quotes | ✅ the one verbatim quote (C10) machine-verified against the evidence file |
| No hallucinated stats | ✅ zero numbers in the entire set |
| Mechanism accurate to evidence | ⚠️ C06's latex/foam claim needs your confirmation |
| No unsupported medical claim | ✅ `qa.py` 0 fail, 0 warn |
| Brand styling | ⚠️ `brand.json` is still placeholder colours — replace before spending |
| Template followed | ✅ all 10 map to a real layout |
| Every visible line traceable | ✅ to a comment or to the felt-experience register |

**Two things to fix before you spend money:** the placeholder brand palette in
`pipeline/brand.json`, and the empty `pipeline/facts.json`. Neither blocks a test
budget; both will cap performance.
