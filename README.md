# adpipe

Raw Reddit VOC in, launch-ready static ads out. Product-agnostic: the pipeline,
the ad layouts and the compliance engine are shared; everything niche-specific
lives in `projects/<name>/`.

**Double-click `Ad Studio.command`** — that's the whole app, in your browser. Three
tabs: **Remix** (upload a product photo, write a brief, choose an execution preset,
pick reference layouts → finished ads), **Pipeline** (run any stage, watch the
output), **Settings** (paste your API keys). No terminal needed.

The CLI still exists if you prefer it:

```bash
./adpipe studio                       # same UI
./adpipe ingest  raw_voc.txt          # filter + dedupe                (code, free)
./adpipe segment                      # discover + assign + evidence   (code + model)
./adpipe run     <segment>            # extract → … → rendered PNGs    (code + model)
```

`run` chains extract → picc → concepts → brief → qa → render. Each stage is also
runnable on its own, and finished stages are skipped on re-run (`--force` to redo).

---

## Two ways to make an ad

| | Compositor (`render`) | Remix (Studio → Remix tab) |
|---|---|---|
| Layouts | the 6 slotted HTML templates | any of the 221 reference ads |
| Your product | a plate behind the copy | dropped into the layout by the model |
| Text | composited — spelling guaranteed | drawn by the image model — usually right |
| Compliance | **`qa.py` checks it automatically** | **you must read every result yourself** |

Use the compositor when the copy must be exact and claim-safe. Use Remix when you
want a specific reference layout rebuilt around your product.

## Execution presets

`reference_docs/execution_levers.md` defines 60 ways to **execute** a static ad —
49 levers each across Targeting, Persuasion, Messaging, Proof, Visual Direction,
Offer and Compliance. They don't decide the message (the brief still does that);
they decide the psychological and visual execution of it. Pain Agitator and
Mechanism Educator can carry the same claim and look nothing alike.

On the Remix tab, **3 · Execution preset** lets you pick one, or leave it off and
let the image model decide from the brief alone. **AI picks** hands the choice to
a cheap text model (Haiku, or DeepSeek via OpenRouter — whichever key is set): it
reads your brief and the layout you selected, returns one preset and its reasoning,
and drops it into the dropdown so you can still overrule it before spending an
image credit.

A preset's Visual Direction will sometimes fight the reference you chose — Pain
Agitator wants no product and a person's face; a Product Hero layout is built
around the product and has neither. Those clashes are detected up front and listed
before you generate, with the call left to you:

- **Keep the reference layout** (default) — composition, panels and text placement
  are fixed; the preset drives colour, mood, expression, density and copy inside them.
- **Follow the preset** — it may restructure the reference, and the result will
  drift from the layout you picked.

Only the four levers that change what a layout *is* raise a warning — product
visibility, human presence, information density, urgency (plus a text-led preset on
an image-led layout). Stylistic differences pass silently, because a warning that
fires on everything gets ignored.

```bash
./.venv/bin/python pipeline/presets.py --list
```

`--show 07` prints the exact block that gets appended to the image prompt, and
`--show 07 --against 09_Offer/566.png` reports the clashes for that pairing.

## Keys

Paste a key once on the **Settings** tab and both Studio and the standalone CLI can
use it. AdPipe saves credentials in a user-only local store outside every project
and Git repository (`~/Library/Application Support/AdPipe/credentials.json` on
macOS, or the platform config directory elsewhere); saved values are never sent
back to the browser. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and
`OPENROUTER_API_KEY` environment variables override the corresponding saved
value. Older browser-only Settings keys migrate automatically the next time the
Studio loads, and are removed from browser storage only after the backend confirms
the save. `ingest`, segment assignment, `qa` and `render` need no key at all.

## The stages

| Stage | What it does | Engine |
|---|---|---|
| `ingest` | Skills 01–02. Splits a dump into individual comments (one comment = one evidence item), strips boilerplate, scores relevance, drops duplicates. | code |
| `segment` | Skills 03–06. Model discovers and validates segments from a sample; assignment and evidence-file building are deterministic, so each item lands in exactly one segment. | both |
| `extract` | Skills 07–26 against one evidence file — 20 dimensions, **batched and cached**. | model |
| `picc` | Skill 27 barrier ranking, then the quick PICC card and 5 angles. | model |
| `concepts` | 10 concepts, 2–3 in-image hooks each in the segment's own words, each mapped to a real layout. Writes `concepts.json`. | model |
| `brief` | Production briefs for the strongest concepts, including a text-free plate prompt. | model |
| `qa` | Compliance gate — claims, quotes, stats, structure. | code |
| `render` | Composites copy into layouts, screenshots to PNG. | code |
| `plates.py` | Optional. Generates background/product images to sit behind the copy. Separate script, separate key — see `docs/IMAGE_SETUP.md`. | image model |

### Why it's cheap

