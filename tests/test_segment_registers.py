"""Registers, the facet vocabulary, and the Stage 05 runner-up.

One taxonomy cannot hold audiences, attributes, journey states and symptoms at
once. These tests pin the two properties that separation exists to buy: a
candidate that is not an audience leaves the taxonomy *before* Stage 05 gives it
evidence, and nothing discovered is deleted on the way out.
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm
import segmentation
from llm import BatchResult


def harvested(candidate_id, key, register, evidence_ids, cue_terms=("cue",)):
    return {
        "candidate_id": candidate_id,
        "candidate_key": key,
        "register": register,
        "evidence_ids": list(evidence_ids),
        "aliases": [key.replace("_", " ").capitalize()],
        "cue_terms": list(cue_terms),
    }


class RegisterPartitionTests(unittest.TestCase):
    def catalogue(self):
        return [
            harvested("a_c000", "desk_workers", "segment", [1, 2]),
            harvested("a_c001", "side_sleepers", "segment", [3]),
            harvested("a_c002", "cost_sensitive", "attribute", [4, 5]),
            harvested("b_c000", "cost_sensitive", "attribute", [5, 6]),
            harvested("b_c001", "awaiting_mri", "journey_state", [7, 8]),
            harvested("b_c002", "night_pain", "symptom", [9, 10]),
        ]

    def test_only_segments_continue_into_consolidation(self):
        segments, facets, symptoms = segmentation.partition_by_register(
            self.catalogue())
        self.assertEqual([row["candidate_key"] for row in segments],
                         ["desk_workers", "side_sleepers"])
        self.assertEqual([row["candidate_key"] for row in facets],
                         ["cost_sensitive", "cost_sensitive", "awaiting_mri"])
        self.assertEqual([row["candidate_key"] for row in symptoms], ["night_pain"])

    def test_partition_loses_no_discovery(self):
        catalogue = self.catalogue()
        parts = segmentation.partition_by_register(catalogue)
        self.assertEqual(sum(len(part) for part in parts), len(catalogue))
        self.assertEqual(
            {row["candidate_id"] for part in parts for row in part},
            {row["candidate_id"] for row in catalogue})

    def test_missing_register_is_treated_as_a_segment(self):
        """The taxonomy a register-less run already produced is preserved."""
        legacy = [{"candidate_id": "a_c000", "candidate_key": "desk_workers",
                   "evidence_ids": [1, 2]}]
        segments, facets, symptoms = segmentation.partition_by_register(legacy)
        self.assertEqual(len(segments), 1)
        self.assertEqual((facets, symptoms), ([], []))

    def test_facet_catalogue_groups_by_key_and_is_deterministic(self):
        _segments, facets, _symptoms = segmentation.partition_by_register(
            self.catalogue())
        catalogue = segmentation.facet_catalogue(facets)
        self.assertEqual([row["facet_key"] for row in catalogue],
                         ["awaiting_mri", "cost_sensitive"])
        cost = catalogue[1]
        self.assertEqual(cost["facet_type"], "attribute")
        self.assertEqual(cost["evidence_ids"], [4, 5, 6])
        self.assertEqual(cost["source_candidate_ids"], ["a_c002", "b_c000"])
        self.assertEqual(segmentation.facet_catalogue(facets), catalogue)

    def test_facet_catalogue_drops_one_off_mentions(self):
        thin = [harvested("a_c000", "kayak_ergonomics", "attribute", [1])]
        self.assertEqual(segmentation.facet_catalogue(thin), [])


class FacetVocabularyTests(unittest.TestCase):
    def provisional(self):
        return segmentation.facet_catalogue([
            harvested("a_c002", "cost_sensitive", "attribute", [4, 5],
                      cue_terms=["expensive"]),
            harvested("b_c000", "cost_sensitive", "attribute", [5, 6],
                      cue_terms=["cheap"]),
        ])

    def test_declared_vocabulary_absorbs_provisional_lineage(self):
        facets, unknown = segmentation.finalize_facets(
            [{"facet_key": "cost_sensitive", "name": "Cost sensitive",
              "definition": "Weighs price heavily", "facet_type": "attribute",
              "source_facet_ids": ["pfac_000"]}],
            self.provisional(), [])
        self.assertEqual(unknown, [])
        self.assertEqual(len(facets), 1)
        self.assertEqual(facets[0]["facet_id"], "fac_001")
        self.assertEqual(facets[0]["discovery_evidence_ids"], [4, 5, 6])
        self.assertEqual(facets[0]["cue_terms"], ["cheap", "expensive"])

    def test_unknown_provisional_reference_is_reported_not_fatal(self):
        facets, unknown = segmentation.finalize_facets(
            [{"facet_key": "cost_sensitive", "name": "Cost sensitive",
              "definition": "", "facet_type": "attribute",
              "source_facet_ids": ["pfac_000", "pfac_404"]}],
            self.provisional(), [])
        self.assertEqual(unknown, [{"facet_key": "cost_sensitive",
                                    "unknown_ids": ["pfac_404"]}])
        self.assertEqual(facets[0]["source_facet_ids"], ["pfac_000"])

    def test_reclassified_candidate_gets_an_entry_even_when_undeclared(self):
        """A forgotten declaration must never delete a candidate."""
        facets, _unknown = segmentation.finalize_facets(
            [], self.provisional(),
            [{"facet_key": "post_surgery", "facet_type": "journey_state",
              "name": "Post surgery", "definition": "Recovering from repair",
              "segment_id": "seg_009", "evidence_ids": [11, 12]}])
        self.assertEqual([row["facet_key"] for row in facets], ["post_surgery"])
        self.assertEqual(facets[0]["facet_type"], "journey_state")
        self.assertEqual(facets[0]["source_segment_ids"], ["seg_009"])
        self.assertEqual(facets[0]["discovery_evidence_count"], 2)

    def test_facet_ids_are_stable_across_replays(self):
        args = ([{"facet_key": "cost_sensitive", "name": "Cost sensitive",
                  "definition": "", "facet_type": "attribute",
                  "source_facet_ids": ["pfac_000"]}],
                self.provisional(),
                [{"facet_key": "awaiting_mri", "facet_type": "journey_state",
                  "name": "Awaiting MRI", "definition": "",
                  "segment_id": "seg_004", "evidence_ids": [3]}])
        first, _ = segmentation.finalize_facets(*args)
        second, _ = segmentation.finalize_facets(*args)
        self.assertEqual(first, second)
        self.assertEqual([row["facet_id"] for row in first], ["fac_001", "fac_002"])


class Stage04ReclassificationTests(unittest.TestCase):
    def candidates(self):
        return [
            {"segment_id": "seg_001", "slug": "desk_workers",
             "name": "Desk workers", "definition": "Desk-driven pain",
             "core_evidence_ids": [1, 2]},
            {"segment_id": "seg_002", "slug": "medication_takers",
             "name": "Medication takers", "definition": "Manage with painkillers",
             "core_evidence_ids": [3, 4, 5]},
        ]

    def decisions(self, facet_key="medication_use"):
        return [
            {"segment_id": "seg_001", "status": "validated",
             "rationale": "durable occupational context", "merged_into": "",
             "split_into": [], "facet_key": "", "facet_type": ""},
            {"segment_id": "seg_002", "status": "reclassified_as_facet",
             "rationale": "a response to the problem, not an audience",
             "merged_into": "", "split_into": [], "facet_key": facet_key,
             "facet_type": "attribute"},
        ]

    def test_reclassified_candidate_never_becomes_a_validated_segment(self):
        decisions = self.decisions()
        validated = [row for row in decisions if row["status"] == "validated"]
        self.assertEqual([row["segment_id"] for row in validated], ["seg_001"])

    def test_reclassification_carries_its_evidence_into_the_vocabulary(self):
        facets = cli._finalize_stage04_facets(
            [{"facet_key": "medication_use", "name": "Medication use",
              "definition": "Manages the problem pharmacologically",
              "facet_type": "attribute", "source_facet_ids": []}],
            [], self.decisions(), self.candidates())
        self.assertEqual(len(facets), 1)
        self.assertEqual(facets[0]["source_segment_ids"], ["seg_002"])
        self.assertEqual(facets[0]["discovery_evidence_ids"], [3, 4, 5])

    def test_reclassification_without_a_key_falls_back_to_the_slug(self):
        decisions = self.decisions(facet_key="")
        facets = cli._finalize_stage04_facets(
            [], [], decisions, self.candidates())
        self.assertEqual([row["facet_key"] for row in facets],
                         ["medication_takers"])
        self.assertEqual(decisions[1]["facet_key"], "medication_takers")


class AssignmentContractTests(unittest.TestCase):
    def test_facet_ids_are_closed_to_the_supplied_vocabulary(self):
        schema = cli.assignment_schema([1, 2], ["seg_001"], ["fac_001"])
        row = schema["properties"]["assignments"]["items"]["properties"]
        self.assertEqual(row["facet_ids"]["items"]["enum"], ["fac_001"])
        self.assertEqual(row["runner_up_segment_id"]["enum"], ["", "seg_001"])

    def test_no_vocabulary_means_no_taggable_facet(self):
        schema = cli.assignment_schema([1], ["seg_001"])
        row = schema["properties"]["assignments"]["items"]["properties"]
        self.assertEqual(row["facet_ids"]["maxItems"], 0)
        self.assertNotIn("enum", row["facet_ids"]["items"])

    def test_older_rows_load_but_are_marked_as_never_asked(self):
        rows = cli._normalize_assignment_contract([
            {"evidence_id": 1, "primary_segment_id": "seg_001",
             "assignment_status": "assigned",
             "secondary_attributes": ["surgery avoidant"]}])
        self.assertFalse(rows[0]["runner_up_recorded"])
        self.assertEqual(rows[0]["runner_up_segment_id"], "")
        self.assertEqual(rows[0]["facet_ids"], [])
        self.assertEqual(rows[0]["legacy_secondary_attributes"],
                         ["surgery avoidant"])
        self.assertNotIn("secondary_attributes", rows[0])

    def test_current_rows_are_marked_as_asked_including_an_honest_none(self):
        rows = cli._normalize_assignment_contract([
            {"evidence_id": 1, "primary_segment_id": "seg_001",
             "assignment_status": "assigned", "runner_up_segment_id": "",
             "facet_ids": ["fac_001"]}])
        self.assertTrue(rows[0]["runner_up_recorded"])
        self.assertEqual(rows[0]["facet_ids"], ["fac_001"])


class CooccurrenceTallyTests(unittest.TestCase):
    validated = [{"segment_id": "seg_001", "slug": "desk_workers"},
                 {"segment_id": "seg_002", "slug": "side_sleepers"},
                 {"segment_id": "seg_003", "slug": "swimmers"}]

    @staticmethod
    def row(winner, runner_up, status="assigned", recorded=True):
        return {"evidence_id": 1, "primary_segment_id": winner,
                "runner_up_segment_id": runner_up, "assignment_status": status,
                "runner_up_recorded": recorded}

    def tally(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            cli._measure_segment_graph(rows, self.validated, None, tmp)
            with open(os.path.join(tmp, "segment_cooccurrence.json"),
                      encoding="utf-8") as fh:
                return json.load(fh)

    def test_pairs_are_undirected_and_counted(self):
        result = self.tally([
            self.row("seg_001", "seg_002"),
            self.row("seg_002", "seg_001"),
            self.row("seg_001", "seg_003"),
        ])
        self.assertEqual(result["measured_from_assignments"], 3)
        self.assertEqual(result["raw_pairs"][0]["segment_ids"], ["seg_001", "seg_002"])
        self.assertEqual(result["raw_pairs"][0]["count"], 2)
        self.assertEqual(result["raw_pairs"][0]["slugs"],
                         ["desk_workers", "side_sleepers"])

    def test_rows_that_were_never_asked_are_excluded(self):
        result = self.tally([self.row("seg_001", "seg_002", recorded=False)])
        self.assertEqual(result["measured_from_assignments"], 0)
        self.assertEqual(result["raw_pairs"], [])

    def test_unassigned_and_demoted_segments_are_excluded(self):
        result = self.tally([
            self.row("seg_001", "seg_002", status="unassigned_ambiguous"),
            self.row("seg_001", "seg_404"),
            self.row("seg_001", ""),
            self.row("seg_001", "seg_001"),
        ])
        self.assertEqual(result["raw_pairs"], [])


class HarvestMigrationTests(unittest.TestCase):
    def test_pre_register_schema_is_the_contract_without_the_field(self):
        schema = segmentation.pre_register_harvest_schema()
        item = schema["properties"]["candidates"]["items"]
        self.assertNotIn("register", item["properties"])
        self.assertNotIn("register", item["required"])
        current = segmentation.HARVEST_SCHEMA["properties"]["candidates"]["items"]
        self.assertIn("register", current["properties"])

    def test_legacy_v3_schema_keeps_its_per_chunk_evidence_enum(self):
        schema = segmentation.legacy_harvest_schema_v3([7, 8])
        properties = schema["properties"]["candidates"]["items"]["properties"]
        self.assertEqual(properties["evidence_ids"]["items"]["enum"], [7, 8])
        self.assertNotIn("register", properties)

    def test_cached_rows_default_to_segment_without_being_re_judged(self):
        migrated = segmentation.migrate_legacy_harvest_rows([
            {"candidate_key": "medication_takers", "provisional_name": "Takers"}])
        self.assertEqual(migrated[0]["register"], "segment")

    def test_migration_never_overwrites_a_declared_register(self):
        migrated = segmentation.migrate_legacy_harvest_rows([
            {"candidate_key": "cost_sensitive", "register": "attribute"}])
        self.assertEqual(migrated[0]["register"], "attribute")

    def test_register_is_the_only_field_a_legacy_catalogue_gains(self):
        """So a migrated run's 03B input is byte-identical and its cache holds."""
        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2, 3],
                 "candidates": [{"candidate_key": "desk_workers",
                                 "provisional_name": "Desk workers",
                                 "audience_cue": "desk work",
                                 "why_commercially_distinct": "workday",
                                 "evidence_ids": [1, 2], "cue_terms": ["desk"],
                                 "discovery_strength": "strong"}]}
        migrated = dict(chunk)
        migrated["candidates"] = segmentation.migrate_legacy_harvest_rows(
            chunk["candidates"])
        cards = segmentation.aggregate_harvest([migrated])
        segments, facets, symptoms = segmentation.partition_by_register(cards)
        self.assertEqual((facets, symptoms), ([], []))
        for card in segments:
            self.assertEqual(card["register"], "segment")
        expected = {"candidate_id", "candidate_key", "occurrence_count",
                    "unique_evidence_count", "unique_chunk_count", "evidence_ids",
                    "chunk_ids", "aliases", "audience_cues", "commercial_cues",
                    "cue_terms", "strengths", "provenance"}
        self.assertEqual(set(segments[0]) - {"register"}, expected)

    def test_a_finished_register_less_chunk_is_still_recognised(self):
        """Silently rejecting these fingerprints re-harvests a whole corpus."""
        import dataclasses
        import llm

        chunk = {"chunk_id": "03a_0000", "evidence_ids": [1, 2],
                 "records": [{"id": 1, "text": "one", "tier": "core"},
                             {"id": 2, "text": "two", "tier": "core"}]}
        job = llm.Job(id=chunk["chunk_id"],
                      prompt=segmentation.harvest_prompt(chunk["records"]),
                      schema=segmentation.harvest_schema(chunk["evidence_ids"]))

        # Exactly what the immediately-previous contract wrote into its artifact.
        persisted = cli._job_fingerprint(
            dataclasses.replace(job, schema=segmentation.legacy_harvest_schema_v3(
                chunk["evidence_ids"])),
            chunk["evidence_ids"], segmentation.LEGACY_HARVEST_SKILL_V3,
            segmentation.LEGACY_HARVEST_CONTRACT_V2)
        compatible = cli._stage03a_compatible_fingerprints([job], [chunk])
        self.assertIn(persisted, compatible[chunk["chunk_id"]])

        # And it is genuinely a different contract, not an accidental match.
        current = cli._job_fingerprint(
            job, chunk["evidence_ids"], segmentation.LEGACY_HARVEST_SKILL_V3,
            segmentation.HARVEST_CONTRACT_VERSION)
        self.assertNotEqual(persisted, current)

    def test_a_fresh_row_must_declare_a_valid_register(self):
        row = {"candidate_key": "desk_workers", "provisional_name": "Desk workers",
               "audience_cue": "desk work", "why_commercially_distinct": "workday",
               "evidence_ids": [1], "cue_terms": ["desk"],
               "discovery_strength": "probable", "register": "audience"}
        with self.assertRaises(segmentation.HarvestContractError):
            segmentation.validate_harvest_rows([row], [1])
        row["register"] = "segment"
        segmentation.validate_harvest_rows([row], [1])


