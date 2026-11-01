"""Retrieval logic (implementation.md Step 8).

    question -> guardrails -> scheme resolver -> intent
       |- I1-I6 (structured)  -> facts.json lookup (exact, no vector search)
       `- I7-I8 / unknown     -> embed -> vector top-k -> scheme filter -> best chunk

Structured facts never go through the vector store; the vector path serves
free-text/process questions and is the fallback when a fact is missing.
Refusals are neutral and carry the AMC page as their single source.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import config
from rag import guardrails, llm

SCHEME_ALIASES = {
    "hdfc-large-direct-growth": ("large cap", "largecap", "large-cap"),
    "hdfc-flexi-direct-growth": ("flexi cap", "flexicap", "flexi-cap", "hdfc equity"),
    "hdfc-elss-direct-growth": ("elss", "tax saver", "taxsaver"),
    "hdfc-mid-direct-growth": ("mid cap", "midcap", "mid-cap"),
}

FIELD_FOR_INTENT = {
    "I1": "expense_ratio",
    "I2": "exit_load",
    "I3": "min_sip",
    "I4": "lock_in",
    "I5": "riskometer",
    "I6": "benchmark",
}

TEMPLATES = {
    "expense_ratio": "{name} has an expense ratio of {value}.",
    "exit_load": "{name} has an exit load of {value}.",
    "min_sip": "{name} has a minimum SIP of {value}.",
    "min_lumpsum": "{name} has a minimum lumpsum investment of {value}.",
    "lock_in": "{name} has a lock-in period of {value}.",
    "riskometer": "{name} is rated {value} on the riskometer.",
    "benchmark": "{name} is benchmarked against the {value}.",
}

REFUSALS = {
    "R1": "I can only share facts, so I can't say whether to buy, sell or hold a fund.",
    "R2": "I don't compare funds or quote returns; please see the official factsheet.",
    "R3": "I can't suggest how much to invest or how to allocate; I only share fund facts.",
    "R4": "I can't give tax advice; please check the official tax guidance.",
    "R5": "Please don't share personal or account details here; I can't use or store them.",
    "R6": "I only cover HDFC Mutual Fund and four of its schemes.",
}

NEEDS_SCHEME = (
    "Which scheme do you mean: Large Cap, Flexi Cap, ELSS or Mid Cap? "
    "Please name one so I can give the right figure."
)
NOT_FOUND = "I couldn't find that in my official sources."
UNVERIFIED = (
    "I couldn't verify that figure against the official page right now, so I "
    "won't state it. Please check the source below."
)
CLARIFY = (
    "I can answer expense ratio, exit load, minimum SIP, lock-in, riskometer, "
    "benchmark or statement downloads for HDFC Large Cap, Flexi Cap, ELSS and "
    "Mid Cap. For example: “ELSS lock-in?”, “HDFC Mid Cap exit load?”, "
    "“How do I download my capital gains statement?”"
)

_URL = re.compile(r"https?://\S+")

# One official link per refusal: SEBI for advice-type asks, the factsheet page
# for performance, the AMC page for everything else.
REFUSAL_URLS = {
    "R1": config.LINK_ONLY_URLS["sebi-investor"],
    "R2": config.LINK_ONLY_URLS["amc-factsheets"],
    "R3": config.LINK_ONLY_URLS["sebi-investor"],
    "R4": config.LINK_ONLY_URLS["sebi-investor"],
}

_INTENT_RULES = (
    ("I1", re.compile(r"expense ratio|\bter\b|expense", re.I)),
    ("I2", re.compile(r"exit load", re.I)),
    ("I3", re.compile(r"\bsip\b|lump ?sum|minimum (investment|amount)", re.I)),
    ("I4", re.compile(r"lock[- ]?in", re.I)),
    ("I5", re.compile(r"riskometer|risk level|how risky|risk", re.I)),
    ("I6", re.compile(r"benchmark|\bindex\b", re.I)),
    ("I7", re.compile(r"statement|capital gains?|\bcas\b", re.I)),
    ("I8", re.compile(r"\bamc\b|fund house|who (runs|manages)|what is hdfc", re.I)),
)


@dataclass
class Answer:
    text: str
    citation_url: str | None
    last_updated: str | None
    intent: str
    scheme_id: str | None
    source: str  # "facts" | "vector" | "refusal" | "clarify" | "unverifiable" | "none"
    chunk_id: str | None = None
    validated: bool | None = None


def resolve_scheme(question: str) -> str | None:
    lowered = question.lower()
    for scheme_id, aliases in SCHEME_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return scheme_id
    return None


def guardrail(question: str) -> str | None:
    """Refusal code for PII, advice, performance, tax or out-of-corpus asks."""
    scope = guardrails.classify_scope(question)
    return None if scope == "ok" else scope


def classify_intent(question: str) -> str:
    for intent, pattern in _INTENT_RULES:
        if pattern.search(question):
            return intent
    intent, confidence = llm.classify(question)
    return intent if confidence >= 0.5 else "UNKNOWN"


def load_facts() -> dict:
    if not config.FACTS.exists():
        return {}
    return json.loads(config.FACTS.read_text(encoding="utf-8"))


def _clip(text: str, sentences: int = 2, limit: int = 320) -> str:
    text = " ".join(_URL.sub("", text).split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    clipped = " ".join(parts[:sentences])
    return clipped if len(clipped) <= limit else clipped[: limit - 1].rstrip() + "…"


def _amc_url() -> str:
    return config.PRIMARY_URLS["groww-amc"]


def _verified(found: Answer, field: str, scheme: dict, validate=None) -> Answer:
    """Live-check the figure; on any failure return unverifiable copy, no number."""
    if validate is None:
        if not config.VALIDATE_LIVE:
            return found
        from rag.citations import validate as validate  # noqa: PLW0127
    result = validate(
        scheme["source_url"], field, scheme["fields"][field]["value"],
        marker=scheme["scheme_name"],
    )
    if result.ok:
        found.validated = True
        return found
    return Answer(
        UNVERIFIED, scheme["source_url"], scheme["fetched_at"],
        found.intent, found.scheme_id, "unverifiable", validated=False,
    )


def _from_facts(
    question: str, intent: str, scheme_id: str, facts: dict, validate=None
) -> Answer | None:
    field = FIELD_FOR_INTENT[intent]
    if field == "min_sip" and re.search(r"lump ?sum", question, re.I):
        field = "min_lumpsum"
    scheme = facts.get(scheme_id)
    entry = (scheme or {}).get("fields", {}).get(field)
    if not entry or not entry.get("value"):
        return None
    date = scheme["fetched_at"]
    text = TEMPLATES[field].format(name=scheme["scheme_name"], value=entry["value"])
    text += f" Source: {scheme['source_url']} — Last updated from sources: {date}."
    found = Answer(text, scheme["source_url"], date, intent, scheme_id, "facts")
    return _verified(found, field, scheme, validate)


_store_cache: dict = {}


def _vector_search(question: str, scheme_id: str | None, store=None, manifest=None):
    from ingest.embed import embed_query, read_embeddings

    if store is None:
        if "store" not in _store_cache:
            from rag.vector_store import get_store

            _store_cache["store"] = get_store()
            _store_cache["manifest"] = read_embeddings()[1]
        store, manifest = _store_cache["store"], _store_cache["manifest"]
    vector = embed_query(question, manifest)
    return store.query(vector, k=config.TOP_K, scheme_id=scheme_id)


def _from_vector(
    question: str, intent: str, scheme_id: str | None, store=None, manifest=None
) -> Answer:
    hits = _vector_search(question, scheme_id, store, manifest)
    hits = [h for h in hits if h.score >= config.SIMILARITY_FLOOR]
    if not hits:
        text = CLARIFY if intent == "UNKNOWN" else NOT_FOUND
        kind = "clarify" if intent == "UNKNOWN" else "none"
        return Answer(text, _amc_url(), None, intent, scheme_id, kind)
    best = hits[0]
    body = best.text.split(" | ", 2)[-1]  # drop the "scheme | section |" prefix
    return Answer(
        _clip(body),
        best.metadata["source_url"],
        str(best.metadata["fetched_at"])[:10],
        intent,
        scheme_id,
        "vector",
        best.chunk_id,
    )


def answer(
    question: str, facts: dict | None = None, store=None, manifest=None, validate=None
) -> Answer:
    question = question.strip()[: config.MAX_INPUT_CHARS]
    if not question:
        return Answer(CLARIFY, _amc_url(), None, "UNKNOWN", None, "clarify")

    refusal = guardrail(question)
    if refusal:
        url = REFUSAL_URLS.get(refusal, _amc_url())
        return Answer(REFUSALS[refusal], url, None, refusal, None, "refusal")

    scheme_id = resolve_scheme(question)
    intent = classify_intent(question)
    if intent in REFUSALS:
        return Answer(REFUSALS[intent], _amc_url(), None, intent, None, "refusal")

    if intent in FIELD_FOR_INTENT:
        if scheme_id is None:
            return Answer(NEEDS_SCHEME, _amc_url(), None, intent, None, "clarify")
        facts = load_facts() if facts is None else facts
        found = _from_facts(question, intent, scheme_id, facts, validate)
        if found and guardrails.within_contract(found.text):
            return found

    return _from_vector(question, intent, scheme_id, store, manifest)
