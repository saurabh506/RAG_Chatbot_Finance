"""Metadata-only request log (architecture.md §6.10).

One JSON line per request. No question text, no answer text, no IP, no session
id: only derived fields, so a PII-bearing question leaves no trace here.
Logging must never break an answer, so every failure is swallowed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import config

LOG_PATH = config.DATA / "requests.log"

_ANSWER_TYPES = {
    "facts": "answer",
    "vector": "answer",
    "refusal": "refusal",
    "clarify": "clarify",
    "none": "clarify",
    "unverifiable": "unverifiable",
}


def corpus_version() -> str:
    """Latest document date, e.g. '2026-11-01.1'; 'unknown' if no corpus."""
    try:
        dates = [
            json.loads(line).get("fetched_at", "")
            for line in config.DOCS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return f"{max(dates)[:10]}.1" if any(dates) else "unknown"
    except (OSError, ValueError):
        return "unknown"


def record(answer, latency_ms: int, validated: bool | None = None) -> dict:
    if validated is None:
        validated = getattr(answer, "validated", None)
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intent": answer.intent,
        "scheme_id": answer.scheme_id,
        "answer_type": _ANSWER_TYPES.get(answer.source, "clarify"),
        "citation_url": answer.citation_url,
        "validated": validated,
        "latency_ms": int(latency_ms),
        "corpus_version": corpus_version(),
    }


def log_request(answer, latency_ms: int, validated: bool | None = None,
                path: Path | None = None) -> None:
    try:
        target = Path(path or LOG_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record(answer, latency_ms, validated)) + "\n")
    except Exception:
        pass
