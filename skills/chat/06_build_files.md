# 06 · Build one file per audience

Pure assembly. No judgement left — you're joining, grouping, sorting and writing.
Do the whole thing in code; there is nothing here to think about.

Attach the assignments from step 05 and the deduplicated evidence.

## The rule the rest of the work rests on

**One comment goes in exactly one file, or none. Never two.**

Attributes, journey states, contexts and stray keywords do not create a second
membership. If someone is a desk worker who mentions sleep, their comment lives in
the desk-worker file only. The sleep angle gets extracted later as a pain point
*inside* that segment.

## GATE — no double membership. Run this in code.

Before writing anything, check that no comment ID appears under two segments. If one
does, stop and show me — don't write the files. That check failing means step 05
broke its own rule, and everything downstream would inherit it.

Also confirm: total items across all files + unassigned = total deduplicated
evidence. If those don't add up, something was lost or invented. Print the sum.

## Build each file

Only for segments marked `validated`. Skip merged, rejected, split and
more-research ones.

Sort deterministically: score down, then margin down, then comment ID up. Same
inputs, same order, every time.

Each file opens with the segment name, its definition, who's in and who's out, the
item count, the number of separate conversations, and a tally of which attributes
and journey states appear inside it (as a share of *this file* — those are never
audience sizes).

Then every comment, verbatim: its ID, source type, title, URL, thread, the score and
margin that put it here, which cues fired, its tags, and the original text unaltered.

Everything unassigned goes in one separate file with its reason. Never into a
segment file.

## What good looks like

Reading any file, I can answer: which audience is this, why was this comment put
here, what did they actually say, where did it come from, and can I trace it back.

And to *"has this comment appeared in another file?"* the answer is always **no**.

## Give me at the end

One file per validated segment, one unassigned file, and a summary table: segment,
item count, thread count, plus the totals reconciling to the input.
