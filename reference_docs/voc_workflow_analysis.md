# VOC Prompt Library — Workflow Analysis and Improvement Recommendations

## Executive assessment

The library implements a sensible evidence-first research workflow:

1. Filter raw VOC.
2. Deduplicate retained evidence.
3. Discover candidate segments.
4. Validate the candidates.
5. Assign each evidence item to a primary segment.
6. Build one evidence file per validated segment.
7. Run independent extraction prompts over each segment file.
8. Select representative language and vocabulary.

Its strongest idea is the separation of preprocessing, segmentation, and dimension extraction. That reduces contamination between tasks and keeps most extractors evidence-grounded.

The library is not yet a fully coherent production workflow. Stages 1–6 use explicit file contracts and deterministic rules, while most stages 8–26 read like standalone qualitative-research prompts. They do not consistently declare inputs, stable IDs, output syntax, evidence lineage, scoring rules, or how their results are combined. There is also no final integration stage that turns the extracted dimensions into a usable segment model, hierarchy, messaging system, or research audit.

## Reconstructed workflow

```text
Raw Reddit / VOC corpus
        ↓
01 Filter VOC
        ↓ retained_voc.jsonl
02 Deduplicate VOC
        ↓ deduplicated_voc.jsonl
03 Discover Segments
        ↓ candidate_segments.yaml
04 Validate Segments
        ↓ validated_segments.yaml
05 Assign Primary Segment
        ↓ segment_assignments.jsonl
06 Build Segment Evidence Files
        ↓ one canonical evidence file per segment
        ├── 07 Pain Points
        ├── 08 Pain Moments
        ├── 09 Desired Outcomes
        ├── 10 Emotional States
        ├── 11 Psychological Drivers
        ├── 12 Beliefs
        ├── 13 Limiting Beliefs
        ├── 14 Failed Solutions
        ├── 15 Assumed Solutions
        ├── 16 Buying Triggers
        ├── 17 Buying Criteria
        ├── 18 Objections
        ├── 19 Mechanisms
        ├── 20 Desired Proof
        ├── 21 Product Mentions
        ├── 22 Competitor Mentions
        ├── 23 Offers
        ├── 24 Representative VOC
        ├── 25 Terminology
        └── 26 Slang
```

The extraction files are implicitly parallel, not sequential. Several refer to “related” concepts from other dimensions, but the workflow does not define whether those related outputs are available as inputs or whether the extractor must rediscover the relationship from raw evidence.

## Highest-priority improvements

### 1. Add a workflow manifest

Create `00_workflow_manifest.md` defining:

- canonical file names and numbering
- the exact execution order
- which stages are global and which are per-segment
- required and optional inputs for every stage
- output names and schemas
- stable ID conventions
- retry and failure policy
- version compatibility
- whether extractors run independently or receive prior outputs

This would eliminate the current broken references to nonexistent files such as `04_build_segment_reports`, `05_build_segment_reports`, and `06_build_segment_reports`.

### 2. Replace exclusive segment membership with primary plus relevance links

A single primary segment is useful for non-duplicated prevalence counts, but forbidding evidence from appearing anywhere else destroys valid cross-segment evidence. A comment can strongly inform more than one audience even when one context is dominant.

Recommended model:

- `primary_segment_id`: exactly one or null, used for prevalence and segment totals
- `relevant_segment_ids`: zero or more, used for qualitative retrieval only
- `assignment_strength` per linked segment
- `counting_eligible`: true only for the primary segment

This preserves clean statistics without hiding useful evidence from secondary segment analysis.

### 3. Standardise every extractor contract

Every extraction prompt should include the same sections:

- purpose and non-goals
- required input
- allowed supplemental inputs
- evidence rules
- unit of extraction
- merge/split rules
- stable concept ID rule
- canonical JSON or Markdown output schema
- scoring formula
- confidence definition
- quality checks
- failure conditions
- execution contract

At present, `07_extract_pain_points.md` is much more operationally complete than stages 8–26. Use a shorter version of file 07 as the common template.

### 4. Add a final synthesis and relationship stage

The library extracts dimensions but never connects them. Add stages such as:

- `27_link_concepts.md`: links pain → moment → emotion → belief → attempted solution → objection → proof → outcome
- `28_build_segment_model.md`: produces a coherent segment dossier
- `29_prioritise_opportunities.md`: ranks evidence-backed commercial opportunities
- `30_audit_final_research.md`: checks unsupported claims, duplicate concepts, weak evidence, and contradictions

Without this, the result is a collection of lists rather than a decision-ready customer model.

