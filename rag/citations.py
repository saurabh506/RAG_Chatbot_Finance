"""Citation validator (architecture.md §6.9).

Before a number is shown, re-fetch its source page and confirm HTTP 200, that
the scheme is on the page, and that the value still appears. Results are cached
for 24 h. Any doubt (host not allowlisted, non-200, value missing, network
error) fails closed; the caller then shows "unverifiable" copy, never the figure.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

import config
from ingest.load import html_to_text

_NIL = {"nil", "none", "na", "n/a", "—", "-"}


@dataclass
class ValidationResult:
    ok: bool
    http_status: int | None
    checked_at: str
    reason: str


def normalize(text: str) -> str:
    """Lowercase, drop ₹ , %, collapse whitespace; nil/none/na/— -> 'nil'."""
    text = re.sub(r"[₹,%]", "", text.lower())
    text = " ".join(text.split())
    return "nil" if text in _NIL else text


def _now_iso(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _key(url: str, field: str, value: str) -> str:
    return hashlib.sha256(f"{url}|{field}|{value}".encode()).hexdigest()


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def fetch_page(url: str) -> tuple[int, str]:
    response = requests.get(
        url,
        timeout=config.VALIDATE_TIMEOUT_S,
        headers={"User-Agent": config.USER_AGENT},
        allow_redirects=False,
    )
    return response.status_code, response.text


def _allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in config.ALLOWED_HOSTS


def _check(status: int, html: str, value: str, marker: str | None) -> tuple[bool, str]:
    if status != 200:
        return False, f"http {status}"
    page = normalize(html_to_text(html)[0])
    if marker and normalize(marker) not in page:
        return False, "scheme not on page"
    if normalize(value) not in page:
        return False, "value not on page"
    return True, "ok"


def validate(
    url: str,
    field: str,
    expected_value: str,
    marker: str | None = None,
    fetch: Callable[[str], tuple[int, str]] | None = None,
    cache_path: Path | None = None,
    now: float | None = None,
) -> ValidationResult:
    now = time.time() if now is None else now
    stamp = _now_iso(now)
    if not _allowed(url):
        return ValidationResult(False, None, stamp, "host not allowed")

    path = Path(cache_path or config.VALIDATION_CACHE)
    cache = _load_cache(path)
    key = _key(url, field, expected_value)
    hit = cache.get(key)
    if hit and now - hit["at"] < config.VALIDATE_TTL_H * 3600:
        return ValidationResult(hit["ok"], hit["status"], hit["checked_at"], hit["reason"])

    try:
        status, html = (fetch or fetch_page)(url)
    except Exception as exc:  # network down: not cached, so the next call retries
        return ValidationResult(False, None, stamp, f"fetch failed: {type(exc).__name__}")

    ok, reason = _check(status, html, expected_value, marker)
    cache[key] = {"ok": ok, "status": status, "checked_at": stamp, "reason": reason, "at": now}
    _save_cache(path, cache)
    return ValidationResult(ok, status, stamp, reason)
