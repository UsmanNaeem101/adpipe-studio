"""Who gets into the research corpus, and on what evidence.

Four rules that were previously prose. Each had the same failure shape on the
montisella run: a skill stated a bar, no code checked it, and the stage that
inherited the consequences had no way to tell a policy decision from a mistake.

  * Stage 01 was told to keep borderline items "and let a downstream skill
    decide" — 86% retained, 28% usable, 1.2% for items kept on one signal.
  * Stage 05 declared score>=6/margin>=2 a run-failure condition and then read
    both fields only as sort keys; 118 rows were assigned below the bar.
  * Skill 04 ruled that one thread is a conversation rather than an audience,
    and validated 70 segments, 19 of them on three threads or fewer.
  * Nothing let a project say which population it was researching, so a corpus
    scraped largely from diagnosis subreddits produced diagnosis segments.
"""

import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import corpus_policy


def retained(evidence_id, reasons, url="", **extra):
    row = {"id": evidence_id, "text": f"comment {evidence_id}", "url": url,
           "decision": "retain", "retention_reasons": list(reasons),
           "rejection_reasons": []}
    row.update(extra)
    return row


def refine(rows, cfg=None):
    """Run the deterministic export and return (production, audit, summary)."""
    with tempfile.TemporaryDirectory() as tmp:
        voc = os.path.join(tmp, "research", "voc")
        os.makedirs(voc)
        cli._write_jsonl(os.path.join(voc, "deduplicated_voc.jsonl"), rows)
        project = dict(cfg or {})
        project["_dir"] = tmp
        summary = cli.refine_voc(project, announce=False)
        return (cli._read_jsonl(os.path.join(voc, cli.PRODUCTION_VOC_FILE)),
                cli._read_jsonl(os.path.join(voc, cli.AUDIT_VOC_FILE)),
                summary)


class CorroborationTests(unittest.TestCase):
    def cut(self, *reasons):
        return corpus_policy.corroboration_cut(
            {"retention_reasons": list(reasons)})

    def test_a_lone_signal_is_not_evidence(self):
        self.assertEqual(self.cut("specific_problem"), corpus_policy.SINGLE_SIGNAL)
        self.assertEqual(self.cut("first_person_experience"),
                         corpus_policy.SINGLE_SIGNAL)

    def test_a_first_person_account_needs_one_corroborating_signal(self):
        self.assertIsNone(self.cut("first_person_experience", "specific_problem"))

    def test_a_second_hand_report_needs_three(self):
        """The measured case: third-person without first-person ran at 6.9%."""
        self.assertEqual(self.cut("third_person_observation", "belief"),
                         corpus_policy.UNCORROBORATED_REPORT)
        self.assertIsNone(
            self.cut("third_person_observation", "belief", "specific_problem"))

    def test_no_reasons_at_all_is_cut_not_crashed(self):
        self.assertEqual(self.cut(), corpus_policy.SINGLE_SIGNAL)

    def test_a_legacy_row_without_reason_codes_is_left_alone(self):
        """Lean production files predate the vocabulary; gating them on absent
        evidence would delete an entire corpus on the next refine-voc."""
        self.assertIsNone(corpus_policy.corroboration_cut(
            {"id": 1, "text": "legacy", "tier": "core"}))

    def test_refine_voc_holds_back_without_losing_anything(self):
        production, audit, summary = refine([
            retained(1, ["first_person_experience", "specific_problem"]),
            retained(2, ["specific_problem"]),
            retained(3, ["third_person_observation", "belief"]),
        ])
        self.assertEqual([row["id"] for row in production], [1])
        self.assertEqual([row["id"] for row in audit], [1, 2, 3])
        self.assertEqual(audit[1]["production_excluded"],
                         corpus_policy.SINGLE_SIGNAL)
        self.assertEqual(audit[2]["production_excluded"],
                         corpus_policy.UNCORROBORATED_REPORT)
        self.assertNotIn("production_excluded", audit[0])
        self.assertEqual(summary["held_back"], {
            corpus_policy.SINGLE_SIGNAL: 1,
            corpus_policy.UNCORROBORATED_REPORT: 1})

    def test_held_back_rows_keep_their_text_for_recovery(self):
        """Reversing the policy must not need another model call."""
        _production, audit, _summary = refine([retained(2, ["specific_problem"])])
        self.assertEqual(audit[0]["text"], "comment 2")
        self.assertEqual(audit[0]["retention_reasons"], ["specific_problem"])


