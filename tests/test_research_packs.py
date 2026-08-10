"""Layer 2: measured co-occurrence, and the weighted research pack.

The property under test throughout is the one that makes borrowing safe:
borrowed evidence changes what a researcher reads and never changes what
anything counts.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import researchpack
import segmentation


def assignment(evidence_id, segment_id, runner_up="", score=8, recorded=True):
    return {"evidence_id": evidence_id, "primary_segment_id": segment_id,
            "runner_up_segment_id": runner_up, "assignment_status": "assigned",
            "runner_up_recorded": recorded, "score": score, "winning_margin": 3,
            "facet_ids": []}


class CooccurrenceMeasurementTests(unittest.TestCase):
    eligible = ["seg_001", "seg_002", "seg_003"]

    def test_a_rate_is_taken_against_the_smaller_segment(self):
        rows = ([assignment(n, "seg_001", "seg_002") for n in range(1, 5)]
                + [assignment(n, "seg_001") for n in range(5, 101)]
                + [assignment(n, "seg_002") for n in range(101, 111)])
        pairs, sizes, measured = segmentation.measure_cooccurrence(
            rows, self.eligible)
        self.assertEqual(measured, 4)
        self.assertEqual(sizes["seg_001"], 100)
        edges = segmentation.measured_edges(pairs, sizes)
        # 4 of the smaller segment's 10 items, not 4 of the larger's 100.
        self.assertEqual(edges[0]["rate"], 0.4)

    def test_a_big_segment_placing_second_everywhere_is_not_an_edge(self):
        rows = ([assignment(n, "seg_002", "seg_001") for n in range(1, 6)]
                + [assignment(n, "seg_002") for n in range(10, 210)]
                + [assignment(n, "seg_001") for n in range(300, 500)])
        pairs, sizes, _measured = segmentation.measure_cooccurrence(
            rows, self.eligible)
        self.assertEqual(segmentation.measured_edges(pairs, sizes), [])

    def test_thresholds_are_adjustable(self):
        rows = ([assignment(n, "seg_001", "seg_002") for n in range(1, 3)]
                + [assignment(n, "seg_002") for n in range(10, 20)])
        pairs, sizes, _measured = segmentation.measure_cooccurrence(
            rows, self.eligible)
        self.assertEqual(segmentation.measured_edges(pairs, sizes), [])
        relaxed = segmentation.measured_edges(
            pairs, sizes, min_count=2, min_rate=0.01)
        self.assertEqual(relaxed[0]["count"], 2)

    def test_rows_predating_runner_up_recording_are_skipped(self):
        rows = [assignment(n, "seg_001", "seg_002", recorded=False)
                for n in range(1, 30)]
        pairs, _sizes, measured = segmentation.measure_cooccurrence(
            rows, self.eligible)
        self.assertEqual((dict(pairs), measured), ({}, 0))


class GraphAuditTests(unittest.TestCase):
    def graph(self):
        built, _ = segmentation.finalize_segment_graph([
            {"from_segment_id": "seg_002", "to_segment_id": "seg_001",
             "edge_type": "specialises", "strength": "strong", "rationale": ""},
            {"from_segment_id": "seg_003", "to_segment_id": "seg_004",
             "edge_type": "adjacent", "strength": "moderate", "rationale": ""},
        ], ["seg_001", "seg_002", "seg_003", "seg_004"])
        return built

    def audit(self, measured_pairs):
        return segmentation.audit_graph(self.graph(), [
            {"segment_ids": list(pair), "edge_type": "co_occurs",
             "count": 9, "rate": 0.3} for pair in measured_pairs])

    def test_a_declared_adjacency_the_evidence_supports_is_confirmed(self):
        audit = self.audit([("seg_003", "seg_004")])
        self.assertEqual(audit["confirmed"], [["seg_003", "seg_004"]])
        self.assertEqual(audit["declared_not_measured"], [])

    def test_a_declared_adjacency_with_no_support_is_flagged(self):
        audit = self.audit([])
        self.assertEqual(audit["declared_not_measured"], [["seg_003", "seg_004"]])

    def test_an_undeclared_overlap_is_flagged(self):
        audit = self.audit([("seg_001", "seg_003")])
        self.assertEqual(audit["measured_not_declared"], [["seg_001", "seg_003"]])

    def test_parent_child_overlap_is_explained_not_flagged(self):
        audit = self.audit([("seg_001", "seg_002")])
        self.assertEqual(audit["measured_not_declared"], [])
        self.assertEqual(audit["explained_by_hierarchy"], [["seg_001", "seg_002"]])


class RelationResolutionTests(unittest.TestCase):
    def graph(self):
        built, _ = segmentation.finalize_segment_graph([
            {"from_segment_id": "seg_002", "to_segment_id": "seg_001",
             "edge_type": "specialises", "strength": "strong", "rationale": ""},
            {"from_segment_id": "seg_003", "to_segment_id": "seg_001",
             "edge_type": "specialises", "strength": "weak", "rationale": ""},
            {"from_segment_id": "seg_004", "to_segment_id": "seg_001",
             "edge_type": "adjacent", "strength": "moderate", "rationale": ""},
        ], ["seg_001", "seg_002", "seg_003", "seg_004"])
        return built

    def test_a_child_sees_its_parent_and_its_siblings(self):
        edges = researchpack.related_edges("seg_002", self.graph(), [])
        self.assertEqual(edges, [("parent", "seg_001", "strong"),
                                 ("sibling", "seg_003", "weak")])

    def test_a_parent_sees_its_children_and_its_neighbours(self):
        edges = researchpack.related_edges("seg_001", self.graph(), [])
        self.assertEqual(edges, [("child", "seg_002", "strong"),
                                 ("child", "seg_003", "weak"),
                                 ("adjacent", "seg_004", "moderate")])

    def test_a_neighbour_is_borrowed_through_one_relationship_only(self):
        measured = [{"segment_ids": ["seg_001", "seg_004"],
                     "edge_type": "co_occurs", "count": 9, "rate": 0.3}]
        edges = researchpack.related_edges("seg_001", self.graph(), measured)
        neighbours = [other for _relation, other, _strength in edges]
        self.assertEqual(len(neighbours), len(set(neighbours)))
        self.assertIn(("adjacent", "seg_004", "moderate"), edges)

    def test_quotas_follow_relation_then_strength(self):
        settings = researchpack.pack_settings({})
        strong_parent = researchpack.quota("parent", "strong", settings)
        weak_parent = researchpack.quota("parent", "weak", settings)
        sibling = researchpack.quota("sibling", "strong", settings)
        self.assertGreater(strong_parent, weak_parent)
        self.assertGreater(weak_parent, sibling)
        self.assertEqual(strong_parent, int(8000 * 0.60 * 1.0))

    def test_weights_are_configurable(self):
        settings = researchpack.pack_settings({"segmentation": {
            "pack_context_budget_tokens": 1000,
            "relation_weights": {"parent": 0.9},
            "strength_factors": {"strong": 0.5}}})
        self.assertEqual(researchpack.quota("parent", "strong", settings), 450)
        # Untouched entries keep their defaults.
        self.assertEqual(settings["relation_weights"]["sibling"], 0.20)


class RollupCountTests(unittest.TestCase):
    def graph(self):
        built, _ = segmentation.finalize_segment_graph([
            {"from_segment_id": "seg_002", "to_segment_id": "seg_001",
             "edge_type": "specialises", "strength": "strong", "rationale": ""},
            {"from_segment_id": "seg_003", "to_segment_id": "seg_002",
             "edge_type": "specialises", "strength": "strong", "rationale": ""},
        ], ["seg_001", "seg_002", "seg_003"])
        return built

    def test_own_and_rollup_are_separate_integers(self):
        counts = {"seg_001": 250, "seg_002": 15, "seg_003": 4}
        self.assertEqual(
            researchpack.rollup_counts("seg_001", self.graph(), counts),
            (250, 269))
        self.assertEqual(
            researchpack.rollup_counts("seg_002", self.graph(), counts), (15, 19))
        self.assertEqual(
            researchpack.rollup_counts("seg_003", self.graph(), counts), (4, 4))


class PackBuildTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"segment_id": "seg_001", "slug": "rotator_cuff",
             "name": "Rotator cuff", "definition": "Rotator cuff pain"},
            {"segment_id": "seg_002", "slug": "swimmers_rc",
             "name": "Swimmers with rotator cuff", "definition": "Swimmers"},
        ]
        self.graph, _ = segmentation.finalize_segment_graph([
            {"from_segment_id": "seg_002", "to_segment_id": "seg_001",
             "edge_type": "specialises", "strength": "strong", "rationale": ""},
        ], ["seg_001", "seg_002"])
        self.evidence, self.rows = {}, {"seg_001": [], "seg_002": []}
        for n in range(1, 41):
            self.evidence[n] = {"text": f"parent voice {n} about shoulder pain",
                                "tier": "core"}
            self.rows["seg_001"].append(assignment(n, "seg_001", score=9 - n % 3))
        for n in range(100, 103):
            self.evidence[n] = {"text": f"freestyle swim voice {n}", "tier": "core"}
            self.rows["seg_002"].append(assignment(n, "seg_002"))
        self.settings = researchpack.pack_settings({})

    def pack(self, segment_id):
        by_id = {row["segment_id"]: row for row in self.segments}
        return researchpack.build_pack(
            by_id[segment_id], by_id, self.rows, self.evidence, self.graph, [],
            self.settings)

    def test_a_child_pack_carries_its_own_evidence_in_full(self):
        text, report = self.pack("seg_002")
        self.assertEqual(report["primary_evidence_count"], 3)
        for evidence_id in (100, 101, 102):
            self.assertIn(f"[{evidence_id}]", text)

    def test_a_child_borrows_its_parent_and_says_so(self):
        text, report = self.pack("seg_002")
        self.assertTrue(report["borrowed_item_count"] > 0)
        self.assertEqual(report["borrowed_from"][0]["relation"], "parent")
        self.assertIn("BORROWED FROM: rotator_cuff (parent, strong)", text)
        self.assertIn("BORROWED CONTEXT", text)

    def test_borrowed_evidence_is_not_counted(self):
        _text, report = self.pack("seg_002")
        self.assertEqual(report["primary_evidence_count"], 3)
        self.assertEqual(report["rollup_evidence_count"], 3)
        self.assertGreater(report["borrowed_item_count"], 0)

    def test_the_parent_reports_own_and_rollup_separately(self):
        text, report = self.pack("seg_001")
        self.assertEqual(report["primary_evidence_count"], 40)
        self.assertEqual(report["rollup_evidence_count"], 43)
        self.assertIn("Primary evidence items (this segment only): 40", text)
        self.assertIn("Rollup with child segments:                 43", text)

    def test_a_quota_bounds_what_one_relationship_contributes(self):
        tight = researchpack.pack_settings(
            {"segmentation": {"pack_context_budget_tokens": 60}})
        by_id = {row["segment_id"]: row for row in self.segments}
        _text, report = researchpack.build_pack(
            by_id["seg_002"], by_id, self.rows, self.evidence, self.graph, [],
            tight)
        self.assertLessEqual(report["borrowed_tokens"], 60)
        self.assertLess(report["borrowed_item_count"], 40)

    def test_borrowing_takes_the_best_scoring_evidence_first(self):
        text, _report = self.pack("seg_002")
        borrowed = text.split("BORROWED CONTEXT", 1)[1]
        scores = [int(line.split(": ")[1])
                  for line in borrowed.splitlines()
                  if line.startswith("ASSIGNMENT SCORE:")]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_packs_are_deterministic(self):
        self.assertEqual(self.pack("seg_002"), self.pack("seg_002"))

    def test_an_unrelated_segment_borrows_nothing(self):
        lone = {"segment_id": "seg_001", "slug": "rotator_cuff",
                "name": "Rotator cuff", "definition": "Rotator cuff pain"}
        _text, report = researchpack.build_pack(
            lone, {"seg_001": lone}, {"seg_001": self.rows["seg_001"]},
            self.evidence, None, [], self.settings)
        self.assertEqual(report["borrowed_item_count"], 0)
        self.assertEqual(report["primary_evidence_count"], 40)

    def test_write_packs_produces_one_file_per_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = researchpack.write_packs(
                tmp, self.segments, self.rows, self.evidence, self.graph, [],
                self.settings)
            names = sorted(os.listdir(tmp))
        self.assertEqual(names, ["rotator_cuff.txt", "swimmers_rc.txt"])
        self.assertEqual([row["slug"] for row in manifest],
                         ["rotator_cuff", "swimmers_rc"])


if __name__ == "__main__":
    unittest.main()
