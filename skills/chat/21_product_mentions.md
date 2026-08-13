# 21 · Find the products people name

Pull out every product, supplement, device, treatment or brand people mention —
and, more importantly, **what happened when they used it.**

Attach the Stage 01 kept file. You don't need segments for this: people name
products regardless of which audience they're in, so this runs on the whole corpus
whenever you like. It also runs on a single segment's file later, unchanged.

Work in batches of a few hundred items, keep a running list, then merge at the end.

## The relationship is the whole point

A bare list of product names is nearly worthless. These are three completely
different pieces of information:

```
"I use X"                 they own it, it's in their life
"someone told me to try X"  a recommendation, not a purchase
"X did nothing for me"      a paid-for disappointment
```

So every mention gets one of these:

```
using        currently has it and still uses it
tried        used it in the past
failed       tried it, it didn't work        <- the most commercially useful one
considered   looking at it, hasn't bought
recommended  they're telling someone else to get it
rejected     looked at it and said no, with a reason
```

Worked example, from one real comment:

> *"I have been taking magnesium glycinate for a few months with no help, will try
> electrolytes"*

```
magnesium glycinate   failed      negative   sustained trial, "a few months", no relief
electrolytes          considered  neutral    switching to it after that failure
```

One sentence, two products, two different relationships, and a visible journey from
one to the next. That's the shape to look for.

## Keep the specifics

Brand and model where they're there — "Tempur Original", not "memory foam pillow".
Dose, size, duration, price when mentioned. Specificity is what makes a mention
something you can act on.

Merge obvious spelling variants onto one entry ("tempurpedic" / "tempur pedic" /
"tempur") but keep the surface forms so I can see how people actually type it — that
wording is ad copy.

**Don't merge things that are positioned differently.** A £15 wedge and a £90
cervical contour are not one "pillow". Different price, different buyer, different ad.

## Don't

- Don't count a generic activity as a product. "Stretching helps" is not a product.
  "A Theragun" is.
- Don't turn a recommendation into ownership. "My physio said try a cervical pillow"
  is `recommended`, not `using`.
- Don't invent an outcome the comment doesn't state. If they say they bought it and
  never say whether it worked, the outcome is blank.
- Don't bring in what you know about these products from outside the corpus.

## GATE — distinct people. Run this in code, not by eye.

Count **distinct authors** per product, not mentions. Ten comments from one thread
is often one person replying to themselves.

```
report separately if  >= 3 distinct people
pool into "single mentions"  otherwise
```

Print how many products cleared the bar and how many went into the pooled list.
Keep the pooled list — a supplement two people swear by is a lead, just not a
finding yet.

## Give me

A table, most-mentioned first:

```
product | category | distinct people | using/tried/failed/considered/recommended/rejected
        | sentiment | what they say it did (or didn't do)
```

Then for the top 20, two or three verbatim quotes each from *different* people,
with the relationship marked on each.

Then three lists, because these are the ones worth money:

1. **Failed** — most people, sorted by how many. What they bought that didn't work.
2. **Considered but not bought** — live intent, and what's stopping them.
3. **Rejected with a reason** — the objection is stated out loud in these.

And the counts: items scanned, products found, products above the gate, pooled.
Save the Python you used as a .txt I can download and re-run.
