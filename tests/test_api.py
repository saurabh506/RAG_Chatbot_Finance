import json
import unittest

import config

config.VALIDATE_LIVE = False  # tests never touch the network

from rag import api


def call(question):
    return api.handle(json.dumps({"question": question}))


class ApiTests(unittest.TestCase):
    def test_invalid_inputs_are_400(self):
        for body in (b"", b"not json", b"[]", json.dumps({"question": 5}),
                     json.dumps({"question": "   "}),
                     json.dumps({"question": "x" * 301})):
            self.assertEqual(api.handle(body)[0], 400, body)

    def test_refusal_is_200_with_one_link(self):
        status, body = call("Should I buy HDFC Mid Cap?")
        self.assertEqual(status, 200)
        self.assertEqual(body["answer_type"], "refusal")
        self.assertTrue(body["citation_url"].startswith("https://"))

    def test_pii_is_422_and_not_echoed(self):
        status, body = call("My PAN is ABCDE1234F, exit load?")
        self.assertEqual(status, 422)
        self.assertNotIn("ABCDE1234F", json.dumps(body))

    def test_payload_shape(self):
        _, body = call("How do I download a capital gains statement?")
        self.assertEqual(
            set(body),
            {"answer_type", "text", "citation_url", "last_updated", "intent",
             "scheme_id", "validated", "corpus_version"},
        )
        self.assertNotIn("http", body["text"])


if __name__ == "__main__":
    unittest.main()
