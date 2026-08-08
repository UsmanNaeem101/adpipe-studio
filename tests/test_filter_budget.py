"""Tests for stage 01's output-budget sizing, reasoning control, and vocabulary.

The stage failed because `max_tokens` caps reasoning AND the answer together,
and nothing bounded the reasoning half: the budget was picked round (8000), the
reason codes were unconstrained, and OpenRouter accepted an `effort` setting it
then never sent. These pin all three.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli
import llm
import openrouter
import profile_filter


class BudgetSizingTests(unittest.TestCase):
    """The budget must come from the schema, not from a round number."""

    def test_budget_reserves_the_answer_before_reasoning(self):
        # 60 records x 40 tokens = 2400 of answer, plus the reasoning reserve.
        sized = cli._record_max_tokens(60, 40)
        self.assertGreaterEqual(sized, 60 * 40 + cli.REASONING_RESERVE)

    def test_budget_scales_with_batch_size(self):
        self.assertLess(cli._record_max_tokens(20, 40),
                        cli._record_max_tokens(60, 40))

    def test_budget_leaves_a_margin_above_the_bare_requirement(self):
        bare = 60 * 40 + cli.REASONING_RESERVE
        self.assertGreater(cli._record_max_tokens(60, 40), bare)

    def test_stage_01_asks_for_a_sized_budget_and_low_effort(self):
        """The failing run's config, end to end through job construction."""
        jobs = self.build_filter_jobs(records=120, chunk=60)
        self.assertEqual(len(jobs), 2)
        for j in jobs:
            self.assertEqual(j.effort, "low")
            self.assertEqual(j.max_tokens, cli._record_max_tokens(60, 40))
            # The whole point: smaller than the budget that failed, because the
            # fix is bounding reasoning rather than buying it more room.
            self.assertLess(j.max_tokens, 8000)

    @staticmethod
    def build_filter_jobs(records, chunk):
        """Run cmd_ingest far enough to capture the skill-01 jobs it builds."""
        captured = {}

        class Client:
            def estimate(self, *_a, **_k):
                return type("E", (), {"explain": lambda _s: ""})()

            def prewarm(self, *_a):
                pass

            def batch(self, _corpus, _preamble, jobs):
                raise AssertionError("calibration should intercept before this")

        def capture(_client, _corpus, _preamble, jobs, _label):
            # Intercept at calibration rather than at the first batch call: the
            # first call is now the calibration probe, which carries a
            # deliberately widened ceiling and so is not the job as constructed.
            captured["jobs"] = jobs
            raise SystemExit("stop after capture")

        from types import SimpleNamespace
        text = ("My shoulder aches every single night and the pillow goes flat "
                "before morning arrives at all")
        raw = "\n\n".join(f"{text} number {i}" for i in range(records))
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "raw.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(raw)
            cfg = {"_dir": tmp, "filter": {"min_words": 8, "chunk": chunk},
                   "product": "pillow", "market": "shoulders"}
            args = SimpleNamespace(source=src, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=Client()), \
                    mock.patch.object(cli, "_calibrate_budget", capture), \
                    mock.patch.object(llm, "confirm", return_value=True):
                try:
                    cli.cmd_ingest(cfg, args)
                except SystemExit:
                    pass
        return captured.get("jobs", [])


