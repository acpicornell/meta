# Meta · Balearic nomenclators

Unified search and exploration site for the **Balearic Islands
historical gazetteer family**: a single web that lets you find a place
and read every entry written about it across five canonical sources,
from the late-Bourbon era to the Restoration.

This repository does **not** re-extract any data. It consumes the
already-curated `web/data.json` published by each sibling project,
resolves every entry against the **NGIB** (Nomenclàtor Geogràfic de
les Illes Balears) as the canonical place authority, and emits a
single static web that aggregates and cross-references the lot.

## Sources

| Year | Sibling project | Author / publisher | Articles |
|---|---|---|---|
| 1787 | `floridablanca` | INE re-transcription of the Floridablanca Census | 111 pueblos |
| 1826 | `minano` | Sebastián Miñano y Bedoya | 211 |
| 1845 | `madoz` | Pascual Madoz | 1 217 |
| 1860 | `nomenclator_1860` | Junta General de Estadística | ~3 000 |
| 1881 | `riera` | Riera y Sans | 123 |

Each of these is a separate repository under
`/Users/acpicornell/nomenclators/`; `meta` only reads their published
JSON.

## Pipeline

```
fetch_sources           ▶ data/sources/*.json (snapshots of the 5 siblings + SHA-256 manifest)
build_gazetteer         ▶ data/gazetteer.parquet (NGIB + ~130 historical Castilian↔Catalan variants)
fetch_wikidata          ▶ data/wikidata.jsonl (~6,900 Balearic Wikidata items with multilingual aliases)
build_wikidata_variants ▶ data/wikidata_variants.parquet (~1,400 extra aliases anchored to NGIB ids)
normalize_sources       ▶ data/normalized/{floridablanca,minano,madoz,nomenclator_1860,riera}.jsonl
resolve_entities        ▶ data/place_links.jsonl  (waterfall: type_no_ngib →
                                                   historical_curated → exact_norm →
                                                   fuzzy_wratio → fuzzy_token_set →
                                                   linked_to_parent → unlinked)
load_db                 ▶ db/meta.duckdb (places, entries, place_links)
export_web_data         ▶ web/{data.json, data-blobs.json}
report                  ▶ data/reports/*.tsv (audit: coverage, in_all_sources,
                                               top_places, low_confidence_sample,
                                               unlinked)
```

All stages are deterministic, idempotent and re-runnable. The
``fetch_wikidata`` step is the only one that needs network access; it
caches its output to ``data/wikidata.jsonl`` so subsequent runs can
skip it. The matcher does **not** trust the lat/lon or
``matched_toponym`` published by sibling projects — every entry is
re-resolved against NGIB from title + place_type + island +
municipality, scoped by NGIB local_type to avoid conflating
conceptually distinct entities (a *cuartón* is not a *municipi*).

## Run

```bash
uv sync
uv run scripts/fetch_sources.py
uv run scripts/build_gazetteer.py
uv run scripts/fetch_wikidata.py            # one-off; cached to data/wikidata.jsonl
uv run scripts/build_wikidata_variants.py
uv run scripts/normalize_sources.py
uv run scripts/resolve_entities.py --stats
uv run scripts/load_db.py
uv run scripts/export_web_data.py
uv run scripts/report.py

cd web && python3 dev_server.py     # no-cache wrapper; defaults to port 8766
```

`dev_server.py` is a tiny `http.server` subclass that adds
`Cache-Control: no-store` on every response. Plain `python -m
http.server` lets the browser cache `app.js` / `style.css` in memory,
so reloads can serve stale JS until you hard-reload (cmd-shift-R) or
open a private tab.

## Map

The **Mapa** tab plots every NGIB place referenced by at least one
sibling article on Leaflet + OpenStreetMap, clustered when zoomed
out, filterable by island / NGIB type / source / has-article. Each
Lloc page also includes a small focused map with the place and its
NGIB sub-features. Leaflet and `leaflet.markercluster` are vendored
under `web/vendor/`.

## License

AGPL-3.0-or-later for the code; the original texts are in the public
domain.
