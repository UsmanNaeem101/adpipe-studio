# Discover Customer Segments — Orchestration Contract

Stage 03 discovers commercially distinct audiences without requiring the full VOC
corpus in one model context.

Run the fixed sequence:

1. `03a_discover_segment_candidates` harvests high-recall provisional audiences
   from deterministic Core-evidence chunks.
2. Code aggregates exact candidate keys and preserves chunk/evidence lineage.
3. `03b_consolidate_segment_candidates` builds the global candidate map from the
   compact catalogue.
4. Targeted classification expands Core, Supporting, and Context evidence against
   that map; code computes every count and source-diversity metric.
5. `03c_audit_segment_coverage` checks unexplained Core evidence for recurring
   missed audiences. At most one novelty cycle may return to 03B.

A segment is an audience, not an attribute. Similar symptoms in commercially
different contexts may be different audiences. One isolated comment cannot create a
candidate. Stage 03 records discovery confidence only; Stage 04 owns validation.

Persist every intermediate artifact. Preserve evidence IDs from final candidate to
03A chunk to original audit VOC. Identical inputs must produce identical chunking,
unions, counts, representative IDs, and artifact ordering.
