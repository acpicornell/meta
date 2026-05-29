#!/usr/bin/env python3
"""Adapt the five sibling JSONs into a uniform place-mention JSONL.

Reads from ``data/sources/`` and emits one
``data/normalized/<source>.jsonl`` per sibling. Every line is a single
place mention with the same schema:

    source_project, source_id, source_year, title, title_norm,
    place_type, island, municipality, source_url,
    hint_lat, hint_lon, hint_match, raw,
    is_supplement, entry_kind_hint, parent_municipality_hint

The two new ``*_hint`` fields are part of the v2 dual-link model:
``resolve_entities.py`` uses them as the starting point for the
3-phase resolution (kind → parent → describes).

The ``raw`` field carries the unmodified original entry — the matcher
ignores it, but ``export_web_data.py`` ships it as the entry blob so
the meta website can render the full sibling article verbatim.
"""
from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / 'data' / 'sources'
NORMALIZED = ROOT / 'data' / 'normalized'

# Hint vocabulary handed to the resolver (Phase 1 decides the final
# entry_kind on this basis + the actual NGIB lookup):
#   'jurisdictional' — place_type matches an administrative concept with
#       no modern NGIB equivalent (cuartón, término, partido judicial,
#       diócesis…). The resolver forces entry_kind='jurisdictional'.
#   'settlement'     — place_type is settlement-ish (villa, lugar,
#       ciudad, aldea, parroquia…). The resolver tries 'municipality'
#       first, falls back to 'feature_*' if title isn't a Municipi name.
#   'feature'        — place_type is a specific feature type (predio,
#       cala, isla, cabo, peñas…). The resolver tries 'feature_with_ngib'
#       within the parent municipi, falls back to 'feature_no_ngib'.
#   'unknown'        — no informative place_type. Resolver defaults to
#       'settlement' handling.

# Per-sibling deployed origin. Used to build deep links when the
# sibling does not already publish a stable ia_url / bdcyl_url.
SIBLING_ORIGIN = {
    'floridablanca':   'https://floridablanca-balears.pages.dev',
    'minano':          'https://minano-balears.pages.dev',
    'madoz':           'https://madoz.pages.dev',
    'nomenclator_1860':'https://nomenclator-1860-balears.pages.dev',
    'riera':           'https://riera-balears.pages.dev',
}

FORMENTERA_FB_NAMES = {
    'FORMENTERA', 'EL PILAR DE LA MOLA', 'PILAR DE LA MOLA',
    'SAN FRANCISCO JAVIER', 'SAN FERNANDO',
}

# 1860 judicial-district → island. Eivissa district straddles
# Formentera; we override per-row by municipality.
JD_1860 = {
    'Palma':   'Mallorca',
    'Inca':    'Mallorca',
    'Manacor': 'Mallorca',
    'Mahon':   'Menorca',
    'Ibiza':   'Eivissa',
}

# Canonicalise the island label across siblings: NGIB and meta use the
# Catalan ``Eivissa`` exclusively. ``Baleares`` (archipelago-wide
# articles) is collapsed to ``None`` so the matcher does not enforce
# island scope on those entries.
ISLAND_CANONICAL = {
    'Ibiza':    'Eivissa',
    'Eivissa':  'Eivissa',
    'Mallorca': 'Mallorca',
    'Menorca':  'Menorca',
    'Formentera': 'Formentera',
    'Cabrera':  'Cabrera',
    'Baleares': None,
    'Balears':  None,
    '':         None,
    None:       None,
}


def canon_island(v):
    return ISLAND_CANONICAL.get(v, v)


sys.path.insert(0, str(ROOT / 'scripts'))
from resolve_entities import (    # type: ignore  # noqa: E402
    classify_place_type, NGIB_SETTLEMENT, NGIB_POSSESSION,
)


def entry_kind_hint(place_type: str | None) -> str:
    """Map a sibling place_type string to a coarse hint for the
    resolver. The resolver makes the final entry_kind decision."""
    if not place_type:
        return 'unknown'
    preferred_types, force_unlinked = classify_place_type(place_type)
    if force_unlinked:
        return 'jurisdictional'
    if preferred_types is NGIB_SETTLEMENT:
        return 'settlement'
    return 'feature'


def normalize(s: str) -> str:
    """Lightweight normaliser used as ``title_norm`` for fast match
    indexing later. The matcher in ``resolve_entities.py`` reuses
    ``build_gazetteer.normalize`` for the final decision, but we
    precompute a cheap version here for shortlist queries."""
    if not s:
        return ''
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.upper().strip()
    for ch in '.,;:¡¿!?()[]{}«»"\'`/\\-':
        s = s.replace(ch, ' ')
    return ' '.join(s.split())


