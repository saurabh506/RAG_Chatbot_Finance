import json
import tempfile
import unittest
from pathlib import Path

from obs import log
from rag import retrieve
from rag.guardrails import PII_PATTERNS

from tests.test_guardrails import ALL_FACTS
from tests.test_retrieve import MANIFEST, StubStore

QUESTIONS = (
    "My PAN is ABCDE1234F, exit load?",
    "OTP 482913 — what's the lock-in?",
    "Call me on 9876543210",
    "Mail me at a@b.com",
    "aadhaar 1234 5678 9012 expense ratio",
    "folio no 12345678 exit load",
    "Should I buy HDFC Mid Cap?",
    "ELSS lock-in?",
    "HDFC Mid Cap exit load?",
    "asdkjhqwe",
)
EXPECTED_KEYS = {
    "ts", "intent", "scheme_id", "answer_type", "citation_url",
    "validated", "latency_ms", "corpus_version",
}


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "requests.log"
        for question in QUESTIONS:
            result = retrieve.answer(question, ALL_FACTS, StubStore(0.1), MANIFEST)
            log.log_request(result, 12, path=self.path)
        self.text = self.path.read_text(encoding="utf-8")

    def test_one_line_per_request(self):
        self.assertEqual(len(self.text.splitlines()), len(QUESTIONS))

    def test_records_have_only_metadata_keys(self):
        for line in self.text.splitlines():
            self.assertEqual(set(json.loads(line)), EXPECTED_KEYS)

    def test_no_pii_patterns_in_log(self):
        for label, pattern in PII_PATTERNS.items():
            self.assertIsNone(pattern.search(self.text), label)

    def test_no_question_text_in_log(self):
        lowered = self.text.lower()
        for question in QUESTIONS:
            self.assertNotIn(question.lower(), lowered)
        for fragment in ("abcde1234f", "482913", "9876543210", "a@b.com", "asdkjhqwe"):
            self.assertNotIn(fragment, lowered)

    def test_answer_types_are_recorded(self):
        types = [json.loads(line)["answer_type"] for line in self.text.splitlines()]
        self.assertEqual(types.count("refusal"), 7)
        self.assertEqual(types.count("answer"), 2)

    def test_logging_failure_never_raises(self):
        result = retrieve.answer("ELSS lock-in?", ALL_FACTS, StubStore(), MANIFEST)
        blocked = self.path / "nested" / "x.log"  # parent is a file, so mkdir fails
        log.log_request(result, 1, path=blocked)


if __name__ == "__main__":
    unittest.main()