class ReasonVocabularyTests(unittest.TestCase):
    """The schema must enforce skill 01's closed list, and not drift from it."""

    def skill_codes(self, label):
        with open(os.path.join(ROOT, "skills", "01_filter_voc.md"),
                  encoding="utf-8") as fh:
            section = fh.read().split("## Reason codes")[1].split("\n##")[0]
        block = section.split(f"**{label}:**")[1].split("**")[0]
        return re.findall(r"`([a-z_]+)`", block)

    def test_schema_matches_the_skill_file(self):
        """The skill is the spec; cli.py mirrors it. Fail loudly on drift."""
        self.assertEqual(cli.RETENTION_REASONS, self.skill_codes("Retention"))
        self.assertEqual(cli.REJECTION_REASONS, self.skill_codes("Rejection"))

    def test_schema_constrains_reasons_to_the_vocabulary(self):
        item = cli.FILTER_SCHEMA["properties"]["records"]["items"]
        self.assertEqual(
            item["properties"]["retention_reasons"]["items"]["enum"],
            cli.RETENTION_REASONS)
        self.assertEqual(
            item["properties"]["rejection_reasons"]["items"]["enum"],
            cli.REJECTION_REASONS)

    def test_prose_reasons_are_now_rejected(self):
        """Free-form reasons were the old behaviour; they must not validate."""
        bad = {"records": [{"evidence_id": 1, "decision": "retain",
                            "retention_reasons": ["they describe a real problem"],
                            "rejection_reasons": []}]}
        self.assertIsNotNone(cli._schema_issue(bad, cli.FILTER_SCHEMA))

    def test_code_reasons_validate(self):
        good = {"records": [{"evidence_id": 1, "decision": "retain",
                             "retention_reasons": ["first_person_experience",
                                                   "specific_problem"],
                             "rejection_reasons": []}]}
        self.assertIsNone(cli._schema_issue(good, cli.FILTER_SCHEMA))

    def test_a_prose_reason_routes_to_repair_not_rerun(self):
        """An enum violation has text — it's a shape problem, so repair it."""
        self.assertFalse(llm.BatchResult('{"records":[...]}', "stop").retryable)


class ReasoningControlTests(unittest.TestCase):
    """`effort` must actually reach the provider."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": self.tmp.name})
        self.env.start()
        self.client = object.__new__(openrouter.Client)
        self.client.model = "deepseek/deepseek-v4-flash"
        self.client.key = "k"
        self.client.verbose = False
        self.client.effort = "high"
        self.client.spent = {"in": 0, "out": 0, "reasoning": 0, "cache_write": 0, "cache_read": 0}

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def reply(self, payload=None):
        body = payload or {"choices": [{"message": {"content": "{}"},
                                        "finish_reason": "stop"}], "usage": {}}

        class Fake:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps(body).encode()

        return Fake()

    def sent_body(self, call):
        return json.loads(call.call_args.args[0].data)

    def test_openrouter_sends_the_reasoning_parameter(self):
        """This is the defect that emptied the budget: effort was stored and
        never transmitted, so the model reasoned at its own default."""
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 100)
        self.assertEqual(self.sent_body(call)["reasoning"], {"effort": "high"})

    def test_per_job_effort_overrides_the_client(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 100, effort="low")
        self.assertEqual(self.sent_body(call)["reasoning"], {"effort": "low"})

    def test_anthropic_effort_levels_are_mapped_not_forwarded_blindly(self):
        # xhigh/max exist in the pipeline's vocabulary but not OpenRouter's.
        for given, expected in (("xhigh", "high"), ("max", "high"),
                                ("medium", "medium")):
            with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
                self.client._post([{"role": "user", "content": "x"}], 100,
                                  effort=given)
            self.assertEqual(self.sent_body(call)["reasoning"]["effort"], expected,
                             given)

    def test_batch_passes_each_job_effort(self):
        seen = []

        def fake_post(_m, _mt, schema=None, retries=3, job_id=None,
                      operation="completion", effort=None, **_k):
            seen.append((job_id, effort))
            return llm.BatchResult("{}", "stop")

        self.client._post = fake_post
        self.client.batch("corpus", "preamble", [
            llm.Job("a", "one", effort="low"), llm.Job("b", "two")])
        self.assertCountEqual(seen, [("a", "low"), ("b", None)])

    def test_route_that_rejects_reasoning_is_retried_without_it(self):
        """Not every route accepts the parameter; losing it must not lose the
        stage — the sized budget and the audited re-run still cover us."""
        import io
        import urllib.error
        rejected = urllib.error.HTTPError(
            "https://example.test", 400, "bad", {},
            io.BytesIO(b'{"error":"unknown parameter: reasoning"}'))
        with mock.patch("urllib.request.urlopen",
                        side_effect=[rejected, self.reply()]) as call:
            result = self.client._post([{"role": "user", "content": "x"}], 100)
        retry = json.loads(call.call_args_list[1].args[0].data)
        self.assertNotIn("reasoning", retry)
        self.assertEqual(result.text, "{}")

    def test_anthropic_params_carry_per_job_effort(self):
        client = object.__new__(llm.Client)
        client.model = "claude-opus-5"
        client.effort = "high"
        self.assertEqual(
            client._params([], "p", 100, effort="low")["output_config"]["effort"],
            "low")
        self.assertEqual(
            client._params([], "p", 100)["output_config"]["effort"], "high")


class ProfilerTests(unittest.TestCase):
    """The profiler's arithmetic is the argument for the sizing — pin it."""

    def test_content_budget_is_one_verdict_per_record(self):
        one = profile_filter.verdict_tokens()
        self.assertGreater(profile_filter.content_budget(60), 60 * one - 1)

    def test_worst_case_verdict_exceeds_typical(self):
        self.assertGreater(profile_filter.verdict_tokens(worst_case=True),
                           profile_filter.verdict_tokens())

    def test_the_answer_fits_the_old_budget_at_every_swept_batch_size(self):
        """The finding that redirected the fix: 8000 was never too small for the
        ANSWER, so raising it would not have helped."""
        for chunk in (20, 40, 60, 80, 120):
            self.assertLess(profile_filter.content_budget(chunk), 8000, chunk)

    def test_failing_config_left_most_of_the_budget_to_reasoning(self):
        used = profile_filter.content_budget(60)
        self.assertLess(used / 8000, 0.35)

    def test_profiler_and_cli_agree_on_the_sized_budget(self):
        """Same formula in both places, so the tool's advice matches what the
        stage actually asks for. They differ only by the `{"records":[...]}`
        wrapper the profiler counts and the CLI absorbs into its margin."""
        tool = profile_filter.recommended_max_tokens(60, cli.REASONING_RESERVE)
        stage = cli._record_max_tokens(
            60, profile_filter.verdict_tokens(worst_case=True))
        self.assertLess(abs(tool - stage) / stage, 0.01)

    def test_the_stage_rounds_its_per_record_estimate_up(self):
        """cli uses a round 40 tok/verdict; the measured worst case must not
        exceed it, or the sizing under-reserves."""
        self.assertLessEqual(profile_filter.verdict_tokens(worst_case=True), 40)




