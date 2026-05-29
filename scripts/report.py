#!/usr/bin/env python3
"""Audit report for the linkage stage.

Reads ``db/meta.duckdb`` and emits three TSVs under ``data/reports/``
that the user can scan for matching quality before declaring v1 done:

    - reports/coverage.tsv          per-source link rate + method breakdown
    - reports/in_all_sources.tsv    places that appear in every source
    - reports/low_confidence.tsv    sample of fuzzy matches under 0.95
    - reports/unlinked.tsv          every entry the matcher could not bind
"""
from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'db' / 'meta.duckdb'
REPORTS = ROOT / 'data' / 'reports'

LOW_CONF_SAMPLE = 50


def write_tsv(name: str, header: list[str], rows: list[tuple]):
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(header)
        w.writerows(rows)
    print(f'  → {path.relative_to(ROOT)}  ({len(rows)} rows)', file=sys.stderr)


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # ---- coverage (v2 dual-link) -------------------------------------------
    cov = con.sql("""
        SELECT
            source_project AS source,
            count(*)                                                       AS total,
            count_if(describes_ngib_id IS NOT NULL)                        AS describes,
            count_if(parent_ngib_id IS NOT NULL)                           AS parent,
            count_if(entry_kind = 'municipality')                          AS k_munic,
            count_if(entry_kind = 'feature_with_ngib')                     AS k_feat_w,
            count_if(entry_kind = 'feature_no_ngib')                       AS k_feat_n,
            count_if(entry_kind = 'jurisdictional')                        AS k_jur,
            count_if(describes_ngib_id IS NULL AND parent_ngib_id IS NULL) AS orphans
        FROM entries GROUP BY source_project ORDER BY source_project
    """).fetchall()
    write_tsv('coverage.tsv',
              ['source', 'total', 'describes', 'parent',
               'municipality', 'feature_with_ngib', 'feature_no_ngib',
               'jurisdictional', 'orphans'],
              cov)

    # ---- places present in many sources (counted on describes_ngib_id) ----
    in_all = con.sql("""
        SELECT
            p.ngib_id, p.name_catalan, p.island, p.municipality,
            p.local_type,
            count(DISTINCT e.source_project) AS sources_count,
            string_agg(DISTINCT e.source_project, ',' ORDER BY e.source_project) AS sources
        FROM places p
        JOIN entries e ON e.describes_ngib_id = p.ngib_id
        GROUP BY p.ngib_id, p.name_catalan, p.island, p.municipality, p.local_type
        HAVING sources_count >= 4
        ORDER BY sources_count DESC, p.name_catalan
    """).fetchall()
    write_tsv('in_all_sources.tsv',
              ['ngib_id', 'name_catalan', 'island', 'municipality',
               'local_type', 'sources_count', 'sources'], in_all)

    # ---- top places by article count (entries that describe them) ---------
    top = con.sql("""
        SELECT p.ngib_id, p.name_catalan, p.local_type, p.island,
               count(*) AS entries
        FROM places p
        JOIN entries e ON e.describes_ngib_id = p.ngib_id
        GROUP BY p.ngib_id, p.name_catalan, p.local_type, p.island
        ORDER BY entries DESC LIMIT 30
    """).fetchall()
    write_tsv('top_places.tsv',
              ['ngib_id', 'name_catalan', 'local_type', 'island', 'entries'], top)

    # ---- low-confidence describes samples ----------------------------------
    low = con.sql("""
        SELECT
            e.source_project, e.source_id, e.title, e.island,
            e.entry_kind, e.describes_method, e.describes_confidence,
            p.name_catalan, p.local_type
        FROM entries e
        LEFT JOIN places p ON p.ngib_id = e.describes_ngib_id
        WHERE e.describes_ngib_id IS NOT NULL
          AND e.describes_confidence < 0.95
        ORDER BY e.describes_confidence ASC, e.source_project, e.title
    """).fetchall()
    if len(low) > LOW_CONF_SAMPLE:
        random.seed(20260528)
        sample = random.sample(low, LOW_CONF_SAMPLE)
        sample.sort(key=lambda r: (r[6], r[0]))
    else:
        sample = low
    write_tsv('low_confidence_sample.tsv',
              ['source_project', 'source_id', 'title', 'island',
               'entry_kind', 'describes_method', 'describes_confidence',
               'matched_name', 'matched_type'],
              sample)

    # ---- orphans (no describes, no parent) ---------------------------------
    orphans = con.sql("""
        SELECT source_project, source_id, title, entry_kind,
               island, place_type, municipality, source_url
        FROM entries
        WHERE describes_ngib_id IS NULL AND parent_ngib_id IS NULL
        ORDER BY source_project, title
    """).fetchall()
    write_tsv('orphans.tsv',
              ['source_project', 'source_id', 'title', 'entry_kind',
               'island', 'place_type', 'municipality', 'source_url'], orphans)

    print('\nQuality cues:', file=sys.stderr)
    # in_all columns: ngib_id, name_catalan, island, municipality,
    #                 local_type, sources_count, sources
    n5 = sum(1 for r in in_all if r[5] == 5)
    n4 = sum(1 for r in in_all if r[5] == 4)
    print(f'  places attested in 5 sources: {n5}', file=sys.stderr)
    print(f'  places attested in 4 sources: {n4}', file=sys.stderr)
    print(f'  total low-confidence: {len(low)}, sampled: {len(sample)}', file=sys.stderr)
    print(f'  total orphans:        {len(orphans)}', file=sys.stderr)


if __name__ == '__main__':
    main()
