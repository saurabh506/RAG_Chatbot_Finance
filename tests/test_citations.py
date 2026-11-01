import tempfile
import unittest
from pathlib import Path

import config

config.VALIDATE_LIVE = False  # only the explicit validator below is exercised

from rag import retrieve
from rag.citations import normalize, validate
from tests.test_guardrails import ALL_FACTS
from tests.test_retrieve import MANIFEST, StubStore

URL = "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
PAGE = (
    "<html><body><h1>HDFC ELSS Tax Saver Fund Direct Plan Growth</h1>"
    "<div>Expense ratio 1.20%</div><div>ELSS 3Y Lock-in</div></body></html>"
)


class Fetcher:
    def __init__(self, status=200, html=PAGE, error=None):
        self.status, self.html, self.error, self.calls = status, html, error, 0

    def __call__(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        return self.status, self.html


def run(fetch, value="1.20%", url=URL, now=1000.0, cache=None):
    cache = cache or Path(tempfile.mkdtemp()) / "cache.json"
    return validate(
        url, "expense_ratio", value,
        marker="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        fetch=fetch, cache_path=cache, now=now,
    ), cache


class NormaliseTests(unittest.TestCase):
    def test_symbols_and_nil(self):
        self.assertEqual(normalize("₹1,500 "), "1500")
        self.assertEqual(normalize("1.20%"), "1.20")
        for raw in ("Nil", "None", "N/A", "—"):
            self.assertEqual(normalize(raw), "nil")


class ValidateTests(unittest.TestCase):
    def test_value_present_passes(self):
        result, _ = run(Fetcher())
        self.assertTrue(result.ok)
        self.assertEqual(result.http_status, 200)

    def test_changed_value_fails(self):
        result, _ = run(Fetcher(), value="1.35%")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "value not on page")

    def test_non_200_fails(self):
        result, _ = run(Fetcher(status=404))
        self.assertFalse(result.ok)
        self.assertEqual(result.http_status, 404)

    def test_wrong_scheme_page_fails(self):
        result, _ = run(Fetcher(html="<h1>Other Fund</h1><div>Expense ratio 1.20%</div>"))
        self.assertEqual(result.reason, "scheme not on page")

    def test_non_allowlisted_host_never_fetched(self):
        fetch = Fetcher()
        result, _ = run(fetch, url="https://evil.example.com/x")
        self.assertFalse(result.ok)
        self.assertEqual(fetch.calls, 0)

    def test_http_url_refused(self):
        result, _ = run(Fetcher(), url=URL.replace("https", "http"))
        self.assertFalse(result.ok)

    def test_network_error_fails_closed_and_is_not_cached(self):
        result, cache = run(Fetcher(error=TimeoutError()))
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("fetch failed"))
        self.assertFalse(cache.exists())

    def test_cache_hit_within_ttl_skips_network(self):
        fetch = Fetcher()
        _, cache = run(fetch)
        run(fetch, now=1000.0 + 3600, cache=cache)
        self.assertEqual(fetch.calls, 1)

    def test_cache_expires_after_ttl(self):
        fetch = Fetcher()
        _, cache = run(fetch)
        run(fetch, now=1000.0 + 25 * 3600, cache=cache)
        self.assertEqual(fetch.calls, 2)


class PipelineTests(unittest.TestCase):
    def ask(self, ok):
        class R:
            pass

        def stub(url, field, value, marker=None):
            r = R()
            r.ok = ok
            return r

        return retrieve.answer("ELSS lock-in?", ALL_FACTS, StubStore(), MANIFEST, validate=stub)

    def test_validated_answer_is_flagged(self):
        a = self.ask(True)
        self.assertEqual((a.source, a.validated), ("facts", True))
        self.assertIn("3 years", a.text)

    def test_failed_validation_never_shows_the_number(self):
        a = self.ask(False)
        self.assertEqual((a.source, a.validated), ("unverifiable", False))
        self.assertNotIn("3 years", a.text)
        self.assertEqual(a.citation_url, ALL_FACTS["hdfc-elss-direct-growth"]["source_url"])


if __name__ == "__main__":
    unittest.main()