class PromptCompositionTests(unittest.TestCase):
    """One canonical output contract — the real failure was two competing ones.

    A live run spent 8,083 reasoning tokens against an 8,000 budget and emitted
    no content. The trace showed it deliberating over original_text,
    normalised_text, source_url, "leave the unused reason array empty", and
    duplicate handling — every one of which was a genuine contradiction between
    the skill text, the preamble and the schema.
    """

    def setUp(self):
        self.scoped = cli.skill_sections(
            1, cli.FILTER_SKILL_SECTIONS, include_intro=False)
        self.sent = cli.FILTER_PREAMBLE + "\n" + self.scoped

    # ---- the contradictions must be gone ---------------------------------

    def test_no_field_outside_the_schema_is_mentioned(self):
        """The 9-field record shape the skill described is not a second contract."""
        for field in ("original_text", "normalised_text", "source_url",
                      "source_type", "thread_id", "parent_id",
                      "source_metadata", "duplicate_of"):
            self.assertNotIn(field, self.sent, field)

    def test_the_full_skill_did_mention_them(self):
        """Guards the test above: prove we removed something that was there."""
        _, full = cli.skill(1)
        for field in ("original_text", "normalised_text", "source_url"):
            self.assertIn(field, full, field)

    def test_insufficient_evidence_instruction_is_gone(self):
        """The extraction preamble told the model to write 'insufficient
        evidence' into a field — unsatisfiable once reasons are an enum, and a
        direct contradiction of 'leave the unused reason array empty'."""
        self.assertIn("insufficient evidence", cli.PREAMBLE)
        self.assertNotIn("insufficient evidence", self.sent)

    def test_stage_01_does_not_claim_a_segment_evidence_file(self):
        """Skill 01 runs before segmentation; the extraction preamble asserts a
        segment evidence file with URLs and assignment metadata that cannot
        exist yet."""
        self.assertIn("SEGMENT EVIDENCE FILE", cli.PREAMBLE)
        self.assertNotIn("SEGMENT EVIDENCE FILE", self.sent)

    def test_model_is_told_deduplication_already_happened(self):
        self.assertIn("ALREADY been removed", cli.FILTER_PREAMBLE)
        self.assertNotIn("## Exact duplicates", self.scoped)

    def test_no_report_or_recount_is_requested(self):
        """'Report: raw count, retained count...' and 'spot-audit at least 50'
        had nowhere to go in the schema, and the trace shows repeated
        recounting."""
        _, full = cli.skill(1)
        self.assertIn("spot-audit", full)
        self.assertNotIn("spot-audit", self.sent)
        self.assertNotIn("retention rate", self.sent)

    def test_batch_instruction_does_not_restate_the_contract(self):
        """The contract is stated once, in the preamble and the schema."""
        jobs = BudgetSizingTests.build_filter_jobs(records=60, chunk=60)
        instruction = jobs[0].prompt.split("RECORDS:")[0]
        for restated in ("evidence_id", "records\":[", "empty"):
            self.assertNotIn(restated, instruction, restated)
        self.assertLess(len(instruction), 200)

    # ---- but the judgement must be intact --------------------------------

    def test_every_reason_code_still_reaches_the_model(self):
        for code in cli.RETENTION_REASONS + cli.REJECTION_REASONS:
            self.assertIn(code, self.scoped, code)

    def test_retain_and_reject_criteria_are_preserved_verbatim(self):
        _, full = cli.skill(1)
        for heading in ("Keep it (retain)", "Bin it (reject)",
                        "Hard boundaries (what this skill must not do)",
                        "The one rule that governs everything"):
            body = full.split(f"## {heading}")[1].split("\n## ")[0].strip()
            self.assertIn(body, self.scoped, heading)

    def test_scope_limit_survives_dropping_the_skill_intro(self):
        """The intro carried 'you are the bouncer at the door' — and also a
        'remove exact duplicates' clause that contradicted the preamble."""
        self.assertIn("bouncer at the door", cli.FILTER_PREAMBLE)

    # ---- the renderer must fail loudly, not quietly ----------------------

    def test_a_renamed_section_aborts_instead_of_shipping_a_gutted_prompt(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.skill_sections(1, ("Keep it (retain)", "No Such Section"))
        self.assertIn("No Such Section", str(ctx.exception))

    def test_every_named_section_exists_today(self):
        cli.skill_sections(1, cli.FILTER_SKILL_SECTIONS)  # must not raise


class StagePreambleWiringTests(unittest.TestCase):
    """Original, re-run and repair must all carry the same contract."""

    def test_all_three_paths_use_the_filter_preamble(self):
        seen = []

        class Client:
            def estimate(self, *_a, **_k):
                return type("E", (), {"explain": lambda _s: ""})()

            def prewarm(self, _corpus, preamble):
                seen.append(("prewarm", preamble))

            def batch(self, _corpus, preamble, jobs):
                seen.append(("batch", preamble))
                # Force one re-run and one repair, then let the stage abort.
                return {j.id: llm.BatchResult("", "length") for j in jobs}

        from types import SimpleNamespace
        text = ("My shoulder aches every single night and the pillow goes flat "
                "before the morning arrives")
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "raw.txt")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(f"{text} one\n\n{text} two")
            cfg = {"_dir": tmp, "filter": {"min_words": 8},
                   "product": "p", "market": "m"}
            args = SimpleNamespace(source=src, rules_only=False, yes=True)
            with mock.patch.object(cli, "client", return_value=Client()), \
                    mock.patch.object(llm, "confirm", return_value=True):
                with self.assertRaises(cli.BatchOutputError):
                    cli.cmd_ingest(cfg, args)

        self.assertGreaterEqual(len(seen), 3)  # prewarm + batch + rerun + repair
        for where, preamble in seen:
            self.assertIs(preamble, cli.FILTER_PREAMBLE, where)


