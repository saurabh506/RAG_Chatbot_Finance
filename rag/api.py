"""Transport-free request handler for the HTTP API (architecture.md §8.1).

`handle(body)` takes the raw request body and returns `(status, payload)`, so the
Vercel function stays a thin adapter and this logic is testable without a server.
The payload never echoes the question.
"""

from __future__ import annotations

import json
import time

import config
from obs.log import corpus_version, log_request
from rag.retrieve import Answer, answer

_TYPES = {
    "facts": "answer",
    "vector": "answer",
    "refusal": "refusal",
    "clarify": "clarify",
    "none": "clarify",
    "unverifiable": "unverifiable",
}


def payload_for(result: Answer) -> dict:
    text = result.text
    if result.source == "facts":  # the link is returned separately, once
        text = text.split(" Source:")[0].strip()
    return {
        "answer_type": _TYPES.get(result.source, "clarify"),
        "text": text,
        "citation_url": result.citation_url,
        "last_updated": result.last_updated,
        "intent": result.intent,
        "scheme_id": result.scheme_id,
        "validated": result.validated,
        "corpus_version": corpus_version(),
    }


def handle(body: bytes | str) -> tuple[int, dict]:
    try:
        question = json.loads(body or b"{}").get("question", "")
    except (ValueError, AttributeError):
        return 400, {"error": "invalid_input"}
    if not isinstance(question, str):
        return 400, {"error": "invalid_input"}

    question = question.strip()
    if not question or len(question) > config.MAX_INPUT_CHARS:
        return 400, {"error": "invalid_input"}

    try:
        started = time.perf_counter()
        result = answer(question)
        log_request(result, (time.perf_counter() - started) * 1000)
    except (FileNotFoundError, ValueError):
        return 503, {"error": "corpus_unavailable"}

    status = 422 if result.intent == "R5" else 200
    return status, payload_for(result)
