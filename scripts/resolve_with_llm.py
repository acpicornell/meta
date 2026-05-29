#!/usr/bin/env python3
"""Claude tiebreaker for hard cases the deterministic matcher can't crack.

Runs over two buckets of entries:

  - **unlinked**: every row whose ``method`` is ``unlinked``
    or ``type_no_ngib_equivalent``. For each, we generate a shortlist
    of NGIB candidates (same island, type-compatible + top-fuzzy) and
    ask Claude to pick the right one (or say `null` if none of the
    candidates is a true semantic match).

  - **borderline**: rows already linked with ``confidence < 0.93``
    AND at least two candidates within 3 score points of the winner.
    We ask Claude to confirm the winner or swap it for a better
    candidate.

The script is **opt-in** (``--use-llm`` is required to call the API;
the default is ``--dry-run``). Responses are cached in
``data/llm_cache.jsonl`` keyed by a content hash, so a re-run picks up
where it left off without re-spending tokens.

After processing, the updated decisions are written back into
``data/place_links.jsonl`` with ``method='llm_tiebreaker'``.

Requires ``ANTHROPIC_API_KEY`` (or the Claude Max env equivalent) in
the environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import duckdb
from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent

# Load API key from .env if present. Sibling projects all ship a
# .env with ANTHROPIC_API_KEY; meta inherits the same convention.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass

sys.path.insert(0, str(ROOT / 'scripts'))
from build_gazetteer import normalize  # type: ignore
from resolve_entities import (  # type: ignore
    clean_title, classify_place_type,
    NGIB_BAY, NGIB_CAPE, NGIB_ISLAND, NGIB_POSSESSION,
    NGIB_SETTLEMENT, NGIB_MOUNTAIN, NGIB_DEFENSIVE,
    NGIB_RELIGIOUS, NGIB_WATER,
)

GAZETTEER = ROOT / 'data' / 'gazetteer.parquet'
WIKIDATA_VARIANTS = ROOT / 'data' / 'wikidata_variants.parquet'
NORMALIZED_DIR = ROOT / 'data' / 'normalized'
LINKS_PATH = ROOT / 'data' / 'place_links.jsonl'
CACHE_PATH = ROOT / 'data' / 'llm_cache.jsonl'

DEFAULT_MODEL = 'claude-sonnet-4-6'
BATCH_SIZE = 5
SHORTLIST_SIZE = 10
BORDERLINE_CUTOFF = 0.93

SYSTEM_PROMPT = """\
Ets un assistent que enllaça mencions històriques de llocs balears (segles XVIII–XIX) als seus identificadors canònics del Nomenclàtor Geogràfic de les Illes Balears (NGIB).

