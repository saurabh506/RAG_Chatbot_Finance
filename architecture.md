# ARCHITECTURE — Mutual Fund FAQ Assistant

**Companion to:** `PRD.md` v1.0 (2026-11-01)
**Build plan:** `implementation.md` (9 steps, with gates)
**Scope:** 1 AMC page + 4 HDFC schemes · facts-only Q&A · one citation per answer
**Stack:** Python 3.11 · Streamlit · Chroma (local vector DB) · sentence-transformers
**Revision:** v1.1 — added the load stage (HTML → text); replaced BM25 with embeddings + Chroma

---

## 1. Purpose of this document

`PRD.md` says *what* we build and *why*. This document says *how*: components, data flow, contracts, failure behaviour, and the reasoning behind each structural choice.

The architecture is deliberately boring. Five schemes and ~200 chunks do not justify a queue, a server, or a microservice mesh. The complexity budget is spent on **citation integrity**, **refusal correctness**, and **one trustworthy text pipeline**.

### Architectural drivers

| Driver (from PRD) | Quality attribute | Architectural tactic | Implemented in |
|---|---|---|---|
| Every answer cites one official link | **Traceability** | Structured fact store + template rendering + post-render validation | `rag/compose.py`, `rag/citations.py` |
| Answers ≤3 sentences | **Brevity** | Sentence budget enforced at composition, not generation | `rag/compose.py` |
| No advice, no performance claims | **Compliance** | Deterministic pre-LLM guard; LLM cannot reach the renderer for R1–R6 | `rag/guardrails.py` |
| No PII accepted or stored | **Privacy** | Scrub before model/log; log only derived metadata | `rag/guardrails.py`, `obs/log.py` |
| Facts drift as pages change | **Freshness** | `fetched_at` per document; validator re-checks at answer time | `ingest/fetch.py`, `rag/citations.py` |
| Public sources only, 15–25 URLs | **Provenance** | URL allowlist + admission gates + manifest-generated `sources.csv` | `ingest/manifest.py` |
| Fact text and chunk text must agree | **Consistency** | One load stage writes `docs.jsonl`; parse and chunk both read it | `ingest/load.py` |
| Demo must never break | **Availability** | Build-time artifacts committed; LLM optional; hash-embedding fallback | `data/`, `rag/vector_store.py` |

---

## 2. System context (C4 — Level 1)

```
                    ┌──────────────────────────┐
                    │  Retail user / support   │
                    │  agent (browser)         │
                    └────────────┬─────────────┘
                                 │ HTTPS
                                 ▼
                    ┌──────────────────────────┐
                    │  MF Facts assistant      │
                    │  (Streamlit app)         │
                    │  facts-only, 1 citation  │
                    └───┬──────────────┬───────┘
                        │              │
        build-time      │              │  answer-time (cached, ≤1/day)
        ingestion       │              │  citation validation
                        ▼              ▼
        ┌───────────────────────┐  ┌────────────────────────────┐
        │ Official public pages │  │ Same pages, GET + compare  │
        │ groww.in (scheme/AMC) │  │ (HTTP 200 + value present) │
        │ hdfcfund.com (links)  │  └────────────────────────────┘
        │ amfiindia.com         │
        │ sebi.gov.in           │
        │ camsonline / kfintech │
        └───────────────────────┘
                        │
                        ▼
        ┌───────────────────────┐
        │ Optional LLM API      │  intent fallback + phrasing only
        │ (disabled by default) │  never a source of facts
        └───────────────────────┘
```

**Trust boundary:** the app trusts only `data/facts.json` and the chunks derived from `data/docs.jsonl`, both produced by ingestion from allowlisted URLs. The LLM sits *outside* the trust boundary — its output is never treated as a fact.

---

