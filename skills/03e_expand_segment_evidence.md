# Expand Candidate Evidence

Classify each supplied evidence item against the current canonical candidate
catalogue. This is evidence expansion, not primary-segment assignment: an item may
corroborate more than one candidate.

Use `strong` only when the item's dominant audience context clearly fits a candidate;
use `corroborating` for useful enrichment that supports its worldview, mechanism,
solution history, or constraints; otherwise use `none` and return no candidate IDs.
Evidence strength (`core`, `supporting`, `context`) is not membership: judge fit from
the text, while respecting that Supporting and Context evidence cannot create a
candidate by themselves.

Return exactly one result for every input evidence ID. Never invent candidate or
evidence IDs. Return only the supplied structured output.
