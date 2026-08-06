import os
import sys
import unittest
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli


class ExtractPresetTests(unittest.TestCase):
    def test_fast_test_exact_dimensions(self):
        self.assertEqual(
            cli.PRESETS["fast"],
            [7, 8, 9, 12, 14, 16, 18, 19, 20, 24],
        )

    def test_standard_is_fast_plus_requested_dimensions(self):
        added = [10, 11, 13, 15, 17, 21, 22, 25]
        self.assertEqual(
            cli.PRESETS["standard"],
            sorted(cli.PRESETS["fast"] + added),
        )

    def test_deep_research_runs_every_extractor_07_through_26(self):
        self.assertEqual(cli.PRESETS["deep"], list(range(7, 27)))

    def test_default_remains_all_extractors_for_cli_compatibility(self):
        args = SimpleNamespace(skills=None, preset=None)
        self.assertEqual(cli.chosen_extractors(args), cli.EXTRACTORS)


if __name__ == "__main__":
    unittest.main()