### 5. Separate extraction confidence from commercial priority

Many prompts combine prevalence, intensity, specificity, and usefulness into optional scoring. Define two independent measures:

- `evidence_confidence`: how certain the extraction is
- `commercial_priority`: how strategically useful or important the concept appears

A rare but severe pain can have high commercial priority and low prevalence. A frequent phrase can have high confidence but little strategic value.

### 6. Make prevalence computable

Replace “roughly how many different people” with explicit fields:

- unique evidence count
- unique author count when reliable
- unique thread count
- source diversity count
- corpus share

Use qualitative bands only as derived labels. Otherwise different models will produce inconsistent prevalence estimates.

### 7. Resolve naming and duplicate-file defects

Current defects:

- both `17_extract_objections.md` and `18_extract_objections.md` exist and are 97.8% similar
- both `19_extract_mechanisms copy.md` and `19_extract_mechanisms.md` exist and are 96.4% similar
- buying criteria and objections both use number 17
- the archive includes `.DS_Store`, `__MACOSX`, and a nested `files.zip`

Canonical numbering should be unique, and packaging artefacts should be removed.

## File-by-file analysis

### 01 — Filter VOC

**Role:** Converts raw scrape material into retained and rejected evidence sets.

**Strengths:** Clear single responsibility; preserves original wording; records rejection reasons; includes audit thresholds; correctly retains short but high-signal comments.

**Issues:** It performs exact duplicate detection even though stage 02 is dedicated to deduplication. “Fail closed on ambiguous records” may discard useful ambiguous evidence. Source-quality and author-type flags are absent.

**Improve:** Let stage 01 mark exact duplicates but leave canonical duplicate resolution to stage 02. Add `content_type`, `speaker_type`, `experience_basis`, `promotion_risk`, `language`, and `quality_flags`. Route ambiguity to a review bucket rather than automatic rejection.

### 02 — Deduplicate VOC

**Role:** Removes copied or repeated evidence while retaining independent experiences.

**Strengths:** Correctly distinguishes shared meaning from duplicate testimony; preserves lineage; warns against merging independent experiences.

**Issues:** “Semantic near-duplicate” is underdefined and can cause aggressive merging. No precise similarity or evidence test is given. Replies quoting parent comments need special handling.

**Improve:** Define duplicate classes separately: exact scrape duplicate, quoted duplicate, cross-post copy, edited repost, and template/spam variant. Require near-duplicates to share distinctive wording or provenance, not merely meaning. Preserve quoted portions and novel reply text separately.

### 03 — Discover Segments

**Role:** Generates candidate audiences from the global corpus.

**Strengths:** Avoids treating simple demographics or symptoms as segments; requires commercial distinctiveness and boundaries.

**Issues:** The output `segment_evidence_map.jsonl` partially resembles assignment despite the prompt saying it does not assign evidence. Minimum evidence thresholds and discovery granularity are vague. The execution contract points to a nonexistent stage.

**Improve:** Clarify that the evidence map contains supporting examples only, not exhaustive membership. Require segment hypotheses to specify differentiating context, JTBD, constraint, and likely messaging difference. Add an explicit “segment versus state versus use case” test.

### 04 — Validate Segments

**Role:** Accepts, merges, splits, rejects, or defers segment candidates.

**Strengths:** Decision statuses and lineage are sensible; evidence volume is not the sole criterion.

**Issues:** The prompt says it cannot discover new segments but permits `Split`, which necessarily creates child candidates. Validation thresholds are not operational. The execution contract again references a nonexistent stage.

**Improve:** Allow bounded child-segment creation during a split, with parent lineage. Define thresholds for evidence diversity, contextual coherence, distinguishability, and actionability. Add a “stable under resampling” check so a segment is not driven by one unusually large thread.

### 05 — Assign Primary Segment

**Role:** Assigns each evidence item to one counting segment.

**Strengths:** Allows unassigned evidence; uses margins; prevents keyword-only classification; keeps secondary attributes separate.

**Issues:** The scoring can exceed thresholds through mechanically added cues even when context is weak. Exclusive assignment causes evidence loss. The score scale has no negative evidence or exclusion-boundary penalty.

**Improve:** Add exclusion penalties, contradiction checks, and explicit boundary matching. Retain one primary counting assignment plus secondary relevance links. Calibrate thresholds on a manually labelled sample rather than assuming 6 and 2 are universally suitable.

### 06 — Build Segment Evidence Files

**Role:** Materialises per-segment evidence files for downstream prompts.

**Strengths:** Excellent lineage rules, deterministic ordering, conflict audit, explicit file anatomy, and clear prohibition against extracting insights here.

