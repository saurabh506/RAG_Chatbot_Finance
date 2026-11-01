"""Ingestion package: fetch -> load -> parse -> chunk -> embed -> index."""

from ingest.manifest import SPECS, UrlSpec, by_id, write_sources_csv

__all__ = ["SPECS", "UrlSpec", "by_id", "write_sources_csv"]
