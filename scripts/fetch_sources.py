#!/usr/bin/env python3
"""Snapshot the five Balearic sibling projects' published JSONs.

Copies each sibling's ``web/data.json`` (or, for nomenclator_1860, the
five JSONs under ``web/data/``) into ``data/sources/`` and writes a
manifest with SHA-256 + mtime so downstream stages can detect when a
sibling has changed.

This is the only script in the meta pipeline that hard-codes paths to
the sibling repositories. Everything else reads exclusively from
``data/sources/``.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIBLINGS_ROOT = ROOT.parent
SOURCES_DIR = ROOT / 'data' / 'sources'

# Each entry: (logical name, source path relative to siblings root,
#              destination path relative to data/sources/).
COPY_PLAN: list[tuple[str, str, str]] = [
    ('floridablanca',          'floridablanca/web/data.json',                 'floridablanca.json'),
    ('minano',                 'minano/web/data.json',                        'minano.json'),
    ('madoz',                  'madoz/web/data.json',                         'madoz.json'),
    ('madoz_abbreviations',    'madoz/web/abbreviations.json',                'madoz_abbreviations.json'),
    ('nomenclator_1860_entries',     'nomenclator_1860/web/data/entries.json',        'nomenclator_1860/entries.json'),
    ('nomenclator_1860_notes',       'nomenclator_1860/web/data/notes.json',          'nomenclator_1860/notes.json'),
    ('nomenclator_1860_summaries',   'nomenclator_1860/web/data/summaries.json',      'nomenclator_1860/summaries.json'),
    ('nomenclator_1860_errata',      'nomenclator_1860/web/data/errata.json',         'nomenclator_1860/errata.json'),
    ('nomenclator_1860_source_meta', 'nomenclator_1860/web/data/source_metadata.json','nomenclator_1860/source_metadata.json'),
    ('riera',                  'riera/web/data.json',                         'riera.json'),
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 16), b''):
            h.update(block)
    return h.hexdigest()


def main():
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'siblings_root': str(SIBLINGS_ROOT),
        'files': {},
    }

    for name, src_rel, dst_rel in COPY_PLAN:
        src = SIBLINGS_ROOT / src_rel
        dst = SOURCES_DIR / dst_rel
        if not src.exists():
            print(f'  MISSING  {name:40s} {src}', file=sys.stderr)
            manifest['files'][name] = {'status': 'missing', 'source': str(src)}
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        digest = sha256(dst)
        manifest['files'][name] = {
            'status': 'ok',
            'source': str(src),
            'dest': str(dst.relative_to(ROOT)),
            'bytes': size,
            'sha256': digest,
            'src_mtime': datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc).isoformat(timespec='seconds'),
        }
        print(f'  ok       {name:40s} {size/1024:>8.1f} KB  {digest[:10]}…', file=sys.stderr)

    manifest_path = SOURCES_DIR / '_manifest.json'
    with manifest_path.open('w') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f'Wrote {manifest_path.relative_to(ROOT)}', file=sys.stderr)


if __name__ == '__main__':
    main()