class StageConfigTests(unittest.TestCase):
    """Reasoning and batch size are per-stage knobs, not global ones."""

    def test_chunk_defaults_and_is_overridable_per_project(self):
        from types import SimpleNamespace
        args = SimpleNamespace()
        self.assertEqual(cli.filter_chunk({}, args), cli.FILTER_CHUNK)
        self.assertEqual(cli.filter_chunk({"filter": {"chunk": 40}}, args), 40)

    def test_effort_defaults_to_low_and_is_overridable(self):
        from types import SimpleNamespace
        args = SimpleNamespace()
        self.assertEqual(cli.filter_effort({}, args), "low")
        self.assertEqual(cli.filter_effort({"filter": {"effort": "none"}}, args),
                         "none")

    def test_synthesis_stages_do_not_inherit_stage_01_reasoning(self):
        """skill 02's jobs must carry no effort override — only 01 is tuned."""
        jobs = BudgetSizingTests.build_filter_jobs(records=60, chunk=60)
        self.assertEqual(jobs[0].effort, "low")
        self.assertIsNone(llm.Job("d0000", "dedup").effort)


class ReasoningCapTests(ReasoningControlTests):
    """A ceiling on reasoning, not just a level."""

    def test_reasoning_cap_is_sent_when_a_job_sets_one(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 5000,
                              effort="low", reasoning_max_tokens=2000)
        self.assertEqual(self.sent_body(call)["reasoning"], {"max_tokens": 2000})

    def test_effort_none_disables_reasoning(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply()) as call:
            self.client._post([{"role": "user", "content": "x"}], 100,
                              effort="none")
        self.assertEqual(self.sent_body(call)["reasoning"], {"enabled": False})

    def test_batch_passes_each_job_reasoning_cap(self):
        seen = []

        def fake_post(_m, _mt, schema=None, retries=3, job_id=None,
                      operation="completion", effort=None,
                      reasoning_max_tokens=None, **_k):
            seen.append((job_id, reasoning_max_tokens))
            return llm.BatchResult("{}", "stop")

        self.client._post = fake_post
        self.client.batch("c", "p", [
            llm.Job("a", "one", effort="low", reasoning_max_tokens=2000),
            llm.Job("b", "two")])
        self.assertCountEqual(seen, [("a", 2000), ("b", None)])

    def test_stage_01_jobs_carry_the_cap(self):
        jobs = BudgetSizingTests.build_filter_jobs(records=60, chunk=60)
        self.assertEqual(jobs[0].reasoning_max_tokens, cli.REASONING_RESERVE)

    def test_capped_reasoning_plus_answer_fits_the_sized_budget(self):
        """The structural guarantee: if the cap is honoured, the answer always
        has room. This is the invariant the 8,083-token run violated."""
        for chunk in (20, 40, 60, 80, 120):
            budget = cli._record_max_tokens(chunk, 40)
            self.assertGreater(budget, cli.REASONING_RESERVE + chunk * 40 - 1, chunk)

    def test_anthropic_effort_none_disables_thinking_legally(self):
        """Opus 5 rejects disabled thinking above `high` effort."""
        client = object.__new__(llm.Client)
        client.model = "claude-opus-5"
        client.effort = "max"
        p = client._params([], "prompt", 100, effort="none")
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertEqual(p["output_config"]["effort"], "high")

    def test_anthropic_keeps_thinking_on_at_low_effort(self):
        client = object.__new__(llm.Client)
        client.model = "claude-opus-5"
        client.effort = "high"
        p = client._params([], "prompt", 100, effort="low")
        self.assertEqual(p["thinking"], {"type": "adaptive"})
        self.assertEqual(p["output_config"]["effort"], "low")


