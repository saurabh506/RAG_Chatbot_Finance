"""Load stage: cached HTML -> clean text (`data/docs.jsonl`).

This is the **only** place raw HTML becomes text. `parse.py` and `chunk.py`
read `docs.jsonl` and never touch `data/raw/`, so the number that gets cited
is the same text that gets chunked and embedded.

Validation gates (a failing document is excluded, never silently empty):

    chars >= 5_000                     -> else status "failed"
    scheme page contains "Expense ratio" -> else status "link-only"
    >= 3 headings                      -> warning only (chunk by paragraphs)
    content hash unique                -> later copy status "duplicate"
    no mojibake / replacement chars    -> warning only

Deterministic: same HTML in, same text out. No model, no network.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import config
from ingest.fetch import _cache_path, _load_meta
from ingest.manifest import SPECS, UrlSpec

MIN_CHARS = 5_000
MIN_HEADINGS = 3
SCHEME_MARKER = "Expense ratio"
AMC_MARKER = "HDFC Mutual Fund"

BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "li", "ul", "ol", "dl", "dt", "dd",
    "tr", "table", "thead", "tbody", "td", "th", "br", "hr", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
HEADING_TAGS = {"h1", "h2", "h3"}
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "iframe", "template"}


class TextExtractor(HTMLParser):
    """HTML -> plain text with block newlines and h1-h3 section markers."""

    BODY_MARK = "\x00h1\x00"
    # Groww renders a large link-farm footer inside plain divs (no <footer>),
    # preceded by this one-off breadcrumb. Trimming here removes ~4k chars of
    # navigation from every page without guessing at markup.
    FOOTER_MARK = "Home>Mutual Funds>"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self.title: str = ""
        self.body_marked = False
        self.headings_before_body = 0
        self._skip_depth = 0
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self._in_title = False
        self._title_buf: list[str] = []

    # -- structure ---------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in BLOCK_TAGS:
            self._newline()
        if tag == "h1" and not self.body_marked:
            # Everything before the first h1 is site chrome.
            self.body_marked = True
            self.headings_before_body = len(self.headings)
            self.parts.append(self.BODY_MARK)
        if tag in HEADING_TAGS:
            self._heading_tag = tag
            self._heading_buf = []
        elif tag == "title":
            self._in_title = True
            self._title_buf = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() in {"br", "hr"} and not self._skip_depth:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == self._heading_tag:
            heading = _clean(" ".join(self._heading_buf))
            if heading:
                self.headings.append(heading)
            self._heading_tag = None
            self._heading_buf = []
            self._newline()
        elif tag == "title" and self._in_title:
            self.title = _clean(" ".join(self._title_buf))
            self._in_title = False
            self._title_buf = []
        elif tag in BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.replace("\xa0", " ")
        if not text.strip():
            # Keep a single space so inline words do not fuse.
            self._soft_space()
            return
        if self._in_title:
            self._title_buf.append(text)
            return  # <title> is metadata, not body text
        if self._heading_tag:
            self._heading_buf.append(text)
        self._append(text)

    # -- helpers -----------------------------------------------------------
    def _append(self, text: str) -> None:
        # Adjacent inline nodes ("Monthly SIP" + "One time") otherwise fuse.
        if self.parts:
            previous = self.parts[-1][-1:]
            first = text[:1]
            if previous.isalnum() and first.isalnum():
                self.parts.append(" ")
        self.parts.append(text)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def _soft_space(self) -> None:
        if self.parts and not self.parts[-1].endswith(("\n", " ")):
            self.parts.append(" ")


def _clean(text: str) -> str:
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html: str) -> tuple[str, list[str], str]:
    parser = TextExtractor()
    parser.feed(html)
    parser.close()

    raw = "".join(parser.parts)
    if parser.body_marked and TextExtractor.BODY_MARK in raw:
        raw = raw.split(TextExtractor.BODY_MARK, 1)[1]

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[ \t\f\v]+", " ", raw)
    raw = re.sub(r" *\n *", "\n", raw)
    raw = re.sub(r"\n{2,}", "\n", raw)
    text = raw.strip()

    footer = text.find(TextExtractor.FOOTER_MARK)
    if footer > 0:
        text = text[:footer].strip()

    # Consecutive duplicate headings (same label rendered twice) are noise.
    headings: list[str] = []
    source = parser.headings
    if parser.body_marked:
        source = source[parser.headings_before_body :]
    for heading in source:
        if not headings or headings[-1] != heading:
            headings.append(heading)
    return text, headings, parser.title


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mojibake_score(text: str) -> int:
    return text.count("\ufffd") + text.count("Ã") + text.count("â€")


@dataclass
class LoadedDoc:
    doc_id: str
    url: str
    scheme_id: str | None
    title: str
    purpose: str
    mode: str
    text: str
    headings: list[str]
    chars: int
    content_hash: str
    fetched_at: str
    http_status: int | None
    source_bytes: int
    status: str
    warnings: list[str] = field(default_factory=list)

    def to_record(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "url": self.url,
            "scheme_id": self.scheme_id,
            "title": self.title,
            "purpose": self.purpose,
            "mode": self.mode,
            "text": self.text,
            "headings": self.headings,
            "chars": self.chars,
            "content_hash": self.content_hash,
            "fetched_at": self.fetched_at,
            "http_status": self.http_status,
            "source_bytes": self.source_bytes,
            "status": self.status,
            "warnings": self.warnings,
        }


def _fetch_meta(url: str) -> tuple[str, int | None]:
    entry = _load_meta().get(url, {})
    fetched_at = entry.get("fetched_at") or date.today().isoformat()
    return fetched_at, 200


def load_doc(spec: UrlSpec, path: Path | None = None) -> LoadedDoc:
    """Load one cached HTML file. Never raises on bad content; encodes it."""
    path = path or _cache_path(spec.url)
    fetched_at, http_status = _fetch_meta(spec.url)
    html = path.read_text(encoding="utf-8", errors="replace")
    text, headings, page_title = html_to_text(html)

    warnings: list[str] = []
    status = "ok"

    if len(text) < MIN_CHARS:
        status = "failed"
        warnings.append(f"text too short ({len(text)} < {MIN_CHARS})")

    marker = AMC_MARKER if spec.purpose == "amc-overview" else SCHEME_MARKER
    if status == "ok" and marker not in text:
        # Server did not render the content: SPA shell or bot wall.
        status = "link-only"
        warnings.append(f"marker missing: {marker!r}")

    if status == "ok" and len(headings) < MIN_HEADINGS:
        warnings.append(f"only {len(headings)} headings; chunk by paragraph")

    bad = _mojibake_score(text)
    if bad > 0:
        warnings.append(f"{bad} mojibake/replacement characters")

    scheme_id = spec.id if spec.purpose == "scheme-facts" else None
    title = page_title or spec.title

    return LoadedDoc(
        doc_id=spec.id,
        url=spec.url,
        scheme_id=scheme_id,
        title=title,
        purpose=spec.purpose,
        mode=spec.mode,
        text=text,
        headings=headings,
        chars=len(text),
        content_hash=_sha256(text) if text else "",
        fetched_at=fetched_at,
        http_status=http_status,
        source_bytes=len(html.encode("utf-8")),
        status=status,
        warnings=warnings,
    )


def _mark_spa_shells(docs: list[LoadedDoc]) -> None:
    """Different URLs with identical size and no headings are an SPA shell."""
    groups: dict[tuple[int, tuple[str, ...]], list[LoadedDoc]] = {}
    for doc in docs:
        if doc.status != "ok":
            continue
        groups.setdefault((doc.chars, tuple(doc.headings)), []).append(doc)
    for group in groups.values():
        if len(group) > 1 and not group[0].headings:
            for doc in group:
                doc.status = "link-only"
                doc.warnings.append("SPA shell signature (identical chars, no headings)")


def load_all(specs=SPECS) -> list[LoadedDoc]:
    config.ensure_dirs()
    docs: list[LoadedDoc] = []
    for spec in specs:
        if spec.mode != "extract":
            continue
        path = _cache_path(spec.url)
        if not path.exists():
            docs.append(
                LoadedDoc(
                    doc_id=spec.id,
                    url=spec.url,
                    scheme_id=spec.id if spec.purpose == "scheme-facts" else None,
                    title=spec.title,
                    purpose=spec.purpose,
                    mode=spec.mode,
                    text="",
                    headings=[],
                    chars=0,
                    content_hash="",
                    fetched_at="",
                    http_status=None,
                    source_bytes=0,
                    status="missing",
                    warnings=["no cached HTML; run --fetch"],
                )
            )
            continue
        docs.append(load_doc(spec, path))

    _mark_spa_shells(docs)

    seen: dict[str, str] = {}
    for doc in docs:
        if doc.status != "ok":
            continue
        if doc.content_hash in seen:
            doc.status = "duplicate"
            doc.warnings.append(f"same text as {seen[doc.content_hash]}")
        else:
            seen[doc.content_hash] = doc.doc_id
    return docs


def write_docs(docs: list[LoadedDoc], path: Path | None = None) -> Path:
    path = path or config.DOCS
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for doc in docs:
            handle.write(json.dumps(doc.to_record(), ensure_ascii=False) + "\n")
    return path


def read_docs(path: Path | None = None) -> list[dict]:
    """Read docs.jsonl. Downstream stages must filter status == "ok"."""
    path = path or config.DOCS
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def print_report(docs: list[LoadedDoc]) -> None:
    print(f"{'doc_id':<30} {'chars':>7} {'heads':>6} {'status':<10} warnings")
    for doc in docs:
        note = "; ".join(doc.warnings)
        print(f"{doc.doc_id:<30} {doc.chars:>7} {len(doc.headings):>6} {doc.status:<10} {note}")
