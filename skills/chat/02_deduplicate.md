# 02 · Remove duplicates

Collapse genuine repeats into one copy. Leave every distinct human voice standing.

Attach the kept evidence from step 01. Run across the whole corpus at once, not per
segment — the same comment gets cross-posted to different subs, and segments don't
exist yet anyway.

## The judgement this whole step rests on

Two records are duplicates when they carry **the same experience from the same
source**. Not when they're about the same thing.

Ten people saying "my pillow went flat in a week" is ten data points. Merging them
would be the worst mistake available here, because it quietly turns your strongest
recurring finding into a single anecdote. Shared topic is never enough. When you're
unsure, don't merge.

## What is actually a duplicate

```
exact           identical text, usually the scraper catching a page twice
quoted          someone quoting another comment and adding nothing of their own
cross-posted    the same person's content in two threads or subs
near-identical  same meaning, no new information, very high similarity
```

## Never merge

Different people describing the same pain. Records that differ in outcome, feeling,
what they tried, what they bought, or what they believe caused it. A comment that
quotes someone but adds their own context.

**If a record carries any unique evidence at all, it survives on its own.**

## GATE — merge threshold. Run this in code, not by eye.

Exact and quoted duplicates: match on normalised text (strip whitespace, HTML
entities, permalink junk, tracking parameters).

Near-identical: require genuine semantic equivalence, not keyword overlap. High word
overlap alone has never justified a merge — "shoulder pain at night" appears in
hundreds of unrelated comments.

Print how many groups you formed and how many records each one absorbed. Then show
me ten near-identical groups in full so I can check you haven't merged two people.

## Picking which copy to keep

Richest metadata → longest text → earliest capture → lowest ID. Link every removed
record to the one that survived, so I can always reconstruct what happened.

## Give me at the end

The deduplicated evidence, the duplicate groups with their members, and the counts:
in, groups formed, records removed, out.
