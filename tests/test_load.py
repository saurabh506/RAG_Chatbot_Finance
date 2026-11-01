"""Load-stage tests: HTML -> text. Stdlib unittest, no network."""

from __future__ import annotations

import re
import unittest

import config
from ingest.fetch import _cache_path
from ingest.load import (
    MIN_CHARS,
    LoadedDoc,
    _mark_spa_shells,
    html_to_text,
    load_all,
    read_docs,
)
from ingest.manifest import SPECS

PAGE = """
<html><head><title>Page Title - Groww</title>
<style>.x{color:red}</style><script>var secret = 1;</script></head>
<body>
  <div>NAV CHROME Stocks IPO Demat</div>
  <h1>HDFC Test Fund Direct Growth</h1>
  <div>Equity Flexi Cap <span>Very High Risk</span></div>
  <h2>Expense ratio</h2>
  <div>Expense ratio <span>0.75%</span></div>
  <h2>Exit load, stamp duty and tax</h2>
  <div>Exit load Nil</div>
  <h2>Minimum investments</h2>
  <div>Minimum SIP Investment is set to &#8377;100.</div>
  <div>Home&gt;Mutual Funds&gt;HDFC Test Fund</div>
  <div>FOOTER SIP Calculator Blog Careers</div>
</body></html>
"""


class TestHtmlToText(unittest.TestCase):
    def test_strips_script_and_style(self) -> None:
        text, _, _ = html_to_text(PAGE)
        self.assertNotIn("secret", text)
        self.assertNotIn("color:red", text)

    def test_title_is_metadata_only(self) -> None:
        text, _, title = html_to_text(PAGE)
        self.assertEqual(title, "Page Title - Groww")
        self.assertNotIn("Page Title", text)

    def test_chrome_before_h1_is_dropped(self) -> None:
        text, headings, _ = html_to_text(PAGE)
        self.assertNotIn("NAV CHROME", text)
        self.assertTrue(text.startswith("HDFC Test Fund Direct Growth"))
        self.assertEqual(headings[0], "HDFC Test Fund Direct Growth")

    def test_footer_is_trimmed(self) -> None:
        text, _, _ = html_to_text(PAGE)
        self.assertNotIn("FOOTER", text)
        self.assertNotIn("SIP Calculator", text)
        self.assertIn("Exit load Nil", text)

    def test_entities_and_currency(self) -> None:
        text, _, _ = html_to_text(PAGE)
        self.assertIn("₹100", text)
        self.assertIn("Exit load Nil", text)
        self.assertNotIn("&#8377;", text)

    def test_inline_nodes_do_not_fuse(self) -> None:
        html = "<h1>X</h1><div>Monthly SIP<span>One time</span></div>"
        text, _, _ = html_to_text(html)
        self.assertIn("Monthly SIP One time", text)

    def test_heading_markers_are_collected(self) -> None:
        _, headings, _ = html_to_text(PAGE)
        self.assertIn("Expense ratio", headings)
        self.assertIn("Exit load, stamp duty and tax", headings)
        self.assertIn("Minimum investments", headings)

    def test_deterministic(self) -> None:
        first = html_to_text(PAGE)
        second = html_to_text(PAGE)
        self.assertEqual(first, second)

    def test_empty_html(self) -> None:
        text, headings, title = html_to_text("")
        self.assertEqual(text, "")
        self.assertEqual(headings, [])
        self.assertEqual(title, "")


