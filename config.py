"""Central configuration for the MF FAQ assistant.

Single source of paths, limits and the URL allowlist.
Nothing here performs I/O at import time except `ensure_dirs()` when called.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent

DATA = ROOT / "data"
RAW = DATA / "raw"                 # cached HTML from ingest/fetch.py
TEXT = DATA / "text"               # optional plain-text dumps for debugging
CHROMA = DATA / "chroma"           # persistent Chroma collection

DOCS = DATA / "docs.jsonl"         # load.py output (clean text, single source)
FACTS = DATA / "facts.json"        # parse.py output
CHUNKS = DATA / "chunks.jsonl"     # chunk.py output
EMBEDDINGS = DATA / "embeddings.npy"
EMBED_IDS = DATA / "embed_ids.json"
MANIFEST = DATA / "manifest.json"
FETCH_LOG = DATA / "fetch_log.json"
VALIDATION_CACHE = DATA / "validation_cache.json"

DOCS_DIR = ROOT / "docs"
SOURCES_CSV = DOCS_DIR / "sources.csv"

# --------------------------------------------------------------------------
# Corpus scope
# --------------------------------------------------------------------------

AMC_ID = "hdfc-amc"

SCHEME_IDS = (
    "hdfc-large-direct-growth",
    "hdfc-flexi-direct-growth",
    "hdfc-elss-direct-growth",
    "hdfc-mid-direct-growth",
)

# The five pages we extract facts from (Step 2).
PRIMARY_URLS = {
    "groww-amc": "https://groww.in/mutual-funds/amc/hdfc-mutual-funds",
    "hdfc-large-direct-growth": (
        "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth"
    ),
    "hdfc-flexi-direct-growth": (
        "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth"
    ),
    "hdfc-elss-direct-growth": (
        "https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth"
    ),
    "hdfc-mid-direct-growth": (
        "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth"
    ),
}

# Supporting pages: cited, never parsed for asserted values.
LINK_ONLY_URLS = {
    "amc-home": "https://www.hdfcfund.com/",
    "amc-factsheets": "https://www.hdfcfund.com/mutual-funds/factsheets",
    "amc-statements": (
        "https://www.hdfcfund.com/services/consolidated-account-statement"
    ),
    "amc-ter": (
        "https://www.hdfcfund.com/statutory-disclosure/"
        "total-expense-ratio-of-mutual-fund-schemes/reports"
    ),
    "amc-nav": "https://www.hdfcfund.com/nav-and-idcw",
    "amc-portfolio": (
        "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio"
    ),
    "amfi-home": "https://www.amfiindia.com/",
    "amfi-mutual-fund": "https://www.amfiindia.com/mutual-fund",
    "amfi-nav": "https://www.amfiindia.com/spages/NAVAll.txt",
    "sebi-home": "https://www.sebi.gov.in/",
    "sebi-investor": "https://investor.sebi.gov.in/",
    "sebi-regulations": (
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
        "?doListing=yes&sid=1&ssid=3&smid=0"
    ),
    "sebi-circulars": (
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
        "?doListing=yes&sid=1&ssid=7&smid=0"
    ),
    "cams": "https://www.camsonline.com/",
    "kfintech": "https://www.kfintech.com/",
    "groww-help-statement": (
        "https://groww.in/help/mutual-funds/how-to-download-capital-gains-statement"
    ),
    "groww-help-home": "https://groww.in/help/mutual-funds",
}

# --------------------------------------------------------------------------
# Network policy
# --------------------------------------------------------------------------

ALLOWED_HOSTS = frozenset(
    {
        "groww.in",
        "hdfcfund.com",
        "www.hdfcfund.com",
        "amfiindia.com",
        "www.amfiindia.com",
        "sebi.gov.in",
        "www.sebi.gov.in",
        "investor.sebi.gov.in",
        "camsonline.com",
        "www.camsonline.com",
        "kfintech.com",
        "www.kfintech.com",
    }
)

USER_AGENT = "mf-faq-prototype/1.0 (+educational prototype; facts-only)"
FETCH_DELAY_S = 2.0
FETCH_TIMEOUT_S = 20
FETCH_RETRIES = 2

# --------------------------------------------------------------------------
# Answer contract
# --------------------------------------------------------------------------

MAX_INPUT_CHARS = 300
SENTENCE_LIMIT = 3
VALIDATE_TTL_H = 24
VALIDATE_TIMEOUT_S = 1.5
RATE_LIMIT_PER_MIN = 10

# --------------------------------------------------------------------------
# Retrieval / embedding
# --------------------------------------------------------------------------

EMBED_BACKEND = os.getenv("EMBED_BACKEND", "minilm")   # "minilm" | "hash"
EMBED_MODEL = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DIM = 384
HASH_EMBED_DIM = 256
VECTOR_BACKEND = os.getenv("VECTOR_BACKEND", "chroma")  # "chroma" | "numpy"
CHROMA_COLLECTION = "mf_chunks"
TOP_K = 4
SIMILARITY_FLOOR = 0.25

def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _secret(name: str) -> str | None:
    """Environment first, then .env, then .streamlit/secrets.toml."""
    if os.getenv(name):
        return os.getenv(name)
    env_values = _read_env_file(ROOT / ".env")
    if env_values.get(name):
        return env_values[name]
    secrets_path = ROOT / ".streamlit" / "secrets.toml"
    if secrets_path.exists():
        try:
            import tomllib

            data = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
            for key in (name, name.lower()):
                if data.get(key):
                    return str(data[key])
        except (OSError, ValueError):
            pass
    return None


LLM_ENABLED = (_secret("LLM_ENABLED") or "false").lower() == "true"
LLM_BASE_URL = _secret("LLM_BASE_URL") or "https://api.groq.com/openai/v1"
LLM_MODEL = _secret("LLM_MODEL") or "openai/gpt-oss-20b"
LLM_REASONING_EFFORT = _secret("LLM_REASONING_EFFORT") or "low"
GROQ_API_KEY = _secret("GROQ_API_KEY")
LLM_TIMEOUT_S = float(_secret("LLM_TIMEOUT_S") or 2.5)

VALIDATE_LIVE = (_secret("VALIDATE_LIVE") or "true").lower() == "true"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

REQUIRED_DIRS = (DATA, RAW, TEXT, CHROMA, DOCS_DIR)


def ensure_dirs() -> None:
    """Create the data directories if they do not exist. Idempotent."""
    for directory in REQUIRED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)
