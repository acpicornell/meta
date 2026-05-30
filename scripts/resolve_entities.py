#!/usr/bin/env python3
"""Resolve every normalized place mention to an NGIB ID.

Input:
    data/normalized/<source>.jsonl  (produced by normalize_sources.py)
    data/gazetteer.parquet          (produced by build_gazetteer.py)

Output:
    data/place_links.jsonl  — one row per (source_project, source_id):
        ngib_id (nullable), method, confidence, candidate_ngib_ids,
        candidate_scores.

Design note (2026-05-28): meta does NOT reuse the NGIB matches that
sibling projects publish — the user has explicitly said those are too
noisy to anchor on (Miñano put 'PORMAÑY cuartón' at the modern Sant
Antoni Municipi coords; etc.). Every entry is re-resolved from
title + place_type + island + municipality.

The matcher is a waterfall — first hit wins:

    0. type_no_ngib_equivalent — sibling place_type belongs to a
                                 historical category (cuartón,
                                 despoblado, partido judicial,
                                 diócesis…) with no modern NGIB
                                 equivalent → force unlinked
                                 without trying string matches.
    1. historical_curated      — exact normalised match against a
                                 ``source='historical'`` row in the
                                 gazetteer (Mahón→Maó, San
                                 Lorenzo→Sant Llorenç…), scoped by
                                 island AND by type-compatibility
                                 with the sibling's place_type.
    2. exact_norm              — exact normalised match against any
                                 gazetteer row scoped by island and
                                 type.
    3. fuzzy_wratio            — rapidfuzz WRatio, score_cutoff=88,
                                 scoped by island and type.
    4. fuzzy_token_set         — token_set_ratio, score_cutoff=85,
                                 multi-word titles only.
    5. linked_to_parent        — title didn't match anything in the
                                 right type, but the declared
                                 municipality does match an NGIB
                                 Municipi row → link to the parent
                                 with a distinct method label (the
                                 UI surfaces these separately).
    6. unlinked                — visible in the meta UI as
                                 «Sense vincle NGIB».

If a step finds no match within the *preferred* (type-scoped) pool,
the matcher tries the same step on a *widened* pool (no type scope)
with a -0.05 confidence penalty. Cross-island widening is only used
when the entry has no declared island.

Run:
    uv run python scripts/resolve_entities.py
    uv run python scripts/resolve_entities.py --stats
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from build_gazetteer import normalize  # type: ignore

# Strip editorial / Madoz-style qualifiers from the title before
# matching. Examples handled:
#   "ALBERCUIX (cala de)"   → "ALBERCUIX"
#   "ALAYOR (addicional)"   → "ALAYOR"
#   "ADAYA ó DADAYA"        → "ADAYA"
#   "LLOSETA (adición)"     → "LLOSETA"
#   "Pq. de San Salvador"   → "San Salvador"
#   "SALINAS, LAS"          → "LAS SALINAS"
#   "CAMPOS DEL PUERTO"     → "CAMPOS DEL PUERTO"  (untouched)
_PAREN_RX = re.compile(r'\s*\([^)]*\)\s*')
_BRACKET_RX = re.compile(r'\s*\[[^\]]*\]\s*')
_OR_VARIANT_RX = re.compile(r'\s+ó\s+\S.*$', re.IGNORECASE)
_EDITORIAL_TRAIL_RX = re.compile(
    r'\s*[—–-]\s*(?:adici[oó]n(?:es)?|adicional|coordenades|estad[ií]sticas? de aceite)\s*$',
    re.IGNORECASE,
)
# After more aggressive cleaning, drop any free-form em-dash trailer.
# Note: we only match em/en-dashes with surrounding spaces, so a normal
# hyphen inside a toponym ("BINI-SAFAYA", "SAN-LORENZO") is untouched.
_TRAIL_DASH_PROSE_RX = re.compile(r'\s+[—–]\s+\S.*$')
_LEADING_PQ_RX = re.compile(r'^\s*Pq\.?\s+(?:de\s+)?', re.IGNORECASE)
_TRAIL_COMMA_ARTICLE_RX = re.compile(
    r'^(?P<head>.+?),\s+(?P<art>EL|LA|LOS|LAS|ELS|SES)\s*$',
    re.IGNORECASE,
)
# Leading topographic markers — for entries like "CABO BAJOLÍ", "ISLA
# DE IBIZA", "PUNTA DE LAS AGUILAS", "TORRE DEL CARREGADOR". When the
# title starts with one of these, NGIB usually stores the toponym
# without the topographic prefix (Bajolí, Eivissa, etc.). We strip the
# leading marker plus an optional "de [la/las/el/los/l']" article so
# fuzzy match has a chance.
_LEADING_FEATURE_RX = re.compile(
    r"""^\s*(?:CABO|CAP|ISLAS?|ISLOTES?|ISLETAS?|ILLES?|ILLOTS?|
              CALA|CALAS|MONTE|MONT|SIERRA|SERRA|PUIG|CERRO|
              TORRE|CASTILLO|FORTALEZA|FUERTE|ATALAYA|FARO|
              PUNTA|MORRO|PLAYA|PORT|PUERTO|EMBARCADERO|MOLL|
              PARROQUIA|ERMITA|SANTUARIO|ORATORIO|IGLESIA)
       \s+(?:DE(?:\s+(?:LA|LAS|EL|LOS|L'|L’))?\s+)?""",
    re.IGNORECASE | re.VERBOSE,
)


# Madoz files Catalan saints under the proper-name letter and tucks
# the saint qualifier in parens — "LUIS (San)" = Sant Lluís,
# "MARIA (Santa) del Camí" = Santa Maria del Camí, "ANTONIO (San)"
# = Sant Antoni. Reverse the order so the historical-variant table
# can match the Castilian "San X" form.
_SAINT_INVERSE_RX = re.compile(
    r'^\s*(?P<head>[^()]+?)\s*\(\s*(?P<saint>San|Santa|Sant|Sta\.?)\s*\)\s*(?P<tail>.*)$',
    re.IGNORECASE,
)


def _reverse_saint_inversion(t: str) -> str:
    m = _SAINT_INVERSE_RX.match(t)
    if not m:
        return t
    head = m.group('head').strip()
    saint = m.group('saint')
    tail = m.group('tail').strip()
    if not head:
        return t
    new = f'{saint} {head}'
    if tail:
        new = f'{new} {tail}'
    return new


def clean_title(t: str) -> str:
    if not t:
        return ''
    # Reverse Madoz's "X (San/Santa)" alphabetical filing BEFORE the
    # paren strip throws the saint marker away.
    t = _reverse_saint_inversion(t)
    t = _LEADING_PQ_RX.sub('', t)
    # Drop the explicit editorial trailers first, then any other dash
    # trailer (we do this before paren-stripping because some
    # parenthetical qualifiers come at the very end).
    t = _EDITORIAL_TRAIL_RX.sub('', t)
    t = _TRAIL_DASH_PROSE_RX.sub('', t)
    # «ADAYA ó DADAYA» → keep the head form only.
    t = _OR_VARIANT_RX.sub('', t)
    # Trailing parenthetical and bracketed qualifiers.
    t = _PAREN_RX.sub(' ', t).strip()
    t = _BRACKET_RX.sub(' ', t).strip()
    # «SALINAS, LAS» → «LAS SALINAS»
    m = _TRAIL_COMMA_ARTICLE_RX.match(t)
    if m:
        t = f"{m.group('art')} {m.group('head').strip()}"
    # Strip leading topographic marker (CABO / ISLA / PUNTA / …).
    t = _LEADING_FEATURE_RX.sub('', t).strip() or t
    return t.strip()

GAZETTEER = ROOT / 'data' / 'gazetteer.parquet'
WIKIDATA_VARIANTS = ROOT / 'data' / 'wikidata_variants.parquet'
NORMALIZED = ROOT / 'data' / 'normalized'
OUT = ROOT / 'data' / 'place_links.jsonl'

LOCAL_TYPE_RANK = [
    'Municipi',
    'Nucli de població capital de municipi',
    'Altre nucli de població, llogaret',
    'Barri',
    'Barriada',
    'Urbanització, barriada (aïllat)',
]


def _type_rank(t: str | None) -> int:
    try:
        return LOCAL_TYPE_RANK.index(t or '')
    except ValueError:
        return len(LOCAL_TYPE_RANK)


# --------- place_type → NGIB local_type categories ---------------------------
# Built empirically from the place_type vocabularies of the five
# siblings + the NGIB local_type taxonomy seen in data/gazetteer.parquet.

NGIB_SETTLEMENT = {
    'Municipi', 'Nucli de població capital de municipi',
    'Altre nucli de població, llogaret',
    'Barri', 'Barriada', 'Urbanització, barriada (aïllat)',
    'Paratge, àrea menor', 'Paratge, àrea gran',
}
NGIB_POSSESSION = {
    'Finca, possessió, lloc, casa pagesa, caseta',
    'Construcció agroindustrial',
    'Paratge, àrea menor', 'Paratge, àrea gran',
}
NGIB_ISLAND = {
    'Illa gran', 'Illa mitjana',
    'Accident petit, relleu del fons marí, illot',
}
NGIB_CAPE = {
    'Cap, punta, morro principal',
    'Cap, punta, morro mitjà',
    'Cap, punta, morro petit',
}
NGIB_BAY = {
    'Estret, cala, badia principal',
    'Estret, cala, badia mitjana',
    'Estret, cala petita, rada',
    'Platja principal', 'Platja mitjana', 'Platja petita, platgeta',
    'Moll, dic, pantalà, varador',
}
NGIB_MOUNTAIN = {
    'Elevació gran', 'Elevació petita',
    'Pic, cim gran (puntual)', 'Pic, cim petit (puntual)',
    'Serra petita', 'Coll petit (puntual)', 'Pas',
}
NGIB_DEFENSIVE = {
    # NGIB does not have a dedicated 'Castell' type. Defensive
    # structures fall under generic categories or are absent
    # altogether; widening is acceptable for these (fallback_ok).
    'Edifici singular', 'Monument',
}
NGIB_RELIGIOUS = {
    'Edifici religiós', 'Cementeri',
}
NGIB_WATER = {
    'Torrent', 'Font, surgència', 'Bassa', "Salt d'aigua",
    'Canal, síquia, aqüeducte', 'Salines',
    'Zona pantanosa, aiguamoll', 'Cova, balma, avenc',
}

# Sibling place_type → NGIB category, matched against whole words to
# avoid substring traps (`'illa' in 'villa'` is a real example — it
# silently routed every "Villa" entry to NGIB_ISLAND for a while).
_RX_NO_NGIB = re.compile(
    r'\b(?:cuart[oó]n|despoblado|t[eé]rmino|partido(?:\s+judicial)?|'
    r'di[oó]cesis|obispado|provincia|desaparecid\w*|antigu\w*|'
    r'jurisdicci[oó]n|feligres[ií]a|archipi[eé]lago)\b',
    re.I,
)
_RX_SETTLEMENT = re.compile(
    r'\b(?:villa|vila|lugar|ciudad|aldea|ald[eé]a|pueblo|n[uú]cleo|'
    r'ayuntamiento|agregado|arrabal|valle|barri[oa]|parroquia|'
    r'feligresia|llogaret|capital)\b',
    re.I,
)
_RX_POSSESSION = re.compile(
    r'\b(?:pr[eé]dio|alquer\w+|casa\s+de\s+\w+|cortijo|estancia|'
    r'huert\w*|molino\w*|ace[nñ]a\w*|almazara|f[aá]brica|tejero\w*|'
    r'tinte|venta|albergue\w*|reuni[oó]n|caser[ií]o\w*|'
    r'porci[oó]n\s+de\s+terreno|rafal|possessi[oó]|finca|'
    r'coto(?:\s+redondo)?)\b',
    re.I,
)
_RX_ISLAND = re.compile(r'\b(?:isla|islote|isleta|illa|illot)s?\b', re.I)
_RX_CAPE = re.compile(r'\b(?:cabo|cap|punta|morro)\b', re.I)
_RX_BAY = re.compile(
    r'\b(?:cala|bah[ií]a|rada|ensenada|puerto|playa|moll|muelle)\b',
    re.I,
)
_RX_MOUNTAIN = re.compile(
    r'\b(?:monte|monta[nñ]a|sierra|cerro|puig|serra|cim|pico|cumbre|'
    r'elevaci[oó]n|pe[nñ]\w*|penyes|roca|roques|collado|paso)\b',
    re.I,
)
_RX_DEFENSIVE = re.compile(
    r'\b(?:castillo|fortaleza|fuerte|torre|atalaya|faro)\b', re.I,
)
_RX_RELIGIOUS = re.compile(
    r'\b(?:santuario|santu[aà]ri|parroqui|parr[oó]quia|oratori\w*|'
    r'iglesia|esgl[eé]sia|capilla|ermita|monaster\w*|cementeri\w*)\b',
    re.I,
)
_RX_WATER = re.compile(
    r'\b(?:fuente|font|lago|laguna|salina|torrent|mina|bajo|bassa|'
    r'aig[uü]a)\b',
    re.I,
)


def classify_place_type(pt):
    """Return (preferred_types_or_None, force_unlinked).

    preferred_types  — set of NGIB local_type strings to scope by, or
                       None when no scoping should apply (unknown type).
    force_unlinked   — True when the sibling place_type belongs to a
                       category with no modern NGIB equivalent.

    Whole-word matching (regex \\b…\\b) is mandatory: simple substring
    checks silently mis-categorise common types like "villa" (contains
    "illa") as an island.
    """
    if not pt:
        return (None, False)
    if _RX_NO_NGIB.search(pt):
        return (None, True)
    if _RX_POSSESSION.search(pt):
        return (NGIB_POSSESSION, False)
    if _RX_ISLAND.search(pt):
        return (NGIB_ISLAND, False)
    if _RX_BAY.search(pt):
        return (NGIB_BAY, False)
    if _RX_CAPE.search(pt):
        return (NGIB_CAPE, False)
    if _RX_MOUNTAIN.search(pt):
        return (NGIB_MOUNTAIN, False)
    if _RX_DEFENSIVE.search(pt):
        return (NGIB_DEFENSIVE, False)
    if _RX_RELIGIOUS.search(pt):
        return (NGIB_RELIGIOUS, False)
    if _RX_WATER.search(pt):
        return (NGIB_WATER, False)
    if _RX_SETTLEMENT.search(pt):
        return (NGIB_SETTLEMENT, False)
    # Unknown / non-matching place_type — fall back to settlement
    # (the broad/default category) so we still get matches.
    return (NGIB_SETTLEMENT, False)


# ---------- gazetteer loaders -------------------------------------------------

def load_gazetteer() -> tuple[list[dict], dict]:
    """Returns:
        rows       — list of every gazetteer row (dict per row).
        by_island  — {island: [rows…]} for matching pools.
    """
    con = duckdb.connect(':memory:')
    sql = f"""
        SELECT id, spelling, normalized, municipality, island,
               local_type, lon, lat, source, is_settlement
        FROM read_parquet('{GAZETTEER}')
    """
    if WIKIDATA_VARIANTS.exists():
        sql += f"""
        UNION ALL
        SELECT id, spelling, normalized, municipality, island,
               local_type, lon, lat, source, is_settlement
        FROM read_parquet('{WIKIDATA_VARIANTS}')
        """
    raw = con.sql(sql).fetchall()
    rows = []
    by_island: dict[str | None, list[dict]] = defaultdict(list)
    for (rid, sp, nm, mun, isl, lt, lon, lat, src, settle) in raw:
        r = {
            'id': rid, 'spelling': sp, 'normalized': nm, 'municipality': mun,
            'island': isl, 'local_type': lt, 'lon': lon, 'lat': lat,
            'source': src, 'is_settlement': settle,
        }
        rows.append(r)
        by_island[isl].append(r)
    return rows, by_island


# ---------- per-step resolvers ------------------------------------------------

def historical_to_ngib(hist_row: dict, pool: list[dict]) -> str | None:
    """A historical-variants row carries the *modern Catalan* target
    in ``municipality``. Translate it into an NGIB ID by looking up
    the canonical row whose spelling matches the modern form within
    the same island pool."""
    target = hist_row.get('municipality')
    if not target:
        return None
    for r in pool:
        if r['source'] == 'ngib' and r['spelling'] == target:
            return r['id']
    # Fallback: same island, normalised match against the target.
    norm_target = normalize(target)
    for r in pool:
        if r['source'] == 'ngib' and r['normalized'] == norm_target:
            return r['id']
    return None


def resolve_row_to_ngib_id(row: dict, pool: list[dict]) -> str | None:
    if row['source'] == 'ngib':
        return row['id']
    if row['source'] == 'historical':
        return historical_to_ngib(row, pool)
    # 'ngib_variants' rows were already merged with their parent NGIB
    # id by build_gazetteer (their id is the same geographic_name_id).
    return row['id']


def sort_exact_hits(hits: list[dict]) -> list[dict]:
    """Order exact-normalised hits so that the most authoritative row
    wins: Municipi > historical > everything else."""
    return sorted(hits, key=lambda r: (
        _type_rank(r.get('local_type') or ''),
        0 if r['source'] == 'historical' else 1,
        0 if r['source'] == 'ngib' else 1,
    ))


# ---------- the waterfall -----------------------------------------------------

def _try_string_steps(norm: str, pool: list[dict], pool_full: list[dict],
                      penalty: float = 0.0) -> dict | None:
    """Run the four string-matching steps (historical_curated,
    exact_norm, fuzzy_wratio, fuzzy_token_set) against a single pool.
    Returns a decision dict on hit, or None on miss. ``penalty`` is
    subtracted from the confidence — used when the pool was widened
    past the type-compatible filter."""
    if not norm or len(norm) < 3:
        return None

    # Step 1 — historical_curated.
    hist_hits = [r for r in pool if r['source'] == 'historical' and r['normalized'] == norm]
    if hist_hits:
        nid = historical_to_ngib(hist_hits[0], pool_full)
        if nid:
            return {
                'ngib_id': nid, 'method': 'historical_curated',
                'confidence': round(0.99 - penalty, 4),
                'candidate_ngib_ids': [nid],
                'candidate_scores': [round(0.99 - penalty, 4)],
            }

    # Step 2 — exact_norm.
    exact = [r for r in pool if r['normalized'] == norm]
    if exact:
        ordered = sort_exact_hits(exact)
        nid = resolve_row_to_ngib_id(ordered[0], pool_full)
        if nid:
            return {
                'ngib_id': nid, 'method': 'exact_norm',
                'confidence': round(0.97 - penalty, 4),
                'candidate_ngib_ids': [nid],
                'candidate_scores': [round(0.97 - penalty, 4)],
            }

    # Step 3 — fuzzy_wratio with length guard.
    min_len = max(4, int(len(norm) * 0.6))
    pool_fuzzy = [r for r in pool if r['normalized'] and len(r['normalized']) >= min_len]
    if pool_fuzzy:
        choices = [r['normalized'] for r in pool_fuzzy]
        results = process.extract(
            norm, choices, scorer=fuzz.WRatio, score_cutoff=88, limit=10,
        )
        if results:
            scored = []
            for _, score, idx in results:
                r = pool_fuzzy[idx]
                nid = resolve_row_to_ngib_id(r, pool_full)
                if nid:
                    scored.append((nid, score, r))
            if scored:
                scored.sort(key=lambda t: (-t[1], _type_rank(t[2].get('local_type'))))
                top_id, top_score, _ = scored[0]
                return {
                    'ngib_id': top_id, 'method': 'fuzzy_wratio',
                    'confidence': round(top_score / 100 - penalty, 4),
                    'candidate_ngib_ids': [t[0] for t in scored[:3]],
                    'candidate_scores': [round(t[1] / 100 - penalty, 4) for t in scored[:3]],
                }

    # Step 4 — fuzzy_token_set for multi-word titles.
    if len(norm.split()) >= 2 and pool_fuzzy:
        choices = [r['normalized'] for r in pool_fuzzy]
        results = process.extract(
            norm, choices, scorer=fuzz.token_set_ratio, score_cutoff=85, limit=10,
        )
        if results:
            scored = []
            for _, score, idx in results:
                r = pool_fuzzy[idx]
                nid = resolve_row_to_ngib_id(r, pool_full)
                if nid:
                    scored.append((nid, score, r))
            if scored:
                scored.sort(key=lambda t: (-t[1], _type_rank(t[2].get('local_type'))))
                top_id, top_score, _ = scored[0]
                return {
                    'ngib_id': top_id, 'method': 'fuzzy_token_set',
                    'confidence': round(top_score / 100 - penalty, 4),
                    'candidate_ngib_ids': [t[0] for t in scored[:3]],
                    'candidate_scores': [round(t[1] / 100 - penalty, 4) for t in scored[:3]],
                }

    return None


def _resolve_parent(parent_hint: str | None, pool_island: list[dict],
                    ngib_by_id: dict) -> tuple[str | None, str, float]:
    """Look up the NGIB Municipi for the given hint.
    Returns (ngib_id, method, confidence)."""
    if not parent_hint:
        return (None, 'unresolved', 0.0)
    # Apply the same cleaner used on titles so historical forms like
    # "PUEBLA, LA" → "LA PUEBLA" canonicalise before normalising.
    norm_hint = normalize(clean_title(parent_hint))
    # Pass 1 — Castilianised hint via historical_curated.
    for r in pool_island:
        if r['source'] == 'historical' and r['normalized'] == norm_hint:
            nid = historical_to_ngib(r, pool_island)
            if nid:
                target = ngib_by_id.get(nid)
                if target and target.get('local_type') == 'Municipi':
                    return (nid, 'hint_historical', 0.99)
    # Pass 2 — direct Catalan match.
    for r in pool_island:
        if (r['source'] == 'ngib' and r.get('local_type') == 'Municipi'
                and r['normalized'] == norm_hint):
            return (r['id'], 'hint_explicit', 0.99)
    # Pass 3 — Cabrera and similar non-municipality islands: fall back
    # to the Illa NGIB row when no Municipi exists for the hint.
    for r in pool_island:
        if (r['source'] == 'ngib'
                and (r.get('local_type') or '').startswith('Illa')
                and r['normalized'] == norm_hint):
            return (r['id'], 'hint_island_fallback', 0.95)
    return (None, 'unresolved', 0.0)


def resolve(entry: dict, by_island: dict, all_rows: list[dict],
            ngib_by_id: dict) -> dict:
    """3-phase resolution for one normalized JSONL row.

    Returns a dict with:
        entry_kind, parent_ngib_id, parent_method, parent_confidence,
        describes_ngib_id, describes_method, describes_confidence,
        parent_candidate_ngib_ids, describes_candidate_ngib_ids,
        notes.
    """
    title = entry['title'] or ''
    island = entry['island']
    place_type = entry.get('place_type')
    kind_hint = entry.get('entry_kind_hint') or 'unknown'
    parent_hint = entry.get('parent_municipality_hint')
    norm_title = normalize(clean_title(title))
    pool_island = by_island.get(island, []) if island else all_rows

    # ---------- Phase 2 — resolve parent_ngib_id. -----------------
    # (Phase 2 runs first because Phase 1 may need to override the
    # parent when the title itself is a Municipi name.)
    parent_id, parent_method, parent_conf = _resolve_parent(
        parent_hint, pool_island, ngib_by_id,
    )

    # ---------- Phase 1 — decide entry_kind. ----------------------
    entry_kind = None
    if kind_hint == 'jurisdictional':
        entry_kind = 'jurisdictional'
        # Jurisdictional entries are usually named after a Municipi
        # (Madoz "MAHON partit jud.", Miñano "ALAYOR término").
        # Try to find the matching Municipi via the title so the
        # entry shows up under the parent Place's «Articles
        # jurisdiccionals» section.
        if not parent_id:
            for r in pool_island:
                if (r['source'] == 'ngib'
                        and r.get('local_type') == 'Municipi'
                        and r['normalized'] == norm_title):
                    parent_id = r['id']
                    parent_method = 'jurisdictional_title_match'
                    parent_conf = 0.95
                    break
            if not parent_id:
                for r in pool_island:
                    if r['source'] == 'historical' and r['normalized'] == norm_title:
                        nid = historical_to_ngib(r, pool_island)
                        if nid:
                            target = ngib_by_id.get(nid)
                            if target and target.get('local_type') == 'Municipi':
                                parent_id = nid
                                parent_method = 'jurisdictional_title_historical'
                                parent_conf = 0.97
                                break
    else:
        # If title is a known Municipi name on this island, the entry
        # MAY describe that municipality. Three rules guard against
        # spurious matches:
        #   (a) When parent_id is already set and the title-derived
        #       Municipi is a DIFFERENT one, prefer the parent_hint:
        #       a Madoz «SAN JUAN (so)» predio with municipality=Alaró
        #       names a possessió, not the Sant Joan municipi.
        #   (b) Override the parent only for settlement/unknown
        #       kind_hints AND only when the title-derived Municipi IS
        #       the one resolved from the hint. Required for Madoz
        #       «FORMENTERA (isla)» with hint='Ibiza' to switch to
        #       Formentera Municipi.
        #   (c) When kind_hint is 'feature' (predio, cala, peñas,
        #       isla, …) and parent_id is already set, don't override.

        # Promoting a feature-typed entry to municipality is only
        # safe in one narrow case: place_type is 'isla'/'illa' AND
        # the matched Municipi is the eponymous island-municipality
        # (Formentera, Mallorca-the-island in archipelago articles).
        # All other feature place_types ('peñas', 'puerto', 'cabo',
        # 'cala', 'predio', …) name a sub-feature that coincidentally
        # shares its toponym with a Municipi name.
        plain_title = norm_title == normalize(title)
        feature_is_island = bool(
            place_type and ('isla' in place_type.lower()
                            or 'illa' in place_type.lower())
        )

        def _try_title_municipi(target_ngib_id: str | None,
                                method: str, conf: float):
            nonlocal entry_kind, parent_id, parent_method, parent_conf
            if not target_ngib_id:
                return False
            if kind_hint == 'feature' and not feature_is_island:
                return False
            if kind_hint == 'feature' and not plain_title:
                return False
            if parent_id and target_ngib_id != parent_id:
                if kind_hint == 'feature' and not feature_is_island:
                    return False
            entry_kind = 'municipality'
            if not parent_id:
                parent_id = target_ngib_id
                parent_method = method
                parent_conf = conf
            elif parent_id != target_ngib_id:
                parent_id = target_ngib_id
                parent_method = method + '_override'
                parent_conf = conf - 0.01
            return True

        for r in pool_island:
            if (r['source'] == 'ngib'
                    and r.get('local_type') == 'Municipi'
                    and r['normalized'] == norm_title):
                if _try_title_municipi(r['id'], 'self_municipality', 0.97):
                    break
        if entry_kind is None:
            # Castilianised Municipi name (ALAYOR → Alaior).
            for r in pool_island:
                if r['source'] == 'historical' and r['normalized'] == norm_title:
                    nid = historical_to_ngib(r, pool_island)
                    if nid:
                        target = ngib_by_id.get(nid)
                        if target and target.get('local_type') == 'Municipi':
                            if _try_title_municipi(nid, 'self_municipality_historical', 0.98):
                                break
    # Phase 1 fallback — the title may name an NGIB sub-feature
    # (llogaret, possessió, barri) that lives inside a Municipi.
    # Floridablanca pueblos like BINIARAIX/CAIMARI/ESTABLIMENTS fit
    # here, as do entries whose title is a curated historical variant
    # pointing at a sub-feature (SARRECÓ → s'Arracó llogaret).
    subfeature_shortcut_id = None
    subfeature_shortcut_method = None

    def _try_subfeature(target_row, sub_method: str):
        nonlocal subfeature_shortcut_id, subfeature_shortcut_method
        nonlocal parent_id, parent_method, parent_conf
        subfeature_shortcut_id = target_row['id']
        subfeature_shortcut_method = sub_method
        mun_name = target_row.get('municipality')
        if mun_name and not parent_id:
            norm_mun = normalize(mun_name)
            for rr in pool_island:
                if (rr['source'] == 'ngib'
                        and rr.get('local_type') == 'Municipi'
                        and rr['normalized'] == norm_mun):
                    parent_id = rr['id']
                    parent_method = 'derived_from_subfeature'
                    parent_conf = 0.95
                    break

    if (entry_kind is None and not parent_id
            and kind_hint in ('settlement', 'unknown', 'feature')):
        # The Phase-1 sub-feature shortcut only runs when no parent
        # has been resolved yet. If the entry's parent IS known (most
        # 1860, Madoz, Riera, Floridablanca cases), Phase 3 will
        # search within that terme directly — and the search is
        # restricted to the right municipality, avoiding spurious
        # cross-terme matches like "Port (El)" in Sóller landing on
        # "es Port" in Banyalbufar.
        # Direct NGIB match against a non-Municipi row.
        for r in pool_island:
            if (r['source'] == 'ngib'
                    and r.get('local_type') != 'Municipi'
                    and r['normalized'] == norm_title):
                _try_subfeature(r, 'exact_norm_subfeature')
                break
        # Historical-variant pointing at a sub-feature.
        if subfeature_shortcut_id is None:
            for r in pool_island:
                if r['source'] == 'historical' and r['normalized'] == norm_title:
                    nid = historical_to_ngib(r, pool_island)
                    if nid and nid in ngib_by_id:
                        target = ngib_by_id[nid]
                        if target.get('local_type') != 'Municipi':
                            _try_subfeature(target, 'historical_curated_subfeature')
                            break
                        elif kind_hint == 'feature':
                            # Curated form points at a Municipi but the
                            # entry is a feature within that terme
                            # (e.g. "CAP DE PERA" castle ⇒ Capdepera).
                            # Use the Municipi as parent so Phase 3 can
                            # search within the terme.
                            parent_id = nid
                            parent_method = 'historical_curated_parent'
                            parent_conf = 0.92
                            break
    if entry_kind is None:
        entry_kind = 'feature'  # provisional; Phase 3 confirms _with_ngib or _no_ngib

    # ---------- Phase 3 — resolve describes_ngib_id. --------------
    describes_id = None
    describes_method = 'none'
    describes_conf = 0.0
    describes_candidates: list[str] = []
    notes = []

    if entry_kind == 'municipality':
        describes_id = parent_id
        describes_method = 'is_municipality'
        describes_conf = parent_conf
    elif entry_kind == 'jurisdictional':
        describes_method = 'jurisdictional_no_equivalent'
    elif subfeature_shortcut_id is not None:
        # Phase 1 already found a matching sub-feature NGIB row by
        # exact-norm against the title. Use it.
        describes_id = subfeature_shortcut_id
        describes_method = subfeature_shortcut_method
        describes_conf = 0.95
        describes_candidates = [subfeature_shortcut_id]
        entry_kind = 'feature_with_ngib'
    else:
        # feature — search strictly within the parent's terme.
        if parent_id and parent_id in ngib_by_id:
            parent_row = ngib_by_id[parent_id]
            terme_name = parent_row.get('municipality')
            if terme_name:
                norm_terme = normalize(terme_name)
                # NGIB and wikidata rows can be filtered directly by
                # their ``municipality`` column (which is the terme).
                # Historical rows are tricky: their ``municipality``
                # field stores the SPELLING of the target NGIB row,
                # not the terme it lives in (e.g. "San Francisco
                # Javier" → municipality="Sant Francesc de
                # Formentera", which is the target's name, not the
                # Formentera terme). Resolve each historical row's
                # target NGIB and use the target's terme.
                def hist_target_terme(h: dict) -> str:
                    nid = historical_to_ngib(h, pool_island)
                    if not nid:
                        return ''
                    target = ngib_by_id.get(nid)
                    if not target:
                        return ''
                    return normalize(target.get('municipality') or '')

                pool_terme = []
                for r in pool_island:
                    if r['source'] == 'historical':
                        if hist_target_terme(r) == norm_terme:
                            pool_terme.append(r)
                    elif r.get('municipality') and normalize(r['municipality']) == norm_terme:
                        pool_terme.append(r)
                preferred_types, _ = classify_place_type(place_type)
                # First pass — type-scoped within terme.
                if preferred_types is not None:
                    ngib_type_by_id_local = {
                        r['id']: (r.get('local_type') or '')
                        for r in pool_terme if r['source'] == 'ngib'
                    }
                    def _h_target_type(h):
                        return ngib_type_by_id_local.get(
                            historical_to_ngib(h, pool_island) or '', '',
                        )
                    pool_scoped = [
                        r for r in pool_terme
                        if (r['source'] == 'historical'
                            and _h_target_type(r) in preferred_types)
                        or (r['source'] != 'historical'
                            and r.get('local_type') in preferred_types)
                    ]
                    decision = _try_string_steps(
                        norm_title, pool_scoped, pool_island, penalty=0.0,
                    )
                    if decision:
                        describes_id = decision['ngib_id']
                        describes_method = decision['method']
                        describes_conf = decision['confidence']
                        describes_candidates = decision.get('candidate_ngib_ids', [])
                # Second pass — widened to entire terme (no type filter),
                # only for broad place_type categories.
                if not describes_id:
                    widen_ok = (
                        preferred_types is None
                        or preferred_types is NGIB_SETTLEMENT
                        or preferred_types is NGIB_POSSESSION
                    )
                    if widen_ok:
                        decision = _try_string_steps(
                            norm_title, pool_terme, pool_island, penalty=0.05,
                        )
                        if decision:
                            describes_id = decision['ngib_id']
                            describes_method = decision['method']
                            describes_conf = decision['confidence']
                            describes_candidates = decision.get('candidate_ngib_ids', [])
        if describes_id:
            entry_kind = 'feature_with_ngib'
        else:
            entry_kind = 'feature_no_ngib'
            describes_method = 'no_ngib_equivalent_in_terme'
            if not parent_id:
                notes.append('no parent municipi resolved; describes lookup skipped')

    return {
        'entry_kind':                    entry_kind,
        'parent_ngib_id':                parent_id,
        'parent_method':                 parent_method,
        'parent_confidence':             round(parent_conf, 4),
        'parent_candidate_ngib_ids':     [parent_id] if parent_id else [],
        'describes_ngib_id':             describes_id,
        'describes_method':              describes_method,
        'describes_confidence':          round(describes_conf, 4),
        'describes_candidate_ngib_ids':  describes_candidates,
        'notes':                         '; '.join(notes) if notes else None,
    }


# ---------- legacy (v1) — kept temporarily for reference, unused -----------
def _resolve_v1_unused(entry, by_island, all_rows):
    """Old single-link resolver. Replaced by the 3-phase resolve() above.
    Kept here only to preserve git blame for inspection; not referenced.
    """
    title = entry['title'] or ''
    island = entry['island']
    place_type = entry.get('place_type')
    norm = normalize(clean_title(title))
    pool_island = by_island.get(island, []) if island else all_rows
    preferred_types, force_unlinked = classify_place_type(place_type)
    if force_unlinked:
        return {
            'ngib_id': None, 'method': 'type_no_ngib_equivalent',
            'confidence': 0.0,
            'candidate_ngib_ids': [], 'candidate_scores': [],
            'reason': f'place_type={place_type!r} has no modern NGIB equivalent',
        }

    # Steps 1-4 against the type-compatible pool (preferred).
    if preferred_types is not None:
        # Build an NGIB-id → local_type lookup so we can type-scope the
        # historical-variants rows by their actual TARGET (e.g. the
        # historical row "Alayor" points at the Alaior Municipi NGIB
        # row, so it's a settlement; including it for a place_type of
        # "peñas" or "puerto" would conflate the toponym with a
        # different concept).
        ngib_type_by_id = {
            r['id']: (r.get('local_type') or '')
            for r in pool_island
            if r['source'] == 'ngib'
        }

        def hist_target_type(h):
            target = h.get('municipality')
            if not target:
                return ''
            target_norm = normalize(target)
            for r in pool_island:
                if r['source'] == 'ngib' and (
                    r['spelling'] == target or r['normalized'] == target_norm
                ):
                    return r.get('local_type') or ''
            return ''

        pool_preferred = [
            r for r in pool_island
            if (r['source'] == 'historical' and hist_target_type(r) in preferred_types)
            or (r['source'] != 'historical' and r.get('local_type') in preferred_types)
        ]
        decision = _try_string_steps(norm, pool_preferred, pool_island, penalty=0.0)
        if decision:
            return decision

    # Steps 1'-4' widened to the full island pool, with -0.05 penalty.
    # We only widen when the entry's place_type is broad (settlement
    # or possession) or unknown. For sharply-typed entries (peñas,
    # puerto, isla, cabo, cala, castell, ermita, etc.) widening would
    # produce conceptual conflations — ALAYOR (peñas) matching the
    # Alaior Municipi just because the toponym coincides. Better to
    # leave those as «Sense vincle NGIB» so they don't pollute the
    # canonical place's timeline.
    widen_ok = (
        preferred_types is None
        or preferred_types is NGIB_SETTLEMENT
        or preferred_types is NGIB_POSSESSION
    )
    if widen_ok:
        decision = _try_string_steps(norm, pool_island, pool_island, penalty=0.05)
        if decision:
            return decision

    # Step 5 — linked_to_parent: title didn't match anything in the
    # right type, but the declared municipality does match an NGIB
    # Municipi row. The UI surfaces these separately because the
    # entry is *about* a sub-feature of the municipality, not the
    # municipality itself.
    mun_value = entry.get('municipality')
    if mun_value:
        norm_mun = normalize(mun_value)
        mun_hits = [
            r for r in pool_island
            if r['source'] == 'ngib' and r['normalized'] == norm_mun
            and r.get('local_type') == 'Municipi'
        ]
        if mun_hits:
            nid = mun_hits[0]['id']
            return {
                'ngib_id': nid, 'method': 'linked_to_parent',
                'confidence': 0.80,
                'candidate_ngib_ids': [nid], 'candidate_scores': [0.80],
                'mun_value': mun_value,
            }

    # Step 6 — unlinked.
    return {
        'ngib_id': None, 'method': 'unlinked', 'confidence': 0.0,
        'candidate_ngib_ids': [], 'candidate_scores': [],
    }


# ---------- driver ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stats', action='store_true',
                    help='also print a per-source breakdown by entry_kind')
    args = ap.parse_args()

    print('Loading gazetteer…', file=sys.stderr)
    all_rows, by_island = load_gazetteer()
    ngib_by_id = {r['id']: r for r in all_rows if r['source'] == 'ngib'}
    print(f'  {len(all_rows):,} gazetteer rows · {len(ngib_by_id):,} NGIB primaries',
          file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    f_out = OUT.open('w', encoding='utf-8')

    # Per-source counters.
    per_src = defaultdict(lambda: defaultdict(int))

    for src_path in sorted(NORMALIZED.glob('*.jsonl')):
        source = src_path.stem
        with src_path.open() as f:
            for line in f:
                entry = json.loads(line)
                decision = resolve(entry, by_island, all_rows, ngib_by_id)
                row = {
                    'source_project': entry['source_project'],
                    'source_id':      entry['source_id'],
                    'title':          entry['title'],
                    'island':         entry['island'],
                    **decision,
                }
                f_out.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')))
                f_out.write('\n')
                per_src[source]['total'] += 1
                per_src[source][f'kind:{decision["entry_kind"]}'] += 1
                if decision.get('describes_ngib_id'):
                    per_src[source]['describes_linked'] += 1
                if decision.get('parent_ngib_id'):
                    per_src[source]['parent_linked'] += 1
        print(f'  {source:18s} ✓', file=sys.stderr)

    f_out.close()
    print(f'Wrote {OUT.relative_to(ROOT)}', file=sys.stderr)

    if args.stats:
        print('\n=== resolution stats by source ===', file=sys.stderr)
        kinds = ['kind:municipality', 'kind:feature_with_ngib',
                 'kind:feature_no_ngib', 'kind:jurisdictional']
        header = (f'{"source":<22} {"total":>6} '
                  f'{"describes":>9} {"d_pct":>6} '
                  f'{"parent":>7} {"p_pct":>6}  ')
        header += '  '.join(f'{k[5:][:7]:>7}' for k in kinds)
        print(header, file=sys.stderr)
        for src in sorted(per_src):
            d = per_src[src]
            t = d['total']
            dl = d['describes_linked']
            pl = d['parent_linked']
            d_pct = (dl * 100 / t) if t else 0
            p_pct = (pl * 100 / t) if t else 0
            row = (f'{src:<22} {t:>6} '
                   f'{dl:>9} {d_pct:>5.1f}% '
                   f'{pl:>7} {p_pct:>5.1f}%  ')
            row += '  '.join(f'{d[k]:>7}' for k in kinds)
            print(row, file=sys.stderr)


if __name__ == '__main__':
    main()
