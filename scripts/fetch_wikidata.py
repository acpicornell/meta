#!/usr/bin/env python3
"""Fetch Wikidata items located in the Balearic Islands.

For each of the five islands (Mallorca, Menorca, Eivissa, Formentera,
Cabrera) we query Wikidata for every item with coordinates that has
the island in its transitive ``located in administrative territorial
entity`` (P131) chain. We collect the Catalan / Castilian / English
labels and aliases plus the ``instance of`` (P31) values.

The output, ``data/wikidata.jsonl``, is then used by
``build_gazetteer.py`` as an extra ``source='wikidata'`` set of alias
rows. The matcher can then link Castilian and historical forms it had
no chance of resolving before — Wikidata systematically records
"Mahón" as an alias of Maó, "Pormany" / "San Antonio Abad" as aliases
of Sant Antoni de Portmany, etc.

This file is checked in so the pipeline does not need the network on
every run; re-fetch when Wikidata adds significant new data.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'data' / 'wikidata.jsonl'

ENDPOINT = 'https://query.wikidata.org/sparql'
USER_AGENT = (
    'meta-balears/0.1 (https://github.com/acpicornell/meta; '
    'historical gazetteer cross-linking) Python-urllib'
)

# Q-codes verified 2026-05-28.
ISLANDS = {
    'Mallorca':   'Q8828',
    'Menorca':    'Q52636',
    'Eivissa':    'Q52631',
    'Formentera': 'Q31916364',
    'Cabrera':    'Q52650',
}

# ``wdt:P131*`` is the transitive "located in administrative
# territorial entity" property. We anchor on each island so the result
# can be tagged with that island unambiguously. P625 is "coordinate
# location"; we keep only items that have one (filters out abstract
# entities like cycling teams or radio stations that ``P131`` would
# otherwise return).
SPARQL_TEMPLATE = """
SELECT
    ?item ?itemLabelCa ?itemLabelEs ?itemLabelEn
    ?coord ?descriptionCa
    (GROUP_CONCAT(DISTINCT ?aliasCa; SEPARATOR="|") AS ?aliasesCa)
    (GROUP_CONCAT(DISTINCT ?aliasEs; SEPARATOR="|") AS ?aliasesEs)
    (GROUP_CONCAT(DISTINCT ?aliasEn; SEPARATOR="|") AS ?aliasesEn)
    (GROUP_CONCAT(DISTINCT ?instance; SEPARATOR="|") AS ?instances)
    (GROUP_CONCAT(DISTINCT ?instanceLabel; SEPARATOR="|") AS ?instanceLabels)
WHERE {
  ?item wdt:P131* wd:%(island_q)s .
  ?item wdt:P625 ?coord .
  OPTIONAL { ?item rdfs:label ?itemLabelCa . FILTER(LANG(?itemLabelCa) = "ca") }
  OPTIONAL { ?item rdfs:label ?itemLabelEs . FILTER(LANG(?itemLabelEs) = "es") }
  OPTIONAL { ?item rdfs:label ?itemLabelEn . FILTER(LANG(?itemLabelEn) = "en") }
  OPTIONAL { ?item schema:description ?descriptionCa . FILTER(LANG(?descriptionCa) = "ca") }
  OPTIONAL { ?item skos:altLabel ?aliasCa . FILTER(LANG(?aliasCa) = "ca") }
  OPTIONAL { ?item skos:altLabel ?aliasEs . FILTER(LANG(?aliasEs) = "es") }
  OPTIONAL { ?item skos:altLabel ?aliasEn . FILTER(LANG(?aliasEn) = "en") }
  OPTIONAL {
    ?item wdt:P31 ?instance .
    OPTIONAL { ?instance rdfs:label ?instanceLabel . FILTER(LANG(?instanceLabel) = "ca") }
  }
}
GROUP BY ?item ?itemLabelCa ?itemLabelEs ?itemLabelEn ?coord ?descriptionCa
"""

QID_RX = re.compile(r'/entity/(Q\d+)$')
COORD_RX = re.compile(r'Point\(([-\d.]+)\s+([-\d.]+)\)')


def sparql(query: str) -> dict:
    """POST a SPARQL query (POSTed to bypass URL-length and proxy
    caches) and return the JSON response."""
    data = urllib.parse.urlencode({'query': query, 'format': 'json'}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'application/sparql-results+json',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))


def parse_qid(uri: str) -> str | None:
    m = QID_RX.search(uri)
    return m.group(1) if m else None


def parse_coord(point: str) -> tuple[float, float] | tuple[None, None]:
    m = COORD_RX.match(point or '')
    if not m:
        return (None, None)
    lon, lat = m.groups()
    return (float(lon), float(lat))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--island', help='Restrict to one island (for testing)')
    args = ap.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    seen_qids: set[str] = set()
    targets = {args.island: ISLANDS[args.island]} if args.island else ISLANDS

    with OUT.open('w', encoding='utf-8') as f_out:
        for island_name, island_q in targets.items():
            print(f'  → {island_name:11s} ({island_q})… ', end='', file=sys.stderr, flush=True)
            t0 = time.time()
            query = SPARQL_TEMPLATE % {'island_q': island_q}
            result = sparql(query)
            rows = result.get('results', {}).get('bindings', [])
            kept = 0
            for r in rows:
                qid = parse_qid(r.get('item', {}).get('value', ''))
                if not qid or qid in seen_qids:
                    continue
                seen_qids.add(qid)
                lon, lat = parse_coord(r.get('coord', {}).get('value', ''))
                aliases_ca = (r.get('aliasesCa', {}).get('value') or '').split('|')
                aliases_es = (r.get('aliasesEs', {}).get('value') or '').split('|')
                aliases_en = (r.get('aliasesEn', {}).get('value') or '').split('|')
                instance_qids = [
                    parse_qid(u) for u in
                    (r.get('instances', {}).get('value') or '').split('|') if u
                ]
                instance_labels = [
                    s for s in (r.get('instanceLabels', {}).get('value') or '').split('|') if s
                ]
                obj = {
                    'qid':           qid,
                    'island':        island_name,
                    'label_ca':      r.get('itemLabelCa', {}).get('value'),
                    'label_es':      r.get('itemLabelEs', {}).get('value'),
                    'label_en':      r.get('itemLabelEn', {}).get('value'),
                    'description_ca':r.get('descriptionCa', {}).get('value'),
                    'lon': lon, 'lat': lat,
                    'aliases_ca':    [a for a in aliases_ca if a],
                    'aliases_es':    [a for a in aliases_es if a],
                    'aliases_en':    [a for a in aliases_en if a],
                    'instance_qids': [q for q in instance_qids if q],
                    'instance_labels': instance_labels,
                }
                f_out.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
                f_out.write('\n')
                kept += 1
                total += 1
            dt = time.time() - t0
            print(f'{kept:>5d} new items in {dt:5.1f}s', file=sys.stderr)

    print(f'\nTotal: {total:,} unique Wikidata items → {OUT.relative_to(ROOT)}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
