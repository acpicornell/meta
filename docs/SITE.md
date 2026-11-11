# Site — front-end and deployment

---

## The front-end at a glance

Vanilla JS, no build step, no framework. Loads `web/data.json` once (~2.5 MB) on first render. `web/data-blobs.json` (~3.1 MB) is lazy-loaded on the first Lloc click so the home page comes up immediately.

**Tabs** (in nav order):

| Tab | Content |
|---|---|
| Inici | Hero card with KPIs, the 5 sources tinted by century palette, 7 action pills (including a «Lloc a l'atzar»), short narrative |
| Explorar | 1.520 places with island/type/confidence/coverage filters + source-checkbox set, with compact source-dots per row |
| Mapa | Leaflet + OSM, marker-cluster, filterable by island / type / source / has-article, with per-source filter |
| Lloc | A single place: header + mini-map + chronological timeline of attesting articles + sub-features grid + minor + jurisdictional entries |
| Fonts | Per-source deep-dive: author, dates, editorial structure, biases, sibling link |
| Abreviatures | 242 abbreviations across 5 sources, searchable, multi-column at wide widths |
| Estadístiques | 7 chart blocks: distribution by source-count, by island, coverage per source, corpus composition, NGIB types, top-20 most documented, source × island heatmap |
| Notes | Methodology in depth + reliability caveats + license |

The site uses a **per-source year palette** (mossy green / slate blue / mauve / ochre / sienna for 1787 / 1826 / 1845 / 1860 / 1881) on source-card top stripes, year badges, source-dots, Fonts left-borders, timeline year markers and Stats heatmap. This is the only colour signal in the site beyond the dominant beige / brown academic palette.

---

## Deploying to Cloudflare Workers

The site runs on **Cloudflare Workers Static Assets**, not Pages. The config lives at `wrangler.jsonc`:

```jsonc
{
  "name": "meta-balears",
  "compatibility_date": "2026-05-28",
  "assets": { "directory": "web" }
}
```

Deploy:

```bash
npx wrangler login         # one-off, opens the browser
npx wrangler deploy        # ships ./web to https://nomenclators.corpusbalear.org
```

The `_headers` file inside `web/` is honoured by Workers Static Assets (the syntax matches the Pages convention). A manual `wrangler deploy` ships `web/` from disk. On every push to `main`, `.github/workflows/deploy.yml` also deploys the committed `web/` (after running `scripts/check_site.py web`), once the `CLOUDFLARE_API_TOKEN` secret is set; nothing is rebuilt in CI.