def write_jsonl(path: Path, rows):
    """Write one JSON object per line. The ``raw`` field is serialised
    as a JSON-encoded string so DuckDB sees it as VARCHAR and does not
    unify struct schemas across sibling files (which would otherwise
    add every field from every source to every blob)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for r in rows:
            r = dict(r)
            if 'raw' in r:
                r['raw'] = json.dumps(r['raw'], ensure_ascii=False, separators=(',', ':'))
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
            f.write('\n')


# ---------- adapters ----------------------------------------------------------

def adapt_floridablanca() -> list[dict]:
    data = json.load((SOURCES / 'floridablanca.json').open())
    # Build a parent-cod lookup so parishes (Pq.) inherit the modern
    # municipality name of their parent pueblo.
    name_by_cod = {p['cod']: p.get('name_current') for p in data['pueblos']}
    out = []
    for p in data['pueblos']:
        district = p.get('district')
        if district == 'MAL':
            island = 'Mallorca'
        elif district == 'MEN':
            island = 'Menorca'
        elif district == 'IBI':
            island = 'Formentera' if (p.get('name_current') or '').upper() in FORMENTERA_FB_NAMES else 'Eivissa'
        else:
            island = None
        title = p.get('name_current') or p.get('name_1787') or ''
        out.append({
            'source_project': 'floridablanca',
            'source_id':      str(p['cod']),
            'source_year':    1787,
            'title':          title,
            'title_norm':     normalize(title),
            'place_type':     p.get('category_label'),
            'island':         canon_island(island),
            'municipality':   None,   # Floridablanca pueblos ARE the
                                      # municipalities at that date.
            'source_url':     f'{SIBLING_ORIGIN["floridablanca"]}/#cod-{p["cod"]}',
            'hint_lat':       None,
            'hint_lon':       None,
            'hint_match':     None,
            'is_supplement':  False,
            # Floridablanca pueblos ARE the municipality at that date,
            # so the entry's own modern name doubles as the parent
            # municipality hint. The resolver collapses the two when
            # entry_kind=='municipality'.
            'entry_kind_hint':         entry_kind_hint(p.get('category_label')),
            # Parishes (Pq.) sit inside their parent pueblo (parent_cod);
            # everything else IS its own municipality.
            'parent_municipality_hint':(
                name_by_cod.get(p['parent_cod'])
                if p.get('parent_cod')
                else p.get('name_current')
            ),
            'raw':            p,
        })
    return out


def adapt_minano() -> list[dict]:
    data = json.load((SOURCES / 'minano.json').open())
    out = []
    for e in data['entries']:
        title = e.get('title') or ''
        # Tom XI (1829) is the Suplemento. Either the volume number or
        # an explicit "(adición)" in the title flags it.
        vol = (e.get('vol') or '').strip()
        is_supplement = (vol == '11') or 'adici' in title.lower()
        out.append({
            'source_project': 'minano',
            'source_id':      str(e['id']),
            'source_year':    1826,
            'title':          title,
            'title_norm':     normalize(title),
            'place_type':     e.get('place_type'),
            'island':         canon_island(e.get('island')),
            'municipality':   e.get('municipality'),
            'source_url':     e.get('ia_url') or f'{SIBLING_ORIGIN["minano"]}/#entry-{e["id"]}',
            'hint_lat':       e.get('lat'),
            'hint_lon':       e.get('lon'),
            'hint_match':     e.get('matched_toponym'),
            'is_supplement':  is_supplement,
            # Miñano carries neither an explicit municipality field nor
            # a cross_references list, so the parent must usually be
            # inferred later (LLM or absent → orphan). The exception is
            # when place_type implies the entry IS its own municipality.
            'entry_kind_hint':         entry_kind_hint(e.get('place_type')),
            'parent_municipality_hint':None,
            'raw':            e,
        })
    return out


def adapt_madoz() -> list[dict]:
    data = json.load((SOURCES / 'madoz.json').open())
    out = []
    for e in data['entries']:
        title = e.get('title') or ''
        # Madoz Tom XVI (1850) is the Adiciones volume.
        vol = (e.get('vol') or '').strip()
        is_supplement = (vol == '16')
        out.append({
            'source_project': 'madoz',
            'source_id':      str(e['id']),
            'source_year':    1845,
            'title':          title,
            'title_norm':     normalize(title),
            'place_type':     e.get('place_type'),
            'island':         canon_island(e.get('island')),
            'municipality':   e.get('municipality'),
            'source_url':     e.get('ia_url') or f'{SIBLING_ORIGIN["madoz"]}/#entry-{e["id"]}',
            'hint_lat':       None,
            'hint_lon':       None,
            'hint_match':     None,
            'is_supplement':  is_supplement,
            # Madoz has explicit ``municipality`` for most sub-features;
            # for the rest, cross_references[0] usually names the parent
            # village.
            'entry_kind_hint':         entry_kind_hint(e.get('place_type')),
            'parent_municipality_hint':(
                e.get('municipality')
                or (e.get('cross_references') or [None])[0]
            ),
            'raw':            e,
        })
    return out


def adapt_nomenclator_1860() -> list[dict]:
    """1860 entries; skip is_municipality_total / is_district_total
    summary rows. Derive island from judicial_district, overriding
    Ibiza→Formentera when the municipality is FORMENTERA."""
    data = json.load((SOURCES / 'nomenclator_1860' / 'entries.json').open())
    out = []
    for e in data:
        if e.get('is_municipality_total') or e.get('is_district_total'):
            continue
        jd = e.get('judicial_district')
        island = JD_1860.get(jd)
        if jd == 'Ibiza' and (e.get('municipality') or '').upper() == 'FORMENTERA':
            island = 'Formentera'
        title = e.get('place') or e.get('municipality') or ''
        out.append({
            'source_project': 'nomenclator_1860',
            'source_id':      str(e['id']),
            'source_year':    1860,
            'title':          title,
            'title_norm':     normalize(title),
            'place_type':     e.get('place_class') or e.get('class_normalized'),
            'island':         canon_island(island),
            'municipality':   e.get('municipality'),
            'source_url':     f'{SIBLING_ORIGIN["nomenclator_1860"]}/#entry-{e["id"]}',
            'hint_lat':       None,
            'hint_lon':       None,
            'hint_match':     None,
            'is_supplement':  False,
            # The 1860 schema is 100% sub-municipal: every row carries
            # an explicit ``municipality`` field, so the parent is
            # always known. The actual entry kind depends on whether
            # ``place`` is the municipality name (rare) or a sub-feature.
            'entry_kind_hint':         entry_kind_hint(
                e.get('place_class') or e.get('class_normalized')
            ),
            'parent_municipality_hint':e.get('municipality'),
            'raw':            e,
        })
    return out


def adapt_riera() -> list[dict]:
    data = json.load((SOURCES / 'riera.json').open())
    out = []
    for e in data['entries']:
        title = e.get('title') or ''
        # Riera is a single-volume publication, but in-text addicional
        # entries serve the same role as Miñano's Tom XI: an update or
        # correction appended to the main article. Mark them so they
        # sort after the primary entry within the same year.
        tl = title.lower()
        is_supplement = ('addicion' in tl) or ('adicional' in tl) or ('(adici' in tl)
        out.append({
            'source_project': 'riera',
            'source_id':      str(e['id']),
            'source_year':    1881,
            'title':          title,
            'title_norm':     normalize(title),
            'place_type':     e.get('place_type'),
            'island':         canon_island(e.get('island')),
            'municipality':   e.get('municipality'),
            'source_url':     e.get('bdcyl_url') or f'{SIBLING_ORIGIN["riera"]}/#entry-{e["id"]}',
            'hint_lat':       e.get('lat'),
            'hint_lon':       e.get('lon'),
            'hint_match':     e.get('matched_toponym'),
            'is_supplement':  is_supplement,
            'entry_kind_hint':         entry_kind_hint(e.get('place_type')),
            'parent_municipality_hint':(
                e.get('municipality')
                or (e.get('cross_references') or [None])[0]
            ),
            'raw':            e,
        })
    return out


# ---------- driver ------------------------------------------------------------

ADAPTERS = {
    'floridablanca':    adapt_floridablanca,
    'minano':           adapt_minano,
    'madoz':            adapt_madoz,
    'nomenclator_1860': adapt_nomenclator_1860,
    'riera':            adapt_riera,
}


def main():
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, fn in ADAPTERS.items():
        rows = fn()
        out_path = NORMALIZED / f'{name}.jsonl'
        write_jsonl(out_path, rows)
        # quick stats
        by_island = {}
        for r in rows:
            by_island[r['island']] = by_island.get(r['island'], 0) + 1
        print(
            f'  {name:18s} {len(rows):>5d} rows  '
            f'→ {out_path.relative_to(ROOT)}  ({by_island})',
            file=sys.stderr,
        )
        total += len(rows)
    print(f'Total: {total:,} place mentions across 5 sources.', file=sys.stderr)


if __name__ == '__main__':
    main()