class TestSpaShell(unittest.TestCase):
    def test_identical_chars_without_headings_becomes_link_only(self) -> None:
        def doc(name: str) -> LoadedDoc:
            return LoadedDoc(
                doc_id=name, url=f"https://groww.in/help/{name}", scheme_id=None,
                title="help", purpose="process", mode="extract", text="x" * 9000,
                headings=[], chars=9000, content_hash=f"sha256:{name}",
                fetched_at="2026-11-01", http_status=200, source_bytes=1,
                status="ok",
            )

        docs = [doc("a"), doc("b")]
        _mark_spa_shells(docs)
        self.assertEqual([d.status for d in docs], ["link-only", "link-only"])

    def test_distinct_pages_are_untouched(self) -> None:
        docs = [
            LoadedDoc(
                doc_id="a", url="https://groww.in/a", scheme_id=None, title="a",
                purpose="scheme-facts", mode="extract", text="x" * 9000,
                headings=["Expense ratio"], chars=9000, content_hash="sha256:a",
                fetched_at="2026-11-01", http_status=200, source_bytes=1, status="ok",
            ),
            LoadedDoc(
                doc_id="b", url="https://groww.in/b", scheme_id=None, title="b",
                purpose="scheme-facts", mode="extract", text="y" * 9100,
                headings=["Expense ratio"], chars=9100, content_hash="sha256:b",
                fetched_at="2026-11-01", http_status=200, source_bytes=1, status="ok",
            ),
        ]
        _mark_spa_shells(docs)
        self.assertEqual([d.status for d in docs], ["ok", "ok"])


class TestCachedCorpus(unittest.TestCase):
    """Runs only when data/raw is present (gitignored, regenerable)."""

    @classmethod
    def setUpClass(cls) -> None:
        missing = [s for s in SPECS if s.mode == "extract" and not _cache_path(s.url).exists()]
        if missing:
            raise unittest.SkipTest("no cached HTML; run `py -3.11 -m ingest.run --fetch`")
        cls.docs = load_all()

    def test_all_documents_ok(self) -> None:
        self.assertEqual(len(self.docs), 5)
        self.assertEqual([d.doc_id for d in self.docs if d.status != "ok"], [])

    def test_size_gate(self) -> None:
        for doc in self.docs:
            self.assertGreaterEqual(doc.chars, MIN_CHARS, doc.doc_id)

    def test_scheme_pages_have_marker_and_headings(self) -> None:
        for doc in self.docs:
            if doc.purpose != "scheme-facts":
                continue
            self.assertIn("Expense ratio", doc.text, doc.doc_id)
            self.assertGreaterEqual(len(doc.headings), 3, doc.doc_id)

    def test_no_script_or_mojibake(self) -> None:
        for doc in self.docs:
            self.assertNotIn("<script", doc.text, doc.doc_id)
            self.assertNotIn("\ufffd", doc.text, doc.doc_id)
            self.assertNotIn("Ã", doc.text, doc.doc_id)

    def test_no_site_chrome(self) -> None:
        for doc in self.docs:
            self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", doc.text, doc.doc_id)
            self.assertNotIn("Groww Pay - Privacy policy", doc.text, doc.doc_id)

    def test_elss_exit_load_is_nil(self) -> None:
        elss = next(d for d in self.docs if d.doc_id == "hdfc-elss-direct-growth")
        flat = re.sub(r"\s+", " ", elss.text)
        self.assertIn("Exit load Nil", flat)
        self.assertNotIn("Exit load of 1% if redeemed", flat)

    def test_other_schemes_have_one_percent_exit_load(self) -> None:
        for doc in self.docs:
            if doc.purpose != "scheme-facts" or doc.doc_id == "hdfc-elss-direct-growth":
                continue
            flat = re.sub(r"\s+", " ", doc.text)
            self.assertIn("Exit load of 1% if redeemed within 1 year", flat, doc.doc_id)


class TestDocsJsonl(unittest.TestCase):
    def test_written_records_have_required_keys(self) -> None:
        if not config.DOCS.exists():
            self.skipTest("data/docs.jsonl not built")
        records = read_docs()
        self.assertTrue(records)
        required = {
            "doc_id", "url", "scheme_id", "title", "purpose", "mode", "text",
            "headings", "chars", "content_hash", "fetched_at", "status",
        }
        for record in records:
            self.assertTrue(required <= set(record), record.get("doc_id"))
            self.assertEqual(record["chars"], len(record["text"]))
            self.assertTrue(record["content_hash"].startswith("sha256:"))

    def test_hashes_are_unique(self) -> None:
        if not config.DOCS.exists():
            self.skipTest("data/docs.jsonl not built")
        hashes = [r["content_hash"] for r in read_docs() if r["status"] == "ok"]
        self.assertEqual(len(hashes), len(set(hashes)))


if __name__ == "__main__":
    unittest.main()