**Issues:** At 517 lines it is over-specified for a straightforward join/group task. Markdown evidence files can become huge and expensive for LLMs. The one-file-only rule compounds lost evidence. It does not define chunking for large segments.

**Improve:** Keep JSONL as the canonical source and generate Markdown views only when needed. Add deterministic chunking by token or evidence count, with a segment manifest and overlap-free batches. Distinguish counting membership from retrieval relevance.

### 07 — Extract Pain Points

**Role:** Produces canonical problems from one segment corpus.

**Strengths:** Most complete extractor; strong observed-versus-inferred discipline; substantial examples; explicit input contract and audit logic.

**Issues:** Disproportionately long relative to the rest of the library. Its complexity becomes the de facto standard but is not shared by later prompts. It risks overfitting the model to taxonomy and scoring rather than evidence. Some output fields appear optimized for a software pipeline the wider library does not consistently use.

**Improve:** Distil it into a reusable common extraction core plus pain-specific rules. Keep examples, boundary cases, and output schema; remove repeated restatements. Use the common core in every extractor.

### 08 — Extract Pain Moments

**Role:** Extracts concrete scenes in which a pain becomes salient.

**Strengths:** The scene test and “smallest complete moment” rule are excellent. Strong distinction from broad pain points.

**Issues:** No explicit input contract or stable evidence IDs in the visible output specification. Optional scoring reduces consistency. It may merge moments that share an activity but differ by trigger, setting, or consequence.

**Improve:** Require actor/context/action/trigger/failure/consequence fields where present. Preserve temporal stage and environment. Make scoring either mandatory and defined or remove it.

### 09 — Extract Desired Outcomes

**Role:** Identifies immediate and longer-term success states.

**Strengths:** Separates functional, emotional, and identity outcomes; preserves success bars.

**Issues:** Outcomes can easily become inferred marketing aspirations. No explicit link to the pain or moment generating the desire. Immediate outcomes and end states may be mixed in one concept.

**Improve:** Require `outcome_horizon`, `originating_pain_ids`, `stated_or_inferred`, and `success_measure`. Split relief, capability, lifestyle, emotional, and identity outcomes when the evidence supports distinct jobs.

### 10 — Extract Emotional States

**Role:** Extracts context-bound emotions.

**Strengths:** Correctly separates momentary reactions from persistent states and ties feelings to context.

**Issues:** Emotion labels are highly vulnerable to model inference. Intensity and prevalence are not standardised. It may duplicate psychological drivers.

**Improve:** Require an explicit linguistic or behavioural cue for inferred emotion. Store emotion, context, trigger, duration, intensity cue, and evidence basis separately. Ban personality diagnosis.

### 11 — Extract Psychological Drivers

**Role:** Identifies deeper motives explaining multiple behaviours.

**Strengths:** Encourages explanatory power and distinguishes drivers from neighbouring concepts.

**Issues:** This is the most inference-heavy dimension. The “commercial implication” can encourage speculative persuasion logic. Drivers may become generic universals such as control, confidence, or safety.

**Improve:** Require at least two distinct observed manifestations and multiple evidence items for a canonical driver. Include disconfirming evidence and alternative explanations. Mark single-evidence drivers as hypotheses, not findings.

### 12 — Extract Beliefs

**Role:** Captures propositions customers hold to be true.

**Strengths:** Preserves polarity and conviction; useful belief taxonomy.

**Issues:** It can overlap heavily with mechanisms, limiting beliefs, assumed solutions, and objections. There is no shared ontology for resolving collisions.

**Improve:** Define belief as the parent concept and use typed subcategories. Add a cross-dimension collision rule: causal beliefs go to mechanisms; action expectations go to assumed solutions; purchase resistance goes to objections; self-blocking consequences go to limiting beliefs.

### 13 — Extract Limiting Beliefs

**Role:** Captures beliefs that constrain action, persistence, or hope.

**Strengths:** Truth-neutral and non-pathologising; attends to behavioural consequence.

**Issues:** “Limiting” is an analyst judgement and can import a coaching framework into customer research. A realistic constraint may be mislabeled as a belief.

**Improve:** Require a stated proposition plus observed behavioural restriction. Separate `belief`, `constraint`, and `uncertainty`. Rename to “action-limiting beliefs” to make the boundary explicit.

### 14 — Extract Failed Solutions

**Role:** Captures attempted remedies and why they failed.

**Strengths:** Strongly commercial and evidence-rich; preserves failure mode and reason.

**Issues:** It may merge different brands or implementations into a generic solution and lose actionable detail. Success-with-tradeoff cases need a distinct status.

