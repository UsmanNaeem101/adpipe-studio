# 04 · Validate the audiences

Be the sceptic. A candidate earns "validated" — it doesn't get granted it.

Attach the candidates from step 03 with their counts and sample evidence.

## Default to doubt

The failure here is rubber-stamping, and the specific way it happens is validating
on volume alone. A big pile of evidence with no distinct message is not a segment. A
modest pile with a genuinely different audience and a different ad might be.

## Check the pile first

Before anything else: is this an audience at all? Apply step 03's test again, because
things slip through.

**Upstream of the problem or independent of it → audience. A response to it → not.**

Cost-sensitive, brace user, takes painkillers, distrusts anatomy claims — all
responses to the problem or the market. Awaiting an MRI, post-op, deciding on
surgery — journey states. These are not audiences and never become segments.

They are not rejected either. Move them to the attribute or journey list, where they
survive as tags. Reject only what has no substance in any pile.

Doing this now costs nothing. Doing it after you've assigned evidence is impossible
to do cleanly — the comments have a home by then, and taking that home away either
loses them or dumps them into a neighbouring segment and inflates it.

## GATE — floors. Run this in code, not by eye.

```
drop if  unique threads < 4        (fewer conversations than that is a conversation,
                                     not an audience — sixty comments from four
                                     threads is four people and their repliers)
drop if  unique comments < 8
```

Print every candidate's thread and comment count, and which ones died.

Thread diversity is a floor, not a factor. A candidate drawn from very few
conversations cannot be validated no matter how good the other signals look.

## Then judge what's left

Weigh all of it: how much evidence, how many separate conversations, how many
different subs, whether the context holds together, whether the audience is really
distinct, whether their pains are distinct, whether they want something different,
whether they'd need a different message, and whether you could actually target them.

Volume is one input of nine. Never the only one.

Watch for the near-duplicate pair: two candidates describing the same people in
slightly different words. Each looks fine alone, and together they guarantee that
any comment matching both gets thrown out as ambiguous later. Merge them.

## Say which of these each candidate gets

```
validated        recurring independent evidence AND a distinct message
merge            same people as another candidate
split            actually two different audiences in one
attribute        real, but a response to the problem — becomes a tag
journey          real, but a point in treatment — becomes a tag
reject           weak, incidental, or no commercial distinction
more research    promising, not enough evidence yet
```

Every candidate gets exactly one, and every one gets a written reason. Nothing
disappears silently — a rejected candidate stays on the list with its reason.

## Then tell me how they relate

Two kinds, and they're different:

```
X sits inside Y     every swimmer with a cuff tear is someone with a cuff tear
X sits beside Y     desk workers and side sleepers — loads of people are both,
                    but neither contains the other
```

Only say "inside" when the containment is genuinely total. Desk workers and side
sleepers are the case people get wrong: inventing a parent group to hold them would
name an audience nobody would ever target. That's "beside".

Say how strong each relationship is — strong, moderate or weak. Don't give me a
number; you can't calibrate one and neither can I.

Not everything needs a relationship. A wrong parent is worse than none.
