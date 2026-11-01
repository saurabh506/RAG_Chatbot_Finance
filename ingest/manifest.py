"""Allowlist of the 22 public URLs in the corpus.

5 pages are `extract` (parsed for facts). 17 are `link-only` (cited, never parsed).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from config import LINK_ONLY_URLS, PRIMARY_URLS, SCHEME_IDS

SCHEME_FIELDS = (
    "expense_ratio",
    "exit_load",
    "min_sip",
    "min_lumpsum",
    "lock_in",
    "riskometer",
    "benchmark",
)


@dataclass(frozen=True)
class UrlSpec:
    id: str
    tier: str          # groww | amc | amfi | sebi | rta
    title: str
    url: str
    purpose: str       # scheme-facts | amc-overview | process | education | regulatory
    mode: str          # extract | link-only
    fields: tuple[str, ...] = ()


_SCHEME_TITLES = {
    "hdfc-large-direct-growth": "HDFC Large Cap Fund - Direct Growth",
    "hdfc-flexi-direct-growth": "HDFC Flexi Cap - Direct Plan Growth",
    "hdfc-elss-direct-growth": "HDFC ELSS Tax Saver - Direct Plan Growth",
    "hdfc-mid-direct-growth": "HDFC Mid Cap Fund - Direct Growth",
}

_LINK_ONLY_META = {
    "amc-home": ("amc", "HDFC Mutual Fund - home", "amc-overview"),
    "amc-factsheets": ("amc", "HDFC MF - factsheets", "performance-link"),
    "amc-statements": ("amc", "HDFC MF - consolidated account statement", "process"),
    "amc-ter": ("amc", "HDFC MF - total expense ratio reports", "regulatory"),
    "amc-nav": ("amc", "HDFC MF - NAV and IDCW", "reference"),
    "amc-portfolio": ("amc", "HDFC MF - monthly portfolio", "reference"),
    "amfi-home": ("amfi", "AMFI - home", "education"),
    "amfi-mutual-fund": ("amfi", "AMFI - mutual fund section", "education"),
    "amfi-nav": ("amfi", "AMFI - NAV file (NAVAll.txt)", "reference"),
    "sebi-home": ("sebi", "SEBI - home", "regulatory"),
    "sebi-investor": ("sebi", "SEBI Investor - education", "education"),
    "sebi-regulations": ("sebi", "SEBI - regulations", "regulatory"),
    "sebi-circulars": ("sebi", "SEBI - circulars", "regulatory"),
    "cams": ("rta", "CAMS - investor services", "process"),
    "kfintech": ("rta", "KFintech - investor services", "process"),
    "groww-help-statement": ("groww", "Groww Help - capital gains statement", "process"),
    "groww-help-home": ("groww", "Groww Help - mutual funds", "process"),
}


def _build_specs() -> tuple[UrlSpec, ...]:
    specs: list[UrlSpec] = [
        UrlSpec(
            id="groww-amc",
            tier="groww",
            title="HDFC Mutual Fund - AMC overview",
            url=PRIMARY_URLS["groww-amc"],
            purpose="amc-overview",
            mode="extract",
        )
    ]
    for scheme_id in SCHEME_IDS:
        specs.append(
            UrlSpec(
                id=scheme_id,
                tier="groww",
                title=_SCHEME_TITLES[scheme_id],
                url=PRIMARY_URLS[scheme_id],
                purpose="scheme-facts",
                mode="extract",
                fields=SCHEME_FIELDS,
            )
        )
    for spec_id, (tier, title, purpose) in _LINK_ONLY_META.items():
        specs.append(
            UrlSpec(
                id=spec_id,
                tier=tier,
                title=title,
                url=LINK_ONLY_URLS[spec_id],
                purpose=purpose,
                mode="link-only",
            )
        )
    return tuple(specs)


SPECS: tuple[UrlSpec, ...] = _build_specs()
_INDEX = {spec.id: spec for spec in SPECS}


def by_id(spec_id: str) -> UrlSpec:
    return _INDEX[spec_id]


def by_tier(tier: str) -> list[UrlSpec]:
    return [spec for spec in SPECS if spec.tier == tier]


def write_sources_csv(results, path: Path) -> Path:
    """Write docs/sources.csv from fetch results. Never hand-edited."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "tier",
                "title",
                "url",
                "purpose",
                "mode",
                "fetched_at",
                "http_status",
                "bytes",
                "used_for_facts",
            ]
        )
        for result in results:
            spec = result.spec
            writer.writerow(
                [
                    spec.id,
                    spec.tier,
                    spec.title,
                    spec.url,
                    spec.purpose,
                    spec.mode,
                    result.fetched_at or "",
                    result.status,
                    result.bytes,
                    "yes" if spec.mode == "extract" else "no",
                ]
            )
    return path
