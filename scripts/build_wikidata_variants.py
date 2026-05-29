#!/usr/bin/env python3
"""Anchor every Wikidata item to its NGIB id and emit alias rows.

Input:
    data/wikidata.jsonl       (produced by fetch_wikidata.py)
    data/gazetteer.parquet    (NGIB primaries + curated historical variants)

Output:
    data/wikidata_variants.parquet   — same row shape as gazetteer.parquet,
                                        with source='wikidata'.

Anchoring strategy:
    Match each Wikidata item's Catalan label against the NGIB
    ``spelling`` column within the same island. If that fails, try the
    Castilian label. Items that cannot be anchored are skipped (we
    have no NGIB ID for them) — but the count is reported so the user
    can audit what was lost.

For each anchored item we emit one alias row per:
    label_es, label_en (when distinct from label_ca);
    every entry of aliases_ca, aliases_es, aliases_en (also when not
    already in the gazetteer for the same (normalized, ngib_id)).

The resolver (resolve_entities.py) unions this parquet with the main
gazetteer at load time, so the new aliases participate in every
matching step from historical_curated downwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from build_gazetteer import normalize  # type: ignore

WD_JSONL = ROOT / 'data' / 'wikidata.jsonl'
GAZETTEER = ROOT / 'data' / 'gazetteer.parquet'
OUT = ROOT / 'data' / 'wikidata_variants.parquet'


def main():
    con = duckdb.connect(':memory:')

    # Index of NGIB primaries by (normalized, island) → (id, municipality, local_type, lon, lat).
    print('Loading gazetteer NGIB index…', file=sys.stderr)
    ngib_rows = con.sql(f"""
        SELECT id, spelling, normalized, municipality, island, local_type, lon, lat
        FROM read_parquet('{GAZETTEER}')
        WHERE source = 'ngib'
    """).fetchall()
    by_norm_island: dict[tuple[str, str], dict] = {}
    by_id: dict[str, dict] = {}
    for rid, sp, nm, mun, isl, lt, lon, lat in ngib_rows:
        key = (nm, isl or '')
        rec = {
            'id': rid, 'spelling': sp, 'normalized': nm,
            'municipality': mun, 'island': isl, 'local_type': lt,
            'lon': lon, 'lat': lat,
        }
        # Prefer the Municipi row when there's a homonym on the same island.
        prev = by_norm_island.get(key)
        if prev is None or (
            (lt or '').startswith('Municipi') and
            not (prev.get('local_type') or '').startswith('Municipi')
        ):
            by_norm_island[key] = rec
        by_id[rid] = rec
    print(f'  {len(by_norm_island):,} (norm, island) keys', file=sys.stderr)

    # Load Wikidata items.
    print('Reading Wikidata items…', file=sys.stderr)
    wd_items = [json.loads(l) for l in WD_JSONL.open()]
    print(f'  {len(wd_items):,} items', file=sys.stderr)

    # Anchor each WD item to an NGIB id.
    anchored = 0
    skipped = 0
    out_rows: dict[tuple[str, str], dict] = {}  # (normalized, ngib_id) → row

    # Track every (normalized, ngib_id) already present in the gazetteer
    # so we do not re-emit them with source='wikidata' (duplicate noise).
    existing_keys = set()
    for nm, isl in con.sql(f"""
        SELECT normalized, id FROM read_parquet('{GAZETTEER}')
    """).fetchall():
        existing_keys.add((nm, isl))

    for item in wd_items:
        island = item['island']
        # 1) Try the Catalan label first.
        anchor_label = item.get('label_ca') or item.get('label_es') or item.get('label_en')
        if not anchor_label:
            skipped += 1
            continue
        anchor_norm = normalize(anchor_label)
        ngib = by_norm_island.get((anchor_norm, island))
        if not ngib and item.get('label_es'):
            ngib = by_norm_island.get((normalize(item['label_es']), island))
        if not ngib and item.get('label_en'):
            ngib = by_norm_island.get((normalize(item['label_en']), island))
        if not ngib:
            skipped += 1
            continue
        anchored += 1

        # 2) Emit alias rows for every label/alias distinct from the
        # canonical NGIB spelling.
        candidates: list[str] = []
        for s in [
            item.get('label_es'), item.get('label_en'), item.get('label_ca'),
            *(item.get('aliases_ca') or []),
            *(item.get('aliases_es') or []),
            *(item.get('aliases_en') or []),
        ]:
            if s:
                candidates.append(s)

        # De-duplicate by normalized form within this item.
        seen_local = set()
        for s in candidates:
            nm = normalize(s)
            if not nm or len(nm) < 3:
                continue
            if nm == ngib['normalized']:
                # Already in NGIB under its canonical spelling — skip.
                continue
            if nm in seen_local:
                continue
            seen_local.add(nm)
            key = (nm, ngib['id'])
            if key in existing_keys:
                continue
            # If two Wikidata items both anchor to the same NGIB and
            # both have the same alias, keep the first.
            if key in out_rows:
                continue
            out_rows[key] = {
                'id':            ngib['id'],
                'spelling':      s,
                'normalized':    nm,
                'tokens':        nm,
                'first_token':   nm.split()[0] if nm else nm,
                'municipality':  ngib['municipality'],
                'island':        ngib['island'],
                'local_type':    ngib['local_type'],
                'is_settlement': (ngib['local_type'] or '').startswith('Municipi'),
                'lon':           ngib['lon'],
                'lat':           ngib['lat'],
                'source':        'wikidata',
            }

    print(f'\n  anchored: {anchored:,} / {len(wd_items):,} Wikidata items',
          file=sys.stderr)
    print(f'  skipped:  {skipped:,} (no NGIB match found by label)',
          file=sys.stderr)
    print(f'  emitting: {len(out_rows):,} new alias rows',
          file=sys.stderr)

    # Write parquet via duckdb.
    rows_list = list(out_rows.values())
    cols = [
        'id VARCHAR', 'spelling VARCHAR', 'normalized VARCHAR',
        'tokens VARCHAR', 'first_token VARCHAR',
        'municipality VARCHAR', 'island VARCHAR', 'local_type VARCHAR',
        'is_settlement BOOLEAN', 'lon DOUBLE', 'lat DOUBLE',
        'source VARCHAR',
    ]
    con.execute(f"CREATE TABLE out ({', '.join(cols)})")
    con.executemany(
        "INSERT INTO out VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(r[k] for k in [
            'id','spelling','normalized','tokens','first_token',
            'municipality','island','local_type','is_settlement',
            'lon','lat','source']) for r in rows_list]
    )
    con.execute(f"COPY out TO '{OUT}' (FORMAT 'parquet')")
    print(f'\n  → {OUT.relative_to(ROOT)}', file=sys.stderr)


if __name__ == '__main__':
    main()
