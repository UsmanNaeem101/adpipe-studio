# Canonical Segment VOC Signal Extraction

You are reading one bounded chunk of evidence already assigned to one canonical
commercial audience. Extract recurring or decision-useful VOC signals without
inventing a new audience or changing the Stage-05 primary assignment.

Extract these dimensions independently:

- `pain_points`
- `desired_outcomes`
- `failed_solutions`
- `triggers`
- `beliefs`
- `emotional_states`
- `objections`
- `buying_triggers`
- `representative_language`

For each signal, give a concise semantic label and cite only the exact evidence IDs
that genuinely support it. One evidence item may support several signals. Not every
item must support a recurring signal. Beliefs and causal theories are customer VOC,
not medical facts.

`representative_quotes` must be short verbatim substrings of the cited evidence;
preserve original wording and do not clean up grammar. For failed solutions, use
`typical_outcome` and `common_complaint` when the evidence states them; otherwise use
an empty string. These fields should be empty for dimensions where they do not
apply.

Do not estimate frequency, merge across unseen chunks, create segment IDs, cite an
ID outside this chunk, or force keyword mentions into semantic support. Code will
deduplicate IDs, calculate exact counts, and perform the segment-wide coalescing.
Return only the requested structured output.