class PopulationScopeTests(unittest.TestCase):
    def test_the_default_scope_is_everyone(self):
        self.assertEqual(corpus_policy.scope({}), corpus_policy.SCOPE_ALL)
        self.assertEqual(corpus_policy.excluded_subreddits({}), frozenset())

    def test_a_misspelled_scope_is_refused_rather_than_assumed(self):
        with self.assertRaises(ValueError) as caught:
            corpus_policy.scope({"audience": {"population_scope": "manstream"}})
        self.assertIn("manstream", str(caught.exception))

    def test_mainstream_excludes_the_clinical_communities(self):
        cfg = {"audience": {"population_scope": "mainstream"}}
        for name in ("ehlersdanlos", "EhlersDanlos", "r/endometriosis",
                     "Fibromyalgia", "ChronicPain"):
            self.assertTrue(corpus_policy.subreddit_excluded(name, cfg), name)
        for name in ("flexibility", "Fitness", "Ergonomics", "formcheck"):
            self.assertFalse(corpus_policy.subreddit_excluded(name, cfg), name)

    def test_a_project_can_exclude_communities_at_any_scope(self):
        cfg = {"audience": {"excluded_subreddits": ["evilautism", "r/90DayFiance"]}}
        self.assertTrue(corpus_policy.subreddit_excluded("evilautism", cfg))
        self.assertTrue(corpus_policy.subreddit_excluded("90dayfiance", cfg))
        self.assertFalse(corpus_policy.subreddit_excluded("ehlersdanlos", cfg))

    def test_inclusion_overrules_both_the_default_list_and_the_project(self):
        """A brace brand selling into scoliosis must be able to say so."""
        cfg = {"audience": {"population_scope": "mainstream",
                            "excluded_subreddits": ["scoliosis"],
                            "included_subreddits": ["scoliosis"]}}
        self.assertFalse(corpus_policy.subreddit_excluded("scoliosis", cfg))
        self.assertTrue(corpus_policy.subreddit_excluded("ehlersdanlos", cfg))

    def test_a_missing_subreddit_is_never_excluded(self):
        cfg = {"audience": {"population_scope": "mainstream"}}
        self.assertFalse(corpus_policy.subreddit_excluded(None, cfg))
        self.assertFalse(corpus_policy.subreddit_excluded("", cfg))

    def test_the_scope_rule_reaches_the_model_only_when_scoped(self):
        self.assertEqual(corpus_policy.scope_prompt({}), "")
        prompt = corpus_policy.scope_prompt(
            {"audience": {"population_scope": "mainstream"}})
        self.assertIn("AUDIENCE SCOPE", prompt)
        self.assertIn("desk work", prompt)

    def test_the_unscoped_schema_is_the_shared_object(self):
        """Callers and test doubles identify the stage by `is FILTER_SCHEMA`."""
        self.assertIs(cli.filter_schema({}), cli.FILTER_SCHEMA)

    def test_only_a_scoped_run_may_reject_for_who_someone_is(self):
        base = cli.filter_schema({})["properties"]["records"]["items"][
            "properties"]["rejection_reasons"]["items"]["enum"]
        self.assertNotIn(corpus_policy.CLINICAL_POPULATION, base)
        scoped = cli.filter_schema(
            {"audience": {"population_scope": "mainstream"}})
        codes = scoped["properties"]["records"]["items"]["properties"][
            "rejection_reasons"]["items"]["enum"]
        self.assertIn(corpus_policy.CLINICAL_POPULATION, codes)
        self.assertIsNot(scoped, cli.FILTER_SCHEMA)

    def test_refine_voc_applies_scope_to_an_already_filtered_corpus(self):
        """Re-scoping is a free re-run of refine-voc, not another Stage 01."""
        rows = [
            retained(1, ["first_person_experience", "specific_problem"],
                     url="https://www.reddit.com/r/flexibility/comments/a1/x/"),
            retained(2, ["first_person_experience", "specific_problem"],
                     url="https://www.reddit.com/r/ehlersdanlos/comments/b2/y/"),
        ]
        production, audit, summary = refine(
            rows, {"audience": {"population_scope": "mainstream"}})
        self.assertEqual([row["id"] for row in production], [1])
        self.assertEqual(audit[1]["production_excluded"],
                         corpus_policy.OUT_OF_SCOPE_SUBREDDIT)
        self.assertEqual(summary["audience_scope"], "mainstream")

    def test_the_same_corpus_is_whole_again_when_scope_is_lifted(self):
        rows = [retained(2, ["first_person_experience", "specific_problem"],
                         url="https://www.reddit.com/r/ehlersdanlos/comments/b2/y/")]
        self.assertEqual(len(refine(rows)[0]), 1)
        self.assertEqual(
            len(refine(rows, {"audience": {"population_scope": "mainstream"}})[0]),
            0)


