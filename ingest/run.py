"""Ingestion CLI.

    python -m ingest.run --list
    python -m ingest.run --fetch [--force] [--only groww|elss-id]
    python -m ingest.run --load
    python -m ingest.run --chunk
    python -m ingest.run --embed [--backend hash|minilm]
    python -m ingest.run --all

Later steps add: --parse
"""

from __future__ import annotations

import argparse
import sys

import config
from ingest.chunk import build_chunks, print_report as print_chunk_report, read_chunks, write_chunks
from ingest.embed import (
    build_embeddings,
    print_report as print_embed_report,
    sanity_check,
    write_embeddings,
)
from ingest.fetch import fetch_all
from ingest.load import load_all, print_report as print_load_report, read_docs, write_docs
from ingest.manifest import SPECS, by_id, by_tier, write_sources_csv


def _select(only: list[str] | None):
    if not only:
        return list(SPECS)
    selected = []
    for value in only:
        try:
            selected.append(by_id(value))
            continue
        except KeyError:
            pass
        tier = by_tier(value)
        if not tier:
            raise SystemExit(f"unknown spec id or tier: {value}")
        selected.extend(tier)
    return selected


def _print_manifest() -> None:
    print(f"{'id':<30} {'tier':<7} {'mode':<10} url")
    for spec in SPECS:
        print(f"{spec.id:<30} {spec.tier:<7} {spec.mode:<10} {spec.url}")
    print(f"\n{len(SPECS)} URLs ({sum(s.mode == 'extract' for s in SPECS)} extract, "
          f"{sum(s.mode == 'link-only' for s in SPECS)} link-only)")


def _run_load(specs) -> int:
    docs = load_all(specs)
    if docs and all(doc.status == "missing" for doc in docs) and config.DOCS.exists():
        # Never clobber a good artifact with "missing" records.
        print_load_report(docs)
        print("\nload: no cached HTML; run `--fetch` first. docs.jsonl left unchanged.")
        return 1
    path = write_docs(docs)
    print()
    print_load_report(docs)
    ok = [doc for doc in docs if doc.status == "ok"]
    print(f"\nload: {len(ok)}/{len(docs)} ok | docs.jsonl: {path}")
    return 0 if len(ok) == len(docs) else 1


def _run_chunk() -> int:
    docs = read_docs()
    if not docs:
        print("chunk: no docs.jsonl; run --load first.")
        return 1
    chunks = build_chunks(docs)
    if not chunks:
        print("chunk: no usable documents (status == 'ok'); run --load first.")
        return 1
    path = write_chunks(chunks)
    print()
    print_chunk_report(chunks)
    print(f"chunks.jsonl: {path}")
    return 0


def _run_embed(backend: str | None) -> int:
    chunks = read_chunks()
    if not chunks:
        print("embed: no chunks.jsonl; run --chunk first.")
        return 1
    matrix, manifest = build_embeddings(chunks, backend=backend)
    path, ids_path = write_embeddings(matrix, manifest)
    print()
    sanity = sanity_check(matrix, manifest)
    print_embed_report(chunks, matrix, manifest, sanity=sanity)
    print(f"embeddings.npy: {path}\nembed_ids.json: {ids_path}")
    return 0 if sanity else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest.run", description=__doc__)
    parser.add_argument("--list", action="store_true", help="show the URL allowlist")
    parser.add_argument("--fetch", action="store_true", help="fetch extract-mode pages")
    parser.add_argument("--load", action="store_true", help="HTML -> data/docs.jsonl")
    parser.add_argument("--chunk", action="store_true", help="docs.jsonl -> data/chunks.jsonl")
    parser.add_argument("--embed", action="store_true", help="chunks.jsonl -> data/embeddings.npy")
    parser.add_argument(
        "--backend",
        choices=("minilm", "hash"),
        default=None,
        help="override EMBED_BACKEND for --embed",
    )
    parser.add_argument("--all", action="store_true", help="run every implemented stage")
    parser.add_argument("--force", action="store_true", help="ignore cache, revalidate")
    parser.add_argument("--only", action="append", metavar="ID|TIER", help="subset")
    args = parser.parse_args(argv)

    config.ensure_dirs()

    if args.list:
        _print_manifest()
        return 0

    if not (args.fetch or args.load or args.chunk or args.embed or args.all):
        parser.print_help()
        return 0

    specs = _select(args.only)
    status = 0

    if args.fetch or args.all:
        results = fetch_all(specs, force=args.force)
        write_sources_csv(results, config.SOURCES_CSV)
        extract = [r for r in results if r.spec.mode == "extract"]
        ok = [r for r in extract if r.ok]
        print(f"\nfetch: {len(ok)}/{len(extract)} ok | sources.csv: {len(results)} rows")
        if len(ok) != len(extract):
            status = 1

    if args.load or args.all:
        status = max(status, _run_load(specs))

    if args.chunk or args.all:
        status = max(status, _run_chunk())

    if args.embed or args.all:
        status = max(status, _run_embed(args.backend))

    return status


if __name__ == "__main__":
    sys.exit(main())
