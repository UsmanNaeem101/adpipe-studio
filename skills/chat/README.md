# Chat skills

The same judgement as `skills/*.md`, written for a chat window instead of a
pipeline. Paste one, attach your file, do the step, keep the output, move on.

## Why these exist

A pipeline skill is two documents fused together: instructions for doing the job,
and acceptance criteria for checking the artefact afterwards. In Studio that works
— code is the thing being checked. In a chat there is no code, so the acceptance
criteria ("produce deterministic outputs", "every item has exactly one segment ID")
are addressed to a reader who cannot act on them. They get read as atmosphere.

So these files keep the judgement and drop the contract. Roughly a fifth the
length, and nothing in them is something a model can't do.

## The one thing that makes or breaks a chat run

Every stage has a **GATE** — a rule that throws things out. Gates written as prose
do not hold. On a real run of these stages in ChatGPT:

- a score >= 6 rule was stated and 77% of the kept evidence was below it
- a "50+ comments per segment" rule was stated and segments shipped with 10
- a corroboration filter was built, ran correctly, and then wasn't plugged in

Nobody was careless. A chat has nothing between one message and the next that
checks what the last message promised.

So every gate below says **run this in code**. Have the model apply it in its code
interpreter and print the before/after counts. A gate you can see the numbers for
is a gate that happened.

## Order

```
01 filter     raw scrape        -> evidence worth reading
02 dedupe     evidence          -> one copy per experience
03 discover   evidence          -> candidate audiences
04 validate   candidates        -> real audiences
05 assign     evidence          -> one audience each
06 build      assignments       -> one file per audience
```

Each step's output is the next step's input. Don't skip 01 — everything
downstream inherits whatever it lets through.
