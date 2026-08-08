"""A project is created empty, and a product is added later — not at the same time.

The order these run in is the point. Research decides what is worth selling, so a
project must be usable with no product at all, and adding one afterwards must be
an ordinary operation rather than a repair of what creation guessed.
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

import app
import paths
import products


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(m, "ROOT", self.tmp.name)
                        for m in (app, paths, products)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def project_file(self, name, *parts):
        return os.path.join(self.tmp.name, "projects", name, *parts)

    # ------------------------------------------------------------ creation
    def test_new_project_has_no_products(self):
        app.create_project("lumbar", "lumbar cushion", "desk workers")
        self.assertEqual(products.list_products("lumbar"), [])

    def test_new_project_scaffolds_config_and_facts(self):
        cfg = app.create_project("lumbar", "lumbar cushion", "desk workers")
        self.assertTrue(os.path.exists(self.project_file("lumbar", "project.json")))
        with open(self.project_file("lumbar", "facts.json"), encoding="utf-8") as fh:
            facts = json.load(fh)
        # The research subject is project-level context for the VOC stages. It is
        # recorded whether or not a product sheet exists — the two are separate.
        self.assertEqual(cfg["product"], "lumbar cushion")
        self.assertEqual(cfg["market"], "desk workers")
        self.assertEqual(facts["product"], "lumbar cushion")

    def test_new_project_reports_that_it_has_no_products(self):
        """Zero products is a state the code already models, not an error path
        invented for this change — the message names the project and says why."""
        app.create_project("lumbar", "", "")
        with self.assertRaises(products.ProductError) as caught:
            products.resolve_product("lumbar")
        self.assertIn("no products yet", str(caught.exception))

    def test_project_without_a_subject_still_names_itself(self):
        cfg = app.create_project("lumbar", "", "")
        self.assertEqual(cfg["product"], "lumbar")

    # ------------------------------------------------------- adding later
    def test_product_can_be_added_after_the_research_exists(self):
        app.create_project("lumbar", "lumbar cushion", "desk workers")
        # Stand in for a completed ingest: the product is added to a project that
        # already holds research, which is the order this is meant to support.
        voc = self.project_file("lumbar", "research", "voc")
        os.makedirs(voc, exist_ok=True)
        with open(os.path.join(voc, "filtered_voc.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")

        doc = products.blank_product()
        doc["identity"]["name"] = products.cell(
            "Montisella lumbar cushion", "user_approved", "merchant")
        out = app.add_product("lumbar", doc)

        self.assertEqual(out["project"], "lumbar")
        self.assertEqual(products.list_products("lumbar"), [out["product"]])
        self.assertEqual(products.resolve_product("lumbar"), out["product"])
        # Adding a product must not disturb the research already in the project.
        self.assertTrue(os.path.exists(os.path.join(voc, "filtered_voc.jsonl")))

    def test_several_products_can_be_added_to_one_project(self):
        app.create_project("lumbar", "lumbar cushion", "desk workers")
        for name in ("Lumbar cushion", "Seat wedge"):
            doc = products.blank_product()
            doc["identity"]["name"] = products.cell(name, "user_approved", "merchant")
            app.add_product("lumbar", doc)
        self.assertEqual(len(products.list_products("lumbar")), 2)

    def test_adding_the_same_product_twice_is_refused(self):
        app.create_project("lumbar", "", "")
        doc = products.blank_product()
        doc["identity"]["name"] = products.cell(
            "Lumbar cushion", "user_approved", "merchant")
        app.add_product("lumbar", doc)
        with self.assertRaises(ValueError):
            app.add_product("lumbar", doc)

    # ------------------------------------------------------------- the UI
    def test_page_binds_the_create_project_button_once(self):
        """Two handlers were bound to #npbtn; the later one silently won. Whatever
        the create form is told to do next, one binding decides it."""
        self.assertEqual(len(re.findall(r"#npbtn'\)\.onclick", app.PAGE)), 1)

    def test_create_form_does_not_promise_a_product(self):
        self.assertIn("no product is created", app.PAGE)


if __name__ == "__main__":
    unittest.main()
