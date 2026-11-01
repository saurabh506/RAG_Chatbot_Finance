import unittest

import config

config.VALIDATE_LIVE = False  # unit tests never touch the network

from rag import retrieve
from rag.vector_store import NumpyStore, VectorHit

FACTS = {
    "hdfc-elss-direct-growth": {
        "scheme_name": "HDFC ELSS Tax Saver Fund Direct Plan Growth",
        "source_url": "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth",
        "fetched_at": "2026-11-01",
        "fields": {
            "expense_ratio": {"value": "1.20%"},
            "lock_in": {"value": "3 years"},
            "min_sip": {"value": "₹500"},
            "min_lumpsum": {"value": "₹500"},
        },
    }
}


class StubStore:
    def __init__(self, score=0.6):
        self.score = score
        self.calls = []

    def query(self, vector, k=4, scheme_id=None):
        self.calls.append(scheme_id)
        meta = {"source_url": "https://groww.in/x", "fetched_at": "2026-11-01"}
        return [VectorHit("c#1", "First. Second. Third see https://a.b/c.", self.score, meta)]


MANIFEST = {"backend": "hash", "model": "hash-md5-256", "dim": 256}


class GuardrailTests(unittest.TestCase):
    def test_refusal_codes(self):
        cases = {
            "my PAN is ABCDE1234F": "R5",
            "should I buy HDFC Mid Cap?": "R1",
            "which fund has better returns": "R2",
            "how much should I invest": "R3",
            "how much tax will I save with ELSS": "R4",
            "expense ratio of SBI bluechip": "R6",
            "expense ratio of HDFC ELSS": None,
        }
        for question, code in cases.items():
            self.assertEqual(retrieve.guardrail(question), code, question)

    def test_refusal_has_single_official_source(self):
        a = retrieve.answer("should I buy this fund")
        self.assertEqual(a.source, "refusal")
        self.assertEqual(a.citation_url, retrieve.config.LINK_ONLY_URLS["sebi-investor"])


class RoutingTests(unittest.TestCase):
    def test_scheme_resolver(self):
        self.assertEqual(retrieve.resolve_scheme("ELSS lock-in?"), "hdfc-elss-direct-growth")
        self.assertEqual(retrieve.resolve_scheme("mid cap fee"), "hdfc-mid-direct-growth")
        self.assertIsNone(retrieve.resolve_scheme("exit load?"))

    def test_intents(self):
        self.assertEqual(retrieve.classify_intent("what is the exit load"), "I2")
        self.assertEqual(retrieve.classify_intent("minimum SIP amount"), "I3")
        self.assertEqual(retrieve.classify_intent("download capital gains statement"), "I7")

    def test_structured_answer_skips_vector_store(self):
        store = StubStore()
        a = retrieve.answer("ELSS expense ratio?", FACTS, store, MANIFEST)
        self.assertEqual(a.source, "facts")
        self.assertIn("1.20%", a.text)
        self.assertEqual(a.text.count("http"), 1)
        self.assertEqual(store.calls, [])

    def test_lumpsum_uses_lumpsum_field(self):
        a = retrieve.answer("ELSS minimum lumpsum", FACTS, StubStore(), MANIFEST)
        self.assertIn("lumpsum", a.text)

    def test_missing_scheme_asks_for_one(self):
        a = retrieve.answer("what is the exit load", FACTS, StubStore(), MANIFEST)
        self.assertEqual(a.source, "clarify")

    def test_missing_fact_falls_back_to_vector_with_scheme_filter(self):
        store = StubStore()
        a = retrieve.answer("ELSS exit load", FACTS, store, MANIFEST)
        self.assertEqual(a.source, "vector")
        self.assertEqual(store.calls, ["hdfc-elss-direct-growth"])

    def test_vector_answer_is_clipped_and_url_free(self):
        a = retrieve.answer("how do I download my statement", {}, StubStore(), MANIFEST)
        self.assertEqual(a.source, "vector")
        self.assertNotIn("http", a.text)
        self.assertEqual(a.text, "First. Second.")

    def test_below_floor_returns_not_found(self):
        a = retrieve.answer("how do I download my statement", {}, StubStore(0.1), MANIFEST)
        self.assertEqual(a.source, "none")

    def test_long_input_is_capped(self):
        a = retrieve.answer("x" * 5000, {}, StubStore(0.1), MANIFEST)
        self.assertEqual(a.source, "clarify")


class RealCorpusTests(unittest.TestCase):
    def test_process_question_hits_statement_chunk(self):
        try:
            store = NumpyStore()
            from ingest.embed import read_embeddings

            manifest = read_embeddings()[1]
        except (FileNotFoundError, ValueError):
            self.skipTest("index artifacts not built")
        a = retrieve.answer(
            "How do I download a capital gains statement?", {}, store, manifest
        )
        self.assertEqual(a.source, "vector")
        self.assertTrue(a.citation_url.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
