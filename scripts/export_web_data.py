#!/usr/bin/env python3
"""Pivot the DuckDB by NGIB place and write web/data.json (v2 dual-link).

Each canonical NGIB id referenced by either describes_ngib_id OR
parent_ngib_id becomes its own ``places[]`` entry. For each place:

  - ``entries[]`` — articles whose ``describes_ngib_id`` equals this
    place (the timeline that's specifically about this entity).
  - ``child_places[]`` — only when this place is a Municipi: every
    sub-feature NGIB place inside the terme that has at least one
    entry referring to it. Renders as a compact list (link to the
    sub-feature's own page).
  - ``minor_entries[]`` — only when this is a Municipi: entries with
    ``parent_ngib_id`` == this AND ``describes_ngib_id`` IS NULL AND
    entry_kind == 'feature_no_ngib' (predios genèrics, casas de labor,
    sub-features sense identitat NGIB).
  - ``jurisdictional_entries[]`` — only when this is a Municipi:
    entries with ``parent_ngib_id`` == this AND entry_kind ==
    'jurisdictional' (cuartones, partidos judiciales, diòcesis, …).

Plus ``orphans``: entries with no parent and no describes — typically
archipelago-wide articles (BALEARES, MALLORCA-isla) that did not
anchor to anything.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Strip Miñano Tom XI / Madoz Tom XVI / Riera supplement markers
# from a title when computing the variants list. The supplement
# itself is already flagged with the is_supplement badge on each
# timeline card, so the parenthetical marker is redundant noise in
# the place-header variants line.
_SUPP_TITLE_RX = re.compile(
    # Accept one or two 'd's (Miñano «(adición)», Riera
    # «(addicional)»), and the bare Catalan «(addició)».
    r'\s*\(\s*ad+ici[oó]n(?:al)?\s*\)\s*$|'
    r'\s*\(\s*ad+ici[oó]\s*\)\s*$',
    re.IGNORECASE,
)


def strip_supp_suffix(t: str) -> str:
    if not t:
        return ''
    return _SUPP_TITLE_RX.sub('', t).strip()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'db' / 'meta.duckdb'
OUT = ROOT / 'web' / 'data.json'
OUT_BLOBS = ROOT / 'web' / 'data-blobs.json'


def confidence_band(c: float) -> str:
    if c >= 0.95:
        return 'alta'
    if c >= 0.88:
        return 'mitjana'
    if c > 0:
        return 'baixa'
    return 'sense'


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)

    # ---- canonical places (everything referenced as parent OR describes) ----
    places_rows = con.sql("""
        SELECT ngib_id, name_catalan, municipality, island, local_type, lat, lng
        FROM places
        ORDER BY name_catalan
    """).fetchall()

    # ---- all entries -----------------------------------------------------
    entry_rows = con.sql("""
        SELECT
            id, source_project, source_id, source_year,
            title, place_type, island, municipality, source_url,
            entry_kind,
            describes_ngib_id, parent_ngib_id,
            describes_method, parent_method,
            describes_confidence, parent_confidence,
            is_supplement, blob
        FROM entries
        ORDER BY describes_ngib_id, source_year, is_supplement, source_project
    """).fetchall()

    # Index entries by describes and by parent.
    by_describes: dict[str, list[dict]] = defaultdict(list)
    by_parent_kind: dict[tuple[str, str], list[dict]] = defaultdict(list)
    blobs: dict[str, object] = {}
    by_source_total = defaultdict(int)
    by_source_describes_linked = defaultdict(int)
    by_source_parent_linked = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    orphans = defaultdict(list)

    for (eid, src, sid, year, title, ptype, island, mun, url,
         kind, d_ngib, p_ngib, d_method, p_method, d_conf, p_conf,
         is_supp, blob_str) in entry_rows:
        by_source_total[src] += 1
        key = f'{src}:{sid}'
        blobs[key] = json.loads(blob_str) if isinstance(blob_str, str) else blob_str

        entry_view = {
            'year': year,
            'source': src,
            'source_id': sid,
            'title': title,
            'place_type': ptype,
            'source_url': url,
            'entry_kind': kind,
            'describes_method': d_method,
            'describes_confidence': round(d_conf or 0.0, 3),
            'describes_band': confidence_band(d_conf or 0.0),
            'parent_method': p_method,
            'parent_confidence': round(p_conf or 0.0, 3),
            'parent_band': confidence_band(p_conf or 0.0),
            'parent_ngib_id': p_ngib,
            'is_supplement': bool(is_supp),
        }

        by_kind[kind] += 1
        if d_ngib:
            by_source_describes_linked[src] += 1
            by_describes[d_ngib].append(entry_view)
        if p_ngib:
            by_source_parent_linked[src] += 1
            by_parent_kind[(p_ngib, kind)].append(entry_view)

        if not d_ngib and not p_ngib:
            orphans[src].append({
                'source_id': sid,
                'title': title,
                'place_type': ptype,
                'island': island,
                'municipality': mun,
                'source_url': url,
                'entry_kind': kind,
            })

    # ---- assemble places list -------------------------------------------
    # Index place metadata by ngib_id for child_places lookup.
    place_meta = {ngib_id: dict(zip(
        ['ngib_id','name','municipality','island','local_type','lat','lng'],
        row,
    )) for row, ngib_id in (
        (r, r[0]) for r in places_rows
    )}

    # For each Municipi place we need to know which child NGIB places
    # (those whose municipality == this municipi's name) are referenced
    # by at least one entry (as describes or as parent).
    referenced_ngib_ids = set(by_describes.keys()) | {
        k[0] for k in by_parent_kind.keys()
    }
    children_by_municipality_name: dict[str, list[str]] = defaultdict(list)
    for ngib_id, p in place_meta.items():
        if p['local_type'] == 'Municipi':
            continue          # the Municipi itself is not its own child
        if ngib_id in referenced_ngib_ids:
            mun_name = p.get('municipality')
            if mun_name:
                children_by_municipality_name[mun_name].append(ngib_id)

    places = []
    place_types: set[str] = set()
    for (ngib_id, name, mun, island, local_type, lat, lng) in places_rows:
        ents = list(by_describes.get(ngib_id, []))
        minor_ents = list(by_parent_kind.get((ngib_id, 'feature_no_ngib'), []))
        jurisdictional_ents = list(by_parent_kind.get((ngib_id, 'jurisdictional'), []))
        if not ents and not minor_ents and not jurisdictional_ents:
            # An NGIB id can only show up here if some entry has it as
            # parent OR describes. If we filtered to entries-with-content
            # this should always be non-empty. Defensive skip.
            continue
        place_types.add(local_type or '')

        # Variants are titles of entries that NAME this entity.
        # Drop supplement-marker suffixes ((adición), (addicional),
        # (addició)) so e.g. Miñano "ALGAIDA (adición)" and Riera
        # "ALGAIDA (addicional)" collapse with the primary "ALGAIDA"
        # into a single canonical variant.
        variants = sorted({
            strip_supp_suffix(e['title']) for e in ents if e['title']
        } - {''})

        # Child places (only for Municipi).
        child_places = []
        if local_type == 'Municipi' and name:
            for child_id in children_by_municipality_name.get(name, []):
                cm = place_meta.get(child_id)
                if not cm:
                    continue
                entry_count = len(by_describes.get(child_id, []))
                child_places.append({
                    'ngib_id':    child_id,
                    'name':       cm['name'],
                    'local_type': cm['local_type'],
                    'entry_count':entry_count,
                })
            child_places.sort(key=lambda c: (-c['entry_count'], c['name']))

        # For sub-feature places, expose a breadcrumb to the parent
        # municipality.
        breadcrumb = None
        if local_type != 'Municipi' and mun:
            # Find the Municipi NGIB id for this sub-feature.
            for pid, pm in place_meta.items():
                if pm['local_type'] == 'Municipi' and pm['name'] == mun:
                    breadcrumb = {'ngib_id': pid, 'name': pm['name']}
                    break

        places.append({
            'ngib_id':                ngib_id,
            'name':                   name,
            'municipality':           mun,
            'island':                 island,
            'local_type':             local_type,
            'lat':                    lat,
            'lng':                    lng,
            'variants':               variants,
            'breadcrumb_parent':      breadcrumb,
            'entries':                ents,
            'child_places':           child_places,
            'minor_entries':          minor_ents,
            'jurisdictional_entries': jurisdictional_ents,
        })

    totals = {
        'places':                  len(places),
        'entries_by_kind':         dict(by_kind),
        'orphan_count':            sum(len(v) for v in orphans.values()),
        'by_source': {
            src: {
                'total':            by_source_total[src],
                'describes_linked': by_source_describes_linked[src],
                'parent_linked':    by_source_parent_linked[src],
            }
            for src in sorted(by_source_total)
        },
    }

    out_obj = {
        'generated_with': 'scripts/export_web_data.py (v2)',
        'generated_at':   datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'totals':         totals,
        'place_types':    sorted(t for t in place_types if t),
        'places':         places,
        'orphans':        dict(orphans),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', encoding='utf-8') as f:
        json.dump(out_obj, f, ensure_ascii=False, separators=(',', ':'))
    with OUT_BLOBS.open('w', encoding='utf-8') as f:
        json.dump(blobs, f, ensure_ascii=False, separators=(',', ':'))

    size_kb = OUT.stat().st_size / 1024
    blobs_kb = OUT_BLOBS.stat().st_size / 1024
    print(f'  places:           {totals["places"]:>5d}', file=sys.stderr)
    print(f'  entries by kind:  {totals["entries_by_kind"]}', file=sys.stderr)
    print(f'  orphan count:     {totals["orphan_count"]:>5d}', file=sys.stderr)
    print(f'  → {OUT.relative_to(ROOT)}        ({size_kb:>6.0f} KB)', file=sys.stderr)
    print(f'  → {OUT_BLOBS.relative_to(ROOT)} ({blobs_kb:>6.0f} KB, lazy-loaded)', file=sys.stderr)


if __name__ == '__main__':
    main()
