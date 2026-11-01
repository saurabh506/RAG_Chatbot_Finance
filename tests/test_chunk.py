"""Tests for the section-aware chunker (ingest/chunk.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ingest import chunk


def doc(text: str, headings: list[str], status: str = "ok", doc_id: str = "hdfc-large-direct-growth") -> dict:
    return {
        "doc_id": doc_id,
        "url": "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
        "scheme_id": "hdfc-large-direct-growth",
        "title": "HDFC Large Cap Fund",
        "text": text,
        "headings": headings,
        "fetched_at": "2026-11-01",
        "status": status,
    }


class SectionTests(unittest.TestCase):
    def test_sections_follow_document_order(self) -> None:
        text = "Intro line\nAlpha\na1\nBeta\nb1\nAlpha\na2"
        sections = chunk.split_sections(text, ["Alpha", "Beta", "Alpha"])
        self.assertEqual(
            sections,
            [
                ("Overview", "Intro line"),
                ("Alpha", "a1"),
                ("Beta", "b1"),
                ("Alpha", "a2"),
            ],
        )

    def test_repeated_heading_is_not_a_later_boundary(self) -> None:
        text = "Alpha\na\nBeta\nb\nAlpha\nc"
        sections = chunk.split_sections(text, ["Alpha", "Beta"])
        self.assertEqual(sections, [("Alpha", "a"), ("Beta", "b\nAlpha\nc")])

    def test_no_headings_falls_back_to_overview(self) -> None:
        self.assertEqual(
            chunk.split_sections("just body text", []),
            [("Overview", "just body text")],
        )

    def test_empty_body_is_skipped(self) -> None:
        sections = chunk.split_sections("Alpha\nBeta\nb", ["Alpha", "Beta"])
        self.assertEqual(sections, [("Beta", "b")])

    def test_heading_must_occupy_whole_line(self) -> None:
        text = "Alpha beta is a sentence\nAlpha\na"
        sections = chunk.split_sections(text, ["Alpha"])
        self.assertEqual(sections[0][0], "Overview")
        self.assertIn("Alpha beta is a sentence", sections[0][1])
        self.assertEqual(sections[1], ("Alpha", "a"))


class PackingTests(unittest.TestCase):
    def test_short_body_stays_one_chunk(self) -> None:
        parts = chunk.pack_units(chunk._units("Min. for SIP is 100. Exit load is 1%."))
        self.assertEqual(len(parts), 1)
        self.assertIn("Min. for SIP is 100.", parts[0])

    def test_long_body_is_windowed_and_overlaps(self) -> None:
        body = " ".join(f"w{i}." for i in range(900))
        parts = chunk.pack_units(chunk._units(body))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part.split()), chunk.MAX_WORDS)
        first_tail = parts[0].split()[-chunk.OVERLAP_WORDS :]
        second_head = parts[1].split()[: chunk.OVERLAP_WORDS]
        self.assertEqual(first_tail, second_head)

    def test_single_unit_over_max_is_split(self) -> None:
        body = " ".join(f"w{i}" for i in range(1200))
        parts = chunk.pack_units(chunk._units(body))
        self.assertTrue(all(len(part.split()) <= chunk.MAX_WORDS for part in parts))
        self.assertEqual(len(parts), 3)

    def test_blank_lines_are_ignored(self) -> None:
        self.assertEqual(chunk._units("\n\n  \n"), [])


class ChunkRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = doc(
            "HDFC Large Cap Fund\nExit load, stamp duty and tax\nExit load is 1%.",
            ["HDFC Large Cap Fund", "Exit load, stamp duty and tax"],
        )

    def test_records_carry_citation_metadata(self) -> None:
        records = chunk.chunk_doc(self.document)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["chunk_id"], "hdfc-large-direct-growth#exit-load-stamp-duty-and-tax-0")
        self.assertEqual(record["scheme_id"], "hdfc-large-direct-growth")
        self.assertEqual(record["section"], "Exit load, stamp duty and tax")
        self.assertEqual(record["source_url"], self.document["url"])
        self.assertEqual(record["fetched_at"], "2026-11-01")
        self.assertTrue(record["text"].startswith("HDFC Large Cap Fund | Exit load"))

    def test_ids_are_unique_across_sections(self) -> None:
        records = chunk.chunk_doc(self.document)
        self.assertEqual(len({r["chunk_id"] for r in records}), len(records))

    def test_curated_statement_chunks_have_official_urls(self) -> None:
        records = chunk.curated_chunks("2026-11-01")
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertTrue(record["source_url"].startswith("https://"))
            self.assertEqual(record["section"], "Statements")
            self.assertIn("statement", record["text"].lower())

    def test_build_chunks_filters_non_ok_documents(self) -> None:
        good = doc("Alpha\na", ["Alpha"])
        bad = doc("Alpha\nb", ["Alpha"], status="failed", doc_id="broken-doc")
        records = chunk.build_chunks([good, bad])
        self.assertTrue(any(r["doc_id"] == good["doc_id"] for r in records))
        self.assertFalse(any(r["doc_id"] == bad["doc_id"] for r in records))

    def test_build_chunks_adds_curated_only_when_usable(self) -> None:
        self.assertEqual(chunk.build_chunks([]), [])
        records = chunk.build_chunks([doc("Alpha\na", ["Alpha"])])
        self.assertTrue(any(r["chunk_id"].startswith("process#") for r in records))

    def test_slug(self) -> None:
        self.assertEqual(chunk.slug("Exit load, stamp duty and tax"), "exit-load-stamp-duty-and-tax")
        self.assertEqual(chunk.slug("!!!"), "section")


class PersistenceTests(unittest.TestCase):
    def test_write_read_roundtrip(self) -> None:
        records = chunk.chunk_doc(doc("Alpha\na", ["Alpha"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chunks.jsonl"
            chunk.write_chunks(records, path)
            loaded = chunk.read_chunks(path)
        self.assertEqual(loaded, records)

    def test_read_missing_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(chunk.read_chunks(Path(tmp) / "missing.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
