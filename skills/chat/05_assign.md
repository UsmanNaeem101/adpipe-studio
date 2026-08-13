# 05 · Give every comment one home

Every comment gets exactly one segment, or none. This is what stops the same person
being counted in six audiences and every number being wrong.

Attach the validated segments and the deduplicated evidence. Work in batches and
return every comment ID exactly once.

## One comment, one segment

Pick the segment the comment is actually *about* — its dominant context, not its
keywords.

> *"I'm an accountant and my shoulder kills from sitting all day, and it wrecks my
> sleep."*

That's a desk worker. The sleep bit is an attribute of this person, recorded
separately. It does **not** also put them in side sleepers.

**Unassigned is a correct answer.** Forcing an ambiguous comment somewhere to avoid
a blank is worse than leaving it out.

## Score the fit

```
says outright which group they're in .......... 6
the whole comment sits in that context ........ 6
a problem specific to that segment ............ 4
a constraint specific to that segment ......... 3
a failed solution specific to that segment .... 3
a passing keyword ............................. 0
```

The top two are worth the bar on their own; nothing else is. Someone who tells you
which group they're in has told you. A problem or a constraint is circumstantial by
comparison and needs a second cue to back it up. A keyword on its own carries
nothing, ever.

## GATE — the assignment bar. Run this in code, not by eye.

```
assign only if  winning score >= 6
          AND   winner beats second place by >= 2
otherwise       unassigned
```

**This is the gate that gets skipped.** On a real run it was stated in the prompt,
agreed to, and then 77% of the assigned evidence was below it — including a bra
return-policy complaint sitting at position one in the biggest segment.

Apply it in code. Print the score distribution and how many rows it removed. If it
removed nothing, it didn't run.

## Also record, for every comment

- **The runner-up segment.** Not a fallback — nothing goes there. It's how we learn
  which audiences genuinely overlap. Two segments that keep coming first and second
  in the same comments are adjacent in the market, whatever the taxonomy says.
- **Any attributes or journey states** from step 04's tag lists. These describe the
  same person; they're not a second membership and they never touch a count.
- The score, the margin, which cues fired, and one line of why.

## Give me at the end

Every comment with its segment (or blank), its score, its margin, its runner-up, its
tags, and its reason. Plus the counts: assigned, unassigned, and the split by reason.
