# Method — linking, resolution and caveats

How `meta` ties the five sibling gazetteers to a single canonical identity. For the scripts and commands see [PIPELINE.md](PIPELINE.md); for the web front-end and deployment see [SITE.md](SITE.md).

---

## Why this exists

Each of the five sibling projects (`floridablanca`, `minano`, `madoz`, `nomenclator_1860`, `riera`) is a digital edition of **one** nomenclator — it transcribes, structures and publishes that source's Balearic content. Read in isolation, each tells you what Madoz, or Riera, or the 1860 census says *about a place at a moment*.

But the historiographical value is in the **diachronic reading**: how does the entry for Sóller change from Floridablanca's 1787 tabular census to Madoz's 1845 narrative to the 1860 building count to Riera's 1881 administrative compendium? You can only do that if a single canonical identity ties the five articles together.

`meta` provides that identity. Every sibling article is resolved against the **NGIB** (Nomenclàtor Geogràfic de les Illes Balears, the official Balearic toponym registry), so that *Mahón / Maó*, *San Lorenzo / Sant Llorenç des Cardassar*, *Lluchmayor / Llucmajor*, *Iviza / Eivissa*, *Bunyola / Bunyola* all collapse onto a single `geographic_name_id` regardless of which 18th/19th-century spelling the source used.

---

## The corpus