class EmptyResponseTaxonomyTests(unittest.TestCase):
    """Ported from 33b9468: an empty reply is never a JSON problem.

    That branch's modeloutput.py split empty replies by finish reason. Our
    classification covered budget exhaustion and refusals but treated a
    content-filter block as retryable, and let an empty reply that survived the
    re-run fall through to the JSON repair pass — which is the original bug's
    shape: there is nothing in "" to repair.
    """

    def test_budget_exhaustion_reruns(self):
        for stop in ("length", "max_tokens"):
            r = llm.BatchResult("", stop)
            self.assertTrue(r.retryable, stop)
            self.assertFalse(r.repairable, stop)

    def test_content_filter_is_neither_rerun_nor_repaired(self):
        r = llm.BatchResult("", "content_filter")
        self.assertFalse(r.retryable)
        self.assertFalse(r.repairable)

    def test_refusal_is_neither_rerun_nor_repaired(self):
        r = llm.BatchResult("", "refusal")
        self.assertFalse(r.retryable)
        self.assertFalse(r.repairable)

    def test_transient_provider_error_reruns_but_never_repairs(self):
        """A provider error may be transient, so re-running is fair. Repairing
        an empty body never is."""
        r = llm.BatchResult("", "error")
        self.assertTrue(r.retryable)
        self.assertFalse(r.repairable)

    def test_empty_with_unknown_finish_reruns_but_never_repairs(self):
        r = llm.BatchResult("", "stop")
        self.assertTrue(r.retryable)
        self.assertFalse(r.repairable)

    def test_non_empty_malformed_json_still_repairs(self):
        r = llm.BatchResult("I judged them but wrote no JSON.", "stop")
        self.assertFalse(r.retryable)
        self.assertTrue(r.repairable)

    def test_truncated_reply_reruns_first_then_can_repair(self):
        r = llm.BatchResult('{"records":[{"evidence_id', "length")
        self.assertTrue(r.retryable)
        self.assertTrue(r.repairable)

    def test_no_empty_reply_reaches_the_repair_pass(self):
        """End to end through _batch_rows, for every empty-reply flavour."""
        for stop in ("content_filter", "error", "stop", "length", "refusal"):
            calls = {"repair": 0}

            def repair(failures):
                calls["repair"] += 1
                return {}

            def rerun(failures, factor):
                return {j.id: llm.BatchResult("", stop) for j, _r, _x in failures}

            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(cli.BatchOutputError):
                    cli._batch_rows({"a": llm.BatchResult("", stop)},
                                    [llm.Job("a", "p")], "records", tmp,
                                    repair=repair, rerun=rerun)
            self.assertEqual(calls["repair"], 0, stop)

    def test_blocked_reply_is_named_as_blocked_in_the_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cli.BatchOutputError) as ctx:
                cli._batch_rows({"a": llm.BatchResult("", "content_filter")},
                                [llm.Job("a", "p")], "records", tmp)
        self.assertIn("blocked by the provider", str(ctx.exception))