**Improve:** Use fields for solution category, specific product/service, usage pattern, expected outcome, actual outcome, failure mode, abandonment status, and residual benefit. Include partial success and unsustainable success.

### 15 — Extract Assumed Solutions

**Role:** Captures what customers believe should solve the problem.

**Strengths:** Separates explicit expectations from inferred assumptions and records conviction.

**Issues:** Strong overlap with desired outcomes, beliefs, buying criteria, and mechanisms. Inferred assumptions can be speculative.

**Improve:** Require a clear “therefore I need X” bridge. Link each assumed solution to the belief or mechanism that makes it seem appropriate. Keep unstated analyst-inferred solution logic in a separate hypothesis section.

### 16 — Extract Buying Triggers

**Role:** Identifies events that move a customer toward action.

**Strengths:** Emphasises trigger-to-action sequence and distinguishes triggers from chronic pain.

**Issues:** It may confuse research triggers, category-entry triggers, purchase triggers, and switching triggers. Trigger latency is absent.

**Improve:** Classify trigger stage, trigger event, prior state, resulting action, time-to-action, and urgency. Separate category entry from final conversion and switching.

### 17 — Extract Buying Criteria

**Role:** Captures standards customers use to evaluate options.

**Strengths:** Handles positive and negative direction, implied criteria, and contradictory priorities.

**Issues:** Number collision with objections. Criteria strength and trade-off behaviour are underdeveloped. A desired feature can be mistaken for a true decision criterion.

**Improve:** Require evidence that the factor affects selection, rejection, or comparison. Add must-have/nice-to-have/dealbreaker, trade-off accepted, and criterion object. Renumber canonically as 17.

### Duplicate 17 / 18 — Extract Objections

**Role:** Captures resistance to buying or acting.

**Strengths:** Preserves the object of resistance and blocking strength; avoids rebutting the customer.

**Issues:** Two nearly identical versions exist. The prompts do not clearly distinguish objection from criterion, risk, question, distrust, or failed-solution residue. “Implied” objections can be invented too readily.

**Improve:** Keep only one canonical `18_extract_objections.md`. Require decision relevance, objection object, underlying risk, and behavioural effect. Separate stated objection, unresolved question, and inferred hesitation.

### 19 — Extract Mechanisms

**Role:** Captures causal models about problems and solutions.

**Strengths:** Excellent cause–via–effect framing; records conviction, source type, competing mechanisms, and truth status without automatically endorsing claims.

**Issues:** Duplicate copy exists. `externally-supported` cannot be determined from VOC alone unless external evidence is supplied. Customer, practitioner, and brand claims may be mixed in a segment corpus. “Validation status” risks pseudo-validation.

**Improve:** Rename the field to `epistemic_status_in_corpus` unless external sources are explicitly provided. Separate observed customer mechanism, practitioner claim, brand claim, and analyst inference. Keep one canonical file only.

### 20 — Extract Desired Proof

**Role:** Identifies evidence customers need before believing or buying.

**Strengths:** Strong link between proof demand and doubt; outputs actionable asset implications.

**Issues:** Implied proof demands can simply mirror an objection without actual evidence that the customer values that proof type. Proof credibility varies by segment and claim.

**Improve:** Separate explicit demanded proof from analyst-mapped proof opportunity. Add claim being proved, trusted source, acceptable standard, and rejected proof forms.

### 21 — Extract Product Mentions

**Role:** Inventories named products and categories.

**Strengths:** Preserves relationship and sentiment; avoids inferring ownership from recommendation.

**Issues:** Entity normalisation, aliases, versions, bundles, and generic category mentions are underspecified. Prevalence can be inflated by repeated mentions from one person.

**Improve:** Add canonical entity ID, surface form, brand, model, category, ownership certainty, author/thread counts, and entity-resolution confidence.

### 22 — Extract Competitor Mentions

**Role:** Captures direct and substitute competitors and switching evidence.

**Strengths:** Separates brand claims from customer experience and includes switching reasons and trust signals.

**Issues:** “Competitor” depends on the target offer, which is not a required input. Generic products may be both product mentions and substitutes.

**Improve:** Require `target_product_context`. Define direct, indirect, substitute, DIY, professional service, and do-nothing alternatives. Link entities to stage 21 rather than extracting duplicate entity records.

### 23 — Extract Offers

**Role:** Captures observed offer structures and customer reactions.

**Strengths:** Separates brand-presented and customer-desired offers; preserves terms and catches.

