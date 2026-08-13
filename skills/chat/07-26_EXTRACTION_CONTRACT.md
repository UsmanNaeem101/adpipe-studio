# The extraction contract

Paste this **above** whichever dimension skill you're running. It's the part every
extraction shares, so it's written once here instead of twenty times.

---

You're reading one segment's evidence file and pulling out exactly one dimension.
Not two. Whatever else is in there, you ignore.

## Read the whole file first

All of it, before you write anything. If it's too long for one pass, work in
batches, keep a running list, and merge at the end — and tell me the batch
boundaries so I know nothing was skipped.

## Work only from what's in the file

No outside knowledge. If you know something about this product category, this
condition, or these brands that isn't in the comments, it doesn't go in. You are
reporting what these people said, not what is true.

Never correct someone. A customer with a wrong theory about their own anatomy is
giving you a belief, and the belief is the data. Record it as held.

## Every item needs evidence

Each thing you report carries the comment IDs that support it. If you can't cite
it, you can't report it. Don't round a number up, don't estimate, don't say
"many" when you mean four.

Mark each item as **observed** (they said it) or **inferred** (you're reading
between the lines). Inferred items need a visible pattern behind them, not a
plausible story.

## Quotes stay verbatim

Copy them exactly. Typos, swearing, bad grammar, all of it. The moment you tidy a
quote it stops being evidence and becomes your writing. If a quote needs trimming
for length, cut from the ends and use an ellipsis — never rewrite the middle.

## GATE — recurrence. Run this in code, not by eye.

```
report separately         >= 3 distinct people
pool into "single mentions"   fewer
```

Count **people**, not comments. One person posting five replies in a thread is one
person. Print how many items cleared the bar and how many got pooled — and keep the
pooled list, because a thin signal is still a lead.

## If you're reading a Layer 2 research pack

Some segment files have a `BORROWED CONTEXT` section from related segments, each
item labelled with where it came from. If yours does:

- Every count is of **primary** evidence only.
- Borrowed context can inform your wording and your reading of a thin signal.
- If a finding rests on borrowed context, say so and give the primary count
  separately. Never add the two together.
- A finding that appears *only* in borrowed context is not this segment's finding.
  Mark it as context or leave it out.

## Never chain extractions

Read the evidence file. Not another extraction's output. If pain points were
extracted yesterday, you still read the evidence — otherwise you're doing analysis
of analysis and errors compound silently.

(Two exceptions, and they say so in their own files: 24 representative VOC and 27
buying barriers are *synthesis* steps and read other outputs by design.)

## What to give me

Items ranked most-supported first. For each: a short recognisable name, one
sentence of what it is, how many distinct people, observed or inferred, and two or
three verbatim quotes from *different* people.

Then the pooled single mentions as a plain list.

Then the counts: comments read, items found, items above the gate, items pooled.

Save the Python you used as a .txt I can download and re-run.
