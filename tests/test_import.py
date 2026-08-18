"""Adopting a segmentation that was run somewhere else.

Stages 01--06 run perfectly well pasted into a chat window, and what comes back
is one markdown file per audience rather than the evidence files stage 06
writes. Importing them is a translation, and a translation that drops something
is silent: nothing downstream parses an evidence file, so a comment lost here
does not raise -- it just never reaches the twenty skills that were supposed to
read it, and the extraction looks fine.

So these tests are mostly about not losing anything, and about not adding
anything either: the source carries no evidence tier and no rationale, and
filling those in with something plausible would put words the research never
said in front of skills about to believe them.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import importer  # noqa: E402
import store  # noqa: E402


def audience(name, comments, facets=(), definition="The people who do the thing."):
    """A stage-06 audience file, in the shape a chat run actually returns."""
    out = [f"# {name}", "",
           f"- **Definition:** {definition}",
           "- **Who's in:** Comments whose single Stage 05 home is this audience.",
           "- **Who's out:** Every comment assigned elsewhere.",
           f"- **Item count:** {len(comments)}",
           "- **Separate conversations:** 2", ""]
    if facets:
        out += ["## Attribute / journey-state tally", "",
                "| Attribute / journey tag | Comments | Share of this file |",
                "|---|---:|---:|"]
        out += [f"| {label} | {count} | 10.0% |" for label, count in facets]
        out.append("")
    out += ["## Comments", ""]
    for c in comments:
        out += [f"### Comment {c['id']}", "",
                f"- **ID:** {c['id']}",
                "- **Source type:** comment",
                f"- **Title:** {c.get('title', 'A thread')}",
                f"- **URL:** https://example.test/{c['id']}",
                f"- **Thread:** RECORD_ID {c.get('thread', 1)}",
                f"- **Score:** {c.get('score', 10)}",
                f"- **Margin:** {c.get('margin', 4)}"]
        if c.get("runner_up"):
            out.append(f"- **Runner-up:** {c['runner_up']}")
        out += ["- **Cues fired:** explicit group identity +6",
                f"- **Tags:** {c.get('tags', '(none)')}", "",
                "**Original text**", "", "```text", c["text"], "```", ""]
    return "\n".join(out)


# The text that broke a line-shape parser: a comment quoting a numbered list,
# where a line inside the fence looks exactly like a markdown heading.
HASH_TEXT = ("Here's a sneak peek of /r/xxfitness:\n"
             "#1: Not so humble brag\n"
             "### Comment 999\n"
             "## Comments\n"
             "that last line is part of what they wrote, not structure")

SIMPLE = audience("Golfers", [
    {"id": 12898, "text": "This is why I golf lefty now.", "score": 16, "margin": 16},
    {"id": 2864, "text": "Line one.\n\nLine three after a blank.",
     "tags": "Strengthening / exercise users; Cost-sensitive sufferers",
     "runner_up": "Healthcare workers"},
    {"id": 5948, "text": HASH_TEXT},
], facets=[("In physical therapy / physiotherapy", 14), ("Chiropractic users", 1)])


class ParsingTests(unittest.TestCase):
    def test_every_comment_is_found(self):
        doc = importer.parse_audience_file(SIMPLE)
        self.assertEqual(doc["name"], "Golfers")
        self.assertEqual(len(doc["comments"]), 3)
        self.assertEqual([c["fields"]["ID"] for c in doc["comments"]],
                         ["12898", "2864", "5948"])

    def test_a_heading_inside_a_comment_is_that_persons_words(self):
        # The failure this prevents is not an exception. A parser that trusts
        # line shape splits comment 5948 into three, and the extra two carry no
        # ID -- so the corpus grows fake items and loses a real one, quietly.
        doc = importer.parse_audience_file(SIMPLE)
        self.assertEqual(len(doc["comments"]), 3)
        self.assertEqual(doc["comments"][2]["text"], HASH_TEXT)

    def test_blank_lines_inside_a_comment_survive(self):
        doc = importer.parse_audience_file(SIMPLE)
        self.assertEqual(doc["comments"][1]["text"],
                         "Line one.\n\nLine three after a blank.")

    def test_the_facet_tally_is_read(self):
        doc = importer.parse_audience_file(SIMPLE)
        self.assertEqual(doc["facets"],
                         [("In physical therapy / physiotherapy", 14),
                          ("Chiropractic users", 1)])

    def test_recognising_an_audience_file_survives_a_multi_line_head(self):
        # DOC_TITLE is anchored for matching one line at a time. Searching a
        # multi-line head with it never matches, and the symptom is an upload
        # that cheerfully reports finding nothing at all.
        self.assertTrue(importer.looks_like_audience_file(SIMPLE))
        self.assertFalse(importer.looks_like_audience_file("no heading here"))
        self.assertFalse(importer.looks_like_audience_file("# Title\n\nbut no comments"))


class RenderingTests(unittest.TestCase):
    def setUp(self):
        doc = importer.parse_audience_file(SIMPLE)
        self.text = importer.render_evidence(doc, "golfers", "seg_013")

    def test_it_carries_the_fields_the_skills_read(self):
        for expected in ("GOLFERS", "Segment slug: golfers", "Evidence items: 3",
                         "SEGMENT DEFINITION", "INCLUSION", "EXCLUSION",
                         "FACETS PRESENT", "EVIDENCE ITEMS",
                         "[12898] TYPE: comment", "ASSIGNMENT SCORE: 16",
                         "WINNING MARGIN: 16", "RUNNER-UP: Healthcare workers"):
            self.assertIn(expected, self.text, expected)

    def test_every_comments_text_reaches_the_evidence_file(self):
        self.assertIn("This is why I golf lefty now.", self.text)
        self.assertIn("Line three after a blank.", self.text)
        self.assertIn("that last line is part of what they wrote", self.text)

    def test_it_does_not_invent_a_tier_or_a_rationale(self):
        # 'context' is one of three real tiers and a claim about the evidence.
        # The source never made it, so neither does this.
        self.assertIn("EVIDENCE TIER: unspecified", self.text)
        self.assertNotIn("EVIDENCE TIER: context", self.text)
        self.assertNotIn("RATIONALE:", self.text)

    def test_an_empty_tag_list_does_not_become_a_facet(self):
        # "(none)" written into a FACETS line reads to a skill as a facet named
        # none, and skill 24 will happily quote it back.
        self.assertNotIn("FACETS: (none)", self.text)
        self.assertIn("FACETS: Strengthening / exercise users", self.text)

    def test_it_says_on_its_face_that_it_was_imported(self):
        self.assertIn("Validation status: imported", self.text)
        self.assertIn("Origin: imported", self.text)


class PlanTests(unittest.TestCase):
    def members(self):
        return [("golfers.md", SIMPLE),
                ("back_sleepers.md", audience("Back sleepers",
                                              [{"id": 1, "text": "hi"}])),
                ("unassigned.md", audience("Unassigned", [{"id": 2, "text": "x"}])),
                ("_stage06_summary.md", audience("Stage 06", [{"id": 3, "text": "y"}])),
                ("notes.md", "# Notes\n\njust prose, no comments")]

    def test_reports_are_not_audiences(self):
        slugs = [r["slug"] for r in importer.plan(self.members())]
        self.assertEqual(slugs, ["back_sleepers", "golfers"])

    def test_ids_are_stable_across_runs(self):
        # A caller shows this plan and then writes it. If a second look assigns
        # different ids, the thing written is not the thing approved.
        first = {r["slug"]: r["segment_id"] for r in importer.plan(self.members())}
        second = {r["slug"]: r["segment_id"] for r in importer.plan(self.members())}
        self.assertEqual(first, second)
        self.assertEqual(first, {"back_sleepers": "seg_001", "golfers": "seg_002"})


class ZipTests(unittest.TestCase):
    @staticmethod
    def zipped(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return buf.getvalue()

    def test_it_reads_a_plain_zip_of_audience_files(self):
        blob = self.zipped([("files/golfers.md", SIMPLE)])
        self.assertEqual([n for n, _ in importer.audience_members(blob)], ["golfers.md"])

    def test_it_reads_a_zip_inside_a_zip(self):
        # What a chat export actually looks like: the export contains its own
        # export, so the audience files are one level further down than anyone
        # expects.
        inner = self.zipped([("golfers.md", SIMPLE)])
        blob = self.zipped([("export/audience_files.zip", inner)])
        self.assertEqual([n for n, _ in importer.audience_members(blob)], ["golfers.md"])

    def test_macos_zip_litter_is_ignored(self):
        # A zip made on a Mac carries a shadow copy of every file. Reading those
        # would double every audience, and the duplicate parses to zero comments.
        blob = self.zipped([("golfers.md", SIMPLE),
                            ("__MACOSX/._golfers.md", SIMPLE),
                            (".DS_Store", "binary junk")])
        self.assertEqual([n for n, _ in importer.audience_members(blob)], ["golfers.md"])

    def test_a_zip_of_something_else_yields_nothing_rather_than_guessing(self):
        blob = self.zipped([("readme.md", "# Readme\n\nnothing to import")])
        self.assertEqual(importer.audience_members(blob), [])


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(self.project, "research", "evidence"))
        self.rows = importer.plan([("golfers.md", SIMPLE)])

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_writes_one_evidence_file_per_audience(self):
        written = importer.write(self.project, self.rows, "imported from a test")
        self.assertEqual(len(written), 1)
        self.assertTrue(store.exists(written[0]["path"]))
        self.assertIn("GOLFERS", store.read_text(written[0]["path"]))

    def test_the_import_is_recorded_as_an_import(self):
        # _provenance.json had an 'imported' origin and nothing that could
        # produce one. Every later stage prints this before it runs, which is
        # the only thing standing between borrowed evidence and a run that
        # looks native.
        importer.write(self.project, self.rows, "imported from a test")
        prov = importer.read_provenance(self.project)
        self.assertEqual(prov["golfers"]["origin"], "imported")
        self.assertIn("3 assigned items", prov["golfers"]["detail"])

    def test_recording_one_segment_leaves_the_others_alone(self):
        importer.record_provenance(self.project, "runners", "pipeline", "skills 01-06")
        importer.write(self.project, self.rows, "imported from a test")
        prov = importer.read_provenance(self.project)
        self.assertEqual(sorted(prov), ["golfers", "runners"])
        self.assertEqual(prov["runners"]["origin"], "pipeline")


if __name__ == "__main__":
    unittest.main()