Skills 07–26 all read the **same** evidence file (up to ~540KB) and differ only in
the instruction. So the corpus goes in a cached system block and only the skill
varies — the classic shared-prefix case. A `max_tokens: 0` warm-up writes the cache
first (parallel requests can't read an entry another is still writing), then all 20
extractions go out as **one batch at 50% off**. Cache reads cost ~10%, so 20
extractions over one corpus cost roughly one full read plus change.

An empty extraction response is never written as a 0-byte Markdown file. Only the
affected skill is retried immediately up to three times; if all retries are empty,
the stage exits with a clear failure and keeps the other successful outputs. In the
Studio choose **Pipeline → Extract → Individual skill rerun** to rerun any one skill,
or use `./adpipe extract <segment> --skills 7 --force` from the CLI.

**Every model stage prints a cost estimate and waits for a yes.** `--yes` skips the
prompt; `--effort` overrides the model's effort level for one run.

### Model audit logs

Every generation request is logged before it is sent. Each request gets a
timestamped directory under `projects/<project>/logs/model/<date>/` containing:

- `request.json` — provider, model, stage/job ID, schema and the complete system,
  corpus and user prompt;
- `response.json` — the complete provider payload, extracted text, usage, stop
  reason, elapsed time and an explicit `empty_text` flag;
- `events.jsonl` — retries, HTTP/provider errors and fallback routing;
- `response.png` — the raw returned image for image-generation/edit requests.

These logs can contain customer evidence and are intentionally gitignored. They
also appear in the Studio **Outputs → Logs** accordion. Set `ADPIPE_LOG_DIR` to an
absolute directory to override the default location.

---

## Layout

```
adpipe                    launcher (uses .venv)
pipeline/
  cli.py                  stage orchestrator
  llm.py                  Claude API layer — caching, batching, cost, errors
  render.py  qa.py        compositor + compliance gate
  presets.py              60 execution presets — parse, prompt block, conflicts
  templates/              6 shared ad layouts
  brand.json  facts.json  brand tokens · the only numbers allowed in an ad
skills/                   27 skill files — source of truth for each stage
reference_docs/           execution_levers.md — the 60-preset lever library
references/               221 competitor ads by style, mapped to templates
projects/<name>/
  project.json            niche regexes, compliance profile, model config
  voc/  evidence/  extractions/  output/
```

Start a second product by copying `projects/montisella/` and rewriting
`project.json` — the filter regexes, the compliance profile, and the creative
defaults are the whole of what's niche-specific.

## Plates are optional

Every layout has a no-plate mode: with no image the ad becomes a typographic /
colour-block creative rather than a frame around a void. All 10 side-sleeper ads
ship complete with no photography at all. To upgrade, generate plates
(`pipeline/plates.py`, see `docs/IMAGE_SETUP.md`), wire them in, and re-render —
the templates switch modes automatically.

## The compositor

Image models cannot spell, so **no headline ever goes through one**. The model
generates the background/product *plate*; `render.py` composites real text over it
in a slotted HTML template and screenshots it with headless Chrome. A second
measuring pass fails the build when a slot's text is clipped — a sliced headline is
a dead ad, and that should be a build error, not a discovery on Meta.

| Template | Framework | Use when |
|---|---|---|
| `double_hook_proof_chip` | Pain → Promise | pain-led angle, one hard proof point |
| `split_screen_comparison` | Comparison → Advantage → CTA | failed-solution angle |
| `ugc_quote_card` | Claim → Proof | desired-outcome, in their words |
| `objection_killer_tile` | Objection → Reframe | barrier is trust/risk, not pain |
| `before_after_night` | Before → After | two felt states, same night |
| `problem_mechanism_benefit` | Problem → Mechanism → Benefit | mechanism angle, educational |

## Compliance — non-negotiable

Health-adjacent wellness product on Meta. The evidence is full of medical framing
because that is how sufferers talk. **None of it becomes a product claim.**

- ✅ Felt experience — tension that won't switch off, waking up stiff, the day's
  tightness following you to bed, sleeping through, waking recovered.
- ❌ Never — corrects posture, realigns spine/neck, relieves nerve compression,
  treats or cures any named condition, any medical-causation claim.

`qa.py` hard-fails on: any medical/causation claim, any quote not found verbatim in
the evidence file, any stat missing from `facts.json`. **`facts.json` ships empty on
purpose** — every number warns until it is substantiated. Trial length, warranty,
foam density and price are all still `NEEDS INPUT`.

If a barrier can only be answered with a claim you cannot substantiate, the gate
flags it — that means the angle is wrong, not that you may overclaim.

## Current state: RAMP

No ad has won yet, so: one segment, one awareness stage, one quick PICC card, 5
angles, 10 concepts, hooks, brief. **Don't build the factory.** A win crosses the
fork to FACTORY for that segment only. A loss → new segment or new offer, re-run
the RAMP. Don't rebuild the machine after a loss; change the input.

Log per ad: segment · barrier · message need · angle · style · concept · hook ·
template · CTR · CVR · CPA · ROAS. Learn at the lever level ("belief-reframe beat
mechanism-explainer for problem-aware side sleepers"), not just "this ad won."
