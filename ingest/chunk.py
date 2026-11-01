"""Chunk stage: `docs.jsonl` -> `chunks.jsonl`.

Section-aware, not fixed windows:

- Boundaries come from the `headings` captured by `ingest/load.py`, matched in
  document order (a heading repeated later, e.g. "Understand terms", opens a
  second section; a fund name inside an "Also manages" list does not).
- A section <= 500 words stays one chunk. Merging it with a neighbour to reach
  the 300-word floor would mix unrelated topics ("Minimum investments" +
  "Returns and rankings") and hurt retrieval, so short sections stay short.
- A section > 500 words is windowed at 400 words with 15% (60-word) overlap,
  cut on sentence/line boundaries so a fact is never split.
- Every chunk is prefixed with `scheme | section` and carries the citation
  metadata the composer needs: `scheme_id`, `section`, `source_url`,
  `fetched_at`.

Two curated chunks cover statement downloads: the Groww help page and CAMS
are SPA shells, so there is no text to extract from them, but they are valid
citations and the intent (I7) needs a retrievable target.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import config
from ingest.manifest import by_id

TARGET_WORDS = 400
MAX_WORDS = 500
OVERLAP_WORDS = 60  # 15% of the target
MIN_WORDS = 300

# Short, human-readable prefixes embed better than slugs.
SCHEME_LABELS = {
    "hdfc-large-direct-growth": "HDFC Large Cap Fund",
    "hdfc-flexi-direct-growth": "HDFC Flexi Cap Fund",
    "hdfc-elss-direct-growth": "HDFC ELSS Tax Saver Fund",
    "hdfc-mid-direct-growth": "HDFC Mid Cap Fund",
    "hdfc-amc": "HDFC Mutual Fund (AMC)",
}


def slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "section"


# --------------------------------------------------------------------------
# Sectioning
# --------------------------------------------------------------------------


def _find_line(text: str, needle: str, start: int) -> int | None:
    """Find `needle` as a whole line at or after `start`."""
    needle = needle.strip()
    if not needle:
        return None
    pos = text.find(needle, start)
    while pos != -1:
        before_ok = pos == 0 or text[pos - 1] == "\n"
        after = pos + len(needle)
        after_ok = after >= len(text) or text[after] == "\n"
        if before_ok and after_ok:
            return pos
        pos = text.find(needle, pos + 1)
    return None


def split_sections(text: str, headings: list[str]) -> list[tuple[str, str]]:
    """Return [(section_name, body)] in document order.

    Headings are matched sequentially, so the same label can open multiple
    sections while a later line that merely equals an earlier heading (a fund
    name in a list) is not mistaken for a boundary.
    """
    boundaries: list[tuple[int, str]] = []
    cursor = 0
    for heading in headings:
        index = _find_line(text, heading, cursor)
        if index is None:
            continue  # multi-line heading (e.g. fund manager block)
        boundaries.append((index, heading.strip()))
        cursor = index + len(heading.strip())

    sections: list[tuple[str, str]] = []
    if not boundaries:
        body = text.strip()
        return [("Overview", body)] if body else []

    if boundaries[0][0] > 0:
        preface = text[: boundaries[0][0]].strip()
        if preface:
            sections.append(("Overview", preface))

    for i, (start, name) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        body = text[start + len(name) : end].strip()
        if body:
            sections.append((name, body))
    return sections


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------


def _units(body: str) -> list[str]:
    """Lines and sentences — the smallest unit a chunk may cut at."""
    units: list[str] = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if sentence:
                units.append(sentence)
    return units


def _split_long_unit(unit: str) -> list[str]:
    words = unit.split()
    return [
        " ".join(words[i : i + MAX_WORDS])
        for i in range(0, len(words), MAX_WORDS)
    ]


def pack_units(units: list[str]) -> list[str]:
    """Pack units into 300-500 word windows with ~15% overlap."""
    flat: list[str] = []
    for unit in units:
        if len(unit.split()) > MAX_WORDS:
            flat.extend(_split_long_unit(unit))
        else:
            flat.append(unit)

    chunks: list[str] = []
    start = 0
    total = len(flat)
    while start < total:
        words = 0
        end = start
        while end < total:
            size = len(flat[end].split())
            if words and words + size > MAX_WORDS:
                break
            words += size
            end += 1
            if words >= TARGET_WORDS:
                break
        chunks.append(" ".join(flat[start:end]))
        if end >= total:
            break

        # Carry the trailing ~60 words into the next window.
        back = end
        carried = 0
        while back > start and carried < OVERLAP_WORDS:
            back -= 1
            carried += len(flat[back].split())
        next_start = back if back > start else end - 1
        start = next_start if next_start > start else start + 1
    return chunks


# --------------------------------------------------------------------------
# Chunk records
# --------------------------------------------------------------------------


def chunk_doc(doc: dict) -> list[dict]:
    scheme_id = doc.get("scheme_id") or "hdfc-amc"
    label = SCHEME_LABELS.get(scheme_id, scheme_id)
    sections = split_sections(doc["text"], doc.get("headings", []))

    records: list[dict] = []
    index = 0
    for section, body in sections:
        for part in pack_units(_units(body)):
            if not part.strip():
                continue
            records.append(
                {
                    "chunk_id": f"{doc['doc_id']}#{slug(section)}-{index}",
                    "doc_id": doc["doc_id"],
                    "scheme_id": scheme_id,
                    "section": section,
                    "source_url": doc["url"],
                    "fetched_at": doc["fetched_at"],
                    "words": len(part.split()),
                    "text": f"{label} | {section} | {part}",
                }
            )
            index += 1
    return records


def curated_chunks(fetched_at: str) -> list[dict]:
    """Statement-download targets (SPA pages cannot be parsed for text)."""
    cas = by_id("amc-statements")
    gains = by_id("groww-help-statement")
    return [
        {
            "chunk_id": "process#statement-cas",
            "doc_id": "process-statement",
            "scheme_id": "hdfc-amc",
            "section": "Statements",
            "source_url": cas.url,
            "fetched_at": fetched_at,
            "words": 46,
            "text": (
                "HDFC Mutual Fund (AMC) | Statements | Download a consolidated "
                "account statement (CAS) covering all mutual fund folios, and a "
                "capital gains statement for tax filing. Statements are issued by "
                "the AMC and the registrars CAMS and KFintech; the HDFC Mutual "
                "Fund consolidated account statement page is the official starting "
                "point."
            ),
        },
        {
            "chunk_id": "process#statement-capital-gains",
            "doc_id": "process-statement",
            "scheme_id": "hdfc-amc",
            "section": "Statements",
            "source_url": gains.url,
            "fetched_at": fetched_at,
            "words": 41,
            "text": (
                "HDFC Mutual Fund (AMC) | Statements | How to download a capital "
                "gains statement for mutual fund investments: open the help page "
                "for the step-by-step process, then use the registrar or AMC "
                "statement service. A capital gains statement lists purchase and "
                "redemption dates for tax computation."
            ),
        },
    ]


# --------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------


def build_chunks(docs: list[dict]) -> list[dict]:
    usable = [d for d in docs if d.get("status") == "ok"]
    chunks: list[dict] = []
    for doc in usable:
        chunks.extend(chunk_doc(doc))
    if usable:
        latest = max((d.get("fetched_at") or date.today().isoformat()) for d in usable)
        chunks.extend(curated_chunks(latest))
    return chunks


def write_chunks(chunks: list[dict], path: Path | None = None) -> Path:
    path = path or config.CHUNKS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return path


def read_chunks(path: Path | None = None) -> list[dict]:
    path = path or config.CHUNKS
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def print_report(chunks: list[dict]) -> None:
    per_doc: dict[str, int] = {}
    for chunk in chunks:
        per_doc[chunk["doc_id"]] = per_doc.get(chunk["doc_id"], 0) + 1
    words = [chunk["words"] for chunk in chunks] or [0]
    print(f"{'doc_id':<30} {'chunks':>6}")
    for doc_id, count in sorted(per_doc.items()):
        print(f"{doc_id:<30} {count:>6}")
    print(
        f"\nchunks: {len(chunks)} | words min/avg/max: "
        f"{min(words)}/{sum(words) // len(words)}/{max(words)}"
    )
