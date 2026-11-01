"""Polite, cached fetching of allowlisted public pages.

Scope is deliberately narrow: **one request per manifest entry, no crawling.**
The HTML is never parsed for links, so the fetcher cannot discover or follow
internal pages, PDFs, sitemaps, or "related fund" links. The 17 supporting URLs
exist only as citations and are never fetched.

- 2 s between requests, 20 s timeout, 2 retries (never retries 403).
- Caches HTML at data/raw/<sha256(url)[:16]>.html and supports ETag revalidation.
- Redirects are followed only within the same host (max 3), with robots
  re-checked for the new path; anything else is refused.
- robots.txt is enforced for `extract` specs; unreachable robots => skip.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import config
from ingest.manifest import UrlSpec

_META_PATH = config.DATA / "fetch_meta.json"
_ROBOTS: dict[str, list[tuple[bool, str]] | None] = {}
MAX_REDIRECTS = 3
REDIRECT_CODES = {301, 302, 303, 307, 308}


@dataclass
class FetchResult:
    spec: UrlSpec
    status: int | str
    bytes: int = 0
    path: str | None = None
    sha256: str | None = None
    fetched_at: str | None = None
    elapsed_ms: int = 0
    error: str | None = None
    from_cache: bool = False
    final_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200


# --------------------------------------------------------------------------
# Cache + metadata
# --------------------------------------------------------------------------


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return config.RAW / f"{digest}.html"


def _load_meta() -> dict:
    if _META_PATH.exists():
        try:
            return json.loads(_META_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_meta(meta: dict) -> None:
    config.ensure_dirs()
    _META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def _parse_robots(text: str) -> list[tuple[bool, str]]:
    rules: list[tuple[bool, str]] = []
    applies = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            applies = value == "*"
        elif key in {"allow", "disallow"} and applies and value:
            rules.append((key == "allow", value))
    return rules


def _robots_for(url: str, session: requests.Session):
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base in _ROBOTS:
        return _ROBOTS[base]
    try:
        response = session.get(
            f"{base}/robots.txt",
            timeout=10,
            headers={"User-Agent": config.USER_AGENT},
        )
        _ROBOTS[base] = (
            _parse_robots(response.text) if response.status_code == 200 else None
        )
    except requests.RequestException:
        _ROBOTS[base] = None
    return _ROBOTS[base]


def _robots_allows(url: str, session: requests.Session) -> bool | None:
    """True/False when robots is known, None when it cannot be read."""
    rules = _robots_for(url, session)
    if rules is None:
        return None
    path = urlparse(url).path or "/"
    best_len = -1
    allowed = True
    for is_allow, prefix in rules:
        if path.startswith(prefix) and len(prefix) >= best_len:
            best_len = len(prefix)
            allowed = is_allow
    return allowed


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in config.ALLOWED_HOSTS


def _request(session: requests.Session, url: str, headers: dict, timeout: int):
    """GET with manual redirects: same host only, max 3 hops.

    Returns (response, final_url, error). No automatic redirect following, so
    a redirect cannot silently move the fetch to a non-allowlisted host.
    """
    current = url
    response = None
    for _ in range(MAX_REDIRECTS + 1):
        response = session.get(
            current, timeout=timeout, headers=headers, allow_redirects=False
        )
        if response.status_code not in REDIRECT_CODES:
            return response, current, None

        location = response.headers.get("Location") or ""
        if not location:
            return response, current, "redirect without Location header"
        target = urljoin(current, location)
        if urlparse(target).hostname != urlparse(url).hostname:
            return (
                response,
                current,
                f"redirect to another host refused: {urlparse(target).hostname}",
            )
        if _robots_allows(target, session) is False:
            return response, current, "redirect target disallowed by robots.txt"
        current = target
    return response, current, "too many redirects"


def fetch(spec: UrlSpec, force: bool = False, session: requests.Session | None = None) -> FetchResult:
    """Fetch one spec. Never raises; failures are encoded in the result."""
    if spec.mode != "extract":
        return FetchResult(spec=spec, status="link-only", fetched_at=None)

    if not _host_allowed(spec.url):
        return FetchResult(spec=spec, status="blocked", error="host not allowlisted")

    config.ensure_dirs()
    session = session or requests.Session()
    path = _cache_path(spec.url)
    meta = _load_meta()
    entry = meta.get(spec.url, {})

    allowed = _robots_allows(spec.url, session)
    if allowed is None:
        return FetchResult(spec=spec, status="robots-unavailable", error="robots.txt unreadable")
    if not allowed:
        return FetchResult(spec=spec, status="robots-disallowed")

    if path.exists() and not force:
        body = path.read_bytes()
        return FetchResult(
            spec=spec,
            status=200,
            bytes=len(body),
            path=str(path.relative_to(config.ROOT)),
            sha256=hashlib.sha256(body).hexdigest(),
            fetched_at=entry.get("fetched_at") or date.today().isoformat(),
            from_cache=True,
        )

    headers = {"User-Agent": config.USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"}
    if force and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]

    started = time.perf_counter()
    last_error: str | None = None
    response = None
    final_url = spec.url
    for attempt in range(config.FETCH_RETRIES + 1):
        time.sleep(config.FETCH_DELAY_S)
        try:
            response, final_url, redirect_error = _request(
                session, spec.url, headers, config.FETCH_TIMEOUT_S
            )
        except requests.RequestException as exc:  # network-level failure
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
            continue

        if redirect_error:
            last_error = redirect_error
            break

        if response.status_code == 200:
            break
        if response.status_code == 304 and path.exists():
            body = path.read_bytes()
            return FetchResult(
                spec=spec,
                status=200,
                bytes=len(body),
                path=str(path.relative_to(config.ROOT)),
                sha256=hashlib.sha256(body).hexdigest(),
                fetched_at=entry.get("fetched_at") or date.today().isoformat(),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                from_cache=True,
                final_url=final_url,
            )
        if response.status_code == 403:
            last_error = "HTTP 403 (bot protection; not retried)"
            break
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = f"HTTP {response.status_code}"
            time.sleep(1.5 * (attempt + 1))
            continue
        last_error = f"HTTP {response.status_code}"
        break

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else "error"
        return FetchResult(
            spec=spec,
            status=status,
            elapsed_ms=elapsed_ms,
            error=last_error,
            final_url=final_url,
        )

    body = response.content
    path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    fetched_at = date.today().isoformat()
    meta[spec.url] = {
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "sha256": sha,
        "fetched_at": fetched_at,
    }
    _save_meta(meta)

    return FetchResult(
        spec=spec,
        status=200,
        bytes=len(body),
        path=str(path.relative_to(config.ROOT)),
        sha256=sha,
        fetched_at=fetched_at,
        elapsed_ms=elapsed_ms,
        final_url=final_url,
    )


def fetch_all(specs, force: bool = False) -> list[FetchResult]:
    config.ensure_dirs()
    session = requests.Session()
    results: list[FetchResult] = []
    for spec in specs:
        result = fetch(spec, force=force, session=session)
        results.append(result)
        _print_result(result)
    _write_log(results)
    return results


def _print_result(result: FetchResult) -> None:
    if result.status == "link-only":
        print(f"{result.spec.id:<30} {'link-only':>12} {'-':>9}  (not fetched)")
        return
    kb = result.bytes / 1024
    suffix = " cached" if result.from_cache else ""
    error = f"  {result.error}" if result.error else ""
    print(f"{result.spec.id:<30} {str(result.status):>12} {kb:>7.1f} KB{suffix}{error}")


def _write_log(results: list[FetchResult]) -> None:
    payload = [
        {
            "id": result.spec.id,
            "url": result.spec.url,
            "tier": result.spec.tier,
            "mode": result.spec.mode,
            "status": result.status,
            "bytes": result.bytes,
            "path": result.path,
            "sha256": result.sha256,
            "fetched_at": result.fetched_at,
            "elapsed_ms": result.elapsed_ms,
            "from_cache": result.from_cache,
            "final_url": result.final_url,
            "error": result.error,
        }
        for result in results
    ]
    (config.DATA / "fetch_log.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
