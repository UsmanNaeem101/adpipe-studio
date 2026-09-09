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


class PiccSkillTests(unittest.TestCase):
    def test_skill_28_resolves_and_names_the_card_sections(self):
        name, body = cli.skill(28)
        self.assertEqual(name, "28_picc_card")
        for heading in ("## Buying barriers", "## PICC card", "## Constraints",
                        "## Angles", "## Leads with"):
            self.assertIn(heading, body)
        # The one field the brief stage builds a hook on must be verbatim.
        self.assertIn("word-for-word", body)
        # Dimensions select; they are not copy.
        self.assertIn("SELECTORS, not copy", body)

    def test_cmd_picc_sends_both_skills_and_the_creative_settings(self):
        cfg = {"name": "t", "_dir": "/nowhere",
               "creative": {"awareness": "solution-aware", "traffic": "warm"}}
        args = SimpleNamespace(segment="s", force=False, yes=True, product=None)
        seen = {}

        def fake_synth(cfg_, args_, stage, prompt, dest, max_tokens=16000, **_k):
            seen.update(stage=stage, prompt=prompt, dest=dest)
            return ""

        with mock.patch.object(cli, "read_extractions", return_value="EXTRACTIONS"), \
                mock.patch.object(cli, "segment_context", return_value=""), \
                mock.patch.object(cli, "product_context", return_value="PRODUCT"), \
                mock.patch.object(cli, "synth", side_effect=fake_synth):
            cli.cmd_picc(cfg, args)

        _, s27 = cli.skill(27)
        _, s28 = cli.skill(28)
        self.assertEqual(seen["stage"], "picc")
        self.assertIn(s27.strip(), seen["prompt"])
        self.assertIn(s28.strip(), seen["prompt"])
        self.assertIn("EXTRACTIONS", seen["prompt"])
        self.assertIn("PRODUCT", seen["prompt"])
        self.assertIn("solution-aware", seen["prompt"])
        self.assertIn("warm", seen["prompt"])
        self.assertTrue(seen["dest"].endswith("01_picc_card.md"))


if __name__ == "__main__":
    unittest.main()
