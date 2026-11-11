# Nomenclàtors balears — aggregator

[![Deploy](https://github.com/acpicornell/meta/actions/workflows/deploy.yml/badge.svg)](https://github.com/acpicornell/meta/actions/workflows/deploy.yml)

A unified diachronic reader over the **five great historical gazetteers of the Balearic Islands** (1787 – 1881). Every article of the five sibling editions is resolved against the NGIB, the official Balearic toponym registry, so that one place can be read across a century of sources at once.

**Live site:** [nomenclators.corpusbalear.org](https://nomenclators.corpusbalear.org) — part of the [Corpus Balear](https://corpusbalear.org) family.

## What's in it

- **5** sources aggregated: Floridablanca (1787), Miñano (1826-29), Madoz (1845-50), Nomenclàtor 1860, Riera (1881-87).
- **4 627** sibling articles ingested, over **1 520** canonical NGIB places.
- **4 435** articles (95.9 %) linked to NGIB by parent or by what they describe; 192 left as orphans rather than linked wrongly.
- A curated table of **~360** historical variants and **~6 900** Wikidata items anchored as aliases.

## How it was made

`meta` re-transcribes nothing: it reads each sibling project's published data, normalises it, and re-resolves every article from scratch against NGIB. Each article gets two links — the municipi it sits in and the entity it describes — through a three-phase resolver (kind → parent → describes) that never searches outside the parent's terme. Castilianised and OCR-damaged spellings are bridged by a curated variants table and Wikidata aliases; an optional, cached LLM tiebreaker settles borderline candidates. The output is a static Python pipeline feeding a vanilla-JS single-page site on Cloudflare Workers Static Assets, with no backend.

## Run locally

The sibling repos must sit next to this one (`../madoz`, `../riera`, …).

```bash
uv sync
uv run scripts/fetch_sources.py
uv run scripts/build_gazetteer.py
uv run scripts/fetch_wikidata.py             # one-off; cached
uv run scripts/build_wikidata_variants.py
uv run scripts/normalize_sources.py
uv run scripts/resolve_entities.py --stats
uv run scripts/load_db.py
uv run scripts/export_web_data.py
uv run scripts/report.py
cd web && python3 dev_server.py              # http://localhost:8766
```

## Documentation

- [docs/METHOD.md](docs/METHOD.md) — why this exists, the corpus, the dual-link data model, the 3-phase resolver, the historical-variants table, NGIB, Wikidata, the LLM tiebreaker, quality caveats and limitations.
- [docs/PIPELINE.md](docs/PIPELINE.md) — pipeline stages and outputs, repo structure, and the full local build.
- [docs/SITE.md](docs/SITE.md) — the front-end's tabs and palette, and deployment to Cloudflare Workers.

## License

- **Code**: [AGPL-3.0-or-later](https://www.gnu.org/licenses/agpl-3.0.html).
- **Sibling-project transcriptions**: each sibling's licence applies. The original texts (1787 – 1881) are in the public domain.
- **NGIB toponyms and coordinates**: CC BY (Govern de les Illes Balears, IDEIB).
- **OpenStreetMap tiles**: ODbL.
