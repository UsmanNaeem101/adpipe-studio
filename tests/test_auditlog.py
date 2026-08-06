import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import auditlog


class AuditLogTests(unittest.TestCase):
    def test_request_response_and_empty_output_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": tmp}):
            with auditlog.scope(project="shoulder", stage="extract"):
                audit = auditlog.start(
                    "openrouter", "test/model", "pipeline_batch_job",
                    {"messages": [{"role": "user", "content": "full prompt"}]},
                    job_id="07_extract_pain_points")
                audit.response(
                    {"choices": [{"message": {"content": ""}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 0}},
                    text="", stop_reason="stop")

            with open(os.path.join(audit.directory, "request.json"),
                      encoding="utf-8") as fh:
                request = json.load(fh)
            with open(os.path.join(audit.directory, "response.json"),
                      encoding="utf-8") as fh:
                response = json.load(fh)

        self.assertEqual(request["request"]["messages"][0]["content"], "full prompt")
        self.assertEqual(request["job_id"], "07_extract_pain_points")
        self.assertEqual(request["context"]["project"], "shoulder")
        self.assertTrue(response["empty_text"])
        self.assertEqual(response["provider_response"]["usage"]["prompt_tokens"], 10)

    def test_binary_response_is_saved_as_a_file_not_base64(self):
        data = b"\x89PNG\r\n\x1a\nexample"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"ADPIPE_LOG_DIR": tmp}):
            audit = auditlog.start("openai", "image", "image_generation",
                                   {"prompt": "make an image"})
            audit.binary_response(data, {"data": [{"b64_json": "[stored]"}]})
            with open(os.path.join(audit.directory, "response.png"), "rb") as fh:
                saved = fh.read()
            with open(os.path.join(audit.directory, "response.json"),
                      encoding="utf-8") as fh:
                response = json.load(fh)

        self.assertEqual(saved, data)
        self.assertEqual(response["metadata"]["binary_file"], "response.png")
        self.assertEqual(response["metadata"]["binary_bytes"], len(data))


if __name__ == "__main__":
    unittest.main()
