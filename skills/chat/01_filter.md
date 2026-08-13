# 01 · Filter the raw scrape

You're the bouncer. Real customer signal gets in, page furniture and noise don't,
and everyone who gets in keeps their own words.

Attach the raw scrape. Work in batches of a few hundred items so nothing is skipped
— tell me the batch boundaries as you go.

## Never rewrite anything

Keep the customer's exact words. Strip Reddit chrome from *around* a comment, never
touch the comment itself. Misspelled, sweary, badly punctuated evidence is often the
best evidence — the rawness is the asset. If you catch yourself tidying, stop.

## Tag what each kept item is carrying

Give every item you keep one or more of these:

```
first_person_experience    it happened to them
specific_problem           a concrete symptom or problem
specific_context           the situation it happens in
attempted_solution         something they tried
outcome                    what happened next
belief                     their theory of the cause
product_experience         a product or brand they used
competitor_experience      a rival product
objection                  a hesitation or doubt
buying_trigger             what made them start looking
buying_criterion           what they need it to do
desired_proof              what would convince them
comparison                 weighing two options
workaround                 a hack or improvised fix
emotional_signal           a real feeling tied to the problem
customer_terminology       their own words for it
third_person_observation   they're describing someone else
```

## GATE — corroboration. Run this in code, not by eye.

```
keep if  first_person_experience is present AND total tags >= 2
keep if  total tags >= 3
drop otherwise
```

Two ways through, because they fail differently. A first-hand account is the
strongest thing in the corpus, so it only needs one other tag to show it's about
something. Everything else — advice, commentary, observation about a friend — is a
report, and a report needs three independent signals before it earns a researcher's
attention.

This is the single highest-value filter in the whole process. On a real corpus,
items kept on one tag alone were usable **1.2%** of the time; items with four or
more were usable **49%** of the time.

Print the before/after count.

## Bin outright

Anything whose *only* content is: page chrome · bot or AutoModerator text · empty
or broken capture · "same" / "this" / generic agreement · thanks · emoji reaction ·
an insult with nothing in it · a joke with nothing in it · unrelated chat · spam or
affiliate links · a bare URL · a quote adding nothing new.

Note the boundary: a joke or an insult that *also* carries real signal stays. You're
rejecting absence of signal, not tone.

## Exact duplicates only

Same text twice → keep one copy, prefer the one with better metadata. Two different
people describing the same experience in their own words are **not** duplicates.
Near-duplicates are step 02's job.

## Don't

Paraphrase · summarise · assign segments (they don't exist yet) · extract pain
points · reject someone for being emotional, informal or wrong about anatomy — a
mistaken folk theory is still evidence · drop minority or contradictory experiences.

## Give me at the end

The kept evidence with its tags, the rejected items with a reason each, and the
counts: raw in, kept, dropped, and how many died at the corroboration gate.