class IngestScopeTests(unittest.TestCase):
    """Excluded communities must not be paid for at all."""

    def judged_records(self, cfg_extra):
        captured = {}

        def capture(_c, _prefix, _preamble, jobs, *args, **kwargs):
            captured["jobs"] = jobs
            raise SystemExit(0)

        # Distinct text per record: the deterministic pre-pass drops
        # byte-identical captures, which would otherwise mask the scope cut.
        raw = "\n\n".join(
            f"URL: https://www.reddit.com/r/{sub}/comments/t{n}/thread/\n"
            f"TITLE: t{n}\n\n"
            f"[1] My {sub} shoulder aches every single night and nothing helps."
            for n, sub in enumerate(("flexibility", "ehlersdanlos", "Ergonomics")))
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "raw.txt")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "pillow", "market": "pain"}
            cfg.update(cfg_extra)
            args = SimpleNamespace(source=source, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=mock.Mock()), \
                    mock.patch.object(cli, "_run_stage01_adaptive", capture), \
                    mock.patch("llm.confirm", return_value=True), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                with self.assertRaises(SystemExit):
                    cli.cmd_ingest(cfg, args)
            texts = [line for job in captured.get("jobs", [])
                     for line in job.prompt.splitlines() if line.startswith("[")]
            return texts, out.getvalue()

    def test_an_excluded_community_never_reaches_the_model(self):
        judged, printed = self.judged_records(
            {"audience": {"population_scope": "mainstream"}})
        self.assertEqual(len(judged), 2)
        self.assertIn("r/ehlersdanlos", printed)
        self.assertIn("audience scope 'mainstream'", printed)

    def test_the_whole_corpus_is_judged_when_unscoped(self):
        judged, _printed = self.judged_records({})
        self.assertEqual(len(judged), 3)


class AssignmentThresholdTests(unittest.TestCase):
    def rows(self, *scores):
        return [{"evidence_id": n, "primary_segment_id": "seg_001",
                 "score": score, "winning_margin": margin,
                 "cue_types": [], "primary_cues": [], "rationale": "why",
                 "assignment_status": "assigned", "secondary_attributes": []}
                for n, (score, margin) in enumerate(scores, 1)]

    def few_bad(self, *scores):
        """The offending rows, padded to a rate the repair path tolerates.

        A demotion test that trips the run-failure limit stops testing demotion,
        so the padding is what keeps these about the mechanics rather than the
        ceiling — which has tests of its own below.
        """
        return self.rows(*([(9, 4)] * 100 + list(scores)))

    def enforce(self, rows, verbose=False):
        with tempfile.TemporaryDirectory() as tmp:
            violations = cli._enforce_assignment_thresholds(rows, tmp, verbose)
            path = os.path.join(tmp, "threshold_violations.jsonl")
            audited = (cli._read_jsonl(path) if os.path.exists(path) else [])
        return violations, audited

    def test_an_assignment_at_the_bar_survives(self):
        rows = self.rows((6, 2))
        violations, _audited = self.enforce(rows)
        self.assertEqual(violations, [])
        self.assertEqual(rows[0]["assignment_status"], "assigned")

    def test_a_below_threshold_score_is_demoted(self):
        """The montisella case: 94 rows assigned at score 5."""
        rows = self.few_bad((5, 3))
        violations, audited = self.enforce(rows)
        self.assertEqual(len(violations), 1)
        self.assertEqual(rows[-1]["assignment_status"],
                         "unassigned_insufficient_evidence")
        self.assertEqual(rows[-1]["primary_segment_id"], "")
        self.assertEqual(rows[0]["assignment_status"], "assigned")
        self.assertEqual(audited[0]["evidence_id"], rows[-1]["evidence_id"])
        self.assertEqual(audited[0]["required"], {"score": 6, "margin": 2})

    def test_a_below_threshold_margin_is_demoted(self):
        """30 of the montisella rows won by a margin under 2."""
        rows = self.few_bad((10, 1))
        violations, _audited = self.enforce(rows)
        self.assertEqual(len(violations), 1)
        self.assertEqual(rows[-1]["assignment_status"],
                         "unassigned_insufficient_evidence")

    def test_the_model_s_own_reasoning_survives_demotion(self):
        """Demoted, not deleted — the score and rationale stay auditable."""
        rows = self.few_bad((5, 3))
        self.enforce(rows)
        self.assertEqual(rows[-1]["score"], 5)
        self.assertEqual(rows[-1]["winning_margin"], 3)
        self.assertEqual(rows[-1]["rationale"], "why")

    def test_unassigned_rows_are_never_touched(self):
        rows = self.rows((0, 0))
        rows[0]["assignment_status"] = "unassigned_no_matching_segment"
        rows[0]["primary_segment_id"] = ""
        violations, _audited = self.enforce(rows)
        self.assertEqual(violations, [])
        self.assertEqual(rows[0]["assignment_status"],
                         "unassigned_no_matching_segment")

    def test_a_stage_working_to_a_different_rule_fails_the_run(self):
        """Quietly repairing this many rows would hide a broken stage."""
        rows = self.rows(*[(5, 3)] * 10)
        with mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.enforce(rows, verbose=True)
        self.assertIn("--reassign", str(caught.exception))

    def test_a_handful_of_slips_is_repaired_and_reported(self):
        rows = self.few_bad((5, 3))
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            violations, _audited = self.enforce(rows, verbose=True)
        self.assertEqual(len(violations), 1)
        self.assertIn("demoted to unassigned", buf.getvalue())

    def test_the_schema_bounds_what_the_model_may_report(self):
        row = cli.ASSIGN_SCHEMA["properties"]["assignments"]["items"]["properties"]
        self.assertEqual(row["score"]["minimum"], 0)
        self.assertEqual(row["winning_margin"]["minimum"], 0)


