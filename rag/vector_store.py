"""Vector store wrapper: chunk embeddings -> searchable index.

Primary backend is **Chroma persistent local mode** (`data/chroma/`). If
`chromadb` is not installed or the client cannot start, the same interface falls
back to a NumPy index over `chunks.jsonl` + `embeddings.npy`, so semantic
retrieval still works offline. The fallback preserves metadata filtering by
`scheme_id`; it just does the cosine search in memory.

Both backends implement:

    add(chunks, embeddings)                  # upsert by chunk_id
    query(vector, k=4, scheme_id=None)       # -> [VectorHit]
    count()
    reset()

`build_index()` validates that chunks and embeddings still agree before writing.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import config
from ingest.chunk import read_chunks
from ingest.embed import read_embeddings

REQUIRED_CHUNK_KEYS = (
    "chunk_id",
    "doc_id",
    "scheme_id",
    "section",
    "source_url",
    "fetched_at",
    "text",
)


@dataclass
class VectorHit:
    chunk_id: str
    text: str
    score: float
    metadata: dict = field(default_factory=dict)


def chroma_available() -> bool:
    try:
        return importlib.util.find_spec("chromadb") is not None
    except (ImportError, ValueError):
        return False


def _validate(
    chunks: list[dict], matrix: np.ndarray, manifest: dict | None
) -> tuple[list[str], np.ndarray]:
    if not chunks:
        raise ValueError("no chunks to index")
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"embeddings must be 2-D, got shape {matrix.shape}")
    if matrix.shape[0] != len(chunks):
        raise ValueError(
            f"chunk/embedding count mismatch: {len(chunks)} chunks, "
            f"{matrix.shape[0]} rows"
        )

    ids = [chunk["chunk_id"] for chunk in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk_id in chunks.jsonl")

    for chunk in chunks:
        missing = [key for key in REQUIRED_CHUNK_KEYS if not chunk.get(key)]
        if missing:
            raise ValueError(f"{chunk.get('chunk_id', '?')} missing {missing}")

    if manifest:
        manifest_ids = manifest.get("ids")
        if manifest_ids and list(manifest_ids) != ids:
            raise ValueError(
                "chunks and embeddings are out of sync; rerun `--embed` "
                "before `--index`"
            )
        dim = manifest.get("dim")
        if dim is not None and int(dim) != matrix.shape[1]:
            raise ValueError(
                f"embedding dimension mismatch: matrix {matrix.shape[1]}, "
                f"manifest {dim}"
            )
    return ids, matrix


def _metadata(chunk: dict) -> dict:
    return {
        "doc_id": chunk["doc_id"],
        "scheme_id": chunk["scheme_id"],
        "section": chunk["section"],
        "source_url": chunk["source_url"],
        "fetched_at": chunk["fetched_at"],
        "words": int(chunk.get("words") or len(chunk["text"].split())),
    }


class NumpyStore:
    """In-memory cosine index over the committed artifacts."""

    backend = "numpy"

    def __init__(
        self,
        chunks: list[dict] | None = None,
        embeddings: np.ndarray | None = None,
        manifest: dict | None = None,
    ) -> None:
        self.fallback_reason: str | None = None
        self.chunks: list[dict] = []
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.manifest: dict = manifest or {}
        if chunks is None:
            chunks = read_chunks()
        if embeddings is None:
            embeddings, loaded_manifest = read_embeddings()
            self.manifest = manifest or loaded_manifest
        if chunks:
            self.add(chunks, embeddings)

    def add(self, chunks: list[dict], embeddings: np.ndarray) -> None:
        ids, matrix = _validate(chunks, embeddings, self.manifest)
        self.chunks = list(chunks)
        self.matrix = matrix
        self.id_to_index = {chunk_id: i for i, chunk_id in enumerate(ids)}
        self.manifest = {
            **self.manifest,
            "ids": ids,
            "dim": int(matrix.shape[1]),
            "count": len(ids),
            "backend": self.backend,
        }

    def reset(self) -> None:
        self.chunks = []
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.id_to_index = {}

    def count(self) -> int:
        return len(self.chunks)

    def query(
        self, vector: np.ndarray, k: int = config.TOP_K, scheme_id: str | None = None
    ) -> list[VectorHit]:
        if self.count() == 0:
            return []
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        if vector.shape[0] != self.matrix.shape[1]:
            raise ValueError(
                f"query dimension {vector.shape[0]} != index dimension "
                f"{self.matrix.shape[1]}"
            )

        indices = np.arange(len(self.chunks))
        if scheme_id:
            indices = np.array(
                [
                    i
                    for i in indices
                    if self.chunks[i]["scheme_id"] == scheme_id
                ],
                dtype=np.int64,
            )
        if indices.size == 0:
            return []

        scores = self.matrix[indices] @ vector
        order = np.argsort(-scores)[: max(0, k)]
        return [
            VectorHit(
                chunk_id=self.chunks[indices[i]]["chunk_id"],
                text=self.chunks[indices[i]]["text"],
                score=float(scores[i]),
                metadata=_metadata(self.chunks[indices[i]]),
            )
            for i in order
        ]


class ChromaStore:
    """Chroma persistent client with a cosine collection."""

    backend = "chroma"

    def __init__(
        self,
        path: Path | None = None,
        collection: str | None = None,
    ) -> None:
        import chromadb
        from chromadb.config import Settings

        self.fallback_reason: str | None = None
        self.path = Path(path or config.CHROMA)
        self.path.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection or config.CHROMA_COLLECTION
        self._client = chromadb.PersistentClient(
            path=str(self.path),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._open_collection()

    def _open_collection(self):
        return self._client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, chunks: list[dict], embeddings: np.ndarray) -> None:
        ids, matrix = _validate(chunks, embeddings, None)
        self._collection.upsert(
            ids=ids,
            documents=[chunk["text"] for chunk in chunks],
            embeddings=matrix.tolist(),
            metadatas=[_metadata(chunk) for chunk in chunks],
        )

    def reset(self) -> None:
        try:
            self._client.delete_collection(self.collection_name)
        except Exception:
            pass
        self._collection = self._open_collection()

    def count(self) -> int:
        return int(self._collection.count())

    def query(
        self, vector: np.ndarray, k: int = config.TOP_K, scheme_id: str | None = None
    ) -> list[VectorHit]:
        if self.count() == 0:
            return []
        vector = np.asarray(vector, dtype=np.float32).reshape(-1)
        result = self._collection.query(
            query_embeddings=[vector.tolist()],
            n_results=min(max(1, k), self.count()),
            where={"scheme_id": scheme_id} if scheme_id else None,
            include=["documents", "metadatas", "distances"],
        )
        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        hits: list[VectorHit] = []
        for index, chunk_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else 0.0
            hits.append(
                VectorHit(
                    chunk_id=chunk_id,
                    text=documents[index] if index < len(documents) else "",
                    score=1.0 - float(distance),
                    metadata=metadatas[index] if index < len(metadatas) else {},
                )
            )
        return hits


def get_store(
    backend: str | None = None,
    chunks: list[dict] | None = None,
    embeddings: np.ndarray | None = None,
    manifest: dict | None = None,
    strict: bool = False,
):
    """Return a Chroma store, or the NumPy fallback when Chroma is unavailable."""
    requested = (backend or config.VECTOR_BACKEND or "chroma").lower()
    if requested == "numpy":
        return NumpyStore(chunks=chunks, embeddings=embeddings, manifest=manifest)
    if requested != "chroma":
        raise ValueError(f"unknown VECTOR_BACKEND: {requested}")

    try:
        return ChromaStore()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if strict:
            raise RuntimeError(f"Chroma unavailable: {reason}") from exc
        print(
            f"vector_store: Chroma unavailable ({reason}); using NumPy fallback",
            file=sys.stderr,
        )
        store = NumpyStore(chunks=chunks, embeddings=embeddings, manifest=manifest)
        store.fallback_reason = reason
        return store


def build_index(
    chunks: list[dict] | None = None,
    embeddings: np.ndarray | None = None,
    manifest: dict | None = None,
    backend: str | None = None,
    reset: bool = True,
    strict: bool = False,
):
    """Validate artifacts, then upsert every chunk into the vector store."""
    if chunks is None:
        chunks = read_chunks()
    if embeddings is None:
        embeddings, loaded_manifest = read_embeddings()
        manifest = manifest or loaded_manifest
    if manifest is None:
        manifest = {
            "ids": [chunk["chunk_id"] for chunk in chunks],
            "dim": int(np.asarray(embeddings).shape[1]),
        }

    _validate(chunks, embeddings, manifest)
    store = get_store(
        backend=backend,
        chunks=chunks,
        embeddings=embeddings,
        manifest=manifest,
        strict=strict,
    )
    if reset:
        store.reset()
    store.add(chunks, embeddings)

    if store.count() < len(chunks):
        raise RuntimeError(
            f"index count too low after build: {store.count()} < {len(chunks)}"
        )
    if reset and store.count() != len(chunks):
        raise RuntimeError(
            f"index count mismatch after rebuild: {store.count()} != {len(chunks)}"
        )
    return store


def print_report(store, chunks: list[dict], dim: int | None = None) -> None:
    per_scheme: dict[str, int] = {}
    for chunk in chunks:
        per_scheme[chunk["scheme_id"]] = per_scheme.get(chunk["scheme_id"], 0) + 1

    print(f"{'scheme_id':<30} {'chunks':>6}")
    for scheme_id, count in sorted(per_scheme.items()):
        print(f"{scheme_id:<30} {count:>6}")
    print(
        f"\nindex: {store.count()} chunks | backend: {store.backend}"
        + (f" | dim: {dim}" if dim else "")
    )
    print(f"fallback: {getattr(store, 'fallback_reason', None) or 'none'}")
