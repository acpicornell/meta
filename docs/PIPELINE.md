# Pipeline — scripts, data layout and local build

The method these scripts implement is described in [METHOD.md](METHOD.md).

---

## The pipeline

```
fetch_sources           ▶ data/sources/*.json           snapshots of the 5 siblings + SHA-256 manifest
build_gazetteer         ▶ data/gazetteer.parquet        NGIB + ~360 historical-variant rows
fetch_wikidata          ▶ data/wikidata.jsonl           ~6 900 Balearic Wikidata items with multilingual aliases
build_wikidata_variants ▶ data/wikidata_variants.parquet ~1 500 extra aliases anchored to NGIB ids
normalize_sources       ▶ data/normalized/*.jsonl       per-sibling rows with entry_kind_hint + parent_municipality_hint
resolve_entities        ▶ data/place_links.jsonl        3-phase resolver: kind → parent → describes
resolve_with_llm        ▶ data/llm_cache.jsonl         optional LLM tiebreaker for borderline cases
load_db                 ▶ db/meta.duckdb               places, entries, entry_resolution_log
export_web_data         ▶ web/{data.json, data-blobs.json}  what the front-end loads
report                  ▶ data/reports/*.tsv           coverage, in_all_sources, top_places, low_confidence, orphans
```

All stages are **deterministic, idempotent and re-runnable**. The only network steps are `fetch_sources` (re-reads sibling repos from disk — no network if siblings are local) and `fetch_wikidata` (SPARQL endpoint; cached). `resolve_with_llm` reads from `data/llm_cache.jsonl` on rerun, so a full re-resolve doesn't re-incur token costs once the borderline cases have been adjudicated.

The matcher does **not** trust the lat/lon or `matched_toponym` published by sibling projects. Every entry is re-resolved from scratch against NGIB using the entry's title + place_type + island + municipality, scoped by NGIB local_type to avoid conflating conceptually distinct entities (a *cuartón* is not a *municipi*). Sibling lat/lons are documented as noisy in `feedback_siblings_ngib_untrusted` (memory note 2026-05-26).

---

## Repo structure

```
meta/
├── README.md                    ← short overview
├── LICENSE
├── docs/                        ← method, pipeline and site documentation
├── pyproject.toml               ← Python deps: duckdb, rapidfuzz, python-dotenv,
│                                  pyarrow; optional extra `llm` = anthropic
├── uv.lock
├── wrangler.jsonc               ← Cloudflare Workers Static Assets config
├── .github/workflows/deploy.yml ← on push: check_site.py, then wrangler deploy
│
├── scripts/                     ← pipeline (deterministic, re-runnable)
│   ├── fetch_sources.py
│   ├── build_gazetteer.py       ← NGIB + curated variants table
│   ├── fetch_wikidata.py
│   ├── build_wikidata_variants.py
│   ├── normalize_sources.py     ← per-sibling JSONL adapters + classify_place_type
│   ├── resolve_entities.py      ← 3-phase resolver, source of truth for the matcher
│   ├── resolve_with_llm.py      ← optional LLM tiebreaker (Claude)
│   ├── load_db.py
│   ├── export_web_data.py       ← writes web/data.json + web/data-blobs.json
│   ├── report.py                ← audit TSVs
│   └── check_site.py            ← lints web/ against its own _headers CSP (CI)
│
├── db/
│   ├── schema.sql               ← v2 dual-link schema
│   └── meta.duckdb              ← rebuilt on every load_db.py
│
├── data/                        ← intermediate (gitignored except wikidata.jsonl)
│   ├── sources/                 ← sibling snapshots
│   ├── gazetteer.parquet
│   ├── wikidata.jsonl           ← committed; avoids re-querying Wikidata
│   ├── normalized/
│   ├── place_links.jsonl
│   ├── llm_cache.jsonl          ← gitignored; LLM decisions cached locally
│   └── reports/
│
└── web/                         ← static site, deployed to Cloudflare
    ├── index.html               ← all editorial content lives inline
    ├── app.js                   ← vanilla JS, no framework
    ├── theme.js                 ← light/dark choice, applied before first paint
    ├── style.css                ← academic beige + per-source year palette,
    │                              light and dark themes
    ├── abbreviations.json       ← 242 abbreviations across the 5 sources
    ├── data.json                ← exported by the pipeline; committed for deploy
    ├── data-blobs.json          ← lazy-loaded on first Lloc open
    ├── dev_server.py            ← local no-cache static server
    ├── _headers                 ← Cloudflare headers (no-store on everything)
    ├── robots.txt, sitemap.xml
    └── vendor/                  ← Leaflet 1.9.4 + markercluster 1.5.3, self-hosted
```

---

## Running it locally

```bash
# 1. Python deps
uv sync

# 2. Pipeline (run in order; each step is independent and idempotent)
uv run scripts/fetch_sources.py
uv run scripts/build_gazetteer.py
uv run scripts/fetch_wikidata.py             # one-off; cached
uv run scripts/build_wikidata_variants.py
uv run scripts/normalize_sources.py
uv run scripts/resolve_entities.py --stats
uv run scripts/load_db.py
uv run scripts/export_web_data.py
uv run scripts/report.py

# 3. Serve the static site with no-cache headers
cd web && python3 dev_server.py     # http://localhost:8766
```

`dev_server.py` is a 30-line `http.server` subclass that adds `Cache-Control: no-store` to every response. Plain `python -m http.server` lets the browser cache `app.js` / `style.css` in memory; after editing JS, normal reload then serves stale code until you hard-reload (`cmd-shift-R`) or open a private tab. The dev server is the local twin of the production `_headers` rule.