class SegmentFloorTests(unittest.TestCase):
    def candidate(self, segment_id, threads, evidence):
        return {"segment_id": segment_id, "slug": f"slug_{segment_id}",
                "unique_thread_count": threads, "unique_evidence_count": evidence}

    def apply(self, candidates, cfg=None):
        decisions = [{"segment_id": row["segment_id"], "status": "validated",
                      "rationale": "looked distinct"} for row in candidates]
        with mock.patch("sys.stdout", io.StringIO()):
            kept = cli._apply_segment_floors(candidates, decisions, cfg or {})
        return [row["segment_id"] for row in kept], decisions

    def test_a_segment_on_too_few_threads_is_demoted(self):
        """62 items from 5 threads is five conversations, not an audience."""
        kept, decisions = self.apply([
            self.candidate("seg_001", 22, 66),
            self.candidate("seg_012", 3, 62),
        ])
        self.assertEqual(kept, ["seg_001"])
        self.assertEqual(decisions[1]["status"], "needs_more_research")
        self.assertIn("evidence floor", decisions[1]["rationale"])
        self.assertIn("looked distinct", decisions[1]["rationale"])

    def test_a_segment_with_too_little_evidence_is_demoted(self):
        kept, _decisions = self.apply([self.candidate("seg_099", 9, 4)])
        self.assertEqual(kept, [])

    def test_demotion_is_to_more_research_not_rejection(self):
        """A thin candidate is usually an under-sampled audience, not a wrong one."""
        _kept, decisions = self.apply([self.candidate("seg_099", 1, 1)])
        self.assertEqual(decisions[0]["status"], "needs_more_research")

    def test_a_segment_exactly_on_both_floors_survives(self):
        floors = corpus_policy.segment_floors({})
        kept, _decisions = self.apply([self.candidate("seg_001", *floors)])
        self.assertEqual(kept, ["seg_001"])

    def test_the_floors_are_configurable(self):
        cfg = {"segmentation": {"min_segment_threads": 10,
                                "min_segment_evidence": 50}}
        kept, _decisions = self.apply([self.candidate("seg_001", 4, 8)], cfg)
        self.assertEqual(kept, [])

    def test_the_montisella_taxonomy_would_have_been_cut(self):
        """19 of 70 validated segments rested on three threads or fewer."""
        floors = corpus_policy.segment_floors({})
        thin = self.candidate("seg_008", 3, 23)
        self.assertIsNotNone(corpus_policy.floor_failure(thin, *floors))

    def test_a_candidate_missing_its_counts_is_demoted_not_crashed(self):
        kept, _decisions = self.apply([{"segment_id": "seg_x", "slug": "x"}])
        self.assertEqual(kept, [])


