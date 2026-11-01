"""MF Facts - Streamlit UI (implementation.md Step 8).

Renders `rag.retrieve.answer()`; contains no fact strings of its own.
"""

from __future__ import annotations

import re
import time
from datetime import date

import streamlit as st

import config
from obs.log import log_request
from rag.retrieve import Answer, answer

DISCLAIMER = "Facts-only. No investment advice."
EXAMPLES = (
    "What is the expense ratio of HDFC ELSS Tax Saver?",
    "What is the exit load of HDFC Mid Cap Fund?",
    "How do I download my capital gains statement?",
)
LABELS = {
    "answer": "Answer",
    "refusal": "I can't help with that",
    "clarify": "Need a bit more detail",
    "unverifiable": "Couldn't verify this figure",
}
STALE_AFTER_DAYS = 30

_SOURCE_SUFFIX = re.compile(r"\s*Source:\s*https?://\S+.*$", re.S)


def answer_kind(a: Answer) -> str:
    if a.source in ("facts", "vector"):
        return "answer"
    if a.source in ("refusal", "unverifiable"):
        return a.source
    return "clarify"


def display_text(a: Answer) -> str:
    """Body text without the inline source line; the UI shows the link once."""
    return _SOURCE_SUFFIX.sub("", a.text).strip()


def is_stale(last_updated: str | None, today: date | None = None) -> bool:
    if not last_updated:
        return False
    try:
        age = (today or date.today()) - date.fromisoformat(last_updated[:10])
    except ValueError:
        return False
    return age.days > STALE_AFTER_DAYS


def rate_limited() -> bool:
    now = time.time()
    recent = [t for t in st.session_state.get("asked", []) if now - t < 60]
    limited = len(recent) >= config.RATE_LIMIT_PER_MIN
    if not limited:
        recent.append(now)
    st.session_state["asked"] = recent
    return limited


def render(a: Answer) -> None:
    kind = answer_kind(a)
    with st.container(border=True):
        st.caption(LABELS[kind])
        st.write(display_text(a))
        if a.citation_url:
            st.markdown(f"[{a.citation_url}]({a.citation_url})")
        if a.last_updated:
            st.caption(f"Last updated from sources: {a.last_updated}")
            if is_stale(a.last_updated):
                st.caption(f"Corpus last refreshed on {a.last_updated}.")
    st.caption(DISCLAIMER)


def main() -> None:
    st.set_page_config(page_title="MF Facts - HDFC Mutual Fund", layout="centered")
    st.title("MF Facts · HDFC Mutual Fund")
    st.caption(DISCLAIMER)

    columns = st.columns(len(EXAMPLES))
    for column, example in zip(columns, EXAMPLES):
        if column.button(example, use_container_width=True):
            st.session_state["q"] = example
            st.session_state["box"] = example

    question = st.text_input(
        "Ask about expense ratio, exit load, lock-in…",
        key="box",
        max_chars=config.MAX_INPUT_CHARS,
    ).strip()
    if not question:
        return
    if rate_limited():
        st.info("You're asking quickly. Please wait a moment and try again.")
        return
    try:
        started = time.perf_counter()
        result = answer(question)
        log_request(result, (time.perf_counter() - started) * 1000)
        render(result)
    except Exception:
        st.info(
            "The service is unavailable right now. You can read the official "
            "scheme pages directly:"
        )
        for url in list(config.PRIMARY_URLS.values())[1:]:
            st.markdown(f"- [{url}]({url})")


if __name__ == "__main__":
    main()
