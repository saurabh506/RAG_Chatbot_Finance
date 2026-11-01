"""Optional LLM client — intent classification only.

Provider: Groq (OpenAI-compatible API). Never a source of facts.
The LLM may only choose an intent code; answers are rendered from
`facts.json` by templates. Disabled unless `LLM_ENABLED=true`.
"""

from __future__ import annotations

import json

import requests

import config

ALLOWED_INTENTS = frozenset(
    {
        "I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8",
        "R1", "R2", "R3", "R4", "R5", "R6",
        "UNKNOWN",
    }
)

SYSTEM_PROMPT = """You classify questions for a facts-only mutual fund FAQ assistant.
Return JSON only, no prose.

Schema:
{"intent": "<code>", "scheme_hint": "<string or null>", "confidence": <0.0-1.0>}

Intent codes:
I1 expense ratio
I2 exit load
I3 minimum SIP / minimum lumpsum
I4 ELSS lock-in
I5 riskometer / risk level
I6 benchmark / index
I7 how to download statement (capital gains, consolidated)
I8 fund or AMC identity
R1 buy/sell/hold or portfolio advice
R2 returns, performance, comparison, "which fund"
R3 allocation, goal or "how much should I invest"
R4 tax advice ("how much tax will I save")
R5 PII or account-specific request (PAN, Aadhaar, folio, OTP, email, phone)
R6 fund or AMC outside the covered corpus
UNKNOWN anything else

Rules:
- Any opinion, recommendation or advice request => R1/R3/R4, never I*.
- Any request to compute, rank or compare returns => R2.
- Never answer the question. Classify only.
"""


def available() -> bool:
    return bool(config.GROQ_API_KEY)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def ping(timeout: float = 10.0) -> dict:
    """Cheap credential check. Raises on HTTP error."""
    response = requests.get(
        f"{config.LLM_BASE_URL}/models", headers=_headers(), timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def _post(payload: dict) -> requests.Response:
    return requests.post(
        f"{config.LLM_BASE_URL}/chat/completions",
        headers=_headers(),
        json=payload,
        timeout=config.LLM_TIMEOUT_S,
    )


def classify(question: str) -> tuple[str, float]:
    """Return (intent, confidence). Fails closed to ("UNKNOWN", 0.0)."""
    if not (config.LLM_ENABLED and available()):
        return "UNKNOWN", 0.0

    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question[: config.MAX_INPUT_CHARS]},
        ],
        "temperature": 0,
        # Reasoning models (gpt-oss) spend part of this budget on hidden
        # reasoning, so keep headroom above the ~20-token JSON answer.
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    if config.LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = config.LLM_REASONING_EFFORT

    try:
        response = _post(payload)
        if response.status_code == 400 and "reasoning_effort" in payload:
            # Older/non-reasoning models reject the parameter; retry without it.
            payload.pop("reasoning_effort")
            response = _post(payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content") or ""
        data = json.loads(content)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return "UNKNOWN", 0.0

    intent = str(data.get("intent", "UNKNOWN")).upper()
    if intent not in ALLOWED_INTENTS:
        intent = "UNKNOWN"
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return intent, max(0.0, min(1.0, confidence))
