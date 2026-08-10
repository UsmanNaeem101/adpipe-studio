"""One evidence item must be one comment.

parse_voc's docstring has always said so, and warned what happens otherwise:
"If whole threads come through as single items, every prevalence count
downstream is inflated by thread length and the segment reports become
meaningless." On the montisella scrape that is exactly what happened, and worse.

old.reddit renders a thread as one unbroken line — no newline between comments,
each introduced by `[–]user 5 points6 points7 points 1 year ago (0 children)`
and closed by `permalinkembedsavereportreply`. Neither the blank-line split nor
the `[n]` split can see inside it, so 986 "evidence items" were whole threads;
the largest held 184 separate people in 107,187 characters.

The chrome filter then finished the job. `permalinkembedsave` and
`sharesavehidereport` were on the boilerplate list, and because that list is
matched against a whole item, every thread dump containing a separator was
deleted entire: 11.8M of 16.2M characters, 26,635 comments, and whole
subreddits at 100% — r/RotatorCuff, r/RSI, r/Posture, r/backpain,
r/bodyweightfitness. The surviving corpus was not a sample of the scrape, it was
whichever threads happened to be captured in the newer layout.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cli

# Verbatim shapes from the montisella dump, trimmed. Written as one line each
# because that is the point: old.reddit puts no newline between two customers.
THREAD = (
    "SPOILER21 commentssharesavehidereportall 21 commentssorted by:&#32;best"
    "Want to add to the discussion?Post a comment!Create an account"
    "[–]Tasarin30HH(UK)/30L(US)&#32;50 points51 points52 points&#32;1 year ago"
    "&nbsp;(0 children)The support from a bra should come from the band, not "
    "the shoulder straps."
    "permalinkembedsavereportreply"
    "[–]28FFthrowaway28GG&#32;33 points34 points35 points&#32;1 year ago"
    "&nbsp;(0 children)My shoulder pain improved significantly once I changed "
    "to the right size."
    "permalinkembedsavereportreply"
    "[–]WampaCat&#32;27 points28 points29 points&#32;1 year ago&nbsp;(2 children)"
    "Where were you fitted? Tons of retailers claiming to be fitting experts "
    "are just not."
    "permalinkembedsavereportreply"
)


def post(body, url="https://www.reddit.com/r/ABraThatFits/comments/1ineqo5/x/"):
    return f"POST URL: {url}\nTitle: Shoulders are killing me\n" \
           f"--- POST BODY ---\n\n--- COMMENTS (3) ---\n{body}\n"


class SplitterTests(unittest.TestCase):
    def test_three_customers_become_three_items(self):
        comments = cli.split_old_reddit(THREAD)
        self.assertEqual(len(comments), 3)
        self.assertTrue(comments[0].startswith("The support from a bra"))
        self.assertTrue(comments[1].startswith("My shoulder pain improved"))
        self.assertTrue(comments[2].startswith("Where were you fitted?"))

    def test_no_username_score_or_action_link_survives(self):
        for comment in cli.split_old_reddit(THREAD):
            for furniture in ("points", "permalink", "[–]", "children",
                              "WampaCat", "Tasarin"):
                self.assertNotIn(furniture, comment)

    def test_text_before_the_first_comment_is_not_evidence(self):
        """The sort bar and signup prompt precede comment one; nobody said them."""
        joined = " ".join(cli.split_old_reddit(THREAD))
        self.assertNotIn("Want to add to the discussion", joined)
        self.assertNotIn("sorted by", joined)

    def test_a_score_hidden_comment_is_still_a_comment(self):
        thread = ("[–]someone&#32;score hidden&#32;3 hours ago&nbsp;(0 children)"
                  "Brand new here and my shoulder is killing me."
                  "permalinkembedsavereportreply")
        self.assertEqual(cli.split_old_reddit(thread),
                         ["Brand new here and my shoulder is killing me."])

    def test_moderator_and_submitter_tags_do_not_hide_a_comment(self):
        """[M] and [S] sit inside the username; 2,493 markers carried one."""
        thread = ("[–]Pricklypear_45[S]&#32;0 points1 point2 points&#32;"
                  "9 months ago&nbsp;(0 children)Yeah I thought so too."
                  "permalinkembedsavereportreply")
        self.assertEqual(cli.split_old_reddit(thread), ["Yeah I thought so too."])

    def test_a_locked_comment_notice_does_not_hide_a_comment(self):
        thread = ("[–]AutoModerator[M]&#32;0 points1 point2 points&#32;"
                  "1 year agolocked comment&nbsp;(0 children)Thank you for "
                  "submitting a post.permalinkembedsavereportreply")
        self.assertEqual(cli.split_old_reddit(thread),
                         ["Thank you for submitting a post."])

    def test_text_that_is_not_old_reddit_is_left_alone(self):
        """Returning [] lets the caller keep whatever the first pass produced."""
        self.assertEqual(cli.split_old_reddit("My shoulder hurts at night."), [])
        self.assertEqual(cli.split_old_reddit(""), [])

    def test_a_header_with_no_body_yields_no_empty_evidence(self):
        thread = ("[–]ghost&#32;1 point2 points3 points&#32;1 year ago"
                  "&nbsp;(0 children)permalinkembedsavereportreply")
        self.assertEqual(cli.split_old_reddit(thread), [])


class ParseVocTests(unittest.TestCase):
    def test_a_thread_dump_is_no_longer_one_evidence_item(self):
        items = cli.parse_voc(post(THREAD))
        self.assertEqual(len(items), 3)
        self.assertTrue(all(len(item["text"]) < 200 for item in items))

    def test_every_recovered_comment_keeps_its_source(self):
        """Attribution is what makes thread and subreddit counts mean anything."""
        items = cli.parse_voc(post(THREAD))
        for item in items:
            self.assertEqual(cli.reddit_source_fields(item["url"]),
                             ("ABraThatFits", "1ineqo5"))

    def test_comments_do_not_leak_across_posts(self):
        """The last comment of a post must stop at that post, not run to EOF."""
        two = post(THREAD) + "\n" + post(
            "[–]other&#32;1 point2 points3 points&#32;2 years ago&nbsp;"
            "(0 children)A different thread entirely."
            "permalinkembedsavereportreply",
            url="https://www.reddit.com/r/backpain/comments/abc123/y/")
        items = cli.parse_voc(two)
        by_sub = {}
        for item in items:
            sub, _thread = cli.reddit_source_fields(item["url"])
            by_sub.setdefault(sub, []).append(item["text"])
        self.assertEqual(len(by_sub["ABraThatFits"]), 3)
        self.assertEqual(by_sub["backpain"], ["A different thread entirely."])

    def test_both_layouts_in_one_post_are_both_parsed(self):
        """A [n]-marked body and an old.reddit dump co-occur in one scrape."""
        items = cli.parse_voc(post(
            "[1] ? (score: 0)\nTitle, I've been fitted and wear proper bras.\n"
            + THREAD))
        texts = [item["text"] for item in items]
        self.assertEqual(len(texts), 4)
        self.assertIn("Title, I've been fitted and wear proper bras.", texts)

    def test_the_scrape_prefix_is_not_presented_as_customer_words(self):
        items = cli.parse_voc(post("[1] ? (score: 0)\nMy shoulders ache daily."))
        self.assertEqual([item["text"] for item in items],
                         ["My shoulders ache daily."])

    def test_a_new_reddit_dump_parses_exactly_as_before(self):
        """The fix is additive: no old.reddit header, no change."""
        plain = post("[1] My shoulder hurts.\n\n[2] Mine too, every night.")
        self.assertEqual([item["text"] for item in cli.parse_voc(plain)],
                         ["My shoulder hurts.", "Mine too, every night."])


class BoilerplateTests(unittest.TestCase):
    """The filter that deleted 76% of the corpus."""

    def test_comment_separators_are_not_treated_as_page_furniture(self):
        for separator in ("permalinkembedsave", "sharesavehidereport"):
            self.assertIsNone(cli.BOILER.search(separator), separator)

    def test_a_recovered_comment_is_not_mistaken_for_chrome(self):
        for comment in cli.split_old_reddit(THREAD):
            self.assertIsNone(cli.BOILER.search(comment), comment)

    def test_real_page_furniture_is_still_rejected(self):
        for chrome in ("Welcome to Reddit", "I am a bot", "AutoModerator",
                       "Privacy Policy", "Create an account"):
            self.assertIsNotNone(cli.BOILER.search(chrome), chrome)


if __name__ == "__main__":
    unittest.main()
