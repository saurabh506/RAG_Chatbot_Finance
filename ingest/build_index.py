"""Build the vector index from `chunks.jsonl` + `embeddings.npy`.

    python -m ingest.build_index
    python -m ingest.build_index --backend numpy
    python -m ingest.build_index --keep          # upsert without clearing

The default backend is Chroma (`data/chroma/`). When `chromadb` is unavailable,
the same command falls back to the NumPy index and says so. Either way the
command verifies count, a scheme-filtered query, and persistence after reopening
the store.
"""

from __future__ import annotations

import argparse
import sys

import config
from ingest.chunk import read_chunks
from ingest.embed import embed_query, read_embeddings
from rag.vector_store import build_index, get_store, print_report

CHECK_SCHEME = "hdfc-large-direct-growth"
CHECK_QUERY = "exit load"


def verify(store, chunks: list[dict], manifest: dict) -> bool:
    """Count, filtered query, and reopen checks. Returns True when all pass."""
    passed = True

    if store.count() != len(chunks):
        print(f"check count: FAIL ({store.count()} != {len(chunks)})")
        passed = False
    else:
        print(f"check count: ok ({store.count()})")

    vector = embed_query(CHECK_QUERY, manifest)
    hits = store.query(vector, k=config.TOP_K, scheme_id=CHECK_SCHEME)
    wrong = [hit for hit in hits if hit.metadata.get("scheme_id") != CHECK_SCHEME]
    if not hits:
        print(f"check filter: FAIL (no hits for {CHECK_SCHEME})")
        passed = False
    elif wrong:
        print(f"check filter: FAIL ({len(wrong)} hits from another scheme)")
        passed = False
    else:
        top = hits[0]
        print(
            f"check filter: ok ({len(hits)} hits, all {CHECK_SCHEME}) | "
            f"top: {top.chunk_id} {top.score:.3f}"
        )

    reopened = get_store(backend=store.backend)
    if reopened.count() != store.count():
        print(f"check reopen: FAIL ({reopened.count()} != {store.count()})")
        passed = False
    else:
        print(f"check reopen: ok ({reopened.count()} chunks)")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest.build_index", description=__doc__
    )
    parser.add_argument(
        "--backend",
        choices=("chroma", "numpy"),
        default=None,
        help="override VECTOR_BACKEND",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="upsert without clearing the collection first",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail instead of falling back when Chroma is unavailable",
    )
    args = parser.parse_args(argv)

    chunks = read_chunks()
    if not chunks:
        print("index: no chunks.jsonl; run `--chunk` first.")
        return 1
    try:
        embeddings, manifest = read_embeddings()
    except (FileNotFoundError, ValueError) as exc:
        print(f"index: {exc}; run `--embed` first.")
        return 1

    try:
        store = build_index(
            chunks,
            embeddings,
            manifest,
            backend=args.backend,
            reset=not args.keep,
            strict=args.strict,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"index: FAIL ({exc})")
        return 1

    print()
    print_report(store, chunks, dim=int(embeddings.shape[1]))
    print()
    passed = verify(store, chunks, manifest)
    print(f"\nindex: {'ok' if passed else 'FAIL'} | backend: {store.backend}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
