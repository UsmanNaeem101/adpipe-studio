"""The PICC card is built from skills/28_picc_card.md, not from prose in cli.py.

The card's judgement used to live inline in cmd_picc, so it could not be
reviewed or revised the way the other skills are. These tests pin the wiring:
skill 28 resolves, carries the sections downstream stages read the card by, and
reaches the prompt alongside skill 27 and the creative settings.
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli  # noqa: E402


# One category's vocabulary, from the health-adjacent qa ruleset and the
# project the pipeline was first built on. None of it may live in a skill.
CATEGORY_WORDS = ("pillow", "neck", "spine", "nerve", "posture", "sleep",
                  "shoulder", "towel", "mattress")


class ProductAgnosticTests(unittest.TestCase):
    def test_skill_28_names_no_product_or_category(self):
        _, body = cli.skill(28)
        found = [w for w in CATEGORY_WORDS if w in body.lower()]
        self.assertEqual(found, [])
        self.assertIn("about the segment, not a product", body)

    def test_the_ramp_rules_carry_no_category_vocabulary(self):
        found = [w for w in CATEGORY_WORDS if w in cli.RAMP_RULES.lower()]
        self.assertEqual(found, [])

    def test_compliance_comes_from_the_project_settings(self):
        strict = cli.compliance_rules({"compliance": {
            "profile": "health_adjacent", "platform": "tiktok",
            "notes": "Never mention the trial length.\nNo before/after photos."}})
        self.assertIn("Tiktok", strict)
        self.assertIn("health-adjacent ruleset", strict)
        self.assertIn("Never mention the trial length.", strict)
        self.assertIn("No before/after photos.", strict)

        general = cli.compliance_rules({"compliance": {"profile": "general",
                                                       "platform": "google"}})
        self.assertIn("general ruleset", general)
        self.assertNotIn("realign", general)
        self.assertNotIn("named condition", general)
        self.assertNotIn("Project notes", general)

    def test_an_old_project_without_compliance_settings_still_gets_a_ruleset(self):
        text = cli.ramp_rules({"name": "old"})
        self.assertIn("SELECTORS, not copy", text)
        self.assertIn("COMPLIANCE", text)
        self.assertIn("FLAG IT", text)


class PiccSkillTests(unittest.TestCase):
    def test_skill_28_resolves_and_names_the_card_sections(self):
        name, body = cli.skill(28)
        self.assertEqual(name, "28_picc_card")
        for heading in ("## Buying barriers", "## PICC card", "## The bar",
                        "## Angles", "## Leads with"):
            self.assertIn(heading, body)
        # The one field the brief stage builds a hook on must be verbatim.
        self.assertIn("word-for-word", body)
        # Dimensions select; they are not copy.
        self.assertIn("SELECTORS, not copy", body)

    def test_cmd_picc_sends_both_skills_and_the_creative_settings(self):
        cfg = {"name": "t", "_dir": "/nowhere",
               "creative": {"awareness": "solution-aware", "traffic": "warm"},
               "compliance": {"profile": "general", "platform": "meta",
                              "notes": "Say nothing about shipping times."}}
        args = SimpleNamespace(segment="s", force=False, yes=True)
        seen = {}

        def fake_synth(cfg_, args_, stage, prompt, dest, max_tokens=16000, **_k):
            seen.update(stage=stage, prompt=prompt, dest=dest)
            return ""

        def never(*_a, **_k):
            raise AssertionError("the PICC card takes no product or compliance input")

        with mock.patch.object(cli, "read_extractions", return_value="EXTRACTIONS"), \
                mock.patch.object(cli, "segment_context", side_effect=never), \
                mock.patch.object(cli, "product_context", side_effect=never), \
                mock.patch.object(cli, "compliance_rules", side_effect=never), \
                mock.patch.object(cli, "synth", side_effect=fake_synth):
            cli.cmd_picc(cfg, args)

        _, s27 = cli.skill(27)
        _, s28 = cli.skill(28)
        self.assertEqual(seen["stage"], "picc")
        self.assertIn(s27.strip(), seen["prompt"])
        self.assertIn(s28.strip(), seen["prompt"])
        self.assertIn("EXTRACTIONS", seen["prompt"])
        self.assertIn("solution-aware", seen["prompt"])
        self.assertIn("warm", seen["prompt"])
        self.assertIn("SELECTORS, not copy", seen["prompt"])
        # A segment document: nothing about a product, no compliance ruleset.
        # Those enter at the concepts stage, where the product is judged.
        self.assertNotIn("COMPLIANCE", seen["prompt"])
        self.assertNotIn("Say nothing about shipping times.", seen["prompt"])
        self.assertTrue(seen["dest"].endswith("01_picc_card.md"))

    def test_concepts_still_gets_the_product_and_the_ruleset(self):
        # The product filter moved out of the card, not out of the pipeline.
        src = open(os.path.join(ROOT, "pipeline", "cli.py"), encoding="utf-8").read()
        concepts = src[src.index("def cmd_concepts"):src.index("def cmd_brief")]
        self.assertIn("product_context(", concepts)
        self.assertIn("ramp_rules(cfg)", concepts)


if __name__ == "__main__":
    unittest.main()
