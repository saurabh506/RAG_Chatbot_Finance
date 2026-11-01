"""Deterministic guardrails (implementation.md Step 9).

Order is fixed: PII -> advice -> performance -> tax -> allocation -> out-of-corpus.
PII is checked first so a PAN never reaches the retriever, the model or a log.
Nothing here calls the LLM or the network.
"""

from __future__ import annotations

import re

PII_PATTERNS = {
    "PAN": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I),
    "AADHAAR": re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"),
    "PHONE": re.compile(r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b"),
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    "OTP": re.compile(r"\botp\b[^\d]{0,12}\d{4,8}\b", re.I),
    "ACCOUNT": re.compile(
        r"\b(?:a/?c|account|folio)\s*(?:no\.?|number)?\s*[:\-]?\s*\d{6,}\b", re.I
    ),
}

_ADVICE = re.compile(
    r"\b(should i|shall i|should we|recommend\w*|suggest\w*|advice|advise|"
    r"buy|sell|hold|worth it|switch|invest in|portfolio|"
    r"(best|good|safe|top) (fund|scheme|investment|option))\b"
    r"|ignore (all |any |previous |the )?(instructions|rules)",
    re.I,
)
_PERFORMANCE = re.compile(
    r"\b(returns?|cagr|xirr|alpha|beta|performance|performed|compare|comparison|"
    r"outperform\w*|beat|which fund|better than|vs|versus)\b",
    re.I,
)
_TAX = re.compile(r"\b(how much tax|tax saving amount|save tax|tax benefit)\b", re.I)
_ALLOCATION = re.compile(
    r"\b(how much (should|to|do i)|allocat\w+|my goal|for my goal)\b", re.I
)
_OTHER_AMC = re.compile(
    r"\b(sbi|icici|axis|kotak|nippon|uti|aditya|birla|tata|dsp|mirae|parag|quant|"
    r"franklin|invesco|bandhan)\b",
    re.I,
)

# Words a rendered fact must never contain (post-render check).
ADVICE_VERBS = re.compile(
    r"\b(should|recommend\w*|best|better|consider|good)\b", re.I
)
_URL = re.compile(r"https?://\S+")


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, type labels). The matched values are never returned."""
    hits: list[str] = []
    clean = text
    for label, pattern in PII_PATTERNS.items():
        if pattern.search(clean):
            hits.append(label)
            clean = pattern.sub(f"[{label}]", clean)
    return clean, hits


def classify_scope(text: str, hdfc_mentioned: bool | None = None) -> str:
    """'ok' or the refusal code R1-R6, checked in the fixed order above."""
    if scrub_pii(text)[1]:
        return "R5"
    if _ALLOCATION.search(text):  # a sharper form of advice: R3, not R1
        return "R3"
    if _ADVICE.search(text):
        return "R1"
    if _PERFORMANCE.search(text):
        return "R2"
    if _TAX.search(text):
        return "R4"
    if hdfc_mentioned is None:
        hdfc_mentioned = "hdfc" in text.lower()
    if _OTHER_AMC.search(text) and not hdfc_mentioned:
        return "R6"
    return "ok"


def count_sentences(text: str) -> int:
    body = _URL.sub("", text)
    return len([p for p in re.split(r"(?<=[.!?])\s+", body.strip()) if p.strip(". ")])


def within_contract(text: str) -> bool:
    """<=3 sentences, no advice verbs, and at most one URL."""
    return (
        count_sentences(text) <= 3
        and not ADVICE_VERBS.search(text)
        and len(_URL.findall(text)) <= 1
    )