class RegisterPipelineIntegrationTests(unittest.TestCase):
    """One run, three registers, and a candidate demoted before it owns evidence."""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def estimate(self, *args, **kwargs):
            return mock.Mock()

        def prewarm(self, *args, **kwargs):
            return None

        def batch(self, corpus, preamble, jobs):
            self.calls.extend(job.id for job in jobs)
            replies = {}
            for job in jobs:
                if job.id.startswith("03a_"):
                    payload = {"candidates": [
                        {"candidate_key": "desk_workers",
                         "provisional_name": "Desk workers",
                         "audience_cue": "desk work dominates the context",
                         "why_commercially_distinct": "workday messaging",
                         "evidence_ids": list(job.expected_ids or (1, 2, 3)),
                         "cue_terms": ["desk"], "discovery_strength": "strong",
                         "register": "segment"},
                        {"candidate_key": "cost_sensitive",
                         "provisional_name": "Cost-sensitive shoppers",
                         "audience_cue": "balks at the price",
                         "why_commercially_distinct": "price-led objection",
                         "evidence_ids": [2, 4],
                         "cue_terms": ["expensive"],
                         "discovery_strength": "probable",
                         "register": "attribute"},
                        {"candidate_key": "night_pain",
                         "provisional_name": "Wakes at night",
                         "audience_cue": "cannot sleep on that side",
                         "why_commercially_distinct": "sleep-led hook",
                         "evidence_ids": [1, 3],
                         "cue_terms": ["night"], "discovery_strength": "probable",
                         "register": "symptom"}]}
                elif job.id.startswith("03e_"):
                    payload = {"matches": [
                        {"evidence_id": eid, "segment_ids": ["seg_001"],
                         "match_strength": "strong"} for eid in job.expected_ids]}
                elif job.id.startswith("05_"):
                    payload = {"assignments": [{
                        "evidence_id": eid, "primary_segment_id": "seg_001",
                        "score": 8, "winning_margin": 3,
                        "cue_types": ["dominant_context_match"],
                        "primary_cues": ["desk"], "rationale": "dominant context",
                        "assignment_status": "assigned",
                        "runner_up_segment_id": "",
                        "facet_ids": ["fac_001"] if eid % 2 else []}
                        for eid in job.expected_ids]}
                elif job.id.startswith("08a_"):
                    ids = job.schema["properties"]["signals"]["items"][
                        "properties"]["evidence_ids"]["items"]["enum"]
                    payload = {"signals": [{
                        "dimension": "pain_points", "label": "Pain after desk work",
                        "evidence_ids": ids, "representative_quotes": [],
                        "typical_outcome": "", "common_complaint": ""}]}
                else:
                    raise AssertionError(f"unexpected batch job {job.id}")
                replies[job.id] = BatchResult(json.dumps(payload), "stop")
            return replies

        def one_result(self, corpus, preamble, prompt, max_tokens, schema, **kwargs):
            job_id = kwargs.get("job_id")
            self.calls.append(job_id)
            if job_id in ("03b_consolidate", "03b_novelty_consolidate"):
                payload = {"candidates": [
                    {"slug": "desk_workers", "name": "Desk workers",
                     "definition": "Desk work is dominant",
                     "commercial_distinction": "workday ergonomic messaging",
                     "inclusion_criteria": ["desk work is dominant"],
                     "exclusion_criteria": ["incidental desk mention"],
                     "source_candidate_ids": ["03a_0000_c000"],
                     "merged_aliases": [], "discovery_status": "strong_candidate"},
                    {"slug": "brace_optimisers", "name": "Brace optimisers",
                     "definition": "Tuning a brace they already wear",
                     "commercial_distinction": "fit-led messaging",
                     "inclusion_criteria": ["already owns a brace"],
                     "exclusion_criteria": ["never tried one"],
                     "source_candidate_ids": ["03a_0000_c000"],
                     "merged_aliases": [], "discovery_status": "probable_candidate"}]}
            elif job_id == "04_validate":
                self.validation_prompt = prompt
                payload = {
                    "decisions": [
                        {"segment_id": "seg_001", "status": "validated",
                         "rationale": "durable occupational context",
                         "merged_into": "", "split_into": [],
                         "facet_key": "", "facet_type": ""},
                        {"segment_id": "seg_002",
                         "status": "reclassified_as_facet",
                         "rationale": "a response to the problem, not an audience",
                         "merged_into": "", "split_into": [],
                         "facet_key": "brace_use", "facet_type": "attribute"}],
                    "facet_vocabulary": [
                        {"facet_key": "cost_sensitive", "name": "Cost sensitive",
                         "definition": "Weighs price heavily",
                         "facet_type": "attribute",
                         "source_facet_ids": ["pfac_000"]},
                        {"facet_key": "brace_use", "name": "Brace use",
                         "definition": "Already wearing a brace",
                         "facet_type": "attribute", "source_facet_ids": []}],
                    "segment_edges": []}
            elif job_id == "07_commercial_coalesce":
                payload = {
                    "canonical_segments": [{
                        "canonical_key": "desk_workers", "slug": "desk_workers",
                        "name": "Desk workers",
                        "definition": "People whose desk work drives their pain.",
                        "commercial_distinction": "Workday context.",
                        "source_segment_ids": ["seg_001"], "subsegments": [],
                        "attributes": [], "inclusion_criteria": ["desk work"],
                        "exclusion_criteria": ["incidental"]}],
                    "research_segment_mappings": [{
                        "segment_id": "seg_001", "disposition": "parent",
                        "canonical_key": "desk_workers", "label": "Desk workers",
                        "rationale": "primary audience"}]}
            elif job_id.startswith("08b_"):
                themes = schema["properties"]["themes"]["items"]["properties"]
                payload = {"themes": [{
                    "dimension": "pain_points", "label": "Pain after desk work",
                    "source_signal_ids": themes["source_signal_ids"]["items"]["enum"],
                    "representative_evidence_ids":
                        themes["representative_evidence_ids"]["items"]["enum"][:1],
                    "summary": "Desk work triggers pain",
                    "typical_outcome": "", "common_complaint": ""}]}
            else:
                raise AssertionError(f"unexpected single job {job_id}")
            return BatchResult(json.dumps(payload), "stop")

    @staticmethod
    def args():
        return SimpleNamespace(
            source=None, rediscover=False, reassign=False, from_stage=None,
            novelty_supporting=False, segment_debug=False, yes=True,
            provider=None, model=None, effort=None)

    def project(self, root):
        voc = os.path.join(root, "research", "voc")
        os.makedirs(voc)
        rows = [{"id": n, "text": text, "tier": tier, "thread_id": f"t{n}",
                 "subreddit": "office"}
                for n, (text, tier) in enumerate([
                    ("I hurt after computer work", "core"),
                    ("My desk job causes this", "core"),
                    ("WFH sitting triggers it", "core"),
                    ("The brace I bought was pricey", "core"),
                    ("Ergonomic advice can help", "supporting")], start=1)]
        cli._write_jsonl(os.path.join(voc, cli.PRODUCTION_VOC_FILE), rows)
        cli._write_jsonl(os.path.join(voc, cli.AUDIT_VOC_FILE), rows)
        return {"_dir": root, "name": "test", "segmentation": {
            "03a_chunk_tokens": 10000, "03e_chunk_tokens": 10000,
            "03c_chunk_tokens": 10000, "05_chunk_tokens": 10000,
            "min_segment_evidence": 4}}

    def run_pipeline(self, tmp):
        cfg = self.project(tmp)
        client = self.FakeClient()
        with mock.patch.object(cli, "client", return_value=client), \
                mock.patch.object(llm, "confirm", return_value=True), \
                mock.patch.object(cli, "record_provenance"):
            cli.cmd_segment(cfg, self.args())
        return client

    def test_registers_partition_and_facets_reach_the_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self.run_pipeline(tmp)
            discovery = os.path.join(tmp, "research", "segments", "discovery")
            voc = os.path.join(tmp, "research", "voc")

            with open(os.path.join(discovery, "03a_register_partition.json"),
                      encoding="utf-8") as fh:
                partition = json.load(fh)
            self.assertEqual(partition["segment_candidates"], 1)
            self.assertEqual(partition["facet_candidates"], 1)
            self.assertEqual(partition["symptom_candidates"], 1)
            # The symptom left the taxonomy but is still on the record.
            self.assertEqual(partition["symptoms"][0]["candidate_key"], "night_pain")

            # 03B never saw the attribute or the symptom.
            with open(os.path.join(discovery, "03b_consolidated_candidates.json"),
                      encoding="utf-8") as fh:
                consolidated = json.load(fh)
            self.assertNotIn("night_pain", [row["slug"] for row in consolidated])

            with open(os.path.join(voc, "facet_vocabulary.json"),
                      encoding="utf-8") as fh:
                facets = json.load(fh)
            self.assertEqual([row["facet_key"] for row in facets],
                             ["brace_use", "cost_sensitive"])

            evidence = os.path.join(tmp, "research", "evidence", "desk_workers.txt")
            with open(evidence, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("FACETS PRESENT", text)
            self.assertIn("Brace use [attribute]", text)
            self.assertIn("FACETS: Brace use", text)

    def test_a_reclassified_candidate_never_owns_evidence(self):
        """The stranded-evidence fix: demotion happens before assignment."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_pipeline(tmp)
            voc = os.path.join(tmp, "research", "voc")
            with open(os.path.join(voc, "validated_segments.json"),
                      encoding="utf-8") as fh:
                decisions = json.load(fh)
            reclassified = [row["segment_id"] for row in decisions
                            if row["status"] == "reclassified_as_facet"]
            self.assertEqual(reclassified, ["seg_002"])

            assignments = [json.loads(line) for line in open(
                os.path.join(voc, "segment_assignments.jsonl"), encoding="utf-8")]
            self.assertTrue(assignments)
            self.assertNotIn("seg_002",
                             {row["primary_segment_id"] for row in assignments})
            # And no evidence file was built for it, so nothing downstream can
            # fold its comments into another segment's count.
            self.assertFalse(os.path.exists(os.path.join(
                tmp, "research", "evidence", "brace_optimisers.txt")))

    def test_the_cooccurrence_tally_is_written_from_recorded_runner_ups(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_pipeline(tmp)
            with open(os.path.join(tmp, "research", "voc",
                                   "segment_cooccurrence.json"),
                      encoding="utf-8") as fh:
                tally = json.load(fh)
            # One validated segment, so every runner-up is honestly empty.
            self.assertEqual(tally["raw_pairs"], [])
            self.assertEqual(tally["measured_from_assignments"], 0)

    def test_layer_two_packs_are_built_and_readable_by_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_pipeline(tmp)
            cfg = {"_dir": tmp, "name": "test", "segmentation": {}}
            pack = cli.research_pack_path(cfg, "desk_workers")
            with open(pack, encoding="utf-8") as fh:
                text = fh.read()
            # No relationships in this fixture, so Layer 2 is Layer 1 plus a
            # header — and the counts still say so explicitly.
            self.assertIn("Primary evidence items (this segment only): 5", text)
            self.assertNotIn("BORROWED CONTEXT", text)

            with open(os.path.join(tmp, "research", "voc",
                                   "research_pack_manifest.json"),
                      encoding="utf-8") as fh:
                manifest = json.load(fh)
            self.assertEqual(manifest["packs"][0]["borrowed_item_count"], 0)
            self.assertEqual(manifest["context_budget_tokens"], 8000)

    def test_a_missing_pack_names_the_fix_rather_than_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"_dir": tmp, "name": "test", "segmentation": {}}
            with self.assertRaises(SystemExit) as caught:
                cli.research_pack_path(cfg, "desk_workers")
            self.assertIn("--pack", str(caught.exception))

    def test_the_graph_is_persisted_and_reused_without_a_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_pipeline(tmp)
            graph_path = os.path.join(tmp, "research", "voc",
                                      "segment_graph.json")
            with open(graph_path, encoding="utf-8") as fh:
                graph = json.load(fh)
            self.assertEqual(graph, {"specialises": [], "adjacent": []})

            # A resume reads the persisted graph rather than re-deriving it, so
            # every child is judged as a child on the second run too.
            cfg = {"_dir": tmp, "name": "test",
                   "segmentation": {"min_segment_evidence": 4}}
            voc = os.path.join(tmp, "research", "voc")
            _validated, _decisions, _rows, reused = (
                cli._completed_segmentation_inputs(cfg, voc))
            self.assertEqual(reused, graph)


if __name__ == "__main__":
    unittest.main()