**Issues:** Inferred opportunities are mixed into an extraction stage. Offer evidence may be sparse in Reddit VOC. It overlaps with desired proof and objections.

**Improve:** Keep observed offers only in the canonical output. Put inferred offer opportunities in the later synthesis stage. Require offer source URL/evidence ID and exact terms when available.

### 24 — Extract Representative VOC

**Role:** Selects quotes that faithfully represent canonical themes.

**Strengths:** Correctly treats quote selection as selection rather than extraction; distinguishes typical exemplars from vivid outliers.

**Issues:** It requires both segment evidence and extracted themes, making it dependent on prior outputs, unlike the other parallel extractors. The workflow does not specify this dependency. Quote diversity rules are limited.

**Improve:** Explicitly run after all canonical dimensions. Enforce author/thread diversity, coverage across top concepts, and a quota for typical versus vivid quotes. Avoid selecting the same quote for many themes unless unavoidable.

### 25 — Extract Terminology

**Role:** Builds a customer-language lexicon.

**Strengths:** Preserves variants, misspellings, register, and segment specificity; useful for search and copy.

**Issues:** Terminology can include ordinary words that are frequent but not distinctive. Meaning inferred from context may be uncertain. Search utility and copy utility differ.

**Improve:** Add distinctiveness, search usefulness, copy usefulness, ambiguity, and unsafe/regulated terminology flags. Compare segment frequency with corpus-wide frequency when available.

### 26 — Extract Slang

**Role:** Captures informal, emotionally charged, or community-specific expressions.

**Strengths:** Preserves exact register and includes safety and commercial-usefulness flags.

**Issues:** “Hook potential” encourages copying language that may sound exploitative or inauthentic. Slang can be ephemeral, offensive, or community-bound. Terminology overlap is unresolved.

**Improve:** Add audience, context, recency, offensiveness, reclamation/community status, and “safe to echo” versus “understand only.” Use slang primarily for comprehension unless repeated customer adoption supports copy use.

## Missing workflow stages

### Corpus and source audit

Before filtering, add a stage that reports corpus size, time range, source distribution, subreddit concentration, query coverage, and likely collection bias. Clean extraction cannot compensate for a skewed corpus.

### Batch reconciliation

Large segment files will need multiple model runs. Add a reconciliation prompt that merges batch-level concepts globally, recalculates counts, resolves aliases, and preserves all evidence IDs.

### Cross-dimension collision audit

Add a stage that detects the same concept being independently classified as a belief, mechanism, objection, criterion, driver, and assumed solution without an explicit relationship.

### Contradiction and minority-view analysis

Add structured handling for competing customer views rather than merging them into a bland average. Preserve minority but commercially important positions.

### Coverage audit

For each evidence item, track whether it was reviewed and which dimensions it contributed to. This catches systematic under-extraction and makes “no evidence found” distinguishable from “not processed.”

## Recommended canonical architecture

```text
00 Corpus Audit and Workflow Manifest
01 Filter VOC
02 Deduplicate VOC
03 Discover Segments
04 Validate Segments
05 Assign Primary + Relevant Segments
06 Build Segment Evidence Manifests / Chunks
07–23 Independent Dimension Extraction
24 Reconcile Batches and Canonicalise Concepts
25 Link Cross-Dimension Concepts
26 Select Representative VOC
27 Build Terminology and Slang Lexicon
28 Build Segment Dossier
29 Prioritise Commercial Opportunities
30 Final Evidence and Coverage Audit
```

## Recommended output model

Every canonical concept should minimally contain:

```yaml
concept_id: pain_point__sleep_interruption
segment_id: chronic_neck_pain_side_sleepers
dimension: pain_point
name: Waking repeatedly because neck pain breaks sleep
definition: A recurring sleep disruption caused by neck discomfort or loss of support.
basis: observed
unique_evidence_count: 18
unique_thread_count: 12
unique_author_count: 16
prevalence_band: common
intensity_band: high
evidence_confidence: high
commercial_priority: high
representative_evidence_ids:
  - ev_0012
  - ev_0478
related_concept_ids:
  - pain_moment__waking_to_reposition
  - desired_outcome__sleep_through_night
contradicting_evidence_ids: []
notes: null
```

## Final recommendation

Do not rewrite all 26 prompts independently again. First create one compact canonical extraction framework, one common schema, and one workflow manifest. Then refactor each dimension prompt to contain only:

1. its unique definition and boundary rules;
2. its dimension-specific examples;
3. its unique fields;
4. the shared extraction contract by reference or embedded compactly.

That will make the library shorter, more consistent across GPT, Claude, Gemini, and local models, and much easier to audit or improve.