REGLES:
1. Llegeix amb atenció el títol (en castellà o català històric), el tipus de lloc declarat (villa, predio, cuartón, isla, puerto…), l'illa i la descripció breu de l'article.
2. Examina la llista de candidats NGIB que se't proporciona. Cada candidat porta el seu ngib_id, la grafia catalana moderna, el tipus NGIB i el municipi pare.
3. Decideix quin candidat (si n'hi ha) correspon a la MATEIXA ENTITAT GEOGRÀFICA REAL — no només a un topònim similar. Un cuartón, un despoblat o una jurisdicció no són el mateix que un municipi modern, encara que comparteixin nom. Un port és diferent del nucli urbà del costat. Una illa és diferent d'una possessió.
4. Quan un candidat porti l'etiqueta [VINCLE ACTUAL AL MUNICIPI PARE], significa que l'entrada actualment està enllaçada per defecte al municipi al qual pertany perquè el matcher determinista no n'ha trobat una de més específica. Si descobreixes a la llista una candidatura NGIB més específica (un llogaret, possessió, accident geogràfic, port, etc.) que correspon exactament al títol i a la descripció, prefereix-la al pare. Si cap candidat específic encaixa, confirma el municipi pare.
5. Si cap candidat —ni tan sols el pare— representa la mateixa entitat real, retorna ngib_id: null.
6. La confiança ha de reflectir la teva certesa: 0.95+ si és claríssim; 0.85-0.94 si raonable però amb dubte; <0.85 millor retornar null.
7. Respon NOMÉS amb JSON vàlid: una array d'objectes, un per entrada, amb id, ngib_id (string o null), confidence (float), reason (breu, en català).
"""


def cache_key(entry_id: str, candidate_ids: list[str]) -> str:
    h = hashlib.sha1()
    h.update(entry_id.encode())
    for c in sorted(candidate_ids):
        h.update(b'|')
        h.update(c.encode())
    return h.hexdigest()


def load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    out = {}
    with CACHE_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            out[r['cache_key']] = r
    return out


def append_cache(rows: list[dict]):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open('a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
            f.write('\n')


def load_gazetteer():
    """Returns (rows, by_island) restricted to NGIB primaries + wikidata
    (we don't pass historical variants to the LLM; they're just lookup
    hops to the same NGIB ids)."""
    con = duckdb.connect(':memory:')
    sql = f"""
        SELECT id, spelling, normalized, municipality, island, local_type
        FROM read_parquet('{GAZETTEER}')
        WHERE source = 'ngib'
    """
    rows = con.sql(sql).fetchall()
    by_island = defaultdict(list)
    for rid, sp, nm, mun, isl, lt in rows:
        by_island[isl].append({
            'id': rid, 'spelling': sp, 'normalized': nm,
            'municipality': mun, 'island': isl, 'local_type': lt,
        })
    return by_island


def shortlist_candidates(entry: dict, pool: list[dict],
                         must_include_id: str | None = None) -> list[dict]:
    """Pick up to SHORTLIST_SIZE NGIB candidates for one entry.

    Strategy:
      - Top-N by fuzz.WRatio on the cleaned title.
      - Plus any candidate whose spelling appears verbatim in the
        description (Madoz often names the parent in the article).
      - Type-compatible candidates float to the top.
      - When ``must_include_id`` is provided (typically the current
        parent municipality for ``linked_to_parent`` entries), that
        NGIB row is guaranteed to be in the shortlist so the LLM can
        confirm or override it.
    """
    title = clean_title(entry['title'] or '')
    title_norm = normalize(title)
    description = (entry.get('blob') or {}).get('description', '') or ''
    desc_lower = description.lower()
    preferred_types, _ = classify_place_type(entry.get('place_type'))

    if not pool:
        return []

    choices = [r['normalized'] for r in pool]
    fuzzy = process.extract(
        title_norm, choices, scorer=fuzz.WRatio, score_cutoff=60,
        limit=SHORTLIST_SIZE * 2,
    )
    # Promote spelling-in-description hits.
    if desc_lower:
        for r in pool:
            if r['spelling'] and r['spelling'].lower() in desc_lower:
                fuzzy.append((r['normalized'], 95.0, pool.index(r)))
    seen_ids = set()
    cands = []
    for (_norm_match, score, idx) in fuzzy:
        r = pool[idx]
        if r['id'] in seen_ids:
            continue
        seen_ids.add(r['id'])
        type_ok = (
            preferred_types is None
            or (r['local_type'] in preferred_types)
        )
        cands.append({
            'ngib_id':     r['id'],
            'spelling':    r['spelling'],
            'local_type':  r['local_type'],
            'municipality':r['municipality'],
            '_score':      float(score),
            '_type_ok':    type_ok,
        })
    # If a must_include_id is given (e.g. the current parent municipi
    # for a linked_to_parent entry) but it's not in the fuzzy hits,
    # inject it so the LLM is given the explicit option of confirming
    # or rejecting it.
    if must_include_id and must_include_id not in seen_ids:
        for r in pool:
            if r['id'] == must_include_id:
                cands.append({
                    'ngib_id':     r['id'],
                    'spelling':    r['spelling'],
                    'local_type':  r['local_type'],
                    'municipality':r['municipality'],
                    '_score':      0.0,
                    '_type_ok':    False,
                    '_current':    True,
                })
                break
    cands.sort(key=lambda c: (not c['_type_ok'], -c['_score']))
    return cands[:SHORTLIST_SIZE]


def build_user_prompt(batch: list[dict]) -> str:
    """One batch → one user-prompt string."""
    lines = ['ENTRADES A ENLLAÇAR:\n']
    for i, item in enumerate(batch, 1):
        e = item['entry']
        blob = item['blob'] or {}
        desc = (blob.get('description')
                or blob.get('description_es')
                or blob.get('observations')
                or '')
        if desc and len(desc) > 600:
            desc = desc[:600].rstrip() + '…'
        lines.append(f'## Entrada {i}')
        lines.append(f'  source       : {e["source_project"]}:{e["source_id"]}')
        lines.append(f'  títol        : {e["title"]!r}')
        lines.append(f'  place_type   : {e.get("place_type")!r}')
        lines.append(f'  illa         : {e.get("island")!r}')
        lines.append(f'  municipi pare: {e.get("municipality")!r}')
        if desc:
            lines.append(f'  descripció   : {desc!r}')
        lines.append('  candidats NGIB:')
        for c in item['candidates']:
            tag = ' [VINCLE ACTUAL AL MUNICIPI PARE]' if c.get('_current') else ''
            lines.append(
                f'    - ngib_id={c["ngib_id"]:>10s}  '
                f'"{c["spelling"]}"  tipus={c["local_type"]!r}  '
                f'municipi={c["municipality"]!r}  '
                f'(fuzzy_score={c["_score"]:.0f}, type_ok={c["_type_ok"]})'
                f'{tag}'
            )
        if not item['candidates']:
            lines.append('    (cap candidat: deixa ngib_id null)')
        lines.append('')
    lines.append(
        'Respon amb una array JSON de longitud {n}, '
        'cada element amb camps "id" (1..{n}), "ngib_id" (string o null), '
        '"confidence" (0.0–1.0), "reason" (breu, en català). '
        'NOMÉS l\'array JSON, sense text introductori.'.format(n=len(batch))
    )
    return '\n'.join(lines)


def call_claude(client, model: str, user_prompt: str) -> list[dict]:
    """One API call with prompt caching on the system message."""
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{
            'type': 'text', 'text': SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{'role': 'user', 'content': user_prompt}],
    )
    text = msg.content[0].text.strip()
    # Strip code fences if Claude wraps the JSON.
    if text.startswith('```'):
        text = text.split('```', 2)[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.strip().rstrip('`').strip()
    return json.loads(text)


def _apply_decisions(links, by_key, decisions) -> dict:
    """Mutate ``links`` in place to apply LLM decisions. Returns a
    counter dict with ``upgraded``, ``confirmed`` and ``new`` counts."""
    cnt = {'upgraded': 0, 'confirmed': 0, 'new': 0}
    for dec in decisions:
        key = dec['key']
        d = dec['decision']
        new_id = d.get('ngib_id')
        r = by_key[key]
        old_id = r.get('ngib_id')
        old_method = r.get('method')

        if not new_id:
            # LLM rejected the link entirely.
            continue
        if old_method == 'linked_to_parent' and new_id == old_id:
            # LLM confirmed: keep the parent-link semantics so the UI
            # still puts it in the «menors» section.
            r['llm_confirmed'] = True
            r['llm_reason'] = d.get('reason')
            cnt['confirmed'] += 1
            continue
        if old_method == 'linked_to_parent' and new_id != old_id:
            cnt['upgraded'] += 1
        elif not old_id:
            cnt['new'] += 1
        r['ngib_id'] = new_id
        r['method'] = 'llm_tiebreaker'
        r['confidence'] = round(d['confidence'], 4)
        r['candidate_ngib_ids'] = [new_id]
        r['candidate_scores'] = [round(d['confidence'], 4)]
        r['llm_reason'] = d.get('reason')
    return cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--use-llm', action='store_true',
                    help='actually call the Anthropic API (default: dry-run)')
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--max', type=int, default=None,
                    help='process at most N entries (for partial runs)')
    ap.add_argument('--include-borderline', action='store_true',
                    help='also revisit borderline matches with conf<0.93')
    ap.add_argument('--include-linked-to-parent', action='store_true',
                    help='also revisit entries currently linked to the '
                         'parent municipality; the LLM may upgrade them to '
                         'a more specific NGIB id when one exists')
    args = ap.parse_args()

    # --- Load resolver state.
    links = []
    with LINKS_PATH.open() as f:
        for line in f:
            links.append(json.loads(line))
    print(f'Loaded {len(links):,} place_links rows', file=sys.stderr)

    # Index links by (source, id) for upsert.
    by_key = {(r['source_project'], r['source_id']): r for r in links}

    # --- Load normalized entries (for blobs/descriptions).
    norm_by_key = {}
    for src_path in NORMALIZED_DIR.glob('*.jsonl'):
        for line in src_path.open():
            n = json.loads(line)
            norm_by_key[(n['source_project'], n['source_id'])] = n
    print(f'Loaded {len(norm_by_key):,} normalized entries', file=sys.stderr)

    # --- Decide which entries to send to Claude.
    todo = []
    for r in links:
        # 'type_no_ngib_equivalent' is excluded by design: the
        # deterministic matcher has already concluded that this
        # entry's place_type (cuartón, despoblado, término, partido
        # judicial, etc.) has no modern NGIB equivalent. Letting the
        # LLM second-guess that and bind it to a same-named municipi
        # produces the exact conflations the user wants to avoid
        # (e.g. "ALAYOR (término)" mapping to the Alaior Municipi).
        is_unlinked = r['method'] == 'unlinked'
        is_borderline = (
            args.include_borderline and r.get('ngib_id') is not None
            and r.get('confidence', 0) < BORDERLINE_CUTOFF
            and len(r.get('candidate_ngib_ids') or []) >= 2
        )
        is_parent_link = (
            args.include_linked_to_parent
            and r['method'] == 'linked_to_parent'
        )
        if is_unlinked or is_borderline or is_parent_link:
            todo.append(r)

    if args.max:
        todo = todo[:args.max]

    print(f'Will process: {len(todo):,} entries', file=sys.stderr)

    if not todo:
        print('Nothing to do. Bye.', file=sys.stderr)
        return

    # --- Shortlist per entry.
    by_island = load_gazetteer()
    batches: list[list[dict]] = []
    current: list[dict] = []
    for r in todo:
        key = (r['source_project'], r['source_id'])
        n = norm_by_key.get(key)
        if not n:
            continue
        # Resolve raw blob (it was stored as a JSON-encoded string by
        # normalize_sources to avoid DuckDB schema unification).
        blob = n.get('raw')
        if isinstance(blob, str):
            try:
                blob = json.loads(blob)
            except Exception:
                blob = None
        pool = by_island.get(n['island'], [])
        # For linked_to_parent entries, surface the current parent in
        # the shortlist so the LLM can confirm or override.
        must_id = r.get('ngib_id') if r['method'] == 'linked_to_parent' else None
        candidates = shortlist_candidates(n, pool, must_include_id=must_id)
        item = {
            'key':         key,
            'entry':       n,
            'blob':        blob,
            'candidates':  candidates,
        }
        current.append(item)
        if len(current) == BATCH_SIZE:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    print(f'Built {len(batches):,} batches of up to {BATCH_SIZE} entries each',
          file=sys.stderr)

    # --- Load cache.
    cache = load_cache()
    print(f'Cache: {len(cache):,} previous decisions', file=sys.stderr)

    # Even in dry-run we apply previously-cached decisions: the script
    # is also the way to re-attach cached LLM decisions after a fresh
    # resolve_entities run that clobbered place_links.jsonl.
    new_decisions = []
    new_cache_rows = []
    cached_only_applied = 0
    if not args.use_llm:
        for batch in batches:
            for it in batch:
                ck = cache_key(f'{it["key"][0]}:{it["key"][1]}',
                               [c['ngib_id'] for c in it['candidates']])
                if ck in cache:
                    new_decisions.append({'key': it['key'], 'decision': cache[ck]['decision']})
                    cached_only_applied += 1
        n_uncached = len(todo) - cached_only_applied
        print(f'\n[dry-run] Cached decisions applied: {cached_only_applied}',
              file=sys.stderr)
        print(f'[dry-run] {n_uncached} entries would need API calls.',
              file=sys.stderr)
        if cached_only_applied:
            _apply_decisions(links, by_key, new_decisions)
            with LINKS_PATH.open('w', encoding='utf-8') as f:
                for r in links:
                    f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
                    f.write('\n')
            print(f'Re-applied cache to {LINKS_PATH.relative_to(ROOT)}.',
                  file=sys.stderr)
        if n_uncached:
            print('\nRe-run with --use-llm to actually call the API on the remaining entries.',
                  file=sys.stderr)
            print('Sample prompt for an uncached batch:\n', file=sys.stderr)
            sample_batch = next(
                (b for b in batches
                 if any(cache_key(f'{it["key"][0]}:{it["key"][1]}',
                                  [c['ngib_id'] for c in it['candidates']]) not in cache
                        for it in b)),
                None,
            )
            if sample_batch:
                print(build_user_prompt(sample_batch)[:2500], file=sys.stderr)
        return

    # --- Call Claude per batch.
    try:
        import anthropic
    except ImportError:
        print('ERROR: anthropic package not installed. '
              'Run: uv sync --extra llm', file=sys.stderr)
        sys.exit(1)
    if not os.environ.get('ANTHROPIC_API_KEY'):
        print('ERROR: ANTHROPIC_API_KEY env var not set.', file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic()
    new_decisions = []
    new_cache_rows = []
    t0 = time.time()

    for bi, batch in enumerate(batches, 1):
        cached_in_batch = []
        unmatched_in_batch = []
        for it in batch:
            ck = cache_key(f'{it["key"][0]}:{it["key"][1]}',
                           [c['ngib_id'] for c in it['candidates']])
            if ck in cache:
                cached_in_batch.append((it, cache[ck]))
            else:
                unmatched_in_batch.append(it)

        # Apply cached.
        for it, c in cached_in_batch:
            new_decisions.append({'key': it['key'], 'decision': c['decision']})

        if not unmatched_in_batch:
            continue

        prompt = build_user_prompt(unmatched_in_batch)
        try:
            results = call_claude(client, args.model, prompt)
        except Exception as ex:
            print(f'  batch {bi}: ERROR {ex!r}; skipping', file=sys.stderr)
            continue

        for i, it in enumerate(unmatched_in_batch, 1):
            res = next((r for r in results if r.get('id') == i), None)
            if not res:
                continue
            decision = {
                'ngib_id':    res.get('ngib_id'),
                'confidence': float(res.get('confidence') or 0.0),
                'reason':     res.get('reason', ''),
            }
            new_decisions.append({'key': it['key'], 'decision': decision})
            ck = cache_key(f'{it["key"][0]}:{it["key"][1]}',
                           [c['ngib_id'] for c in it['candidates']])
            new_cache_rows.append({
                'cache_key': ck,
                'source_project': it['key'][0],
                'source_id':      it['key'][1],
                'decision':       decision,
            })

        if bi % 5 == 0 or bi == len(batches):
            dt = time.time() - t0
            print(f'  batch {bi}/{len(batches)}  ({dt:5.1f}s elapsed)',
                  file=sys.stderr)

    append_cache(new_cache_rows)
    print(f'\nApplied {len(new_decisions):,} LLM decisions; '
          f'{len(new_cache_rows):,} new cache entries.',
          file=sys.stderr)

    # --- Upsert into place_links.
    n_linked_before = sum(1 for r in links if r.get('ngib_id'))
    counters = _apply_decisions(links, by_key, new_decisions)
    n_linked_after = sum(1 for r in links if r.get('ngib_id'))
    print(f'  link rate: {n_linked_before} → {n_linked_after} '
          f'(+{n_linked_after - n_linked_before})', file=sys.stderr)
    print(f'  parent → specific upgrades: {counters["upgraded"]}', file=sys.stderr)
    print(f'  parent links confirmed:     {counters["confirmed"]}', file=sys.stderr)
    print(f'  new links (was unlinked):   {counters["new"]}', file=sys.stderr)

    with LINKS_PATH.open('w', encoding='utf-8') as f:
        for r in links:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
            f.write('\n')
    print(f'Updated {LINKS_PATH.relative_to(ROOT)}.', file=sys.stderr)


if __name__ == '__main__':
    main()
