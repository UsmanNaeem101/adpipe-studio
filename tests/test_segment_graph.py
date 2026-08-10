"""Typed relationships between segments, and the floors that read them.

The hierarchy exists to make narrow audiences survivable, not to make the
taxonomy tidy — so these tests are mostly about what a *child* is allowed to be:
thinner than a root, but never a relabelling of its parent, and never deleted
when it fails.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import corpus_policy
import segmentation


def edge(child, parent, kind="specialises", strength="moderate"):
    return {"from_segment_id": child, "to_segment_id": parent,
            "edge_type": kind, "strength": strength, "rationale": "because"}


class SegmentGraphTests(unittest.TestCase):
    eligible = ["seg_001", "seg_002", "seg_003", "seg_004"]

    def test_hierarchy_and_adjacency_are_separate_edge_types(self):
        graph, dropped = segmentation.finalize_segment_graph([
            edge("seg_002", "seg_001"),
            edge("seg_003", "seg_004", kind="adjacent", strength="strong"),
        ], self.eligible)
        self.assertEqual(dropped, [])
        self.assertEqual(segmentation.parent_map(graph), {"seg_002": "seg_001"})
        self.assertEqual(graph["adjacent"][0]["segment_ids"],
                         ["seg_003", "seg_004"])
        self.assertEqual(graph["adjacent"][0]["strength"], "strong")

    def test_adjacency_is_undirected_and_deduplicated(self):
        graph, dropped = segmentation.finalize_segment_graph([
            edge("seg_004", "seg_001", kind="adjacent"),
            edge("seg_001", "seg_004", kind="adjacent"),
        ], self.eligible)
        self.assertEqual(len(graph["adjacent"]), 1)
        self.assertEqual([row["reason"] for row in dropped],
                         ["duplicate adjacency"])

    def test_a_child_may_have_only_one_parent(self):
        graph, dropped = segmentation.finalize_segment_graph([
            edge("seg_002", "seg_001"),
            edge("seg_002", "seg_003"),
        ], self.eligible)
        self.assertEqual(segmentation.parent_map(graph), {"seg_002": "seg_001"})
        self.assertEqual([row["reason"] for row in dropped],
                         ["child already has a parent"])

    def test_cycles_are_broken_deterministically(self):
        edges = [edge("seg_001", "seg_002"), edge("seg_002", "seg_003"),
                 edge("seg_003", "seg_001")]
        first, dropped = segmentation.finalize_segment_graph(edges, self.eligible)
        second, _ = segmentation.finalize_segment_graph(edges, self.eligible)
        self.assertEqual(first, second)
        self.assertEqual(len(first["specialises"]), 2)
        self.assertIn("closes a specialisation cycle",
                      [row["reason"] for row in dropped])

    def test_edges_onto_unvalidated_segments_are_dropped_not_fatal(self):
        graph, dropped = segmentation.finalize_segment_graph([
            edge("seg_002", "seg_404"), edge("seg_002", "seg_002"),
            {"from_segment_id": "seg_002", "to_segment_id": "seg_001",
             "edge_type": "contains", "strength": "strong", "rationale": ""},
        ], self.eligible)
        self.assertEqual(graph["specialises"], [])
        self.assertEqual(
            sorted(row["reason"] for row in dropped),
            ["endpoint is not a validated segment", "self-edge",
             "unknown edge_type 'contains'"])

    def test_only_validated_decisions_can_anchor_an_edge(self):
        decisions = [
            {"segment_id": "seg_001", "status": "validated"},
            {"segment_id": "seg_002", "status": "validated"},
            {"segment_id": "seg_003", "status": "reclassified_as_facet"},
        ]
        graph = cli._finalize_stage04_graph(
            [edge("seg_002", "seg_001"), edge("seg_003", "seg_001")],
            decisions, verbose=False)
        self.assertEqual(segmentation.parent_map(graph), {"seg_002": "seg_001"})

    def test_children_map_is_sorted(self):
        graph, _ = segmentation.finalize_segment_graph([
            edge("seg_003", "seg_001"), edge("seg_002", "seg_001"),
        ], self.eligible)
        self.assertEqual(segmentation.children_map(graph),
                         {"seg_001": ["seg_002", "seg_003"]})

    def test_parents_are_ordered_before_their_children(self):
        graph, _ = segmentation.finalize_segment_graph([
            edge("seg_002", "seg_001"), edge("seg_003", "seg_002"),
        ], self.eligible)
        order = cli._root_order(["seg_003", "seg_002", "seg_001"], graph)
        self.assertLess(order.index("seg_001"), order.index("seg_002"))
        self.assertLess(order.index("seg_002"), order.index("seg_003"))


class ChildFloorTests(unittest.TestCase):
    parent_texts = [
        "my shoulder hurts at night after work",
        "shoulder pain wakes me up constantly",
        "rotator cuff pain is worst at night",
        "the shoulder aches all day at my desk",
    ]
    swimmer_texts = [
        "freestyle catch phase kills my shoulder",
        "swimming freestyle laps aggravates it",
        "my swim coach changed my freestyle stroke",
    ]

    def floors(self, **overrides):
        return corpus_policy.child_floors({"segmentation": overrides})

    def test_a_narrow_child_survives_where_a_root_would_not(self):
        floors = self.floors()
        self.assertIsNone(corpus_policy.child_floor_failure(
            3, 3, self.swimmer_texts, self.parent_texts, floors))
        self.assertIsNotNone(corpus_policy.floor_failure(
            {"unique_thread_count": 3, "unique_evidence_count": 3}, 4, 8))

    def test_a_child_that_only_relabels_its_parent_fails(self):
        relabel = ["my shoulder hurts at night", "shoulder pain wakes me",
                   "the shoulder aches at my desk"]
        failure = corpus_policy.child_floor_failure(
            3, 3, relabel, self.parent_texts, self.floors())
        self.assertIn("relabelling of its parent", failure)

    def test_a_child_still_has_a_volume_floor(self):
        floors = self.floors()
        self.assertIn("thread(s)", corpus_policy.child_floor_failure(
            1, 5, self.swimmer_texts, self.parent_texts, floors))
        self.assertIn("evidence item(s)", corpus_policy.child_floor_failure(
            2, 2, self.swimmer_texts, self.parent_texts, floors))

    def test_floors_are_configurable(self):
        floors = self.floors(min_child_threads=5, min_child_evidence=9)
        self.assertIn("5 required", corpus_policy.child_floor_failure(
            3, 3, self.swimmer_texts, self.parent_texts, floors))
        relaxed = self.floors(min_distinctive_terms=0)
        self.assertIsNone(corpus_policy.child_floor_failure(
            2, 3, ["shoulder pain"] * 3, self.parent_texts, relaxed))

    def test_a_term_used_once_is_a_turn_of_phrase_not_an_angle(self):
        floors = self.floors()
        once = ["freestyle catch phase hurts", "shoulder pain at night",
                "the shoulder aches"]
        self.assertEqual(
            corpus_policy.distinctive_terms(once, self.parent_texts, floors), [])
        twice = once + ["freestyle again"]
        self.assertIn("freestyle", corpus_policy.distinctive_terms(
            twice, self.parent_texts, floors))


class HierarchicalFloorTests(unittest.TestCase):
    """The end state: what happens to a failing child's evidence."""

    def segments(self):
        return [
            {"segment_id": "seg_001", "slug": "rotator_cuff",
             "name": "Rotator cuff", "unique_thread_count": 20,
             "unique_evidence_count": 40},
            {"segment_id": "seg_002", "slug": "swimmers_rc",
             "name": "Swimmers with rotator cuff", "unique_thread_count": 3,
             "unique_evidence_count": 3},
        ]

    def graph(self):
        built, _ = segmentation.finalize_segment_graph(
            [edge("seg_002", "seg_001")], ["seg_001", "seg_002"])
        return built

    @staticmethod
    def assignment(evidence_id, segment_id):
        return {"evidence_id": evidence_id, "primary_segment_id": segment_id,
                "assignment_status": "assigned", "score": 8, "winning_margin": 3}

    def corpus(self, child_texts):
        parent = ["my shoulder hurts at night after work",
                  "shoulder pain wakes me constantly",
                  "rotator cuff pain is worst at night",
                  "the shoulder aches all day",
                  "shoulder pain again tonight",
                  "my shoulder is sore after work",
                  "the pain wakes me at night",
                  "shoulder trouble all week"]
        by_id, rows = {}, []
        for index, text in enumerate(parent, start=1):
            by_id[index] = {"text": text, "thread_id": f"tp{index}"}
            rows.append(self.assignment(index, "seg_001"))
        for offset, text in enumerate(child_texts, start=1):
            evidence_id = 100 + offset
            by_id[evidence_id] = {"text": text, "thread_id": f"tc{offset}"}
            rows.append(self.assignment(evidence_id, "seg_002"))
        return by_id, rows

    def decisions(self):
        return [{"segment_id": "seg_001", "status": "validated", "rationale": "ok"},
                {"segment_id": "seg_002", "status": "validated", "rationale": "ok"}]

    def test_a_distinct_child_survives_on_three_items(self):
        by_id, rows = self.corpus([
            "freestyle catch phase kills my shoulder",
            "swimming freestyle laps aggravates it",
            "my swim coach changed my freestyle stroke"])
        kept = cli._apply_assigned_floors(
            self.segments(), self.decisions(), rows, by_id, {}, verbose=False,
            graph=self.graph())
        self.assertEqual([row["slug"] for row in kept],
                         ["rotator_cuff", "swimmers_rc"])

    def test_a_failing_child_folds_into_its_parent_with_its_evidence(self):
        by_id, rows = self.corpus([
            "my shoulder hurts at night", "shoulder pain wakes me",
            "the shoulder aches all day"])
        segments = self.segments()
        kept = cli._apply_assigned_floors(
            segments, self.decisions(), rows, by_id, {}, verbose=False,
            graph=self.graph())
        self.assertEqual([row["slug"] for row in kept], ["rotator_cuff"])

        child_rows = [row for row in rows if row["evidence_id"] > 100]
        self.assertTrue(child_rows)
        for row in child_rows:
            # Folded, not orphaned: specialisation means these were always
            # members of the parent.
            self.assertEqual(row["primary_segment_id"], "seg_001")
            self.assertEqual(row["assignment_status"], "assigned")
            self.assertEqual(row["absorbed_from_segment_id"], "seg_002")
        self.assertEqual(kept[0]["absorbed_children"][0]["slug"], "swimmers_rc")
        self.assertEqual(kept[0]["absorbed_children"][0]["evidence_count"], 3)

    def test_a_failing_root_still_returns_its_evidence_to_unassigned(self):
        by_id, rows = self.corpus([])
        segments = [self.segments()[1]]  # the child, with its parent absent
        kept = cli._apply_assigned_floors(
            segments, self.decisions(), rows, by_id, {}, verbose=False, graph=None)
        self.assertEqual(kept, [])
        parent_rows = [row for row in rows if row["evidence_id"] < 100]
        self.assertTrue(all(row["primary_segment_id"] == "seg_001"
                            for row in parent_rows))

    def test_a_child_whose_parent_died_is_judged_as_a_root(self):
        by_id, rows = self.corpus([
            "freestyle catch phase kills my shoulder",
            "swimming freestyle laps aggravates it",
            "my swim coach changed my freestyle stroke"])
        # Strip the parent's evidence so the parent fails its own floor.
        rows = [row for row in rows if row["evidence_id"] > 100]
        decisions = self.decisions()
        kept = cli._apply_assigned_floors(
            self.segments(), decisions, rows, by_id, {}, verbose=False,
            graph=self.graph())
        self.assertEqual(kept, [])
        # And the orphan's evidence went nowhere, because there was no parent
        # left to fold it into.
        for row in rows:
            self.assertEqual(row["assignment_status"],
                             "unassigned_insufficient_evidence")

    def test_an_absorbed_child_stays_visible_in_the_parent_evidence_file(self):
        import tempfile
        from unittest import mock

        by_id, rows = self.corpus([
            "my shoulder hurts at night", "shoulder pain wakes me",
            "the shoulder aches all day"])
        segments = self.segments()
        for segment in segments:
            segment.update({"definition": "Shoulder pain", "name": segment["slug"],
                            "inclusion_criteria": ["shoulder pain"],
                            "exclusion_criteria": ["unrelated"]})
        kept = cli._apply_assigned_floors(
            segments, self.decisions(), rows, by_id, {}, verbose=False,
            graph=self.graph())
        for row in rows:
            row.setdefault("cue_types", [])
            row.setdefault("primary_cues", [])
            row.setdefault("rationale", "dominant context")
        for record in by_id.values():
            record.setdefault("tier", "core")

        with tempfile.TemporaryDirectory() as tmp:
            voc = os.path.join(tmp, "research", "voc")
            os.makedirs(voc)
            with mock.patch.object(cli, "record_provenance"):
                cli.build_evidence_files({"_dir": tmp}, kept, rows, by_id, voc)
            with open(os.path.join(tmp, "research", "evidence",
                                   "rotator_cuff.txt"), encoding="utf-8") as fh:
                evidence = fh.read()
        self.assertIn("ABSORBED SUB-CONTEXTS", evidence)
        self.assertIn("swimmers_rc: 3 item(s)", evidence)
        # 8 parent items plus the 3 it absorbed, counted once each.
        self.assertIn("Evidence items: 11", evidence)

    def test_discovery_floors_also_use_the_lower_child_bar(self):
        decisions = self.decisions()
        kept = cli._apply_segment_floors(
            self.segments(), decisions, {}, verbose=False, graph=self.graph())
        self.assertEqual([row["slug"] for row in kept],
                         ["rotator_cuff", "swimmers_rc"])
        # The same card as a root would not have survived.
        flat = cli._apply_segment_floors(
            self.segments(), self.decisions(), {}, verbose=False, graph=None)
        self.assertEqual([row["slug"] for row in flat], ["rotator_cuff"])


if __name__ == "__main__":
    unittest.main()
