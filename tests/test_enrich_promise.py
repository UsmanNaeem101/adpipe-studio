"""The enrich panel says what it fills. These hold it to that.

The panel used to say only that it "proposes values for its research fields",
which is true and answers nothing. Asked twice what enrich was for, the honest
answer had to be assembled by reading the schema — so the text now names the
fields and states plainly that product fields are never touched.

Copy that describes behaviour is behaviour. If a field is renamed or the enrich
flag moves, the panel starts lying and nothing else notices.
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402
import products  # noqa: E402


def enrichable(sections):
    return {f["label"].lower() for s in sections if isinstance(s, dict)
            for f in (s.get("fields") or []) if isinstance(f, dict) and f.get("enrich")}


class ProductFieldsAreNeverEnrichedTests(unittest.TestCase):
    def test_no_product_field_can_be_filled_from_research(self):
        # The promise the panel now makes in bold, and the reason it can: a
        # customer's desire is not evidence of a capability, so the product
        # schema simply has no field an evidence pass may write.
        self.assertEqual(enrichable(products.PRODUCT_SECTIONS), set())

    def test_the_segment_is_what_gets_filled(self):
        self.assertGreaterEqual(len(enrichable(products.SEGMENT_SECTIONS)), 20)


class ThePanelNamesRealFieldsTests(unittest.TestCase):
    """Every field the panel advertises must exist and be enrich-able."""

    ADVERTISED = [
        "pain points", "pain moments", "desired outcomes", "failed solutions",
        "beliefs", "limiting beliefs", "buying triggers", "purchase criteria",
        "competitor complaints", "proof expectations",
    ]

    def test_each_advertised_field_is_a_real_enrichable_field(self):
        available = enrichable(products.SEGMENT_SECTIONS)
        for label in self.ADVERTISED:
            with self.subTest(label=label):
                self.assertIn(label, available)

    def test_the_panel_text_still_advertises_them(self):
        # Pins the copy to this list, so removing a field from the schema
        # without removing it from the panel fails here rather than in front
        # of somebody wondering what enrich does.
        panel = re.search(r"Fill this product's segment from the research"
                          r"(?:.*?</p>){3}", app.PAGE, re.S)
        self.assertIsNotNone(panel, "the enrich panel's copy has moved")
        # Collapse the source's line wrapping: "pain\n          moments" is one
        # phrase to a reader and must be one phrase to this test.
        text = " ".join(panel.group(0).split()).lower()
        for label in self.ADVERTISED:
            with self.subTest(label=label):
                self.assertIn(label, text)

    def test_the_panel_states_that_products_are_untouched(self):
        # Whitespace-collapsed, because the source wraps the sentence and a test
        # pinned to that wrapping breaks on reflow rather than on meaning.
        flat = " ".join(app.PAGE.split())
        self.assertIn("never fills product-wide fields", flat)

    def test_the_panel_distinguishes_the_two_meanings_of_segment(self):
        # The confusion this rewrite exists to end: "segment" is both the audience
        # the research found and the product's record of it, and enrich reads one
        # to fill the other.
        flat = " ".join(app.PAGE.split())
        self.assertIn("\"Segment\" means two things", flat)
        self.assertIn("research segment", flat)
        self.assertIn("product segment", flat)


if __name__ == "__main__":
    unittest.main()