## 3. Container view (C4 — Level 2)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Streamlit runtime (1 process)                   │
│                                                                          │
│  ┌──────────────┐   question    ┌─────────────────────────────────────┐  │
│  │  UI layer    │──────────────▶│  Orchestrator (rag/pipeline.py)     │  │
│  │  app.py      │◀──────────────│  answer object                      │  │
│  └──────────────┘   render      └───┬─────────────────────────────────┘  │
│         ▲                           │                                    │
│         │                           ├─▶ 1. Guardrails (guardrails.py)    │
│         │                           │      PII → scope → advice → perf   │
│         │                           │                                    │
│         │                           ├─▶ 2. Scheme resolver (schemes.py)  │
│         │                           │      alias table + fuzzy           │
│         │                           │                                    │
│         │                           ├─▶ 3. Intent classifier (intent.py) │
│         │                           │      rules → optional LLM          │
│         │                           │                                    │
│         │                           ├─▶ 4. Facts (facts.json)            │
│         │                           │      └─ miss → embed → Chroma      │
│         │                           │                                    │
│         │                           ├─▶ 5. Composer (compose.py)         │
│         │                           │      template + citation line      │
│         │                           │                                    │
│         │                           └─▶ 6. Citation validator            │
│         │                                  (citations.py) + cache        │
│         │                                                               │
│  ┌──────┴───────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────┐  │
│  │ obs/log.py   │   │ data/*.json  │   │ data/chroma/ │   │ LLM client│  │
│  │ (no raw Q)   │   │ facts, docs  │   │ + MiniLM     │   │ (optional)│  │
│  └──────────────┘   └──────────────┘   └──────────────┘   └───────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

**Key property:** steps 1–3 can terminate the request without ever touching the LLM or the network. Most refusals are decided in **<20 ms** with zero external calls.

**Storage is read-only at runtime.** Chroma and the embedding model are opened at boot; nothing writes to them after ingest.

---

## 4. Build-time pipeline (offline)

Ingestion is a **batch job that produces committed artifacts** — not a runtime scrape. The deployed app never crawls; it reads files and (optionally) re-validates citations.

```
ingest/manifest.py    ── allowlist of 22 URLs (tier, purpose, expected fields)
        │
        ▼
ingest/fetch.py       ── GET with UA, 2 s delay, ETag/If-None-Match,
        │                 disk cache data/raw/<sha256>.html, robots respected
        ▼
ingest/load.py        ── raw HTML → clean text (stdlib html.parser)
        │                 strip script/style/nav/footer; keep h1–h3 as markers
        │                 validate: chars ≥5k, expected marker present,
        │                 ≥3 headings, unique hash → data/docs.jsonl
        ▼
   ┌────┴───────────────────────────┐
   ▼                                ▼
ingest/parse.py                 ingest/chunk.py
   │  docs.jsonl → facts.json        │  docs.jsonl → chunks.jsonl
   │  (7 fields + raw_text)          │  (300–500 words, 15% overlap)
   │                                 ▼
   │                            ingest/embed.py
   │                                 │  chunks → embeddings.npy (384-d)
   │                                 ▼
   │                            ingest/build_index.py
   │                                 │  upsert → data/chroma/
   ▼                                 ▼
data/facts.json · docs.jsonl · chunks.jsonl · embeddings.npy · chroma/ · manifest.json
        │
        └──▶ docs/sources.csv (generated, never hand-edited)
```

**Why the load stage exists:** scraping produces bytes, not text. Without a single load stage, the fact extractor and the chunker each parse raw HTML independently and can drift — the number shown in an answer might not be the text that was embedded and cited. `docs.jsonl` is the one place that conversion happens.

**Hard gates applied at `fetch`/`load`:** URL in allowlist · HTTP 200 · server-rendered text present · no login/paywall. A URL failing a gate is excluded from `facts.json` and flagged in the manifest.

**Observed constraints baked into the pipeline (2026-11-01):**

| Source | Behaviour | Pipeline response |
|---|---|---|
| `groww.in/mutual-funds/*` | Server-rendered, robots-permitted | Primary fact source; full extraction |
| `hdfcfund.com` | `robots.txt` → **403 (Akamai)** | `mode: link-only`; never parsed for asserted values |
| `groww.in/help/*`, `camsonline.com` | SPA shell (identical HTML across URLs) | `mode: link-only` unless content check passes |
| `amfiindia.com` | Some legacy paths 404 | Only verified paths in allowlist |

**Refresh policy:** run manually or nightly via GitHub Actions; commit the regenerated artifacts. The app displays `fetched_at` from the artifact, so a stale corpus is visible, not silent.

### 4.1 Load stage contract (`ingest/load.py`)

| Property | Requirement |
|---|---|
| Input | One cached HTML file per allowlisted URL |
| Output | One `docs.jsonl` record per document |
| Text extraction | stdlib `HTMLParser`; skip `script/style/noscript/svg/nav/footer` |
| Chrome trim | start at first `<h1>`; cut tail at the `Home>Mutual Funds>` breadcrumb (Groww footer has no `<footer>` tag) |
| Structure | `h1–h3` retained as `headings[]` for section-aware chunking |
| Encoding | UTF-8 with `errors="replace"`; entity unescape; whitespace collapse |
| Minimum size | `chars >= 5_000`, else `status: "failed"` |
| Marker check | scheme pages must contain `"Expense ratio"` |
| SPA signature | identical `chars` + empty `headings` across different URLs → `link-only` |
| Dedupe | `content_hash` unique; first occurrence wins |
| Determinism | same HTML in → same text out (no model, no network) |

```json
{"doc_id":"hdfc-elss-direct-growth","url":"https://groww.in/mutual-funds/hdfc-elss-…",
 "scheme_id":"hdfc-elss-direct-growth","title":"HDFC ELSS Tax Saver Fund Direct Plan Growth",
 "text":"HDFC ELSS Tax Saver Fund Direct Plan Growth\nELSS • 3Y Lock-in\nEquity\nELSS\nVery High Risk …",
 "headings":["HDFC ELSS Tax Saver Fund Direct Plan Growth","Return calculator","Holdings ( 64 )","Minimum investments"],
 "chars":8956,"content_hash":"sha256:…","fetched_at":"2026-11-01","status":"ok"}
```

**Downstream rule:** `parse.py` and `chunk.py` accept only `status == "ok"` documents. Neither may read `data/raw/` directly.

---

## 5. Runtime request flow

### 5.1 Answer path (I1–I6, structured path)

```
User            UI            Orchestrator      Guardrails   Resolver   Intent    FactStore   Composer   Validator
 │               │                  │               │           │          │          │          │          │
 │─ question ───▶│                  │               │           │          │          │          │          │
 │               │─ ask(q) ────────▶│               │           │          │          │          │          │
 │               │                  │─ scrub(q) ───▶│           │          │          │          │          │
 │               │                  │◀─ clean|PII ──│           │          │          │          │          │
 │               │                  │─ resolve ────────────────▶│          │          │          │          │
 │               │                  │◀─ scheme_id ───────────────│          │          │          │          │
 │               │                  │─ classify ─────────────────────────▶│          │          │          │
 │               │                  │◀─ intent=I1 ────────────────────────│          │          │          │
 │               │                  │─ lookup(scheme,I1) ──────────────────────────▶│          │          │
 │               │                  │◀─ {value, raw, url, fetched_at} ──────────────│          │          │
 │               │                  │─ compose(fact) ────────────────────────────────────────▶│          │
 │               │                  │◀─ text + citation ─────────────────────────────────────│          │
 │               │                  │─ validate(url, value) ────────────────────────────────────────────▶│
 │               │                  │◀─ ok | fail ───────────────────────────────────────────────────────│
 │               │◀─ answer obj ────│               │           │          │          │          │          │
 │◀─ render ─────│                  │               │           │          │          │          │          │
```

Latency: guard 5 ms · resolve 3 ms · intent 10 ms (rules) · lookup 2 ms · compose 3 ms · validate 15 ms (cache hit) → **~40 ms p50**.

### 5.2 Semantic path (I7/I8, unknown phrasing, missing field)

```
question ─▶ embed(query) ─▶ Chroma.query(k=4, where={scheme_id}) ─▶ top chunk
             (MiniLM, L2)         (cosine)                              │
                                                                        ▼
                                              chunk.source_url + chunk.section ─▶ compose
```

- Query and documents use the **same model and the same L2 normalisation**.
- Filter by `scheme_id` when the resolver succeeded; otherwise search all schemes and require similarity ≥ 0.25, else clarify.
- A chunk without `source_url` is discarded before composition.

### 5.3 Refusal path (R1–R6)

```
q ─▶ PII?  ──yes──▶ R5 template ─▶ render (no log of q)
      │no
      ▼
   advice? ──yes──▶ R1/R3/R4 template + SEBI Investor link ─▶ render
      │no
      ▼
   performance? ──yes──▶ R2 template + factsheet link ─▶ render
      │no
      ▼
   in corpus? ──no──▶ R6 template + AMC page link ─▶ render
      │yes
      ▼
   normal pipeline
```

Guardrails are **ordered** (PII first) so that a PII-bearing question never reaches the LLM, the retriever, the vector DB, or the log writer.

### 5.4 Fallback paths

| Trigger | Response |
|---|---|
| Intent unresolved, question non-empty | Clarify once: "I can answer expense ratio, exit load, minimum SIP, lock-in, riskometer, benchmark, or statement downloads for HDFC Large Cap / Flexi Cap / ELSS / Mid Cap." + 3 examples. No link needed (no fact asserted). |
| Fact present but validator fails | "Unverifiable" copy + the source link + date. `validated: false`. |
| Fact missing (`null` in store) | Same as unverifiable — never infer. |
| Vector store unavailable | Structured answers (I1–I6) unaffected; semantic intents → clarify. |
| Embedding model unavailable | Hash embedder (`EMBED_BACKEND=hash`); lower recall, same interface. |
| LLM timeout (>1.5 s) or error | Silent fallback to rules-only path. User sees no error. |
| Corpus file unreadable | HTTP 503 → static "service unavailable" page listing the 4 scheme URLs. |

---

## 6. Component specifications

### 6.1 `app.py` — UI

- Single-page Streamlit; session state holds only the last answer.
- 3 example chips → set input → submit (same code path as typed input).
- Renders answer types with distinct, neutral styling: `answer` / `refusal` / `clarify` / `unverifiable`.
- Citation as `st.markdown` link with visible URL text, `target="_blank"`.
- Persistent disclaimer (top + under answer) per PRD U2/U9.
- Input guard: `max_chars=300`; empty/whitespace blocked client-side (server re-checks).
- **No** file upload, no chat history persistence, no query params containing user text.

### 6.2 `rag/pipeline.py` — orchestrator

Pure function: `answer(question: str) -> AnswerObject`. No global state except read-only loaded artifacts.

Responsibilities: enforce order of stages, enforce timeouts, map exceptions to fallback types, attach `corpus_version`, call logger with metadata only.

### 6.3 `rag/guardrails.py`

| Function | Method | Notes |
|---|---|---|
| `scrub_pii(text)` | Regexes: PAN `[A-Z]{5}[0-9]{4}[A-Z]`, Aadhaar 12-digit (with/without spaces), 10-digit phone, email, OTP-like 4–8 digits after "otp", account/folio patterns after keywords | Returns `(clean_text, hits[])`; hits are **type labels only** |
| `is_advice(text)` | Verb/intent blocklist: should/shouldn't/buy/sell/hold/recommend/best/good/worth/invest in/switch/portfolio/allocation/how much | Negation-aware for "should I *not*"? → still R1 (conservative) |
| `is_performance(text)` | return/CAGR/XIRR/alpha/beta/compare/outperform/beat/5Y/3Y + "which fund" | R2 |
| `is_tax_advice(text)` | "how much tax", "tax saving amount", "save tax" | R4; factual lock-in still allowed (I4) |
| `in_corpus(scheme)` | Resolver result + AMC allowlist | R6 |

Blocklist matching runs on lowercased, punctuation-stripped text with word boundaries to avoid "good" matching "goods".

### 6.4 `rag/schemes.py` — resolver

Alias table built from `facts.json`:

| Alias examples | → scheme_id |
|---|---|
| "elss", "tax saver", "hdfc elss", "tax saving fund" | `hdfc-elss-direct-growth` |
| "flexi", "flexi cap", "equity fund" | `hdfc-flexi-direct-growth` |
| "large cap", "bluechip" | `hdfc-large-direct-growth` |
| "mid cap", "midcap" | `hdfc-mid-direct-growth` |
| "hdfc", "amc", "fund house" | `hdfc-amc` |

Resolution order: exact alias → token overlap → `difflib.SequenceMatcher` ≥ 0.82 → ambiguous (ask which of two) → unresolved (clarify).

**Ambiguity rule:** if two schemes match with delta < 0.05, ask a disambiguation question listing both names. Never guess.

### 6.5 `rag/intent.py`

Rule-first: keyword patterns per intent I1–I8 (see PRD §7.1). LLM fallback only when rules score 0 and an API key exists:

```json
{ "intent": "I1|I2|...|I8|R1|R2|R3|R4|R6|UNKNOWN",
  "scheme_hint": "string|null",
  "confidence": 0.0 }
```

Constraints: `temperature=0`, `max_tokens=80`, JSON schema validated, 1.5 s timeout, **one** retry. Any malformed output → `UNKNOWN` → clarify. The LLM never receives the full question text if PII was detected (that path exits earlier).

### 6.6 `data/facts.json` — fact store

See PRD §9.3 for schema. Additional invariants:

- `value` is a **display string** copied verbatim (`"1.20%"`, `"₹500"`, `"Nil"`, `"3 years"`). No floats stored.
- `raw_text` + `char_offset` retained for audit and for the validator's substring check.
- `normalized` (derived) used only for comparison: lowercase, strip `₹ , %`, collapse whitespace, map `nil|none|na|—` → `nil`.
- `as_of` populated only when the page states a date (e.g. TER month); otherwise `null` and UI uses `fetched_at`.
- Schema is versioned (`schema_version`); the fact loader refuses unknown majors.

### 6.7 `rag/retrieve.py` — two retrieval paths

| Path | Used for | Mechanism |
|---|---|---|
| **Structured** | I1–I6 (field questions) | `facts.json[(scheme_id, field)]` — exact, no embedding |
| **Semantic** | I7/I8, unknown phrasing, missing field | embed query → Chroma `query(n=4, where={scheme_id})` → best chunk |

Rules:

- Facts never go through the vector DB. Similarity search must not decide an expense ratio.
- Query embedding uses the same model + L2 normalisation as ingest; cosine space.
- `scheme_id` filter when resolved; otherwise global search with a similarity floor (0.25) → below floor, clarify.
- Retrieved chunks must carry `source_url` + `section`; a chunk without a URL is dropped before composition.
- Vector store is read-only at runtime. Rebuild happens only in `ingest/build_index.py`, by `chunk_id` upsert (idempotent).
- Fallback: `EMBED_BACKEND=hash` swaps MiniLM for a 256-dim hashing embedder — same interface, weaker recall, no model download.

### 6.8 `rag/compose.py`

Templates are the only code path that produces user-visible text for facts.

```python
FACT = "{scheme_name} has {label} of {value}. Source: {url} — Last updated from sources: {date}."
```

- Sentence budget check: `count_sentences(text) <= 3` — asserted, not truncated. A violation raises and falls back to the minimal template.
- Advice-verb post-check on every rendered string (`should|recommend|best|better|consider|good`), including LLM-polished variants.
- If optional LLM polish is enabled, the polished string must **contain the exact `value` substring** and pass the verb check, else the template is used.
- One citation only: the composer accepts a single `Citation` object by type.

### 6.9 `rag/citations.py` — validator

```
validate(url, field, expected_value) -> {ok, http_status, checked_at, reason}
```

- Cache: `data/validation_cache.json`, TTL 24 h, keyed by `sha256(url + field + expected_value)`.
- Checks: HTTP 200 → scheme name or section marker present → re-extract field → normalized equality.
- Network egress restricted to the allowlist (§12.3). Non-allowlisted URL → `ok=False` immediately.
- On failure the composer emits the "unverifiable" copy. **The number is never rendered.**

### 6.10 `obs/log.py`

One JSON line per request:

```json
{"ts":"2026-11-01T10:12:03Z","intent":"I1","scheme_id":"hdfc-flexi-direct-growth",
 "answer_type":"answer","citation_url":"https://groww.in/...","validated":true,
 "latency_ms":41,"corpus_version":"2026-11-01.1"}
```

No question text, no free-text user content, no IP, no session id. `question_hash` (salted SHA-256, first 12 chars) only if dedup is needed.

---

## 7. Data architecture

### 7.1 Artifacts

| Artifact | Format | Producer | Consumer | Committed |
|---|---|---|---|---|
| `data/raw/<sha>.html` | HTML | `fetch.py` | `load.py` | No (gitignored) |
| `data/docs.jsonl` | JSONL | `load.py` | `parse.py`, `chunk.py` | Yes |
| `data/facts.json` | JSON | `parse.py` | `retrieve.py`, `compose.py` | Yes |
| `data/chunks.jsonl` | JSONL | `chunk.py` | `embed.py`, audit | Yes |
| `data/embeddings.npy` | NumPy | `embed.py` | `build_index.py` | Yes |
| `data/embed_ids.json` | JSON | `embed.py` | `build_index.py`, `retrieve.py` | Yes |
| `data/chroma/` | Chroma dir | `build_index.py` | `retrieve.py` | Yes |
| `data/manifest.json` | JSON | `manifest.py` | `sources.csv`, UI "sources" page | Yes |
| `data/validation_cache.json` | JSON | `citations.py` | `citations.py` | No |
| `docs/sources.csv` | CSV | `manifest.py` | Reviewer | Yes |

Committing derived artifacts is a deliberate trade: the demo boots with zero network, and reviewers can diff corpus changes in git.

### 7.2 Corpus versioning

`corpus_version = "<fetched_at_max>.<seq>"` (e.g. `2026-11-01.1`). Bumped on every successful full ingest. Attached to every answer object and log line so any answer can be replayed against the exact corpus that produced it.

The version must change when **any** of `docs.jsonl`, `facts.json`, `chunks.jsonl`, `embeddings.npy` or `chroma/` changes — they are one atomic unit.

### 7.3 Freshness contract

- UI shows the **document** date, never "today".
- If `corpus_version` age > 30 days, the UI adds a non-blocking banner: "Corpus last refreshed on <date>."
- Validator cache TTL 24 h prevents showing a number whose page changed today.

---

## 8. Interfaces

### 8.1 Public (app) API

`POST /answer` — request `{"question": "..."}` (≤300 chars); response per PRD §11.

| Status | When | Body |
|---|---|---|
| 200 | answer / refusal / clarify / unverifiable | Answer object |
| 400 | empty or >300 chars | `{"error":"invalid_input"}` |
| 422 | PII detected | Refusal object (never echoes input) |
| 429 | >10 requests/min per session | `{"error":"rate_limited"}` |
| 503 | artifacts unavailable | `{"error":"corpus_unavailable"}` |

### 8.2 Internal module contracts

```python
# ingest/load.py
def load_doc(path: Path, meta: dict) -> Doc          # HTML → clean text + headings

# ingest/embed.py
def embed_texts(texts: list[str]) -> np.ndarray      # (n, 384), L2-normalised

# rag/vector_store.py
def add(chunks: list[Chunk], embeddings: np.ndarray) -> None
def query(vec: np.ndarray, k: int = 4, scheme_id: str | None = None) -> list[Chunk]

# rag/pipeline.py
def answer(question: str) -> AnswerObject

# rag/guardrails.py
def scrub_pii(text: str) -> tuple[str, list[str]]
def classify_scope(text: str, scheme: Scheme | None) -> str  # "ok" | "R1".."R6"

# rag/intent.py
def classify(text: str, scheme: Scheme | None) -> tuple[str, float]

# rag/retrieve.py
def lookup(scheme_id: str, field: str) -> Field | None
def search(query: str, scheme_id: str | None, k: int = 4) -> list[Chunk]

# rag/compose.py
def render_fact(scheme: Scheme, field: str, citation: Citation, date: str) -> str
def render_refusal(kind: str, citation: Citation, date: str) -> str

# rag/citations.py
def validate(url: str, field: str, value: str) -> ValidationResult
```

### 8.3 CLI

```
python -m ingest.run --all            # fetch → load → parse → chunk → embed → index
python -m ingest.run --load           # re-run the load stage only
python -m ingest.run --only groww     # refresh scheme pages only
python -m eval.run_eval --report      # scorecard → eval/report.md
python -m streamlit run app.py        # local UI
```

---

## 9. Deployment topology

```
GitHub repo ──push──▶ GitHub Actions (nightly/manual)
                          │  ingest + eval gate
                          ▼
                   commit docs.jsonl, facts.json, chunks.jsonl,
                   embeddings.npy, chroma/, sources.csv
                          │
                          ▼
             Streamlit Community Cloud (auto-deploy)
                          │
        ┌─────────────────┴──────────────────┐
        │  app process (1 container)         │
        │  read-only artifacts + Chroma      │
        │  MiniLM from local model cache     │
        │  optional LLM key via secrets      │
        └─────────────────┬──────────────────┘
                          │ allowlisted egress only
                          ▼
                 groww.in / hdfcfund.com / amfi / sebi
```

**Config (env / `st.secrets`):**

| Key | Required | Default | Purpose |
|---|---|---|---|
| `LLM_ENABLED` | No | `false` | Enables intent fallback + polish |
| `LLM_API_KEY` | No | – | Provider key |
| `LLM_MODEL` | No | small model | JSON-constrained |
| `EMBED_BACKEND` | No | `minilm` | `minilm` \| `hash` (no model download) |
| `EMBED_MODEL` | No | `all-MiniLM-L6-v2` | Local path or HF id |
| `VALIDATE_LIVE` | No | `true` | Toggle live citation re-check |
| `CORPUS_DIR` | No | `data/` | Artifact location |
| `RATE_LIMIT_PER_MIN` | No | `10` | Session throttle |

**Cold start:** open Chroma (~200 vectors, <200 ms), load MiniLM from cache (1–3 s), import JSON artifacts (<100 ms). No ingest, no network. If the model cache is missing, `EMBED_BACKEND=hash` keeps the app functional.

**Fallback if hosting fails:** Colab notebook that imports the same `rag/` package + 3-minute screen recording (PRD deliverable #1).

---

## 10. Performance

| Stage | p50 | p95 | Notes |
|---|---|---|---|
| PII + scope guard | 3 ms | 8 ms | Regex only |
| Scheme resolve | 2 ms | 5 ms | In-memory alias table |
| Intent (rules) | 8 ms | 15 ms | LLM path adds 400–900 ms |
| Fact lookup | 1 ms | 2 ms | Dict access |
| Query embed (MiniLM) | 12 ms | 30 ms | CPU, short query |
| Chroma query (top-4) | 6 ms | 15 ms | ~200 vectors |
| Compose | 2 ms | 5 ms | String templates |
| Citation validate (cache hit) | 5 ms | 20 ms | Cache miss: 300–900 ms |
| Render | 10 ms | 25 ms | Streamlit rerun |
| **Total (structured, warm)** | **~45 ms** | **~120 ms** | Meets PRD G6 (<2.5 s) |
| **Total (semantic, warm)** | **~65 ms** | **~150 ms** | Embedding + Chroma |

The 2.5 s budget exists to absorb cold-cache validation and Streamlit reruns — not because the pipeline is slow.

---

## 11. Reliability & failure modes (fail-closed matrix)

| Failure | Detection | User-visible behaviour | Data integrity |
|---|---|---|---|
| Source page changed layout | Field extraction returns `null` | "Unverifiable" + link | ✅ No stale number shown |
| Loader yields empty text | `chars < 5_000` | `link-only`; semantic answer falls back to clarify | ✅ |
| SPA shell instead of content | identical `chars`, no headings | `link-only`, never chunked | ✅ |
| Source page 404/500 | Validator `http_status != 200` | "Unverifiable" + link | ✅ |
| Value changed on page | Normalized compare mismatch | "Unverifiable" + link | ✅ Never silently updates |
| Network down | Timeout 1.5 s | Cached result if <24 h, else unverifiable | ✅ |
| LLM down / quota | Exception / timeout | Rules-only path (identical answers for I1–I6) | ✅ |
| LLM returns a number not in store | Substring check fails | Template used | ✅ |
| Embedding model missing | Import/cache error | Hash embedder; semantic recall drops | ⚠️ Degraded, still cited |
| Chroma missing/corrupt | Client exception at boot | Structured answers only; semantic → clarify | ✅ |
| Ambiguous scheme | Resolver delta < 0.05 | Disambiguation question | ✅ No guess |
| PII in input | Regex hit | R5 refusal, no echo, no log | ✅ |
| Prompt injection in input | Scope guard + template rendering | Treated as out-of-scope/clarify | ✅ LLM cannot emit facts |
| Artifacts missing/corrupt | Loader exception at boot | 503 page with 4 direct links | ✅ |
| Two sources disagree | Ingest conflict flag | `confidence: low` + one-clause note | ⚠️ Surfaced, not hidden |

**Invariant:** the renderer will not emit a numeric/factual claim unless it came from `facts.json` *and* passed validation. There is no code path that bypasses this.

---

## 12. Security & privacy

### 12.1 PII data flow

```
input ──▶ scrub ──▶ [hit] ──▶ refusal (type label only)
             │
             └─[clean]─▶ pipeline ──▶ log (metadata only)
```

- Raw input exists only in memory for the duration of the request; never written to disk, never sent to the LLM when a PII hit occurs.
- PII-bearing questions are not embedded either — the guard runs before any model call.
- Streamlit is configured with no telemetry and no query-param echo.
- Log sweep test (`tests/test_privacy.py`) greps the log file for PAN/Aadhaar/email/phone patterns after a full eval run; must return 0 matches.

### 12.2 Secrets

- Only `LLM_API_KEY` is secret; stored in Streamlit secrets, never in repo, never in logs.
- No user credentials, no OAuth, no accounts.

### 12.3 Network egress allowlist

`citations.py` and `fetch.py` share one allowlist module:

```
groww.in/mutual-funds/*, groww.in/help/mutual-funds*
hdfcfund.com, www.hdfcfund.com
amfiindia.com, www.amfiindia.com
sebi.gov.in, www.sebi.gov.in, investor.sebi.gov.in
camsonline.com, www.camsonline.com
kfintech.com, www.kfintech.com
```

Any other host → refused before the socket opens. This blocks SSRF and accidental third-party sourcing in one place.

The fetcher also has **no crawl surface**: it requests exactly the manifest entries and never parses HTML for links, so internal documents, PDFs, sitemaps and related-fund links cannot be reached. Redirects are followed manually, same-host only, max 3 hops, with `robots.txt` re-checked for the target path; anything else is refused.

Embedding runs **locally** — no query text leaves the process. If `LLM_ENABLED=false` (default), the app makes no outbound model calls at all.

### 12.4 Prompt injection

The LLM sees at most a short, delimited question plus a fixed instruction set, and its output is constrained to an intent enum. Because facts are rendered from templates, a successful injection can at worst misclassify an intent — it cannot inject text, links, or numbers into an answer. The composer also re-checks rendered strings for advice verbs and for URL count == 1.

---

## 13. Testing strategy

| Layer | What | Tool | Gate |
|---|---|---|---|
| Unit | PII regexes, scope classifier, sentence counter, normalizer, alias resolver | `unittest` | 100% pass |
| Contract | `AnswerObject` schema, one-citation rule, ≤3 sentences, date format | `unittest` | 100% pass |
| Load | HTML → text on 4 saved fixtures: char count, headings, no `<script>` leakage | `unittest` | 4/4 pass |
| Ingest | Extractor against `docs.jsonl` fixtures (4 schemes) | `unittest` | All 7 fields populated |
| Vector | `count() == len(chunks)`, filtered query returns one scheme, idempotent rebuild | `unittest` | 3/3 pass |
| Integration | End-to-end `answer()` on 40-item golden set | `eval/run_eval.py` | 40/40 grounded |
| Citation | Live URL 200 + value present | `eval/run_eval.py --live` | 40/40 valid |
| Adversarial | Injection, PII bait, multi-intent, long input | `eval/run_eval.py --adversarial` | 0 leaks, correct refusals |
| Privacy | Log file scan | `tests/test_privacy.py` | 0 matches |

**CI gate:** unit + contract + load + ingest + vector + golden set must pass before artifacts are committed. Live citation checks run nightly (network flakiness must not block deploys; they degrade to `unverifiable`).

---

## 14. Architecture decision records

| # | Decision | Alternatives rejected | Rationale |
|---|---|---|---|
| ADR-1 | **Template-first, LLM-second** | Freeform RAG generation | Citation accuracy becomes deterministic; hallucinations structurally impossible |
| ADR-2 | **Build-time ingestion, committed artifacts** | Runtime scraping / live crawl | Demo boots offline; reproducible; no rate-limit risk at query time |
| ADR-3 | **Streamlit single process** | Next.js + FastAPI split | 3-day budget, one language, free hosting; corpus too small for a service split |
| ADR-4 | **JSON fact store, no database** | Postgres/SQLite for facts | 5 schemes × 7 fields; git-diffable artifacts are an audit feature |
| ADR-5 | **Structured lookup for facts, embeddings + Chroma for free text** | Vector search for everything; BM25 only | Exact fields must never depend on similarity; semantic recall only helps I7/I8 |
| ADR-6 | **HDFC MF pages link-only** | Scrape AMC site | `robots.txt` returned 403 via Akamai; link-only is compliant and sufficient |
| ADR-7 | **Deterministic guards before LLM** | LLM-based safety classifier | Refusals must be instant, testable, and identical every run |
| ADR-8 | **Single citation enforced by type** | "Sources" list | PRD requires exactly one link; a scalar removes the failure mode |
| ADR-9 | **No raw question logging** | Full-text logs | PII risk outweighs debugging convenience |
| ADR-10 | **Fail to "unverifiable", never to a guess** | Best-effort answer | A wrong expense ratio is worse than no answer in a regulated domain |
| ADR-11 | **One load stage → `docs.jsonl`** | Parse HTML separately in extractor and chunker | Two parsers drift; the cited number must be the chunked text |
| ADR-12 | **Chroma (local, persistent)** | FAISS (no metadata), pgvector (server) | Metadata filtering by `scheme_id` + zero infrastructure |
| ADR-13 | **MiniLM with a hash-embedding fallback** | Cloud embeddings only | Keeps the demo offline-capable and PII inside the process |

---

## 15. Known technical debt (accepted for v1)

1. **Groww DOM coupling.** The extractor depends on label text and page structure. Mitigated by fixtures + `null` on miss, not by a generic parser.
2. **English-only, single-turn.** No conversation memory, no follow-ups ("what about exit load?") without repeating the scheme name.
3. **Validation cache can mask same-day changes** (24 h TTL). Acceptable given daily ingest.
4. **SPA help pages** may be cited without extractable text; statement answers rely on curated link-level text plus an official AMC link.
5. **No automated drift alerting.** A stale corpus is visible in the UI banner, not pushed to an owner.
6. **Embedding dependency.** MiniLM adds ~90 MB and 1–3 s boot time; the hash fallback is weaker on paraphrases.
7. **Chroma replaces a 40-line BM25 index.** Accepted for metadata filtering and semantic recall; adds one dependency to pin.

---

## 16. Evolution path (post-v1)

| Phase | Change | Architectural impact |
|---|---|---|
| v1.1 | Nightly CI refresh + drift diff in PR | None — pipeline already idempotent |
| v1.2 | 10 more HDFC schemes | Alias table + artifacts only; no code change |
| v1.3 | Factsheet PDF parsing (link + extract) | New `ingest/pdf.py`; same fact schema |
| v2 | Multi-AMC | Add `amc` dimension to resolver + per-AMC allowlist; retrieval unchanged |
| v2.1 | Hindi queries | Query translation before intent, English answers; templates unchanged |
| v3 | Hosted embeddings + eval dashboard | Swap `EMBED_BACKEND`; adds observability surface |

The design keeps the **fact schema, load contract, and citation contract stable** across all of these — that is the part worth protecting.

---

## 17. Appendix — module map

| File | Responsibility | Must not |
|---|---|---|
| `app.py` | Render, input limits, disclaimer | Contain fact strings |
| `rag/pipeline.py` | Stage ordering, timeouts, fallbacks | Know about HTML |
| `rag/guardrails.py` | PII, advice, performance, scope | Call the LLM |
| `rag/schemes.py` | Alias resolution, ambiguity | Guess between ties |
| `rag/intent.py` | Intent classification | Produce answer text |
| `rag/retrieve.py` | Structured lookup + semantic search | Return chunks without URLs |
| `rag/vector_store.py` | Chroma add/query, metadata filters | Embed documents at query time |
| `rag/compose.py` | Templates, sentence budget, one citation | Emit unvalidated numbers |
| `rag/citations.py` | Live verification + cache | Accept non-allowlisted hosts |
| `obs/log.py` | Metadata logging | Persist raw questions |
| `ingest/load.py` | HTML → clean text + headings | Guess at missing content |
| `ingest/parse.py` | Text → facts | Read `data/raw/` |
| `ingest/chunk.py` | Text → chunks | Read `data/raw/` |
| `ingest/embed.py` | Chunks → vectors | Change the model without re-indexing |
| `ingest/build_index.py` | Vectors → Chroma | Append duplicate `chunk_id`s |

**Dependency rule:** `app → pipeline → {guardrails, schemes, intent, retrieve, compose, citations}`. No back-edges, no circular imports. `ingest/` is never imported at runtime.
