#!/usr/bin/env python3
"""Bulk-load the meta DuckDB from the JSONL pipeline outputs (v2 dual-link).

Reads:
    db/schema.sql
    data/normalized/*.jsonl   (with entry_kind_hint + parent_municipality_hint)
    data/place_links.jsonl    (with describes_ngib_id + parent_ngib_id)
    data/gazetteer.parquet    (for the canonical NGIB rows)

Writes:
    db/meta.duckdb  — fresh on every run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'db' / 'meta.duckdb'
SCHEMA = ROOT / 'db' / 'schema.sql'
NORMALIZED_DIR = ROOT / 'data' / 'normalized'
LINKS_PATH = ROOT / 'data' / 'place_links.jsonl'
GAZETTEER = ROOT / 'data' / 'gazetteer.parquet'


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA.read_text())

    all_paths = sorted(NORMALIZED_DIR.glob('*.jsonl'))
    if not all_paths:
        print('No normalized files found.', file=sys.stderr)
        sys.exit(1)

    paths_sql = ', '.join(f"'{p}'" for p in all_paths)
    con.execute(f"""
        CREATE TEMP TABLE _norm AS
        SELECT * FROM read_json_auto([{paths_sql}], format='newline_delimited');
    """)
    con.execute(f"""
        CREATE TEMP TABLE _links AS
        SELECT * FROM read_json_auto('{LINKS_PATH}', format='newline_delimited');
    """)

    con.execute("""
        INSERT INTO entries (
            id, source_project, source_id, source_year,
            title, title_norm, place_type, island, municipality,
            source_url, is_supplement,
            entry_kind,
            describes_ngib_id, parent_ngib_id,
            describes_method, parent_method,
            describes_confidence, parent_confidence,
            blob
        )
        SELECT
            row_number() OVER (ORDER BY n.source_project, n.source_id) AS id,
            n.source_project,
            n.source_id,
            n.source_year,
            n.title,
            n.title_norm,
            n.place_type,
            n.island,
            n.municipality,
            n.source_url,
            COALESCE(n.is_supplement, FALSE),
            l.entry_kind,
            l.describes_ngib_id,
            l.parent_ngib_id,
            l.describes_method,
            l.parent_method,
            l.describes_confidence,
            l.parent_confidence,
            n.raw
        FROM _norm n
        LEFT JOIN _links l USING (source_project, source_id)
    """)

    # Audit log.
    con.execute("""
        INSERT INTO entry_resolution_log (
            source_project, source_id,
            parent_ngib_id, parent_method, parent_confidence,
            parent_candidate_ids, parent_candidate_scores,
            describes_ngib_id, describes_method, describes_confidence,
            describes_candidate_ids, describes_candidate_scores,
            entry_kind, notes
        )
        SELECT
            source_project, source_id,
            parent_ngib_id, parent_method, parent_confidence,
            parent_candidate_ngib_ids,
            CAST(NULL AS DOUBLE[]),
            describes_ngib_id, describes_method, describes_confidence,
            describes_candidate_ngib_ids,
            CAST(NULL AS DOUBLE[]),
            entry_kind, notes
        FROM _links
    """)

    # Places: every NGIB id referenced by either parent or describes,
    # picked from the gazetteer with one row per id (Municipi/Capital
    # preference, prefer coord-bearing row).
    con.execute(f"""
        INSERT INTO places (ngib_id, name_catalan, municipality, island,
                            local_type, lat, lng)
        SELECT ngib_id, name_catalan, municipality, island, local_type, lat, lng
        FROM (
            SELECT
                CAST(g.id AS VARCHAR)        AS ngib_id,
                g.spelling                   AS name_catalan,
                g.municipality               AS municipality,
                g.island                     AS island,
                g.local_type                 AS local_type,
                g.lat                        AS lat,
                g.lon                        AS lng,
                row_number() OVER (
                    PARTITION BY g.id
                    ORDER BY CASE WHEN g.local_type LIKE 'Municipi%' THEN 0
                                  WHEN g.local_type LIKE 'Capital%'   THEN 1
                                  WHEN g.local_type LIKE 'Vila%'      THEN 2
                                  ELSE 3 END,
                             CASE WHEN g.lat IS NOT NULL THEN 0 ELSE 1 END
                ) AS rn
            FROM read_parquet('{GAZETTEER}') g
            WHERE g.source = 'ngib'
              AND g.id IN (
                  SELECT DISTINCT describes_ngib_id FROM entries
                  WHERE describes_ngib_id IS NOT NULL
                  UNION
                  SELECT DISTINCT parent_ngib_id FROM entries
                  WHERE parent_ngib_id IS NOT NULL
              )
        )
        WHERE rn = 1
    """)

    n_entries  = con.sql('SELECT count(*) FROM entries').fetchone()[0]
    n_describes = con.sql('SELECT count(*) FROM entries WHERE describes_ngib_id IS NOT NULL').fetchone()[0]
    n_parents  = con.sql('SELECT count(*) FROM entries WHERE parent_ngib_id IS NOT NULL').fetchone()[0]
    n_places   = con.sql('SELECT count(*) FROM places').fetchone()[0]
    by_kind    = con.sql(
        "SELECT entry_kind, count(*) FROM entries GROUP BY entry_kind"
    ).fetchall()
    pct_d = n_describes * 100 / n_entries if n_entries else 0
    pct_p = n_parents * 100 / n_entries if n_entries else 0
    print(f'  entries:           {n_entries:>5d}', file=sys.stderr)
    print(f'  with describes:    {n_describes:>5d}  ({pct_d:.1f}%)', file=sys.stderr)
    print(f'  with parent:       {n_parents:>5d}  ({pct_p:.1f}%)', file=sys.stderr)
    print(f'  places (NGIB ids): {n_places:>5d}', file=sys.stderr)
    print('  entries by kind:', file=sys.stderr)
    for k, n in by_kind:
        print(f'    {k:<25s} {n:>5d}', file=sys.stderr)
    print(f'\n  → {DB_PATH.relative_to(ROOT)}', file=sys.stderr)
    con.close()


if __name__ == '__main__':
    main()
