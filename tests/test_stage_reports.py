"""The pipeline showing its work.

Every number in these reports was already computed; the reports exist so a
person can see them. The property that matters most is that a report restates
what the stage did and never invents a number of its own -- so the checks below
are mostly arithmetic identities against the artifacts.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import stagereport


class DiscoveryReportTests(unittest.TestCase):
    partition = {
        "segment_candidates": 38, "facet_candidates": 24, "symptom_candidates": 14,
        "provisional_facets": [{"facet_key": "cost_sensitive",
                                "facet_type": "attribute",
                                "unique_evidence_count": 31}],
        "symptoms": [{"candidate_key": "night_pain", "evidence_ids": [1, 2, 3]}],
    }
    candidates = [{
        "segment_id": "seg_001", "slug": "swimmers", "name": "Swimmers",
        "definition": "Masters swimmers.", "commercial_distinction": "Stroke language.",
        "unique_evidence_count": 169, "unique_thread_count": 23,
        "unique_subreddit_count": 4, "discovery_status": "strong_candidate",
        "representative_evidence_ids": [7]}]

    def report(self):
        return stagereport.discovery_report(
            "p", self.partition, list(range(79)), self.candidates,
            {7: {"text": "The catch phase of freestyle is where it bites."}})

    def test_all_four_registers_are_reported(self):
        text = self.report()
        for expected in ("Segment-register candidates: **38**",
                         "Attribute / journey candidates: **24**",
                         "Symptom candidates", "**14**"):
            self.assertIn(expected, text)

    def test_what_left_the_taxonomy_is_still_named(self):
        text = self.report()
        self.assertIn("night_pain", text)
        self.assertIn("cost_sensitive", text)

    def test_counts_come_from_the_artifacts_not_the_prose(self):
        text = self.report()
        self.assertIn("169 comments · 23 conversations · 4 communities", text)

    def test_a_verbatim_quote_survives(self):
        self.assertIn("catch phase of freestyle", self.report())


class AssignmentReportTests(unittest.TestCase):
    validated = [{"segment_id": "seg_001", "slug": "desk", "name": "Desk workers"}]

    @staticmethod
    def row(score, status="assigned", segment="seg_001"):
        return {"score": score, "assignment_status": status,
                "primary_segment_id": segment}

    def test_the_bar_and_what_it_removed_are_both_stated(self):
        rows = [self.row(8), self.row(9),
                self.row(4, "unassigned_ambiguous", ""),
                self.row(3, "unassigned_ambiguous", "")]
        text = stagereport.assignment_report("p", rows, self.validated, 6, 2)
        self.assertIn("Comments judged: **4**", text)
        self.assertIn("Assigned: **2**", text)
        self.assertIn("Scored below the bar: **2**", text)

    def test_a_gate_that_removed_nothing_is_called_out(self):
        """The failure mode this whole pipeline keeps rediscovering."""
        rows = [self.row(8), self.row(9)]
        text = stagereport.assignment_report("p", rows, self.validated, 6, 2)
        self.assertIn("Nothing scored below the bar", text)
        self.assertIn("the gate that historically gets skipped", text)

    def test_a_gate_that_fired_gets_no_warning(self):
        rows = [self.row(8), self.row(3, "unassigned_ambiguous", "")]
        text = stagereport.assignment_report("p", rows, self.validated, 6, 2)
        self.assertNotIn("Nothing scored below the bar", text)


class BuildReportTests(unittest.TestCase):
    report = [("seg_001", "swimmers", 169, 23, {}),
              ("seg_002", "desk", 409, 86, {})]

    def test_the_reconciliation_is_shown_and_passes(self):
        text = stagereport.build_report("p", self.report, 8409, 0, 8987)
        self.assertIn("578 + 8,409 = 8,987", text)
        self.assertIn("**Reconciliation: PASS.**", text)

    def test_numbers_that_do_not_add_up_are_reported_as_a_mismatch(self):
        text = stagereport.build_report("p", self.report, 8409, 0, 9999)
        self.assertIn("MISMATCH", text)
        self.assertNotIn("**Reconciliation: PASS.**", text)

    def test_the_table_is_built_from_files_actually_written(self):
        text = stagereport.build_report("p", self.report, 0, 0, 578)
        self.assertIn("`evidence/swimmers.txt`", text)
        # Largest first, so the reader meets the biggest audience at the top.
        self.assertLess(text.index("desk"), text.index("swimmers"))


class ValidationReportTests(unittest.TestCase):
    def test_reclassified_candidates_are_named_with_their_destination(self):
        decisions = [
            {"segment_id": "seg_001", "status": "validated", "rationale": "ok"},
            {"segment_id": "seg_002", "status": "reclassified_as_facet",
             "facet_key": "brace_use", "facet_type": "attribute",
             "rationale": "a response to the problem"}]
        text = stagereport.validation_report(
            "p", decisions, [{"segment_id": "seg_001", "slug": "desk"}],
            [{"name": "Brace use", "facet_type": "attribute",
              "discovery_evidence_count": 12}],
            {"specialises": [], "adjacent": []}, (4, 8, 2, 3))
        self.assertIn("brace_use", text)
        self.assertIn("Brace use", text)
        self.assertIn("only point at which it costs nothing", text)

    def test_both_floor_sets_are_stated(self):
        text = stagereport.validation_report(
            "p", [], [], [], None, (4, 8, 2, 3))
        self.assertIn("at least **4** conversations and **8** comments", text)
        self.assertIn("at least **2** conversations and **3** comments", text)

    def test_relationships_read_as_english(self):
        graph = {"specialises": [{"from_segment_id": "seg_002",
                                  "to_segment_id": "seg_001",
                                  "strength": "strong"}],
                 "adjacent": [{"segment_ids": ["seg_001", "seg_003"],
                               "strength": "weak"}]}
        validated = [{"segment_id": "seg_001", "slug": "rotator_cuff"},
                     {"segment_id": "seg_002", "slug": "swimmers"},
                     {"segment_id": "seg_003", "slug": "desk"}]
        text = stagereport.validation_report(
            "p", [], validated, [], graph, (4, 8, 2, 3))
        self.assertIn("`swimmers` sits **inside** `rotator_cuff`", text)
        self.assertIn("`rotator_cuff` **beside** `desk`", text)


class WriteTests(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = stagereport.write(tmp, "03_discovery.md", "# hi\n")
            self.assertEqual(sorted(os.listdir(tmp)), ["03_discovery.md"])
            self.assertEqual(open(path).read(), "# hi\n")


if __name__ == "__main__":
    unittest.main()
