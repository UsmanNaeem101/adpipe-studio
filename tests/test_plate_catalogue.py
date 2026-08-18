"""What a project has ready to turn into pictures.

The plate prompts are the one part of a production brief another tool can act on
without understanding the rest of it: each is a complete, deliberately text-free
image prompt. Topic Atlas's node canvas is why this is readable over HTTP.

The risk here is not a crash. A loose reading of the brief returns rows that are
not plate prompts at all, and the next thing that happens is somebody pays an
image model to render a QA checklist.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import plates  # noqa: E402
import store  # noqa: E402


BRIEF = """# Production Brief — side_sleepers

## The four strongest

### C02 — "Too low squishes your shoulder."

**Angle** A2 failed-solution · **Layout** `double_hook_proof_chip`

| Hook | Where it goes |
|---|---|
| C02 | `headline` | the short one |

## Plates

**Every prompt below produces a TEXT-FREE image.**

| Concept | Slot | Prompt |
|---|---|---|
| C01 | `left_image` | A messy stack of two mismatched bed pillows on a plain white sheet, soft overcast light, editorial product photography, absolutely no text anywhere |
| C02 | `image` | A single ergonomic latex pillow photographed side-on at eye level against a plain warm off-white wall, soft directional daylight, no text, no words |

## QA — this batch

| Check | Status |
|---|---|
| C01 | `headline` | ok |
"""


class BriefTableTests(unittest.TestCase):
    def test_it_reads_the_plate_table(self):
        rows = plates.plates_from_brief(BRIEF)
        self.assertEqual([r["concept"] for r in rows], ["C01", "C02"])
        self.assertEqual(rows[0]["slot"], "left_image")
        self.assertTrue(rows[0]["prompt"].startswith("A messy stack"))

    def test_the_other_tables_in_a_brief_are_not_plate_prompts(self):
        # A brief carries several markdown tables. The hook table and the QA
        # checklist both have three columns and a concept id in the first one;
        # what separates a plate prompt is that it is a paragraph, not a word.
        prompts = [r["prompt"] for r in plates.plates_from_brief(BRIEF)]
        self.assertNotIn("the short one", prompts)
        self.assertNotIn("ok", prompts)

    def test_no_plate_table_yields_nothing_rather_than_guessing(self):
        self.assertEqual(plates.plates_from_brief("# Brief\n\nno plates here"), [])
        self.assertEqual(plates.plates_from_brief(""), [])


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "proj")
        self.assets = os.path.join(self.project, "assets")

    def tearDown(self):
        self.tmp.cleanup()

    def segment(self, name, *, brief=None, plates_json=None):
        d = os.path.join(self.assets, name)
        os.makedirs(d, exist_ok=True)
        if brief is not None:
            store.write_text(os.path.join(d, "03_production_brief.md"), brief)
        if plates_json is not None:
            store.write_json(os.path.join(d, "plates.json"), plates_json)

    def test_plates_json_is_preferred_and_says_so(self):
        # It is the file plates.py itself reads, and somebody checked it. The
        # brief's table is a regex over prose; when both exist they should not
        # silently disagree about which one answered.
        self.segment("side_sleepers", brief=BRIEF, plates_json={
            "segment": "side_sleepers",
            "plates": [{"concept": "C09", "slot": "image", "prompt": "checked by a human"}]})
        rows = plates.catalogue(self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "plates.json")
        self.assertEqual([p["concept"] for p in rows[0]["plates"]], ["C09"])

    def test_a_brief_without_plates_json_still_answers(self):
        self.segment("side_sleepers", brief=BRIEF)
        rows = plates.catalogue(self.project)
        self.assertEqual(rows[0]["source"], "brief")
        self.assertEqual(len(rows[0]["plates"]), 2)

    def test_an_empty_plates_json_falls_through_to_the_brief(self):
        # A plates.json written before the brief was, or emptied by hand, is not
        # an answer — and returning nothing for a segment that plainly has
        # prompts reads as "this segment has no ads yet".
        self.segment("side_sleepers", brief=BRIEF, plates_json={"plates": []})
        rows = plates.catalogue(self.project)
        self.assertEqual(rows[0]["source"], "brief")

    def test_a_segment_with_no_brief_is_absent_rather_than_empty(self):
        self.segment("desk_workers")
        self.segment("side_sleepers", brief=BRIEF)
        self.assertEqual([r["segment"] for r in plates.catalogue(self.project)],
                         ["side_sleepers"])

    def test_a_project_with_no_assets_yet_is_not_an_error(self):
        self.assertEqual(plates.catalogue(self.project), [])


if __name__ == "__main__":
    unittest.main()
