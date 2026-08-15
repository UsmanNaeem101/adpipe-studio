"""Every skill the pipeline runs is visible in the frontend.

Stage 03 alone loads five files -- an orchestration contract plus four workers
that exist only because the corpus has to be chunked -- and the UI described the
whole thing as one line. A skill nobody can see is a skill that can be edited,
or fail to be edited, with nothing showing it was in play.

So the mapping lives in cli.py beside the code that loads the skills, and these
tests pin the two properties that make it trustworthy: every file it names
exists, and every file that exists is named.
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli


def surfaced():
    """Every skill filename the frontend would show, across all stages."""
    files = {row["file"] for stage in cli.STAGE_SKILLS
             for row in cli.stage_skill_manifest(stage)}
    files |= {row["file"] for row in cli.extractor_skill_manifest()}
    return {f for f in files if f}


class ManifestTests(unittest.TestCase):
    def test_every_skill_the_manifest_names_exists(self):
        missing = [row["file"] for stage in cli.STAGE_SKILLS
                   for row in cli.stage_skill_manifest(stage)
                   if not row["present"]]
        self.assertEqual(missing, [])

    def test_every_skill_file_on_disk_is_surfaced_somewhere(self):
        """A new skill that nobody wires up should fail here, not go unnoticed."""
        on_disk = {f for f in os.listdir(cli.SKILLS) if f.endswith(".md")}
        self.assertEqual(sorted(on_disk - surfaced()), [])

    def test_the_stage_03_subskills_are_all_named(self):
        files = [row["file"] for row in cli.stage_skill_manifest("segment")]
        for expected in ("03_discover_segments.md",
                         "03a_discover_segment_candidates.md",
                         "03b_consolidate_segment_candidates.md",
                         "03c_audit_segment_coverage.md",
                         "03e_expand_segment_evidence.md"):
            self.assertIn(expected, files)

    def test_the_commercial_layer_is_named(self):
        files = [row["file"] for row in cli.stage_skill_manifest("segment")]
        for expected in ("commercial_07_coalesce.md",
                         "commercial_08a_extract_signals.md",
                         "commercial_08b_coalesce_signals.md"):
            self.assertIn(expected, files)

    def test_every_extractor_resolves_to_a_real_file(self):
        rows = cli.extractor_skill_manifest()
        self.assertEqual(len(rows), len(cli.EXTRACTORS))
        self.assertTrue(all(row["present"] for row in rows))
        self.assertTrue(all(row["lines"] > 0 for row in rows))

    def test_line_counts_are_real_so_an_empty_skill_shows_up(self):
        row = next(r for r in cli.stage_skill_manifest("segment")
                   if r["file"] == "04_validate_segments.md")
        actual = len(open(os.path.join(cli.SKILLS, row["file"]),
                          encoding="utf-8").read().split("\n"))
        self.assertEqual(row["lines"], actual)

    def test_a_stage_the_map_does_not_cover_returns_empty_not_an_error(self):
        self.assertEqual(cli.stage_skill_manifest("render"), [])

    def test_the_manifest_is_json_serialisable_for_the_endpoint(self):
        payload = {name: cli.stage_skill_manifest(name) for name in cli.STAGE_SKILLS}
        payload["extract"] = cli.extractor_skill_manifest()
        self.assertIn("03a_discover_segment_candidates.md", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
