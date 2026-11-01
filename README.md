# MF Facts - HDFC Mutual Fund FAQ assistant

Facts-only assistant over **1 AMC page + 4 HDFC schemes**. Every answer carries
exactly one official source link. No advice, no returns, no PII.

**Design docs**

| File | Contents |
|---|---|
| `PRD.md` | scope, users, answer contract, evaluation gates |
| `architecture.md` | components, data flow, failure modes, ADRs |
| `implementation.md` | 9 build steps with verification gates |

**Status:** Steps 1–6 implemented (structure, scraping, load, chunking, embedding). Facts extraction is Step 4, still pending.

## Setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> On this machine `python` resolves to a Microsoft Store stub. Always use `py -3.11`
> or the venv interpreter directly.

## Scrape the corpus

```powershell
py -3.11 -m ingest.run --list      # show the 22 allowlisted URLs
py -3.11 -m ingest.run --fetch     # 5 pages fetched, 17 recorded link-only
```

Outputs: `data/raw/*.html`, `data/fetch_log.json`, `docs/sources.csv`.

## Load documents (HTML → text)

```powershell
py -3.11 -m ingest.run --load      # -> data/docs.jsonl
```

The loader strips scripts and site chrome (everything before the first `<h1>`,
plus Groww's link-farm footer), keeps `h1–h3` as section markers, and validates
each document: ≥5,000 chars, `Expense ratio` present on scheme pages, unique
content hash, no mojibake. Failures become `status: link-only` / `failed` and are
excluded downstream — never a silent empty document.

## Chunk documents

```powershell
py -3.11 -m ingest.run --chunk     # -> data/chunks.jsonl
```

Section-aware splitting: boundaries come from the headings captured by the
loader, short sections stay short, long sections are windowed at 400 words with
60-word overlap, and every chunk carries `scheme_id`, `section`, `source_url`,
`fetched_at`. Current corpus: **76 chunks**, max 438 words, all with a URL.

## Embed chunks

```powershell
py -3.11 -m ingest.run --embed               # MiniLM, falls back to hash
py -3.11 -m ingest.run --embed --backend hash
```

Outputs `data/embeddings.npy` and `data/embed_ids.json` (backend, model, dim,
unit-norm flag, chunk ids). Rows are L2-normalised, so cosine similarity is a dot
product. If `sentence-transformers` or the model is unavailable, the stage falls
back to a deterministic 256-dim hash embedder and records the reason instead of
failing; query embeddings reuse whichever backend the manifest records.

## Tests

```powershell
py -3.11 -m unittest discover -s tests -v
```

## Run the UI

```powershell
py -3.11 -m streamlit run app.py
```

## Scope and limits

- Only HDFC Mutual Fund and four schemes (Large Cap, Flexi Cap, ELSS, Mid Cap).
- Sources are official public pages only; no blogs, no app back-end.
- No performance claims, no comparisons, no recommendations.
- Values are captured at ingest time; the UI always shows the source date.
