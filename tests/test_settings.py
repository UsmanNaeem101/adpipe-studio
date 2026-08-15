"""Every project setting, editable from the app.

These settings were always real and always doing work -- floors removing
segments, scope excluding communities -- but they lived in a JSON file the app
never mentioned. A setting nobody can find is a hard-coded constant with extra
steps.

The properties worth pinning: a bad value writes nothing, the schema and the
validator cannot disagree, and a saved value actually changes what a stage does.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import corpus_policy
import researchpack
import settings


class SchemaTests(unittest.TestCase):
    def test_every_field_is_addressable_and_described(self):
        keys = set()
        for field in settings.schema():
            self.assertTrue(field["path"], field)
            self.assertTrue(field["label"], field)
            self.assertIn("default", field)
            self.assertNotIn(field["key"], keys, "duplicate key")
            keys.add(field["key"])

    def test_choices_declare_their_own_default(self):
        for field in settings.FIELDS:
            if field["kind"] == "choice":
                self.assertIn(field["default"], field["options"], field["label"])

    def test_current_fills_defaults_for_an_empty_project(self):
        values = settings.current({})
        self.assertEqual(values["segmentation.min_segment_threads"], 4)
        self.assertEqual(values["audience.population_scope"], "all")


class ValidationTests(unittest.TestCase):
    def test_a_bad_value_writes_none_of_them(self):
        cfg = {"segmentation": {"min_segment_threads": 4}}
        with self.assertRaises(settings.SettingsError):
            settings.apply(cfg, {"segmentation.min_segment_evidence": "8",
                                 "segmentation.min_segment_threads": "not a number"})
        self.assertEqual(cfg["segmentation"], {"min_segment_threads": 4})

    def test_out_of_range_is_refused_with_the_bound_named(self):
        with self.assertRaises(settings.SettingsError) as caught:
            settings.apply({}, {"segmentation.min_segment_threads": 999})
        self.assertIn("at most", str(caught.exception))

    def test_a_broken_regex_is_refused_rather_than_matching_nothing(self):
        with self.assertRaises(settings.SettingsError) as caught:
            settings.apply({}, {"filter.topic": "shoulder(("})
        self.assertIn("invalid regex", str(caught.exception))

    def test_an_unknown_key_cannot_be_smuggled_into_the_config(self):
        with self.assertRaises(settings.SettingsError):
            settings.apply({}, {"audience.delete_everything": True})

    def test_subreddit_lists_are_normalised(self):
        out = settings.apply(
            {}, {"audience.excluded_subreddits": "r/AskDocs\n/r/EDS, sleep\n\nr/EDS"})
        self.assertEqual(out["audience"]["excluded_subreddits"],
                         ["AskDocs", "EDS", "sleep"])

    def test_apply_does_not_mutate_the_original(self):
        cfg = {"model": {"id": "claude-opus-5"}}
        settings.apply(cfg, {"model.id": "something-else"})
        self.assertEqual(cfg["model"]["id"], "claude-opus-5")

    def test_write_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "project.json")
            settings.write(path, settings.apply({}, {"model.effort": "low"}))
            self.assertEqual(os.listdir(tmp), ["project.json"])
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["model"]["effort"], "low")


class SettingsActuallyChangeBehaviourTests(unittest.TestCase):
    """A knob that saves but changes nothing is worse than no knob."""

    def test_the_corroboration_bar_responds(self):
        record = {"retention_reasons": ["first_person_experience",
                                        "specific_problem"]}
        self.assertIsNone(corpus_policy.corroboration_cut(record))
        strict = settings.apply({}, {"audience.min_signals_first_person": 3})
        self.assertIsNotNone(corpus_policy.corroboration_cut(record, strict))

    def test_the_segment_floors_respond(self):
        cfg = settings.apply({}, {"segmentation.min_segment_threads": 9,
                                  "segmentation.min_segment_evidence": 20})
        self.assertEqual(corpus_policy.segment_floors(cfg), (9, 20))

    def test_the_child_floors_respond(self):
        cfg = settings.apply({}, {"segmentation.min_child_threads": 5,
                                  "segmentation.min_distinctive_terms": 0})
        floors = corpus_policy.child_floors(cfg)
        self.assertEqual(floors["min_threads"], 5)
        self.assertEqual(floors["min_distinctive_terms"], 0)

    def test_the_pack_budget_responds(self):
        cfg = settings.apply({}, {"segmentation.pack_context_budget_tokens": 500})
        self.assertEqual(
            researchpack.pack_settings(cfg)["context_budget_tokens"], 500)

    def test_the_scope_setting_responds(self):
        cfg = settings.apply({}, {"audience.population_scope": "mainstream"})
        self.assertEqual(corpus_policy.scope(cfg), "mainstream")
        self.assertTrue(corpus_policy.scope_prompt(cfg))


class EndpointContractTests(unittest.TestCase):
    """What the browser receives has to be renderable without extra knowledge."""

    def test_the_payload_is_json_serialisable(self):
        payload = {"schema": settings.schema(), "values": settings.current({})}
        text = json.dumps(payload)
        self.assertIn("min_segment_threads", text)

    def test_every_value_has_a_field_and_every_field_a_value(self):
        values = settings.current({})
        keys = {field["key"] for field in settings.schema()}
        self.assertEqual(set(values), keys)


if __name__ == "__main__":
    unittest.main()
