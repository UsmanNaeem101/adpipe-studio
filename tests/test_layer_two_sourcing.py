"""What the extractors read, and where the operator pack's findings come from.

Two worlds used to exist: skill 07's 980-line definition of a pain point, and the
commercial layer's own thinner one, re-derived from raw text by a stage that had
never read those rules. These tests pin the closing of that split, and the
repointing of the extractors onto Layer 2 that makes it worth doing.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import commercial
import paths


class ExtractionSourceTests(unittest.TestCase):
    def project(self, tmp, *, pack=True, evidence=True):
        if evidence:
            directory = paths.evidence(tmp)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "desk_workers.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("layer one\n")
        if pack:
            directory = paths.research(tmp, "segments", "packs")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "desk_workers.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("layer two\n")
        return {"_dir": tmp, "name": "test"}

    def test_layer_two_is_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            source, layer = cli.extraction_source(
                cfg, "desk_workers", SimpleNamespace(evidence=False))
        self.assertEqual(layer, "pack")
        self.assertIn(os.path.join("segments", "packs"), source)

    def test_the_evidence_flag_forces_layer_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp)
            source, layer = cli.extraction_source(
                cfg, "desk_workers", SimpleNamespace(evidence=True))
        self.assertEqual(layer, "evidence")
        self.assertIn("evidence", source)

    def test_a_project_without_packs_falls_back_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.project(tmp, pack=False)
            source, layer = cli.extraction_source(
                cfg, "desk_workers", SimpleNamespace(evidence=False))
            self.assertTrue(os.path.exists(source))
        self.assertEqual(layer, "evidence")

    def test_the_pack_reading_rule_forbids_adding_the_two_counts(self):
        rule = cli.PACK_READING_RULE
        self.assertIn("PRIMARY evidence only", rule)
        self.assertIn("never add the two together", rule)
        self.assertIn("not this segment's finding", rule.lower())


class SignalSourcingTests(unittest.TestCase):
    canonical = {
        "canonical_segment_id": "cseg_001", "slug": "desk_workers",
        "name": "Desk workers", "definition": "Desk-driven pain",
        "commercial_distinction": "Workday", "subsegments": [], "attributes": [],
        "primary_evidence_ids": [1, 2, 3],
    }

    def write_extraction(self, tmp, slug, name, text="## Pain\n- night pain\n"):
        directory = paths.extractions(tmp, slug)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_only_dimension_owning_extractions_are_gathered(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_dir": tmp}
            self.write_extraction(tmp, "desk_workers", "07_pain_points.md")
            self.write_extraction(tmp, "desk_workers", "09_desired_outcomes.md")
            # Not a commercial signal dimension, so it is not carried across.
            self.write_extraction(tmp, "desk_workers", "19_mechanisms.md")
            documents = cli._extraction_documents(cfg, ["desk_workers"])
        self.assertEqual([row["skill"] for row in documents], [7, 9])
        self.assertEqual(documents[0]["dimensions"], ["pain_points"])

    def test_every_signal_dimension_has_an_owning_skill(self):
        self.assertEqual(sorted(cli.SIGNAL_DIMENSION_SKILLS),
                         sorted(commercial.SIGNAL_DIMENSIONS))
        for numbers in cli.SIGNAL_DIMENSION_SKILLS.values():
            for number in numbers:
                self.assertIn(number, cli.EXTRACTORS)

    def test_empty_and_missing_extractions_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_dir": tmp}
            self.write_extraction(tmp, "desk_workers", "07_pain_points.md", "  \n")
            documents = cli._extraction_documents(
                cfg, ["desk_workers", "never_extracted"])
        self.assertEqual(documents, [])

    def test_documents_from_every_source_research_segment_are_gathered(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_dir": tmp}
            self.write_extraction(tmp, "desk_workers", "07_pain_points.md")
            self.write_extraction(tmp, "side_sleepers", "07_pain_points.md")
            documents = cli._extraction_documents(
                cfg, ["desk_workers", "side_sleepers"])
        self.assertEqual([row["segment_slug"] for row in documents],
                         ["desk_workers", "side_sleepers"])

    def test_chunks_keep_the_canonical_evidence_enum(self):
        documents = [{"segment_slug": "desk_workers", "skill": 7,
                      "dimensions": ["pain_points"], "name": "07_pain_points.md",
                      "text": "x" * 40000},
                     {"segment_slug": "desk_workers", "skill": 9,
                      "dimensions": ["desired_outcomes"],
                      "name": "09_desired_outcomes.md", "text": "y" * 40000}]
        chunks = cli._stage08_extraction_chunks(self.canonical, documents, 8000)
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            # A signal may cite any of the audience's evidence, not just a
            # chunk's -- the documents already span all of it.
            self.assertEqual(chunk["evidence_ids"], [1, 2, 3])
        self.assertEqual([chunk["chunk_id"] for chunk in chunks],
                         ["08a_cseg_001_x0000", "08a_cseg_001_x0001"])

    def test_a_small_document_set_is_one_chunk(self):
        documents = cli._extraction_documents({"_dir": "/nonexistent"}, [])
        self.assertEqual(documents, [])
        chunks = cli._stage08_extraction_chunks(self.canonical, [{
            "segment_slug": "s", "skill": 7, "dimensions": ["pain_points"],
            "name": "07_pain_points.md", "text": "short"}], 8000)
        self.assertEqual(len(chunks), 1)

    def test_the_prompt_says_to_carry_across_rather_than_re_analyse(self):
        prompt = cli._stage08_extraction_prompt(self.canonical, [{
            "segment_slug": "desk_workers", "skill": 7,
            "dimensions": ["pain_points"], "name": "07_pain_points.md",
            "text": "## Pain\n- night pain"}])
        self.assertIn("do not re-analyse the raw text", prompt.lower())
        self.assertIn("07_pain_points.md", prompt)
        self.assertIn("night pain", prompt)


if __name__ == "__main__":
    unittest.main()
