"""Tests for the embedding stage (ingest/embed.py)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import config
from ingest import embed


def make_chunks() -> list[dict]:
    return [
        {
            "chunk_id": "scheme#exit-load-0",
            "doc_id": "scheme",
            "scheme_id": "hdfc-large-direct-growth",
            "section": "Exit load, stamp duty and tax",
            "source_url": "https://groww.in/mutual-funds/example",
            "fetched_at": "2026-11-01",
            "words": 8,
            "text": "HDFC Large Cap Fund | Exit load | Exit load is 1% within 1 year.",
        },
        {
            "chunk_id": "scheme#minimum-investments-1",
            "doc_id": "scheme",
            "scheme_id": "hdfc-large-direct-growth",
            "section": "Minimum investments",
            "source_url": "https://groww.in/mutual-funds/example",
            "fetched_at": "2026-11-01",
            "words": 10,
            "text": "HDFC Large Cap Fund | Minimum investments | Min. for SIP is 100 rupees.",
        },
        {
            "chunk_id": "process#statement-capital-gains",
            "doc_id": "process-statement",
            "scheme_id": "hdfc-amc",
            "section": "Statements",
            "source_url": "https://groww.in/help/example",
            "fetched_at": "2026-11-01",
            "words": 9,
            "text": "HDFC Mutual Fund (AMC) | Statements | Download a capital gains statement.",
        },
    ]


class HashEmbedTests(unittest.TestCase):
    def test_shape_and_unit_norm(self) -> None:
        vector = embed.hash_embed("exit load within one year")
        self.assertEqual(vector.shape, (config.HASH_EMBED_DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=6)

    def test_deterministic(self) -> None:
        text = "minimum sip investment amount"
        np.testing.assert_array_equal(embed.hash_embed(text), embed.hash_embed(text))

    def test_custom_dim(self) -> None:
        self.assertEqual(embed.hash_embed("exit load", dim=64).shape, (64,))

    def test_stopwords_do_not_change_vector(self) -> None:
        plain = embed.hash_embed("exit load")
        padded = embed.hash_embed("What is the exit load?")
        np.testing.assert_allclose(plain, padded, rtol=0, atol=1e-6)

    def test_trailing_period_ignored(self) -> None:
        np.testing.assert_allclose(
            embed.hash_embed("Min. for SIP"),
            embed.hash_embed("min for sip"),
            rtol=0,
            atol=1e-6,
        )

    def test_empty_text_is_zero_vector(self) -> None:
        vector = embed.hash_embed("")
        self.assertEqual(float(np.linalg.norm(vector)), 0.0)


class NormaliseTests(unittest.TestCase):
    def test_rows_become_unit_length(self) -> None:
        matrix = np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32)
        normalised = embed.l2_normalise(matrix)
        np.testing.assert_allclose(np.linalg.norm(normalised, axis=1), [1.0, 1.0])
        np.testing.assert_allclose(normalised[0], [0.6, 0.8], rtol=1e-6)

    def test_zero_row_is_not_nan(self) -> None:
        normalised = embed.l2_normalise(np.zeros((1, 4), dtype=np.float32))
        self.assertFalse(np.isnan(normalised).any())
        self.assertEqual(float(normalised.sum()), 0.0)

    def test_1d_input_is_promoted(self) -> None:
        normalised = embed.l2_normalise(np.array([3.0, 4.0], dtype=np.float32))
        self.assertEqual(normalised.shape, (1, 2))


class EmbedderFactoryTests(unittest.TestCase):
    def test_hash_backend(self) -> None:
        embedder = embed.get_embedder("hash")
        self.assertEqual(embedder.backend, "hash")
        self.assertEqual(embedder.dim, config.HASH_EMBED_DIM)
        self.assertIsNone(embedder.fallback_reason)

    def test_unknown_backend_rejected(self) -> None:
        with self.assertRaises(ValueError):
            embed.get_embedder("word2vec")

    def test_minilm_falls_back_when_import_fails(self) -> None:
        with mock.patch.object(
            embed.MiniLMEmbedder, "_load", side_effect=ModuleNotFoundError("no torch")
        ):
            embedder = embed.get_embedder("minilm")
        self.assertEqual(embedder.backend, "hash")
        self.assertIn("ModuleNotFoundError", embedder.fallback_reason or "")

    def test_minilm_strict_raises(self) -> None:
        with mock.patch.object(
            embed.MiniLMEmbedder, "_load", side_effect=OSError("no model")
        ):
            with self.assertRaises(RuntimeError):
                embed.get_embedder("minilm", strict=True)

    def test_hash_encode_batch(self) -> None:
        texts = ["exit load", "minimum sip", "capital gains statement"]
        matrix = embed.HashEmbedder().encode(texts, batch_size=2)
        self.assertEqual(matrix.shape, (3, config.HASH_EMBED_DIM))
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), [1.0, 1.0, 1.0])


class BuildTests(unittest.TestCase):
    def test_build_embeddings_contract(self) -> None:
        chunks = make_chunks()
        matrix, manifest = embed.build_embeddings(chunks, backend="hash")
        self.assertEqual(matrix.shape, (len(chunks), config.HASH_EMBED_DIM))
        self.assertEqual(manifest["backend"], "hash")
        self.assertEqual(manifest["dim"], config.HASH_EMBED_DIM)
        self.assertEqual(manifest["chunk_count"], len(chunks))
        self.assertEqual(manifest["ids"], [c["chunk_id"] for c in chunks])
        self.assertTrue(manifest["normalised"])
        self.assertEqual(manifest["built_at"][-6:], "+00:00")
        np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, rtol=1e-6)

    def test_build_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            embed.build_embeddings([], backend="hash")

    def test_build_records_fallback_reason(self) -> None:
        with mock.patch.object(
            embed.MiniLMEmbedder, "_load", side_effect=ImportError("missing")
        ):
            _, manifest = embed.build_embeddings(make_chunks(), backend="minilm")
        self.assertEqual(manifest["backend"], "hash")
        self.assertEqual(manifest["requested_backend"], "minilm")
        self.assertIn("fallback_reason", manifest)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "embeddings.npy"
        self.ids_path = self.dir / "embed_ids.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        matrix, manifest = embed.build_embeddings(make_chunks(), backend="hash")
        embed.write_embeddings(matrix, manifest, self.path, self.ids_path)
        loaded, loaded_manifest = embed.read_embeddings(self.path, self.ids_path)
        np.testing.assert_array_equal(loaded, matrix)
        self.assertEqual(loaded_manifest["ids"], manifest["ids"])
        self.assertEqual(
            json.loads(self.ids_path.read_text(encoding="utf-8"))["chunk_count"], 3
        )

    def test_missing_files_raise(self) -> None:
        with self.assertRaises(FileNotFoundError):
            embed.read_embeddings(self.path, self.ids_path)

    def test_row_id_mismatch_raises(self) -> None:
        matrix, manifest = embed.build_embeddings(make_chunks(), backend="hash")
        manifest["ids"] = manifest["ids"][:2]
        embed.write_embeddings(matrix, manifest, self.path, self.ids_path)
        with self.assertRaises(ValueError):
            embed.read_embeddings(self.path, self.ids_path)

    def test_dimension_mismatch_raises(self) -> None:
        matrix, manifest = embed.build_embeddings(make_chunks(), backend="hash")
        manifest["dim"] = 384
        embed.write_embeddings(matrix, manifest, self.path, self.ids_path)
        with self.assertRaises(ValueError):
            embed.read_embeddings(self.path, self.ids_path)


class QueryTests(unittest.TestCase):
    def test_top_k_is_best_first_and_bounded(self) -> None:
        chunks = make_chunks()
        matrix, manifest = embed.build_embeddings(chunks, backend="hash")
        query = embed.hash_embed(chunks[0]["text"])
        hits = embed.top_k(query, matrix, manifest, k=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][0], chunks[0]["chunk_id"])
        self.assertGreaterEqual(hits[0][1], hits[1][1])

    def test_embed_query_uses_manifest_backend(self) -> None:
        _, manifest = embed.build_embeddings(make_chunks(), backend="hash")
        vector = embed.embed_query("exit load", manifest)
        self.assertEqual(vector.shape, (config.HASH_EMBED_DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=6)

    def test_query_finds_expected_chunk(self) -> None:
        chunks = make_chunks()
        matrix, manifest = embed.build_embeddings(chunks, backend="hash")
        vector = embed.embed_query("How do I download a capital gains statement?", manifest)
        top_id, _ = embed.top_k(vector, matrix, manifest, k=1)[0]
        self.assertEqual(top_id, "process#statement-capital-gains")


@unittest.skipUnless(config.CHUNKS.exists(), "data/chunks.jsonl not built")
class RealCorpusTests(unittest.TestCase):
    def test_real_corpus_sanity_queries(self) -> None:
        from ingest.chunk import read_chunks

        chunks = read_chunks()
        matrix, manifest = embed.build_embeddings(chunks, backend="hash")
        self.assertEqual(matrix.shape[0], len(chunks))
        self.assertTrue(embed.sanity_check(matrix, manifest))


if __name__ == "__main__":
    unittest.main()
