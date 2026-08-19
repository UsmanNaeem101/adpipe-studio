"""The request DeepSeek actually receives.

Written against the wire rather than the client's own accessors, because every
way this can be wrong is a difference of one key in one JSON body — and each one
fails at a different moment. A stray `reasoning` key 400s on the first request.
The wrong `response_format` gets accepted and returns prose to a caller that
will try to parse it. An OpenRouter header is merely ignored, right up until it
is not.

None of it could be checked against DeepSeek's documentation: the sandbox this
was written in cannot reach their site. So the defaults are best knowledge, all
three are overridable by environment variable, and what is pinned here is the
shape of the request — which is the part that stays true whatever the endpoint
turns out to be.
"""

import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import credentials  # noqa: E402
import deepseek  # noqa: E402
import openrouter  # noqa: E402


class Sent:
    """Captures the one request the client makes."""

    def __init__(self, reply=None):
        self.reply = reply or {
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        self.url = None
        self.headers = {}
        self.body = None

    def urlopen(self, request, *a, **k):
        self.url = request.full_url
        self.headers = dict(request.headers)
        self.body = json.loads(request.data.decode())

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response(self.reply)


def call(client, schema=None, effort="high"):
    sent = Sent()
    with mock.patch.object(openrouter.urllib.request, "urlopen", sent.urlopen), \
            mock.patch.object(openrouter.json, "load",
                              side_effect=lambda fh: json.loads(fh.read())):
        client._post([{"role": "system", "content": "corpus"},
                      {"role": "user", "content": "do the thing"}],
                     max_tokens=100, schema=schema, effort=effort)
    return sent


def client(**kwargs):
    with mock.patch.object(credentials, "resolve", return_value="sk-test"):
        return deepseek.Client(verbose=False, **kwargs)


class WhereItGoesTests(unittest.TestCase):
    def test_it_posts_to_deepseek_not_openrouter(self):
        sent = call(client())
        self.assertEqual(sent.url, deepseek.API)
        self.assertIn("deepseek.com", sent.url)
        self.assertNotIn("openrouter", sent.url)

    def test_the_key_is_the_deepseek_one(self):
        with mock.patch.object(credentials, "resolve",
                               return_value="sk-deepseek") as resolve:
            built = deepseek.Client(verbose=False)
        resolve.assert_called_once_with("deepseek")
        self.assertEqual(built.key, "sk-deepseek")

    def test_deepseek_has_a_credential_slot_of_its_own(self):
        self.assertEqual(credentials.PROVIDER_ENV["deepseek"], "DEEPSEEK_API_KEY")

    def test_the_endpoint_is_overridable(self):
        # The one lever that matters if the path here turns out to be wrong.
        self.assertTrue(deepseek.API.startswith("https://"))
        with mock.patch.dict(os.environ, {"DEEPSEEK_URL": "https://example/v1/x"}):
            import importlib
            reloaded = importlib.reload(deepseek)
            try:
                self.assertEqual(reloaded.API, "https://example/v1/x")
            finally:
                importlib.reload(deepseek)


class WhatItSendsTests(unittest.TestCase):
    def test_no_openrouter_headers_travel(self):
        sent = call(client())
        for header in ("Http-referer", "X-title", "X-openrouter-metadata"):
            self.assertNotIn(header, sent.headers)
        self.assertEqual(sent.headers["Authorization"], "Bearer sk-test")

    def test_no_reasoning_key_is_sent(self):
        """Reasoning here is the model id, not a parameter.

        OpenRouter takes a `reasoning` object; DeepSeek does not, and sending it
        is at best ignored. Worse, it would imply the effort setting does
        something — when what actually decides is whether the model is
        deepseek-reasoner.
        """
        for effort in ("high", "max", "low", "none"):
            sent = call(client(), effort=effort)
            self.assertNotIn("reasoning", sent.body, effort)

    def test_openrouter_still_sends_its_own_reasoning_key(self):
        # The refactor must not have quietly disarmed the provider it came from.
        with mock.patch.object(credentials, "resolve", return_value="sk-or"):
            other = openrouter.Client(model="x", verbose=False)
        sent = call(other, effort="high")
        self.assertEqual(sent.body["reasoning"], {"effort": "high"})
        self.assertEqual(sent.headers["X-title"], "adpipe")

    def test_the_default_model_does_not_reason(self):
        # Extraction wants its whole output budget on the answer.
        self.assertEqual(deepseek.DEFAULT_MODEL, "deepseek-chat")
        self.assertEqual(call(client()).body["model"], "deepseek-chat")

    def test_an_explicit_model_wins(self):
        self.assertEqual(call(client(model="deepseek-reasoner")).body["model"],
                         "deepseek-reasoner")


class StructuredOutputTests(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"hooks": {"type": "array"}},
              "required": ["hooks"]}

    def test_it_asks_for_json_object_not_json_schema(self):
        # DeepSeek's JSON mode constrains the output to valid JSON and nothing
        # further. Sending OpenRouter's json_schema shape would be rejected.
        sent = call(client(), schema=self.SCHEMA)
        self.assertEqual(sent.body["response_format"], {"type": "json_object"})

    def test_the_schema_goes_into_the_prompt_instead(self):
        # Because json_object says "JSON" and not "this JSON", the shape has to
        # reach the model some other way or the stage gets valid, useless JSON.
        sent = call(client(), schema=self.SCHEMA)
        user = [m for m in sent.body["messages"] if m["role"] == "user"][-1]
        self.assertIn("JSON Schema", user["content"])
        self.assertIn("hooks", user["content"])

    def test_the_corpus_turn_is_left_alone(self):
        # The system block is the corpus and is what a cache keys on. Appending
        # to it would change the key on every differently-shaped stage.
        sent = call(client(), schema=self.SCHEMA)
        system = [m for m in sent.body["messages"] if m["role"] == "system"][0]
        self.assertEqual(system["content"], "corpus")

    def test_no_openrouter_route_pinning_is_sent(self):
        sent = call(client(), schema=self.SCHEMA)
        self.assertNotIn("provider", sent.body)

    def test_a_stage_without_a_schema_asks_for_nothing(self):
        self.assertNotIn("response_format", call(client()).body)


class AccountingTests(unittest.TestCase):
    def test_the_estimate_names_deepseek_and_its_own_prices(self):
        # An estimate that says OpenRouter while billing DeepSeek is worse than
        # none: the number is what somebody approves a spend against.
        built = client(model="deepseek-chat")
        estimate = built.estimate("corpus " * 100, "preamble",
                                  [mock.Mock(prompt="p", max_tokens=1000)])
        text = estimate.explain()
        self.assertIn("DeepSeek", text)
        self.assertNotIn("OpenRouter", text)
        self.assertIn("platform.deepseek.com", text)
        self.assertEqual(estimate.pricing, deepseek.PRICING)

    def test_the_estimate_does_not_assume_a_cache_hit(self):
        # They discount a repeat, but a first run over fresh evidence is all
        # misses, and an estimate that assumed otherwise would under-state.
        built = client()
        text = built.estimate("c", "p", [mock.Mock(prompt="p", max_tokens=10)]).explain()
        self.assertIn("re-sent on EACH request", text)

    def test_the_audit_trail_says_which_provider_was_billed(self):
        built = client()
        with mock.patch.object(openrouter.auditlog, "start") as start:
            call(built)
        self.assertEqual(start.call_args[0][0], "deepseek")


if __name__ == "__main__":
    unittest.main()