class AssignedFloorTests(unittest.TestCase):
    """The floor that bites, applied where the true counts exist.

    A Stage 03 card counts the threads a candidate was discovered across, which
    is an upper bound on its real support: `fitness_overuse_rotator_cuff` showed
    16 on its card and 2 in the finished segment. Gating on cards caught 1 of 70
    montisella segments; gating here catches the 19 that rest on <=3 threads.
    """

    def scenario(self, spread):
        """spread: {segment_id: [thread_id per assigned item]}."""
        validated, decisions, rows, by_id = [], [], [], {}
        evidence_id = 0
        for segment_id, threads in spread.items():
            validated.append({"segment_id": segment_id, "slug": f"s_{segment_id}"})
            decisions.append({"segment_id": segment_id, "status": "validated",
                              "rationale": "distinct"})
            for thread in threads:
                evidence_id += 1
                by_id[evidence_id] = {"id": evidence_id, "thread_id": thread}
                rows.append({"evidence_id": evidence_id,
                             "primary_segment_id": segment_id,
                             "assignment_status": "assigned"})
        return validated, decisions, rows, by_id

    def apply(self, spread, cfg=None):
        validated, decisions, rows, by_id = self.scenario(spread)
        with mock.patch("sys.stdout", io.StringIO()):
            kept = cli._apply_assigned_floors(
                validated, decisions, rows, by_id, cfg or {})
        return [row["segment_id"] for row in kept], decisions, rows

    def test_a_wide_segment_survives(self):
        kept, _d, _r = self.apply({"seg_001": [f"t{n}" for n in range(10)]})
        self.assertEqual(kept, ["seg_001"])

    def test_many_items_from_few_threads_is_a_conversation(self):
        """62 items from 5 threads was a real montisella segment."""
        kept, decisions, _rows = self.apply({"seg_012": ["t1", "t2", "t3"] * 20})
        self.assertEqual(kept, [])
        self.assertEqual(decisions[0]["status"], "needs_more_research")
        self.assertIn("3 thread(s)", decisions[0]["rationale"])

    def test_demoted_evidence_returns_to_the_unassigned_pool(self):
        _kept, _decisions, rows = self.apply({"seg_099": ["t1", "t1"]})
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["assignment_status"],
                             "unassigned_insufficient_evidence")
            self.assertEqual(row["primary_segment_id"], "")

    def test_a_surviving_segment_keeps_its_evidence(self):
        wide = {f"t{n}" for n in range(9)}
        kept, _decisions, rows = self.apply({
            "seg_001": sorted(wide), "seg_099": ["t1"]})
        self.assertEqual(kept, ["seg_001"])
        survivors = [r for r in rows if r["primary_segment_id"] == "seg_001"]
        self.assertEqual(len(survivors), 9)
        for row in survivors:
            self.assertEqual(row["assignment_status"], "assigned")

    def test_items_sharing_one_thread_count_once(self):
        """Twenty replies under one post are one conversation, not twenty."""
        kept, _decisions, _rows = self.apply({"seg_099": ["t1"] * 20})
        self.assertEqual(kept, [])

    def test_evidence_without_a_thread_id_falls_back_to_the_url(self):
        validated = [{"segment_id": "seg_001", "slug": "s"}]
        decisions = [{"segment_id": "seg_001", "status": "validated",
                      "rationale": "r"}]
        rows, by_id = [], {}
        for n in range(8):
            by_id[n] = {"id": n, "url": f"https://example.test/post/{n}"}
            rows.append({"evidence_id": n, "primary_segment_id": "seg_001",
                         "assignment_status": "assigned"})
        with mock.patch("sys.stdout", io.StringIO()):
            kept = cli._apply_assigned_floors(
                validated, decisions, rows, by_id, {})
        self.assertEqual([row["segment_id"] for row in kept], ["seg_001"])

    def test_already_unassigned_rows_are_not_counted_as_support(self):
        validated = [{"segment_id": "seg_001", "slug": "s"}]
        decisions = [{"segment_id": "seg_001", "status": "validated",
                      "rationale": "r"}]
        by_id = {n: {"id": n, "thread_id": f"t{n}"} for n in range(20)}
        rows = [{"evidence_id": n, "primary_segment_id": "seg_001",
                 "assignment_status": "unassigned_ambiguous"} for n in range(20)]
        with mock.patch("sys.stdout", io.StringIO()):
            kept = cli._apply_assigned_floors(
                validated, decisions, rows, by_id, {})
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