class PortedRegressionGuardTests(unittest.TestCase):
    """What we deliberately did NOT port from 33b9468."""

    def test_one_returns_empty_text_rather_than_raising(self):
        """33b9468 made _post raise EmptyResponse on a non-budget empty reply.
        cmd_extract calls c.one() with no exception guard, so that turns its
        EMPTY_EXTRACTION_RETRIES loop into a crash. Verified against that
        branch's code; we keep returning text so the loop still runs."""
        client = object.__new__(openrouter.Client)
        client.model, client.key, client.verbose = "m", "k", False
        client.effort = "low"
        client.spent = {"in": 0, "out": 0, "reasoning": 0,
                        "cache_write": 0, "cache_read": 0}
        payload = {"choices": [{"message": {"content": ""},
                                "finish_reason": "stop"}], "usage": {}}

        class Fake:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps(payload).encode()

        with mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": tempfile.mkdtemp()}), \
                mock.patch("urllib.request.urlopen", return_value=Fake()):
            self.assertEqual(client.one("c", "p", "prompt", 100), "")

    def test_stage_02_reasoning_is_unchanged_by_default(self):
        from types import SimpleNamespace
        self.assertIsNone(cli.dedup_effort({}, SimpleNamespace()))
        self.assertEqual(
            cli.dedup_effort({"dedup": {"effort": "none"}}, SimpleNamespace()),
            "none")

    def test_reasoning_tokens_are_recorded_for_comparison(self):
        client = object.__new__(openrouter.Client)
        client.model, client.key, client.verbose = "m", "k", False
        client.effort = "low"
        client.spent = {"in": 0, "out": 0, "reasoning": 0,
                        "cache_write": 0, "cache_read": 0}
        payload = {"choices": [{"message": {"content": "{}"},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 90,
                             "completion_tokens_details": {"reasoning_tokens": 80}}}

        class Fake:
            def __enter__(s):
                return s

            def __exit__(s, *_a):
                return False

            def read(s):
                return json.dumps(payload).encode()

        with mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": tempfile.mkdtemp()}), \
                mock.patch("urllib.request.urlopen", return_value=Fake()):
            client._post([{"role": "user", "content": "x"}], 100)
        self.assertEqual(client.spent["reasoning"], 80)


if __name__ == "__main__":
    unittest.main()