| Year | Sibling project | Author / publisher | Articles | Sibling URL |
|---|---|---|---:|---|
| 1787 | `floridablanca` | INE re-transcription of the Floridablanca Census | 114 | [floridablanca.corpusbalear.org](https://floridablanca.corpusbalear.org/) |
| 1826-29 | `minano` | Sebastián Miñano y Bedoya (11 vol.) | 211 | [minano.corpusbalear.org](https://minano.corpusbalear.org/) |
| 1845-50 | `madoz` | Pascual Madoz (16 vol.) | 1 217 | [madoz.corpusbalear.org](https://madoz.corpusbalear.org/) |
| 1860 | `nomenclator_1860` | Junta General de Estadística | 2 962 | [1860.corpusbalear.org](https://1860.corpusbalear.org/) |
| 1881-87 | `riera` | Pablo Riera y Sans (12 vol.) | 123 | [riera.corpusbalear.org](https://riera.corpusbalear.org/) |

Each sibling lives in a separate repo next to this one, under the same parent directory (`../floridablanca`, `../madoz`, …; the scripts resolve them from `ROOT.parent`). **`meta` does not re-transcribe anything**: it reads each sibling's published `web/data.json` (committed to that sibling's repo), normalises the columns, and links the result to NGIB.

The two long-form sources (Madoz, Riera) have **supplementary volumes** with late additions / errata. `meta` marks these as `is_supplement = true` and sorts them after the primary entry on the Lloc timeline:

- **Miñano Tom XI (Suplemento, 1829)** — 88 Balearic entries (55 «(adición)» updates + 33 new articles).
- **Madoz Tom XVI (Adiciones, 1850)** — 20 late Balearic entries, mostly minor estates.

Floridablanca, Nomenclàtor 1860 and Riera publish in their bound editions only.

---

## The dual-link data model

Earlier prototypes (v1) flattened every sibling article to **one** `ngib_id`. A systematic audit of the corpus shape revealed that only ~9 % of sibling articles describe a Municipi proper. The other 91 % are sub-features (predis, llogarets, caps, talaies, ports), administrative units (cuartones, partits judicials, diòcesis) or arxipèlag-wide entries. Forcing them all onto a single Municipi id produced systematic conflations.

The current model (v2) carries **two NGIB links per article** plus a kind tag:

| Field | Meaning | Example for «MADOZ — SOLLER (puerto)» |
|---|---|---|
| `describes_ngib_id` | The NGIB entity the article is *about* | NGIB id of *Port de Sóller* (the llogaret) |
| `parent_ngib_id` | The NGIB municipi the entity sits *in* | NGIB id of *Sóller* (the municipi) |
| `entry_kind` | Semantic taxonomy | `feature_with_ngib` |

The `entry_kind` taxonomy:

| Value | What it describes | Share of corpus |
|---|---|---:|
| `municipality` | The article is about a Municipi | 360 (7.8 %) |
| `feature_with_ngib` | Sub-entity that has its own NGIB id (llogaret, port, cap, llogaret) | 1 801 (38.9 %) |
| `feature_no_ngib` | Predi / accident / construction without modern NGIB equivalent | 2 414 (52.2 %) |
| `jurisdictional` | Administrative or editorial category (partit judicial, diòcesi, isla, etc.) | 52 (1.1 %) |
| *(orphan)* | Neither `describes` nor `parent` resolved | 192 (4.1 % of total) |

This is the structural redesign of 2026-05-29 and is the model the site renders against.

---

## The 3-phase resolver

`scripts/resolve_entities.py`. For each sibling article, three phases run in order:

### Phase 1 — Decide `entry_kind`

Determinist where possible:

- If the sibling's `place_type` hint is jurisdictional (`partido judicial`, `cuartón`, `diócesis`, `obispado`, …) the kind is locked to `jurisdictional`.
- If the title normalises to an existing NGIB sub-feature on the same island (e.g. `SARRECÓ` → NGIB *s'Arracó*), a sub-feature shortcut promotes the article to `feature_with_ngib` and sets `describes_ngib_id` directly.
- Otherwise the kind stays provisional (`feature` or `municipality`) and gets confirmed/refined by phases 2-3.

The classifier uses **whole-word regex** rather than substring matching. This avoids real traps that bit earlier versions — `illa` is a substring of `villa`, so a naïve `if 'illa' in place_type` was silently routing every *Villa* entry into the NGIB_ISLAND pool. The regex form (`\b(?:villa|vila|lugar|aldea|…)\b`) refuses to match across word boundaries.

### Phase 2 — Resolve `parent_ngib_id`

Each adapter in `normalize_sources.py` emits a `parent_municipality_hint` per article when available:

| Source | Hint origin | Coverage |
|---|---|---:|
| Floridablanca | `name_current` (each row IS a municipi) | 100 % |
| Nomenclàtor 1860 | `municipality` column (each row's parent) | 100 % |
| Madoz | `municipality` field + first `cross_reference` | ~80 % |
| Riera | `municipality` field + `cross_references` | ~84 % |
| Miñano | Inferred from `description` cross-references | ~40 % |

If the hint is present, `parent_ngib_id` is set to the NGIB Municipi matching that string (via the historical-variants table on the relevant island). When no hint exists, fallbacks are: (a) match the Madoz `cross_references[0]` against same-island Municipis, (b) reuse the parent of another entry from the same source with the same title that resolved as `municipality`. Otherwise `parent_method = 'unresolved'`.

### Phase 3 — Resolve `describes_ngib_id` strictly within the parent's terme

This is the load-bearing change vs. v1. The describes search **never widens to the whole island**. It is scoped to NGIB rows whose `municipality == parent.municipality_name`, plus historical-variants rows whose canonical target is on the same terme. The cascade inside that scoped pool:

1. **`historical_curated`** (confidence 0.99) — exact match against an entry in the curated variants table (see below).
2. **`exact_norm`** (0.97) — exact match after normalising accents, articles, hyphens, punctuation.
3. **`fuzzy_wratio`** (≥ 0.88) — rapidfuzz weighted ratio with a length-similarity guard.
4. **`fuzzy_token_set`** (≥ 0.85) — second pass for multi-token titles unresolved by step 3.

If nothing in the scoped pool exceeds threshold, the article is linked **by parent only**. The article's `entry_kind` is set to `feature_no_ngib` and it appears on the parent municipi's Lloc page under «Articles dins el terme sense entrada NGIB pròpia».

The scoping eliminates an entire class of cross-municipi conflations seen in v1 — for instance, a Madoz «PORT (es)» under Sóller can no longer fuzzy-match to «es Port» in Banyalbufar, because Banyalbufar isn't in the pool.

---

## The curated historical-variants table

`scripts/build_gazetteer.py#HISTORICAL_VARIANTS` (~360 entries). The cascade alone cannot resolve three classes of OCR / orthographic noise; the table fills the gap. Three patterns covered:

### 1. Castilianisations
The 18th-19th-century Spanish administrative forms that the cascade's normaliser does not transform automatically:

```
Mahón                → Maó
San Lorenzo          → Sant Llorenç des Cardassar
Iviza                → Eivissa
Bunyola              → Bunyola         (kept; Castilianised spelling matches modern)
Lluchmayor           → Llucmajor
San Antonio Abad     → Sant Antoni de Portmany
Santa Eulalia del Río → Santa Eulària des Riu
Nuestra Señora de Jesús → Jesús
…
```

### 2. OCR variants
Single-character OCR errors over 19th-century facsimile typography:

```
Escorga              → Escorca         (O/G confusion)
Biniarroig           → Biniarroi       (extra 'g')
Caimary              → Caimari         (Y/I)
Vañalbufar           → Banyalbufar     (V/B + N/NY)
Fornaluig            → Fornalutx       (G/X)
…
```

### 3. Madoz «(San / Santa / Sant)» inversion
Madoz files Catalan saints under the proper name with the qualifier in parens: `LUIS (San)` = Sant Lluís, `MARIA (Santa) del Camí` = Santa Maria del Camí. Without intervention the parenthetical strip would drop the saint marker entirely and the cascade would search for `LUIS` against NGIB, finding nothing.

A helper `_reverse_saint_inversion()` in `resolve_entities.py` reorders these to «San X» / «Santa X» / «Sant X» before paren stripping, so the curated table can match.

The table is **assembled by sweep, not hand-curated entry-by-entry**. Each addition is triggered by:

- The missing-source audit (any Municipi without all 5 source-years attested → investigate which sibling fails and why → add to the table if the failure is a known pattern), or
- The orphan audit (a periodic Playwright-driven fuzzy scan of unresolved entries against same-island NGIB settlements at WRatio ≥ 88 % — see `project_meta_orphan_audit_pattern`).

This is documented to avoid the table growing into hand-maintained noise: every addition has a traceable reason.

---

## NGIB as canonical anchor

The Nomenclàtor Geogràfic de les Illes Balears, published by the Govern de les Illes Balears (IDEIB), is the official Balearic toponym registry. It contains ~55 535 *preferent* canonical names + ~1 366 *variant* spellings, distributed under CC BY ([open data catalogue](https://intranet.caib.es/opendatacataleg/es/dataset/lloc_anomenat)).

`scripts/build_gazetteer.py` reads the local copy from `data/ngib/` in this repo (gitignored). The Miñano project ingests it first, so copy `../minano/data/ngib/` to `data/ngib/` before building the gazetteer. The parquet ingestion produces ~50 000 rows with these fields used by the resolver:

- `geographic_name_id` — the canonical id we link to.
- `spelling` — the preferred spelling.
- `normalized` — the spelling lowercased + accent-stripped + article-aware (`l'Albufera` ↔ `Albufera`).
- `local_type` — INSPIRE class (`Municipi`, `Finca, possessió, lloc…`, `Capital de municipi`, `Cap, punta…`, …). Used to scope the matcher's search pool by category-compatible types.
- `municipality` — the terme this point falls in. Phase 3 scopes its describes search to this column.
- `island` — Mallorca / Menorca / Eivissa / Formentera / Cabrera. Used as a primary filter so the fuzzy step never crosses island boundaries.
- `lat` / `lng` — for the Mapa tab.

**The NGIB does not ship historical-Castilian variants of useful scale.** I verified this against the public MapServer API: the `Variant_nom_geografic` layer has only 2 entries tagged `PRIORITAT = 3 (històric)`, both 20th-century internal name changes. None of `Mahón`, `San Lorenzo`, `Iviza`, `Lluchmayor` exist as recognised variants. The ~360-entry curated table in `build_gazetteer.py` is therefore richer than what the API can provide.

---

## Wikidata enrichment

`scripts/fetch_wikidata.py`. A SPARQL query rooted at [Q107356467](https://www.wikidata.org/wiki/Q107356467) (*elements anomenats dins de les Illes Balears*) pulls ~6 900 Wikidata items with multilingual aliases (`skos:altLabel`). `scripts/build_wikidata_variants.py` then anchors ~1 500 of these to NGIB ids by label match, producing `data/wikidata_variants.parquet`.

The variants enter the resolver's pool alongside the curated table. They cover **official multilingual aliases** that wouldn't make sense to hand-curate one by one — Catalan / Castilian / French / English / German forms of place names that show up in Madoz quotations or Riera's foreign-traveller appendices.

The SPARQL query is cached to `data/wikidata.jsonl`; subsequent pipeline runs don't re-hit the endpoint.

---

## LLM tiebreaker

`scripts/resolve_with_llm.py`. Optional. Sends borderline cases (where the cascade produces 2-3 high-confidence candidates and can't pick) to **Claude Sonnet 4.6** with prompt caching, requesting a JSON decision plus a one-sentence rationale. Cached to `data/llm_cache.jsonl` so subsequent runs cost zero.

The cache currently holds **1 850 decisions** from earlier rounds. With `--include-linked-to-parent`, additional cases can be queued.

Authentication: `ANTHROPIC_API_KEY` is read from this repo's own `.env` (gitignored), loaded by `scripts/resolve_with_llm.py`, following the same convention as the sibling projects.

---

## Quality and honest caveats

The pipeline is **not** an authoritative database. The numbers (1 520 places, 4 435 linked, 95.9 % coverage) are derived from a chain of imperfect tools:

| Step | Failure modes |
|---|---|
| OCR over 19th-century facsimiles | Confuses *ó* / *ò*, *v* / *b*, *m* / *rn*, splits two-column entries |
| LLM extraction (sibling projects) | Hallucinations, occasionally drops a column |
| Regex normalisation | Misses unfamiliar punctuation conventions |
| Fuzzy matching | False positives when the place is generic («Sa Punta») |
| LLM tiebreaker | Token cost makes exhaustive review impractical |

The cascade **prefers not to resolve over resolving wrong**: the 192 orphan articles in the current corpus are mostly cases where we chose to leave the question open. They appear in the Explore tab under «Sense vincle NGIB» so anyone can inspect them.

We document this explicitly in the Notes tab («Sobre la fiabilitat del corpus»). If you spot an article that should be in but isn't, or a link that looks wrong, the repos are public and corrections are welcome.

---

## Limitations and future directions

**Known limitations**:

1. **The 192 orphan articles**. Mostly archipelago-wide articles (*Baleares*, *Mallorca-isla*, *Menorca-diócesi*), lost predis, and fuzzy noise. They're inspectable via the Explore «Sense vincle NGIB» filter.
2. **No diachronic typing**. A place that was a feudal *señorío* in 1787, a Liberal-era *partido judicial* in 1845 and a Restoration *audiencia* member in 1881 carries one NGIB municipi link — its jurisdictional history is visible in the article texts but not modelled in the schema.
3. **No spatial reasoning on the map**. The 1.520 markers cluster but don't carry any rendering of administrative boundaries by year (which would be the natural next layer).
4. **Sub-features without NGIB ids**. 2.414 articles (52 % of the corpus) are sub-features without their own NGIB row. They're correctly anchored to the parent municipi but invisible on the map.

**Possible next steps**:

- Add a `wikidata_id` column to `places` so each canonical NGIB place links to its Wikidata sibling when known.
- Per-century historical-boundary overlays on the map (the GIS data exists, the work is curating which historical sources match which year layer).
- Per-source LLM extraction of the structured fields that the siblings haven't captured yet (postal info from Riera, contribution breakdowns from Madoz).
