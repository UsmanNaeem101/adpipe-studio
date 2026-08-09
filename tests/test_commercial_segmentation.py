import copy
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import commercial
import cli


class CommercialSegmentationTests(unittest.TestCase):
    def setUp(self):
        self.validated = [
            {
                "segment_id": "seg_001", "slug": "strength_training",
                "name": "Strength trainees", "definition": "People lifting weights",
                "commercial_distinction": "Training context changes the promise",
                "inclusion_criteria": ["strength training"],
                "exclusion_criteria": ["no training context"],
            },
            {
                "segment_id": "seg_002", "slug": "form_conscious_lifters",
                "name": "Form-conscious lifters",
                "definition": "Lifters focused on form",
                "commercial_distinction": "Technique language matters",
                "inclusion_criteria": ["form cues"],
                "exclusion_criteria": ["not lifting"],
            },
            {
                "segment_id": "seg_010", "slug": "medication_acceptance",
                "name": "Medication accepters",
                "definition": "A treatment attitude rather than an audience",
                "commercial_distinction": "Preserved as research metadata",
                "inclusion_criteria": ["medication attitude"],
                "exclusion_criteria": ["no treatment attitude"],
            },
        ]
        self.evidence = {
            1: {"id": 1, "tier": "core", "text": "Bench press hurts my shoulder",
                "title": "Bench", "url": "https://example/1"},
            2: {"id": 2, "tier": "supporting", "text": "I want to press overhead again",
                "title": "Press", "url": "https://example/2"},
            3: {"id": 3, "tier": "core", "text": "I keep checking my lifting form",
                "title": "Form", "url": "https://example/3"},
            4: {"id": 4, "tier": "context", "text": "Medication is acceptable to me",
                "title": "Medication", "url": "https://example/4"},
        }
        self.assignments = [
            {"evidence_id": 1, "primary_segment_id": "seg_001",
             "assignment_status": "assigned", "score": 9, "winning_margin": 4,
             "secondary_attributes": []},
            {"evidence_id": 2, "primary_segment_id": "seg_001",
             "assignment_status": "assigned", "score": 8, "winning_margin": 3,
             "secondary_attributes": ["surgery avoidant"]},
            {"evidence_id": 3, "primary_segment_id": "seg_002",
             "assignment_status": "assigned", "score": 8, "winning_margin": 3,
             "secondary_attributes": []},
            {"evidence_id": 4, "primary_segment_id": "seg_010",
             "assignment_status": "assigned", "score": 7, "winning_margin": 2,
             "secondary_attributes": []},
        ]
        self.payload = {
            "canonical_segments": [{
                "canonical_key": "gym_lifters", "slug": "gym_lifters",
                "name": "Gym Lifters and Strength Trainees",
                "definition": "People whose lifting context shapes the problem.",
                "commercial_distinction": "They need training-compatible messaging.",
                "source_segment_ids": ["seg_001", "seg_002"],
                "subsegments": ["form-conscious beginners"],
                "attributes": ["exercise committed"],
                "inclusion_criteria": ["lifting is a dominant context"],
                "exclusion_criteria": ["an incidental gym mention"],
            }],
            "research_segment_mappings": [
                {"segment_id": "seg_001", "disposition": "parent",
                 "canonical_key": "gym_lifters", "label": "Lifting audience",
                 "rationale": "Distinct activity context"},
                {"segment_id": "seg_002", "disposition": "subsegment",
                 "canonical_key": "gym_lifters", "label": "Form conscious",
                 "rationale": "A subprofile of lifters"},
                {"segment_id": "seg_010", "disposition": "research_watchlist",
                 "canonical_key": "", "label": "Medication attitude",
                 "rationale": "Not independently addressable"},
            ],
        }

    def finalize(self, payload=None):
        return commercial.finalize_stage07(
            payload or self.payload, self.validated, self.assignments, self.evidence)

    def synthesis(self):
        catalogue, _mapping = self.finalize()
        canonical = catalogue["canonical_segments"][0]
        signals = [
            {"signal_id": "chunk_1_sig_0001", "dimension": "pain_points",
             "label": "Pain during pressing", "evidence_ids": [1, 2],
             "representative_quotes": [{"evidence_id": 1,
                                         "quote": "Bench press hurts"}],
             "typical_outcome": "", "common_complaint": ""},
            {"signal_id": "chunk_2_sig_0001", "dimension": "desired_outcomes",
             "label": "Return to overhead pressing", "evidence_ids": [2],
             "representative_quotes": [{"evidence_id": 2,
                                         "quote": "press overhead again"}],
             "typical_outcome": "", "common_complaint": ""},
            {"signal_id": "chunk_2_sig_0002", "dimension": "representative_language",
             "label": "Lifting language", "evidence_ids": [1],
             "representative_quotes": [{"evidence_id": 1,
                                         "quote": "Bench press hurts my shoulder"}],
             "typical_outcome": "", "common_complaint": ""},
        ]
        payload = {"themes": [
            {"dimension": "pain_points", "label": "Pain during pressing",
             "source_signal_ids": ["chunk_1_sig_0001"],
             "representative_evidence_ids": [1], "summary": "Pressing causes pain",
             "typical_outcome": "", "common_complaint": ""},
            {"dimension": "desired_outcomes", "label": "Return to pressing",
             "source_signal_ids": ["chunk_2_sig_0001"],
             "representative_evidence_ids": [2], "summary": "Resume training",
             "typical_outcome": "", "common_complaint": ""},
            {"dimension": "representative_language", "label": "Lifting language",
             "source_signal_ids": ["chunk_2_sig_0002"],
             "representative_evidence_ids": [1], "summary": "Exact lifting VOC",
             "typical_outcome": "", "common_complaint": ""},
        ]}
        return catalogue, canonical, commercial.finalize_stage08(
            canonical, signals, payload, self.evidence)

    def test_stage07_schema_constrains_exact_research_ids(self):
        schema = commercial.stage07_schema(["seg_001", "seg_002"])
        canonical_enum = schema["properties"]["canonical_segments"]["items"][
            "properties"]["source_segment_ids"]["items"]["enum"]
        mapping_enum = schema["properties"]["research_segment_mappings"]["items"][
            "properties"]["segment_id"]["enum"]
        self.assertEqual(canonical_enum, ["seg_001", "seg_002"])
        self.assertEqual(mapping_enum, ["seg_001", "seg_002"])

    def test_every_research_segment_is_mapped_exactly_once(self):
        catalogue, mapping = self.finalize()
        self.assertEqual(mapping["research_segment_count"], 3)
        self.assertCountEqual(
            [row["segment_id"] for row in mapping["mappings"]],
            [row["segment_id"] for row in self.validated])
        commercial.validate_stage07_artifacts(
            catalogue, mapping, self.validated, self.assignments)

    def test_missing_duplicate_or_unknown_research_id_fails_loudly(self):
        for mutate in ("missing", "duplicate", "unknown"):
            payload = copy.deepcopy(self.payload)
            if mutate == "missing":
                payload["research_segment_mappings"].pop()
            elif mutate == "duplicate":
                payload["research_segment_mappings"].append(copy.deepcopy(
                    payload["research_segment_mappings"][0]))
            else:
                payload["research_segment_mappings"][0]["segment_id"] = "seg_999"
            with self.subTest(mutate=mutate), self.assertRaises(
                    commercial.CommercialContractError):
                self.finalize(payload)

    def test_canonical_id_is_stable_across_semantic_rename(self):
        first, _ = self.finalize()
        renamed = copy.deepcopy(self.payload)
        renamed["canonical_segments"][0]["canonical_key"] = "strength_athletes"
        renamed["canonical_segments"][0]["slug"] = "strength_athletes"
        renamed["canonical_segments"][0]["name"] = "Strength Athletes"
        for row in renamed["research_segment_mappings"][:2]:
            row["canonical_key"] = "strength_athletes"
        second, _ = self.finalize(renamed)
        self.assertEqual(first["canonical_segments"][0]["canonical_segment_id"],
                         second["canonical_segments"][0]["canonical_segment_id"])

    def test_all_assigned_evidence_survives_without_canonical_duplicates(self):
        catalogue, mapping = self.finalize()
        canonical_ids = catalogue["canonical_segments"][0]["primary_evidence_ids"]
        self.assertEqual(canonical_ids, [1, 2, 3])
        self.assertEqual(len(canonical_ids), len(set(canonical_ids)))
        self.assertEqual(mapping["preserved_evidence_count"], 4)
        self.assertEqual(mapping["assigned_evidence_count"], 4)

    def test_watchlist_keeps_cluster_and_evidence_provenance(self):
        _catalogue, mapping = self.finalize()
        watchlist = [row for row in mapping["mappings"]
                     if row["disposition"] == "research_watchlist"]
        self.assertEqual(watchlist[0]["segment_id"], "seg_010")
        self.assertEqual(watchlist[0]["evidence_ids"], [4])

    def test_pain_counts_are_unique_ids_and_signals_may_overlap(self):
        _catalogue, canonical, synthesis = self.synthesis()
        pain = synthesis["pain_points"][0]
        outcome = synthesis["desired_outcomes"][0]
        self.assertEqual(pain["evidence_count"], 2)
        self.assertEqual(pain["evidence_ids"], [1, 2])
        self.assertIn(2, outcome["evidence_ids"])
        self.assertEqual(canonical["primary_evidence_count"], 3)
        commercial.validate_synthesis(synthesis, canonical)

    def test_false_model_count_cannot_enter_synthesis(self):
        _catalogue, canonical, synthesis = self.synthesis()
        broken = copy.deepcopy(synthesis)
        broken["pain_points"][0]["evidence_count"] = 999
        with self.assertRaises(commercial.CommercialContractError):
            commercial.validate_synthesis(broken, canonical)

    def test_stage09_is_deterministic_and_excludes_internal_telemetry(self):
        catalogue, canonical, synthesis = self.synthesis()
        _catalogue2, mapping = self.finalize()
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = dict(
                final_dir=tmp, project_name="shoulders_and_neck", refined_count=4,
                assigned_count=4, unassigned_count=0, validated=self.validated,
                catalogue=catalogue, mapping=mapping,
                synthesis_by_id={canonical["canonical_segment_id"]: synthesis},
                evidence_by_id=self.evidence)
            first, files = commercial.render_research_pack(**kwargs)
            segment_path = os.path.join(tmp, files[canonical["canonical_segment_id"]])
            with open(segment_path, encoding="utf-8") as handle:
                rendered_first = handle.read()
            second, _ = commercial.render_research_pack(**kwargs)
            with open(segment_path, encoding="utf-8") as handle:
                rendered_second = handle.read()
        self.assertEqual(first, second)
        self.assertEqual(rendered_first, rendered_second)
        self.assertIn("Canonical commercial segments: 1", first)
        self.assertIn("Primary evidence items: 3", first)
        self.assertIn("TOP PAIN POINTS", rendered_first)
        for forbidden in ("reasoning_tokens", "provider", "fingerprint",
                          "winning_margin", "schema"):
            self.assertNotIn(forbidden, rendered_first)

    def test_stage09_does_not_modify_machine_source_artifacts(self):
        catalogue, canonical, synthesis = self.synthesis()
        _catalogue2, mapping = self.finalize()
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "existing_stage05.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.assignments) + "\n")
            with open(source, "rb") as handle:
                before = handle.read()
            commercial.render_research_pack(
                os.path.join(tmp, "final"), "test", 4, 4, 0, self.validated,
                catalogue, mapping,
                {canonical["canonical_segment_id"]: synthesis}, self.evidence)
            with open(source, "rb") as handle:
                after = handle.read()
        self.assertEqual(before, after)

    def test_cli_exposes_new_resume_steps_without_removing_old_ones(self):
        self.assertEqual(cli.SEGMENT_STEP_ORDER,
                         ("03a", "03b", "03c", "04", "05", "06",
                          "07", "08", "09"))


if __name__ == "__main__":
    unittest.main()
