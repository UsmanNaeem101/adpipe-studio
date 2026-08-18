"""Why a project name was refused, in the terms of that name.

This exists because of `shoulders_and_neck`. Lowercase, eighteen characters,
letters and underscores — refused, with a message reciting the rule it already
satisfied. A copy-paste had carried a zero-width space, which neither
JavaScript's trim() nor Python's strip() removes and which is invisible in the
box you would inspect to find it.

An error that restates the rule is only useful when the input visibly breaks it.
When it does not, the message is unfalsifiable: it says nothing about what
happened, and the one thing you would need to see cannot be seen.

The rule itself does not change. These tests hold the explanation to it exactly,
because a name accepted at creation and then refused by a path guard is a worse
failure than the one being fixed.
"""

import itertools
import os
import random
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import app  # noqa: E402


class AcceptanceTests(unittest.TestCase):
    def test_ordinary_names_pass(self):
        for name in ("shoulders_and_neck", "montisella", "ab", "a1", "x-y_z", "0"):
            with self.subTest(name=name):
                self.assertIsNone(app.name_problem(name if name != "0" else "01"))

    def test_the_explanation_accepts_exactly_what_the_path_guards_accept(self):
        # SAFE_NAME still guards four other routes. If these two ever disagree,
        # a name can be created and then refused when something reads its files
        # — which looks like data loss, not a validation bug.
        alphabet = "abz09_-ABZ .​–/"
        random.seed(7)
        checked = 0
        for length in range(0, 4):
            for combo in itertools.product(alphabet, repeat=length):
                value = "".join(combo)
                checked += 1
                self.assertEqual(bool(app.SAFE_NAME.match(value)),
                                 app.name_problem(value) is None, repr(value))
        for _ in range(4000):
            value = "".join(random.choice(alphabet)
                            for _ in range(random.randint(0, 45)))
            checked += 1
            self.assertEqual(bool(app.SAFE_NAME.match(value)),
                             app.name_problem(value) is None, repr(value))
        self.assertGreater(checked, 4000)


class ExplanationTests(unittest.TestCase):
    def test_an_invisible_character_is_named_and_located(self):
        # The case this was written for. The name looks perfect; the message has
        # to say what cannot be seen, and where.
        problem = app.name_problem("shoulders_and_neck​")
        self.assertIn("zero-width space", problem)
        self.assertIn("U+200B", problem)
        self.assertIn("position 19", problem)

    def test_every_invisible_it_knows_is_described_in_words(self):
        # "U+2060" tells a person nothing they can act on.
        for char, description in app.INVISIBLES.items():
            with self.subTest(char=repr(char)):
                problem = app.name_problem(f"ab{char}cd")
                self.assertIn(description, problem)
                self.assertNotIn("only lowercase letters", problem)

    def test_a_fixable_name_is_offered_already_fixed(self):
        self.assertIn("'shoulders_and_neck'", app.name_problem("Shoulders_And_Neck"))
        self.assertIn("'shoulders_and_neck'", app.name_problem("shoulders and neck"))

    def test_length_is_reported_as_a_length(self):
        self.assertIn("1 character", app.name_problem("a"))
        self.assertIn("42 characters", app.name_problem("x" * 42))
        self.assertIn("41", app.name_problem("x" * 42))

    def test_a_leading_underscore_says_so_rather_than_blaming_the_character_set(self):
        # '_' is allowed — just not first. Reporting it as a disallowed
        # character would send someone hunting for a rule that does not exist.
        problem = app.name_problem("_leading")
        self.assertIn("starts with", problem)
        self.assertNotIn("only lowercase", problem)

    def test_no_message_merely_recites_the_rule(self):
        # The failure being fixed: a message that describes the constraint and
        # not the input. Every explanation must quote something from the name.
        for bad in ("Shoulders", "a b", "ab​c", "a", "x" * 42, "_x"):
            with self.subTest(bad=bad):
                problem = app.name_problem(bad)
                self.assertTrue(
                    any(hint in problem for hint in
                        ("position", "character", "starts with", "capital", "space")),
                    f"{bad!r} got a message with nothing specific in it: {problem}")


if __name__ == "__main__":
    unittest.main()
