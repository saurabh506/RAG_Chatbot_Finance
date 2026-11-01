"""Embedding stage: `chunks.jsonl` -> `embeddings.npy` + `embed_ids.json`.

Two interchangeable backends behind one interface:

- `minilm` (default): `all-MiniLM-L6-v2`, 384-dim, lazy-loaded so importing this
  module never pulls in torch. If the package or model is unavailable the stage
  falls back to `hash` instead of failing the demo.
- `hash`: deterministic 256-dim feature hashing (MD5 token buckets, signed).
  Weaker on paraphrases, but no download and no dependencies beyond NumPy.

Both return L2-normalised rows, so cosine similarity is a dot product. The
query side must read `embed_ids.json` and reuse the recorded backend/model —
mixing backends makes similarity meaningless.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import config
from ingest.chunk import read_chunks

BATCH_SIZE = 32
TOKEN_RE = re.compile(r"[a-z0-9\u20b9%.]+")
# Function words carry no retrieval signal and, with only 256 buckets, add
# collisions. Punctuation is trimmed so "Min." and "min" hash alike.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
        "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or",
        "that", "the", "this", "to", "was", "what", "when", "where", "which",
        "who", "why", "with",
    }
)

SANITY_QUERIES: tuple[tuple[str, str], ...] = (
    ("What is the exit load?", "exit load"),
    ("What is the minimum SIP amount?", "min. for sip"),
    ("How do I download a capital gains statement?", "capital gains"),
)


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


def l2_normalise(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation; zero rows stay zero instead of NaN."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def hash_embed(text: str, dim: int = config.HASH_EMBED_DIM) -> np.ndarray:
    """Signed feature hashing over content tokens (see implementation.md).

    Stopwords are dropped and trailing punctuation is trimmed first; both
    changes measurably improve short-query ranking with only 256 buckets.
    """
    vector = np.zeros(dim, dtype=np.float32)
    for raw in TOKEN_RE.findall(text.lower()):
        token = raw.strip(".")
        if not token or token in STOPWORDS:
            continue
        digest = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        vector[digest % dim] += 1.0 if (digest >> 8) & 1 else -1.0
    return l2_normalise(vector)[0]


class HashEmbedder:
    backend = "hash"

    def __init__(self, dim: int = config.HASH_EMBED_DIM) -> None:
        self.dim = dim
        self.model_name = f"hash-md5-{dim}"
        self.fallback_reason: str | None = None

    def encode(self, texts: list[str], batch_size: int = BATCH_SIZE) -> np.ndarray:
        return np.stack([hash_embed(text, self.dim) for text in texts])


class MiniLMEmbedder:
    backend = "minilm"

    def __init__(self, model_name: str = config.EMBED_MODEL) -> None:
        self.model_name = model_name
        self.dim = config.EMBED_DIM
        self.fallback_reason: str | None = None
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            self.dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def encode(self, texts: list[str], batch_size: int = BATCH_SIZE) -> np.ndarray:
        model = self._load()
        vectors = model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return l2_normalise(vectors)


def get_embedder(
    backend: str | None = None,
    model: str | None = None,
    strict: bool = False,
):
    """Return an embedder, falling back to `hash` when MiniLM is unavailable."""
    requested = (backend or config.EMBED_BACKEND or "minilm").lower()
    if requested == "hash":
        return HashEmbedder()
    if requested != "minilm":
        raise ValueError(f"unknown EMBED_BACKEND: {requested}")

    embedder = MiniLMEmbedder(model or config.EMBED_MODEL)
    try:
        embedder._load()
    except Exception as exc:  # ImportError, OSError, network failure, ...
        reason = f"{type(exc).__name__}: {exc}"
        if strict:
            raise RuntimeError(f"MiniLM unavailable: {reason}") from exc
        print(
            f"embed: MiniLM unavailable ({reason}); using hash backend",
            file=sys.stderr,
        )
        fallback = HashEmbedder()
        fallback.fallback_reason = reason
        return fallback
    return embedder


# --------------------------------------------------------------------------
# Build / persist
# --------------------------------------------------------------------------


def build_embeddings(
    chunks: list[dict],
    backend: str | None = None,
    model: str | None = None,
    strict: bool = False,
) -> tuple[np.ndarray, dict]:
    """Embed chunk texts and return (matrix, manifest)."""
    if not chunks:
        raise ValueError("no chunks to embed")

    embedder = get_embedder(backend=backend, model=model, strict=strict)
    texts = [chunk["text"] for chunk in chunks]
    vectors = l2_normalise(embedder.encode(texts))

    if vectors.shape[0] != len(chunks):
        raise RuntimeError(
            f"embedding count mismatch: {vectors.shape[0]} vectors, {len(chunks)} chunks"
        )

    manifest = {
        "backend": embedder.backend,
        "requested_backend": (backend or config.EMBED_BACKEND or "minilm").lower(),
        "model": embedder.model_name,
        "dim": int(vectors.shape[1]),
        "normalised": True,
        "chunk_count": len(chunks),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ids": [chunk["chunk_id"] for chunk in chunks],
    }
    if embedder.fallback_reason:
        manifest["fallback_reason"] = embedder.fallback_reason
    return vectors, manifest


def write_embeddings(
    matrix: np.ndarray,
    manifest: dict,
    path: Path | None = None,
    ids_path: Path | None = None,
) -> tuple[Path, Path]:
    path = path or config.EMBEDDINGS
    ids_path = ids_path or config.EMBED_IDS
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, np.asarray(matrix, dtype=np.float32))
    ids_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path, ids_path


def read_embeddings(
    path: Path | None = None,
    ids_path: Path | None = None,
) -> tuple[np.ndarray, dict]:
    """Load the matrix + manifest, checking they still agree."""
    path = path or config.EMBEDDINGS
    ids_path = ids_path or config.EMBED_IDS
    if not path.exists() or not ids_path.exists():
        raise FileNotFoundError("embeddings.npy or embed_ids.json missing; run --embed")
    matrix = np.load(path)
    manifest = json.loads(ids_path.read_text(encoding="utf-8"))
    if matrix.ndim != 2:
        raise ValueError(f"embeddings.npy must be 2-D, got shape {matrix.shape}")
    if matrix.shape[0] != len(manifest.get("ids", [])):
        raise ValueError(
            f"embeddings/ids mismatch: {matrix.shape[0]} rows, "
            f"{len(manifest.get('ids', []))} ids"
        )
    if matrix.shape[1] != manifest.get("dim"):
        raise ValueError(
            f"dimension mismatch: matrix has {matrix.shape[1]}, "
            f"manifest says {manifest.get('dim')}"
        )
    return matrix, manifest


# --------------------------------------------------------------------------
# Query helpers (shared with the retrieval step)
# --------------------------------------------------------------------------


def embed_query(text: str, manifest: dict) -> np.ndarray:
    """Embed one query with the backend recorded in the index manifest.

    A MiniLM index must never be queried with the hash fallback: the dimensions
    differ (384 vs 256) and similarity would be meaningless. If the recorded
    model is unavailable, fail closed instead of silently switching backends.
    """
    backend = manifest.get("backend")
    embedder = get_embedder(
        backend=backend,
        model=manifest.get("model") if backend == "minilm" else None,
        strict=backend == "minilm",
    )
    return l2_normalise(embedder.encode([text]))[0]


def top_k(
    query_vector: np.ndarray,
    matrix: np.ndarray,
    manifest: dict,
    k: int = config.TOP_K,
) -> list[tuple[str, float]]:
    """Return [(chunk_id, cosine score)] best-first."""
    scores = matrix @ np.asarray(query_vector, dtype=np.float32)
    order = np.argsort(-scores)[: max(0, k)]
    ids = manifest["ids"]
    return [(ids[i], float(scores[i])) for i in order]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def sanity_check(matrix: np.ndarray, manifest: dict) -> bool:
    """3 fixed queries must surface a chunk containing the expected phrase."""
    texts = {chunk["chunk_id"]: chunk["text"] for chunk in read_chunks()}
    passed = True
    print(f"\n{'sanity query':<44} {'top chunk':<46} score")
    for question, expected in SANITY_QUERIES:
        vector = embed_query(question, manifest)
        chunk_id, score = top_k(vector, matrix, manifest, k=1)[0]
        hit = expected in texts.get(chunk_id, "").lower()
        passed = passed and hit
        print(
            f"{question[:43]:<44} {chunk_id[:45]:<46} {score:.3f} "
            f"{'ok' if hit else 'MISS'}"
        )
    return passed


def print_report(
    chunks: list[dict],
    matrix: np.ndarray,
    manifest: dict,
    sanity: bool | None = None,
) -> None:
    norms = np.linalg.norm(matrix, axis=1)
    print(f"{'doc_id':<30} {'chunks':>6}")
    per_doc: dict[str, int] = {}
    for chunk in chunks:
        per_doc[chunk["doc_id"]] = per_doc.get(chunk["doc_id"], 0) + 1
    for doc_id, count in sorted(per_doc.items()):
        print(f"{doc_id:<30} {count:>6}")
    print(
        f"\nembeddings: {matrix.shape[0]} x {matrix.shape[1]} | "
        f"backend: {manifest['backend']} | model: {manifest['model']}"
    )
    print(
        f"unit norm: min {norms.min():.6f} / max {norms.max():.6f} | "
        f"fallback: {manifest.get('fallback_reason', 'none')}"
    )
    if sanity is not None:
        print(f"sanity: {'pass' if sanity else 'FAIL'}")
