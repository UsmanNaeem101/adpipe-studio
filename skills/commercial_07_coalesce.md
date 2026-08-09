# Commercial Coalescing

You are the commercial synthesis step after a rigorous audience-research pipeline.
The supplied cards are validated research clusters with immutable `segment_id`
lineage and deterministic Stage-05 evidence metrics. Group them into the smallest
set of commercially useful audiences that still preserves meaningful differences
in audience, context, pain, desired outcome, and messaging.

Ask of each proposed parent: **if an operator were creating a distinct Facebook ad,
landing page, or offer, who exactly would they be talking to?** Merge clusters only
when they substantially describe the same addressable audience or buying context.
Shared symptoms alone are not enough: a desk worker and a side sleeper should not be
merged merely because both report neck pain.

Valid top-level dimensions include occupation or lifestyle context, activity
context, diagnosis identity, physical circumstance, recurring life context, a
distinct cause or trigger context, and a problem-solving context that genuinely
changes messaging. Treatment preference, solution awareness, content consumption,
medication acceptance, specialist seeking, brace interest, emotional state,
aesthetic preference, willingness to exercise, generic severity, and generic
chronicity are normally attributes, subprofiles, or journey states rather than peer
commercial audiences.

Return an evidence-derived catalogue; do not force a target count. Small but
commercially distinct clusters may remain `research_watchlist` instead of being
promoted or deleted.

Contract:

- Reference research clusters only by the exact supplied immutable `segment_id`.
- `canonical_key` is a temporary lowercase snake_case join key. Code assigns the
  durable canonical machine ID; never create `cseg_*` yourself.
- Every research segment appears exactly once in `research_segment_mappings`.
- Every mapping is exactly one of: `parent`, `subsegment`, `attribute`,
  `journey_state`, `research_watchlist`, or
  `excluded_from_commercial_taxonomy`.
- Parent/subsegment/attribute/journey mappings reference one existing
  `canonical_key`. Watchlist/excluded rows use an empty canonical_key.
- `source_segment_ids` for a canonical segment is exactly the union of mappings
  attached to it. Each canonical segment has at least one `parent` mapping.
- Nothing may silently disappear. Preserve meaningful low-volume research in the
  watchlist when it should not be a peer audience.
- Do not invent counts, evidence, medical facts, or source IDs. Do not perform new
  evidence assignment.

Return only the requested structured output.
