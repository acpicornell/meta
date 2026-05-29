// Meta · Nomenclàtors balears — static web, vanilla JS.

const state = {
  data: null,                 // full data.json
  blobs: null,                // data-blobs.json (lazy)
  blobsPromise: null,         // outstanding fetch promise
  filtered: [],
  search: "",
  island: "",
  type: "",
  conf: "",
  combine: "any",
  sources: new Set(),
  page: 0,
  perPage: 100,
  currentPlace: null,
};

const SOURCE_LABEL = {
  floridablanca:    "Floridablanca",
  minano:           "Miñano",
  madoz:            "Madoz",
  nomenclator_1860: "Nomenclàtor 1860",
  riera:            "Riera",
};
const SOURCE_YEAR = {
  floridablanca: 1787, minano: 1826, madoz: 1845, nomenclator_1860: 1860, riera: 1881,
};
const SOURCE_ORDER = ["floridablanca", "minano", "madoz", "nomenclator_1860", "riera"];

// ---------------------- utilities --------------------------------------------
// Strip Miñano Tom XI / Madoz Tom XVI / Riera supplement suffixes
// ("(adición)", "(addicional)", "(addició)") from a displayed title.
// The SUPLEMENT badge on the card already conveys the same info.
const SUPP_TITLE_RX = /\s*\(\s*ad+ici[oó]n(?:al)?\s*\)\s*$/i;
function stripSupp(t) {
  return (t || "").replace(SUPP_TITLE_RX, "").trim();
}

function esc(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function $(id) { return document.getElementById(id); }
function fmt(n) { return Number(n).toLocaleString("ca-ES"); }
function norm(s) {
  return s ? s.toString().toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "") : "";
}

// ---------------------- tabs -------------------------------------------------
function gotoTab(t) {
  document.querySelectorAll(".tabs .tab").forEach(b =>
    b.classList.toggle("active", b.dataset.toptab === t));
  document.querySelectorAll(".tab-content").forEach(sec =>
    sec.classList.toggle("active", sec.dataset.toptab === t));
  if (t === "stats") renderStats();
  if (t !== "place") {
    // Hide the place tab when leaving it (it appears only after a click).
    const placeTab = document.querySelector('[data-toptab="place"].tab');
    if (placeTab && t !== "place") placeTab.hidden = !state.currentPlace;
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function initTabs() {
  document.querySelectorAll(".tabs .tab").forEach(btn => {
    btn.addEventListener("click", () => gotoTab(btn.dataset.toptab));
  });
  document.querySelectorAll("[data-goto]").forEach(el => {
    el.addEventListener("click", ev => {
      ev.preventDefault();
      gotoTab(el.dataset.goto);
    });
  });
}

// ---------------------- data load -------------------------------------------
// Cache-busting query string for the two JSON payloads. Forces the
// browser to treat each page load as a fresh fetch, regardless of
// disk cache or memory cache state. Combined with the no-store
// headers (HTML <meta> + Cloudflare _headers) this guarantees that
// reloading the page always shows the latest deploy.
const CACHE_BUST = `?t=${Date.now()}`;

async function loadData() {
  const resp = await fetch("data.json" + CACHE_BUST, { cache: "no-store" });
  state.data = await resp.json();
  fillInitialCounters();
  initFilters();
  applyFilters();
  // URL place deep-link.
  const url = new URL(location.href);
  const ngib = url.searchParams.get("ngib");
  if (ngib) openPlace(ngib);
}

async function ensureBlobs() {
  if (state.blobs) return state.blobs;
  if (state.blobsPromise) return state.blobsPromise;
  state.blobsPromise = (async () => {
    const resp = await fetch("data-blobs.json" + CACHE_BUST, { cache: "no-store" });
    state.blobs = await resp.json();
    return state.blobs;
  })();
  return state.blobsPromise;
}

function fillInitialCounters() {
  $("home-stat-places").textContent  = fmt(state.data.totals.places);
  $("home-stat-linked").textContent  = fmt(state.data.totals.linked_entries);
  const inAll = state.data.places.filter(p =>
    new Set(p.entries.map(e => e.source)).size === 5).length;
  $("home-stat-in-all").textContent = fmt(inAll);
}

// ---------------------- filters ---------------------------------------------
function initFilters() {
  const islands = new Set(), types = new Set();
  for (const p of state.data.places) {
    if (p.island) islands.add(p.island);
    if (p.local_type) types.add(p.local_type);
  }
  const fillSelect = (id, items) => {
    const sel = $(id);
    [...items].sort((a, b) => a.localeCompare(b, "ca"))
      .forEach(v => {
        const opt = document.createElement("option");
        opt.value = v; opt.textContent = v;
        sel.appendChild(opt);
      });
  };
  fillSelect("f-island", islands);
  fillSelect("f-type", types);

  $("search").addEventListener("input", ev => {
    state.search = ev.target.value;
    state.page = 0;
    applyFilters();
  });
  $("f-island").addEventListener("change", ev => { state.island = ev.target.value; state.page = 0; applyFilters(); });
  $("f-type").addEventListener("change",   ev => { state.type   = ev.target.value; state.page = 0; applyFilters(); });
  $("f-conf").addEventListener("change",   ev => { state.conf   = ev.target.value; state.page = 0; applyFilters(); });
  $("f-combine").addEventListener("change",ev => { state.combine= ev.target.value; state.page = 0; applyFilters(); });
  document.querySelectorAll(".sources-row input[data-source]").forEach(cb => {
    cb.addEventListener("change", () => {
      state.sources = new Set(
        [...document.querySelectorAll(".sources-row input:checked")]
          .map(c => c.dataset.source));
      state.page = 0;
      applyFilters();
    });
  });
  $("clear-filters").addEventListener("click", clearFilters);
}

function anyFilterActive() {
  return Boolean(
    state.search || state.island || state.type || state.conf
    || state.combine !== "any" || state.sources.size
  );
}

function refreshClearButton() {
  const btn = $("clear-filters");
  if (!btn) return;
  btn.hidden = !anyFilterActive();
}

function clearFilters() {
  state.search = "";
  state.island = "";
  state.type   = "";
  state.conf   = "";
  state.combine = "any";
  state.sources = new Set();
  state.page = 0;
  $("search").value = "";
  $("f-island").value = "";
  $("f-type").value = "";
  $("f-conf").value = "";
  $("f-combine").value = "any";
  document.querySelectorAll(".sources-row input[data-source]")
    .forEach(cb => { cb.checked = false; });
  applyFilters();
}

// ---------------------- filtering -------------------------------------------

// Collect every entry attached to a place across its four buckets.
// The Explore-tab filters and counts treat all four equivalently
// (each is a sibling article about / inside / referring to this
// canonical NGIB place).
function allEntriesOf(p) {
  const out = [];
  for (const e of (p.entries || [])) out.push(e);
  for (const e of (p.minor_entries || [])) out.push(e);
  for (const e of (p.jurisdictional_entries || [])) out.push(e);
  return out;
}

function placeBestConfidenceBand(p) {
  // The strongest confidence band attested by any entry in this place.
  let best = "sense";
  const rank = { sense: 0, baixa: 1, mitjana: 2, alta: 3 };
  for (const e of allEntriesOf(p)) {
    if (rank[e.describes_band] > rank[best]) best = e.describes_band;
  }
  return best;
}

function placeMatchesSearch(p, q) {
  if (!q) return true;
  const qn = norm(q);
  if (norm(p.name).includes(qn)) return true;
  if (norm(p.municipality).includes(qn)) return true;
  for (const v of (p.variants || [])) if (norm(v).includes(qn)) return true;
  // Also search across the titles of every entry — this surfaces
  // matches like «PORMAÑY», «ALAYOR (peñas)», «SARRECÓ» that live in
  // the minor / jurisdictional buckets and are not part of the
  // canonical name.
  for (const e of allEntriesOf(p)) {
    if (norm(e.title).includes(qn)) return true;
  }
  // Search across child place names too (a Municipi page surfaces if
  // it has a sub-feature whose name matches the query).
  for (const c of (p.child_places || [])) {
    if (norm(c.name).includes(qn)) return true;
  }
  return false;
}

function placeMatchesSources(p) {
  if (state.sources.size === 0) return true;
  const present = new Set(allEntriesOf(p).map(e => e.source));
  if (state.combine === "all") {
    for (const s of state.sources) if (!present.has(s)) return false;
    return true;
  } else {
    for (const s of state.sources) if (present.has(s)) return true;
    return false;
  }
}

function applyFilters() {
  const showOrphans = state.conf === "sense";
  if (showOrphans) {
    // Render the orphan tail (entries with neither parent nor
    // describes NGIB id) as if each row were a one-source "place".
    // The data property is `orphans` in v2 (was `unlinked` in v1).
    const orphans = state.data.orphans || {};
    const all = [];
    for (const src of SOURCE_ORDER) {
      for (const u of (orphans[src] || [])) {
        if (state.island && u.island !== state.island) continue;
        if (state.type && u.place_type !== state.type) continue;
        if (state.sources.size && !state.sources.has(src)) continue;
        if (!placeMatchesSearchUnlinked(u, state.search)) continue;
        all.push({ ...u, source: src });
      }
    }
    state.filtered = all;
    renderUnlinkedList();
    renderPagination();
    $("result-count").textContent = `${fmt(all.length)} articles orfes (sense vincle NGIB)`;
    refreshClearButton();
    return;
  }

  state.filtered = state.data.places.filter(p => {
    if (state.island && p.island !== state.island) return false;
    if (state.type && p.local_type !== state.type) return false;
    if (state.conf) {
      const band = placeBestConfidenceBand(p);
      if (band !== state.conf) return false;
    }
    if (!placeMatchesSearch(p, state.search)) return false;
    if (!placeMatchesSources(p)) return false;
    return true;
  });
  renderResults();
  renderPagination();
  $("result-count").textContent =
    `${fmt(state.filtered.length)} lloc${state.filtered.length === 1 ? "" : "s"} canònic${state.filtered.length === 1 ? "" : "s"}`;
  refreshClearButton();
}

function placeMatchesSearchUnlinked(u, q) {
  if (!q) return true;
  const qn = norm(q);
  return norm(u.title).includes(qn) || norm(u.municipality).includes(qn);
}

// ---------------------- results ---------------------------------------------
function renderResults() {
  const start = state.page * state.perPage;
  const slice = state.filtered.slice(start, start + state.perPage);
  const el = $("results");
  if (slice.length === 0) {
    el.innerHTML = `<div class="loading">Cap lloc no coincideix amb els filtres.</div>`;
    return;
  }
  const html = slice.map(p => {
    const all = allEntriesOf(p);
    const present = new Set(all.map(e => e.source));
    const dots = SOURCE_ORDER.map(s => {
      const yr = SOURCE_YEAR[s];
      const has = present.has(s);
      return `<span class="source-dot ${has ? "has-" + yr : "empty"}" title="${esc(SOURCE_LABEL[s])} (${yr})${has ? "" : " — no atestat"}">${has ? yr : "·"}</span>`;
    }).join("");
    const variants = p.variants.filter(v => norm(v) !== norm(p.name));
    const nMain = (p.entries || []).length;
    const nMinor = (p.minor_entries || []).length;
    const nJur = (p.jurisdictional_entries || []).length;
    const nChildren = (p.child_places || []).length;
    const detailBits = [];
    if (nMain) detailBits.push(`${nMain} article${nMain === 1 ? "" : "s"}`);
    if (nChildren) detailBits.push(`${nChildren} sub-lloc${nChildren === 1 ? "" : "s"}`);
    if (nMinor) detailBits.push(`${nMinor} menor${nMinor === 1 ? "" : "s"}`);
    if (nJur) detailBits.push(`${nJur} jurisd.`);
    return `
      <div class="place-row" data-ngib="${esc(p.ngib_id)}">
        <div>
          <div class="place-name">${esc(p.name)}</div>
          <div class="place-meta">
            ${esc(p.island || "—")}
            ${p.municipality && p.municipality !== p.name ? ` · municipi de <strong>${esc(p.municipality)}</strong>` : ""}
            ${p.local_type ? ` · ${esc(p.local_type)}` : ""}
            ${detailBits.length ? ` · ${detailBits.join(" · ")}` : ""}
          </div>
          ${variants.length ? `<div class="variant-list">també: ${variants.map(esc).join(" · ")}</div>` : ""}
        </div>
        <div class="source-dots">${dots}</div>
      </div>
    `;
  }).join("");
  el.innerHTML = html;
  document.querySelectorAll(".place-row[data-ngib]").forEach(row => {
    row.addEventListener("click", () => openPlace(row.dataset.ngib));
  });
}

function renderUnlinkedList() {
  const start = state.page * state.perPage;
  const slice = state.filtered.slice(start, start + state.perPage);
  const el = $("results");
  if (slice.length === 0) {
    el.innerHTML = `<div class="loading">Cap article sense vincle no coincideix.</div>`;
    return;
  }
  // Orphans render as place-rows that act like entry cards — clicking
  // any of them opens the in-page entry modal (delegated handler).
  el.innerHTML = slice.map(u => `
    <div class="place-row place-row-clickable"
         data-entry-source="${esc(u.source)}"
         data-entry-id="${esc(u.source_id)}">
      <div>
        <div class="place-name">${esc(u.title)}</div>
        <div class="place-meta">
          <strong>${esc(SOURCE_LABEL[u.source])}</strong> (${SOURCE_YEAR[u.source]}) ·
          ${esc(u.island || "—")}
          ${u.municipality ? ` · municipi de ${esc(u.municipality)}` : ""}
          ${u.place_type ? ` · ${esc(u.place_type)}` : ""}
        </div>
      </div>
      <div class="source-dots"><span class="conf-pill conf-sense">sense vincle</span></div>
    </div>
  `).join("");
}

// ---------------------- pagination ------------------------------------------
// Re-render the current results page (places or orphans) — used by
// pagination buttons. Pagination only needs to switch the page
// slice; the active filter set is unchanged.
function renderCurrentPage() {
  if (state.conf === "sense") renderUnlinkedList();
  else renderResults();
}

function renderPagination() {
  const totalPages = Math.max(1, Math.ceil(state.filtered.length / state.perPage));
  const el = $("pagination");
  if (totalPages === 1) { el.innerHTML = ""; return; }
  el.innerHTML = `
    <button id="page-prev" ${state.page === 0 ? "disabled" : ""}>‹ Anterior</button>
    <span class="page-num">pàgina ${state.page + 1} de ${totalPages}</span>
    <button id="page-next" ${state.page >= totalPages - 1 ? "disabled" : ""}>Següent ›</button>
  `;
  $("page-prev").addEventListener("click", () => {
    state.page--;
    renderCurrentPage();
    renderPagination();
    window.scrollTo({top: 0, behavior: "smooth"});
  });
  $("page-next").addEventListener("click", () => {
    state.page++;
    renderCurrentPage();
    renderPagination();
    window.scrollTo({top: 0, behavior: "smooth"});
  });
}

// ---------------------- place detail (timeline) -----------------------------
async function openPlace(ngibId) {
  const place = state.data.places.find(p => p.ngib_id === ngibId);
  if (!place) return;
  state.currentPlace = place;
  // Make the Lloc tab visible.
  document.querySelector('[data-toptab="place"].tab').hidden = false;
  // Update URL without reloading.
  const url = new URL(location.href);
  url.searchParams.set("ngib", ngibId);
  history.replaceState(null, "", url);
  // Show loading state, then fetch blobs.
  $("place-detail").innerHTML = `<div class="loading"><span class="spinner"></span>Carregant articles…</div>`;
  gotoTab("place");
  const blobs = await ensureBlobs();
  renderPlaceDetail(place, blobs);
}

function citationFor(entry, blob) {
  const src = entry.source;
  if (src === "floridablanca") {
    return `Instituto Nacional de Estadística (1986). <em>Censo de Floridablanca, 1787. Tomo IV: Comunidades autónomas insulares y limítrofes: Canarias, Baleares, Ceuta y Melilla.</em> Madrid: INE.`;
  }
  if (src === "minano") {
    const vol = blob?.vol ? `vol. ${esc(blob.vol)}` : "";
    const page = blob?.page_printed ? `p. ${esc(blob.page_printed)}` : "";
    return `Miñano y Bedoya, S. (1826–1829). <em>Diccionario geográfico-estadístico de España y Portugal</em>. Madrid: Pierart-Peralta. ${[vol, page].filter(Boolean).join(", ")}.`;
  }
  if (src === "madoz") {
    const vol = blob?.vol ? `vol. ${esc(blob.vol)}` : "";
    const page = blob?.page_printed ? `p. ${esc(blob.page_printed)}` : "";
    return `Madoz, P. (1845–1850). <em>Diccionario geográfico-estadístico-histórico de España y sus posesiones de Ultramar</em>. Madrid. ${[vol, page].filter(Boolean).join(", ")}.`;
  }
  if (src === "nomenclator_1860") {
    return `Junta General de Estadística (1863). <em>Nomenclátor que comprende las poblaciones, grupos, edificios, viviendas, etc., según el recuento verificado en 1860</em>. Madrid: Imprenta de José M. Ducazcal.`;
  }
  if (src === "riera") {
    const vol = blob?.vol ? `vol. ${esc(blob.vol)}` : "";
    const page = blob?.page ? `p. ${esc(blob.page)}` : "";
    return `Riera y Sans, P. (1881–1887). <em>Diccionario geográfico, estadístico, histórico, biográfico, postal, municipal, militar, marítimo y eclesiástico de España y sus posesiones de Ultramar</em>. Barcelona: Imprenta y Librería Religiosa y Científica del Heredero de D. Pablo Riera. ${[vol, page].filter(Boolean).join(", ")}.`;
  }
  return "";
}

function renderBlob(src, blob) {
  if (!blob) return `<div class="blob"><em>(no s'ha trobat l'article original)</em></div>`;
  if (src === "floridablanca") return renderFlorida(blob);
  if (src === "minano")        return renderMinano(blob);
  if (src === "madoz")         return renderMadoz(blob);
  if (src === "nomenclator_1860") return render1860(blob);
  if (src === "riera")         return renderRiera(blob);
  return `<div class="blob"><pre class="raw">${esc(JSON.stringify(blob, null, 2))}</pre></div>`;
}

function row(label, val) {
  if (val == null || val === "") return "";
  return `<p><strong>${esc(label)}:</strong> ${esc(val)}</p>`;
}

// Catalan labels for the structured stats fields used by the three
// statistically-rich sources (Miñano, Riera, the 1860 Nomenclàtor).
// Anything not in this map falls back to a humanised key.
const STAT_LABELS = {
  // Population
  vecinos:             "Veïns",
  habitantes:          "Habitants",
  almas:               "Ànimes",
  // Administration
  parroquias:          "Parròquies",
  bayle:               "Batle",
  alcaldes:            "Batles",
  regidores:           "Regidors",
  ayuntamiento:        "Ajuntament",
  // Economy
  contribucion:        "Contribució",
  contr_territorial:   "Contr. territorial",
  contr_subsidio:      "Contr. subsidi",
  contribuye_con:      "Contribueix amb",
  riqueza_liquida_libras:    "Riquesa líquida (lliures)",
  riqueza_liquida:           "Riquesa líquida",
  // Buildings
  edificios:           "Edificis",
  casas:               "Cases",
  caserios:            "Caserius",
  caserios_y_grupos:   "Caserius i grups",
  viviendas:           "Habitatges",
  viviendas_aisladas:  "Habitatges aïllats",
  grupos:              "Grups",
  // Professions
  jornaleros:          "Jornalers",
  artesanos:           "Artesans",
  // Miñano-specific economic indicators
  molinos:             "Molins",
  molinos_aceite:      "Molins d'oli",
  molinos_harineros:   "Molins fariners",
  molinos_viento:      "Molins de vent",
  cabezas_ganado:      "Caps de bestiar",
  valor_cosechas_libras_mallorquinas: "Valor de les collites (lliures)",
  valor_industria_libras_mallorquinas: "Valor de la indústria (lliures)",
  produccion_libras_mallorquinas:      "Producció (lliures)",
  // 1860 Nomenclàtor
  inhabited_permanent:        "Habitats permanentment",
  inhabited_seasonal:         "Habitats temporalment",
  uninhabited:                "Deshabitats",
  total:                      "Total",
  buildings_1_floor:          "Edificis d'1 pis",
  buildings_2_floors:         "Edificis de 2 pisos",
  buildings_3_floors:         "Edificis de 3 pisos",
  buildings_over_3_floors:    "Edificis de més de 3 pisos",
  shelters:                   "Albergs",
};
function statLabel(k) {
  return STAT_LABELS[k] || k.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
}
function statValue(v) {
  if (v == null) return "—";
  if (typeof v === "number") return fmt(v);
  if (typeof v === "string" && /^[\d.,]+$/.test(v.replace(/\s/g, ""))) {
    const n = Number(v.replace(/[^\d.-]/g, ""));
    return isNaN(n) ? esc(v) : fmt(n);
  }
  return esc(String(v));
}
function renderStatGrid(stats, opts = {}) {
  if (!stats || typeof stats !== "object") return "";
  const entries = Object.entries(stats).filter(([, v]) => v != null && v !== "");
  if (entries.length === 0) return "";
  const heading = opts.heading !== undefined ? opts.heading : "Estadístiques";
  const cells = entries.map(([k, v]) => {
    // Nested objects (rare): fall back to a compact JSON rendering.
    if (typeof v === "object") {
      return `<div class="stat-cell stat-cell-wide">
        <div class="stat-label">${esc(statLabel(k))}</div>
        <pre class="stat-nested">${esc(JSON.stringify(v))}</pre>
      </div>`;
    }
    return `<span class="stat-cell"><span class="stat-num">${statValue(v)}</span><span class="stat-label">${esc(statLabel(k))}</span></span>`;
  }).join("");
  const h = heading ? `<h4>${esc(heading)}</h4>` : "";
  return `${h}<div class="stat-grid">${cells}</div>`;
}

function renderFlorida(b) {
  const cat = b.category_label || b.category || "";
  const auth = b.authority_label || "";
  const jur = b.jurisdiction_label || "";
  const pop = b.population || {};
  const total = pop.marital?.total?.all?.T;
  const occs = pop.occupation || {};
  const topOccs = Object.entries(occs)
    .filter(([k, v]) => v && k !== "total" && typeof v === "number")
    .sort((a, b) => b[1] - a[1]).slice(0, 6)
    .map(([k, v]) => `${esc(k.replace(/_/g, " "))}: ${v}`).join(" · ");
  const rel = (b.religious || []).map(r => esc(r.name || r.type || JSON.stringify(r))).join(" · ");
  return `<div class="blob">
    <h4>Identificació</h4>
    ${row("Topònim 1787", b.name_1787)}
    ${row("Topònim INE 1986", b.name_current)}
    ${row("Categoria", cat)}
    ${row("Autoritat", auth)}
    ${row("Jurisdicció", jur)}
    ${row("Districte", b.district_label)}
    ${row("Pàgina manuscrit", b.manuscript_page)}
    ${row("Fotograma INE", b.ine_photogram)}
    ${total != null ? `<h4>Població</h4><p><strong>Total:</strong> ${fmt(total)} (V ${fmt(pop.marital.total.all.V)} · M ${fmt(pop.marital.total.all.M)})</p>` : ""}
    ${topOccs ? `<h4>Ocupacions principals</h4><p>${topOccs}</p>` : ""}
    ${rel ? `<h4>Institucions religioses</h4><p>${rel}</p>` : ""}
    ${b.observations ? `<h4>Observacions</h4><p>${esc(b.observations)}</p>` : ""}
  </div>`;
}

function renderMinano(b) {
  return `<div class="blob">
    ${row("Tipus", b.place_type)}
    ${row("Illa", b.island)}
    ${row("Municipi", b.municipality)}
    ${b.description ? `<h4>Article</h4><p>${esc(b.description)}</p>` : ""}
    ${renderStatGrid(b.stats)}
    ${b.cross_references?.length ? `<p><em>Vegeu també:</em> ${b.cross_references.map(esc).join(" · ")}</p>` : ""}
    ${b.confidence ? `<p style="color:#7b8794;font-size:.8rem">Confiança d'extracció: ${esc(b.confidence)}</p>` : ""}
  </div>`;
}

function renderMadoz(b) {
  return `<div class="blob">
    ${row("Tipus", b.place_type)}
    ${row("Illa", b.island)}
    ${row("Partit judicial", b.judicial_district)}
    ${row("Municipi", b.municipality)}
    ${b.description ? `<h4>Article</h4><p>${esc(b.description)}</p>` : ""}
    ${b.cross_references?.length ? `<p><em>Vegeu també:</em> ${b.cross_references.map(esc).join(" · ")}</p>` : ""}
    ${b.confidence ? `<p style="color:#7b8794;font-size:.8rem">Confiança d'extracció: ${esc(b.confidence)}</p>` : ""}
  </div>`;
}

function render1860(b) {
  const habitable = {
    inhabited_permanent: b.inhabited_permanent,
    inhabited_seasonal:  b.inhabited_seasonal,
    uninhabited:         b.uninhabited,
    total:               b.total,
  };
  const buildings = {
    buildings_1_floor:        b.buildings_1_floor,
    buildings_2_floors:       b.buildings_2_floors,
    buildings_3_floors:       b.buildings_3_floors,
    buildings_over_3_floors:  b.buildings_over_3_floors,
    shelters:                 b.shelters,
  };
  return `<div class="blob">
    ${row("Tipus de lloc", b.place_class || b.class_normalized)}
    ${row("Municipi", b.municipality)}
    ${row("Partit judicial", b.judicial_district)}
    ${row("Distància al municipi (km)", b.distance_km)}
    ${renderStatGrid(habitable, { heading: "Habitatges i població" })}
    ${renderStatGrid(buildings, { heading: "Edificis per nombre de pisos" })}
    ${b.page ? `<p style="color:#7b8794;font-size:.8rem">Pàgina ${esc(b.page)}</p>` : ""}
  </div>`;
}

function renderRiera(b) {
  const sections = [
    ['Organització judicial', b.org_judicial],
    ['Organització civil', b.org_civil],
    ['Organització militar', b.org_militar],
    ['Organització econòmica', b.org_economica],
    ['Organització eclesiàstica', b.org_eclesiastica],
    ['Servei públic', b.servicio_publico],
    ['Obres públiques', b.obras_publicas],
    ['Instrucció pública', b.instruccion_publica],
    ['Població', b.poblacion],
    ['Indústria', b.industria],
    ['Geografia', b.geografia],
    ['Història', b.historia],
  ];
  return `<div class="blob">
    ${row("Tipus", b.place_type)}
    ${row("Illa", b.island)}
    ${row("Municipi", b.municipality)}
    ${sections.filter(([, v]) => v).map(([h, v]) => `<h4>${esc(h)}</h4><p>${esc(v)}</p>`).join("")}
    ${renderStatGrid(b.stats)}
    ${b.cross_references?.length ? `<p><em>Vegeu també:</em> ${b.cross_references.map(esc).join(" · ")}</p>` : ""}
  </div>`;
}

// Compact card for minor / jurisdictional entries — same visual
// treatment as «Llocs amb identitat NGIB dins el terme»: each is a
// clickable card. Click opens an in-page modal with the article
// content (citation + blob); the meta site stays self-contained
// and never bounces the reader to an external sibling URL.
function entryCard(e, blobs) {
  const blob = blobs[`${e.source}:${e.source_id}`];
  const rawTitle = (e.source === "floridablanca" && blob?.name_1787)
    ? blob.name_1787 : e.title;
  const displayTitle = stripSupp(rawTitle);
  const suppBadge = e.is_supplement ? `<div class="child-count">suplement</div>` : "";
  return `<button type="button" class="child-card"
            data-entry-source="${esc(e.source)}"
            data-entry-id="${esc(e.source_id)}">
    <div class="child-name">${esc(displayTitle)}</div>
    <div class="child-meta">${esc(SOURCE_LABEL[e.source])} · ${e.year}${e.place_type ? ` · ${esc(e.place_type)}` : ""}</div>
    ${suppBadge}
  </button>`;
}

// Lookup an entry by source:id across every place + orphans bucket.
function findEntry(source, sourceId) {
  for (const p of (state.data.places || [])) {
    for (const arr of [p.entries, p.minor_entries, p.jurisdictional_entries]) {
      for (const e of (arr || [])) {
        if (e.source === source && String(e.source_id) === String(sourceId)) {
          return { e, place: p };
        }
      }
    }
  }
  // Orphans are bucketed by source as the dict key; the items
  // themselves don't carry a `source` field.
  const orphans = state.data.orphans || {};
  for (const arr of (orphans[source] || [])) {
    if (String(arr.source_id) === String(sourceId)) {
      return { e: { ...arr, source, year: SOURCE_YEAR[source] } };
    }
  }
  return null;
}

async function openEntryModal(source, sourceId) {
  const blobs = await ensureBlobs();
  const hit = findEntry(source, sourceId);
  if (!hit) return;
  const e = hit.e;
  const blob = blobs[`${e.source}:${e.source_id}`];
  const rawTitle = (e.source === "floridablanca" && blob?.name_1787)
    ? blob.name_1787 : e.title;
  const displayTitle = stripSupp(rawTitle);
  const yr = e.year || SOURCE_YEAR[e.source];
  const overlay = document.createElement("div");
  overlay.className = "entry-modal-overlay";
  overlay.innerHTML = `
    <div class="entry-modal" role="dialog" aria-modal="true">
      <button class="entry-modal-close" type="button" aria-label="Tancar">✕</button>
      <div class="entry-modal-head">
        <div class="timeline-year y-${yr}">${yr}</div>
        <div>
          <h3 class="entry-modal-title">${esc(displayTitle)}${e.is_supplement ? ` <span class="suppl-badge">suplement</span>` : ""}</h3>
          <div class="entry-modal-sub">
            <strong>${esc(SOURCE_LABEL[e.source])}</strong>${e.place_type ? ` · ${esc(e.place_type)}` : ""}
          </div>
        </div>
      </div>
      <div class="entry-modal-body">
        <div class="citation">${citationFor(e, blob)}</div>
        ${renderBlob(e.source, blob)}
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  document.body.style.overflow = "hidden";
  const close = () => {
    overlay.remove();
    document.body.style.overflow = "";
    document.removeEventListener("keydown", onKey);
  };
  const onKey = ev => { if (ev.key === "Escape") close(); };
  overlay.addEventListener("click", ev => {
    if (ev.target === overlay) close();
  });
  overlay.querySelector(".entry-modal-close").addEventListener("click", close);
  document.addEventListener("keydown", onKey);
}

// Delegated click handler — attached once in initTabs.
document.addEventListener("click", ev => {
  const card = ev.target.closest("[data-entry-source]");
  if (card) {
    ev.preventDefault();
    openEntryModal(card.dataset.entrySource, card.dataset.entryId);
  }
});

function timelineCard(e, blobs) {
  const blob = blobs[`${e.source}:${e.source_id}`];
  const band = e.describes_band || "sense";
  const supp = e.is_supplement
    ? ` <span class="suppl-badge" title="${esc(SOURCE_LABEL[e.source])} ${e.source === "minano" ? "Tom XI (Suplemento, 1829)" : "Tom XVI (Adiciones, 1850)"}">suplement</span>`
    : "";
  // For Floridablanca, prefer the literal 1787 spelling
  // (e.g. ANDRAIG) over the INE-modernised re-typed form
  // (ANDRAITX) used in the matcher. Both come from the same blob.
  // Strip the "(adición)" / "(addicional)" supplement suffix from
  // the displayed title — the SUPLEMENT badge already says so.
  const rawTitle = (e.source === "floridablanca" && blob?.name_1787)
    ? blob.name_1787 : e.title;
  const displayTitle = stripSupp(rawTitle);
  const confVal = e.describes_confidence != null ? e.describes_confidence.toFixed(2) : "—";
  return `<div class="timeline-card">
    <div class="timeline-year y-${e.year}">${e.year}</div>
    <div>
      <div class="card-heading">
        <h3 class="card-title">${esc(displayTitle)}</h3>
        <div class="card-meta">
          <span class="card-source">${esc(SOURCE_LABEL[e.source])}</span>
          ${supp}
          <span class="conf-pill conf-${band}">${esc(band)} · ${confVal} · ${esc(e.describes_method || "")}</span>
        </div>
      </div>
      <div class="citation">
        ${citationFor(e, blob)}
        ${e.source_url ? ` · <a href="${esc(e.source_url)}" target="_blank" rel="noopener">↗ Article original</a>` : ""}
      </div>
      ${renderBlob(e.source, blob)}
    </div>
  </div>`;
}

function renderPlaceDetail(place, blobs) {
  // v2 dual-link model:
  //   ents              — articles that describe THIS place (its own timeline)
  //   childPlaces       — sub-features inside this place (only if Municipi)
  //   minorEntries      — feature_no_ngib entries with parent_ngib_id=this
  //   jurisdictionalEnts — jurisdictional entries with parent_ngib_id=this
  //
  // Each entry's data fields now use describes_method / describes_band /
  // describes_confidence instead of the v1 link_method / confidence_band /
  // confidence.
  const sortByYear = (a, b) =>
    a.year - b.year || ((a.is_supplement ? 1 : 0) - (b.is_supplement ? 1 : 0));
  const ents = [...(place.entries || [])].sort(sortByYear);
  const childPlaces = place.child_places || [];
  const minorEnts = [...(place.minor_entries || [])].sort(sortByYear);
  const jurisdictionalEnts = [...(place.jurisdictional_entries || [])].sort(sortByYear);

  const present = new Set(ents.map(e => e.source));
  const dots = SOURCE_ORDER.map(s => {
    const yr = SOURCE_YEAR[s];
    return `<span class="source-dot ${present.has(s) ? "has-" + yr : "empty"}" title="${esc(SOURCE_LABEL[s])} (${yr})${present.has(s) ? "" : " — no atestat"}">${present.has(s) ? yr : "·"}</span>`;
  }).join("");

  const variants = place.variants.filter(v => norm(v) !== norm(place.name));
  const dl = `meta-${place.ngib_id}.json`;
  const isMunicipi = place.local_type === "Municipi";

  const breadcrumb = place.breadcrumb_parent
    ? `<a href="?ngib=${esc(place.breadcrumb_parent.ngib_id)}" class="crumb">${esc(place.breadcrumb_parent.name)}</a> ›`
    : "";

  $("place-detail").innerHTML = `
    <a href="#" class="back-link" data-goto="explore">‹ Tornar a l'explorador</a>
    <div class="place-header">
      <div>
        <div class="place-breadcrumb">${breadcrumb}</div>
        <h1>${esc(place.name)}</h1>
        <div class="place-id">NGIB <code>${esc(place.ngib_id)}</code></div>
        <dl class="place-meta-grid">
          ${place.island        ? `<dt>Illa</dt><dd>${esc(place.island)}</dd>` : ""}
          ${place.municipality  ? `<dt>Municipi</dt><dd>${esc(place.municipality)}</dd>` : ""}
          ${place.local_type    ? `<dt>Tipus NGIB</dt><dd>${esc(place.local_type)}</dd>` : ""}
          ${place.lat != null   ? `<dt>Coordenades</dt><dd>${place.lat.toFixed(5)}, ${place.lng.toFixed(5)}</dd>` : ""}
          ${variants.length     ? `<dt>Variants</dt><dd>${variants.map(esc).join(" · ")}</dd>` : ""}
          <dt>Articles sobre aquest lloc</dt><dd>${dots} (${ents.length} article${ents.length === 1 ? "" : "s"})</dd>
        </dl>
      </div>
      <button class="download-btn" id="download-place">↓ Descarregar JSON</button>
    </div>

    ${ents.length ? `
      <h2 class="section-h">Articles sobre aquest lloc</h2>
      <div class="timeline">
        ${ents.map(e => timelineCard(e, blobs)).join("")}
      </div>
    ` : `<p class="empty-note">Cap article descriu aquesta entitat directament; només té sub-features o articles administratius.</p>`}

    ${isMunicipi && childPlaces.length ? `
      <h2 class="section-h">Llocs amb identitat NGIB dins el terme (${childPlaces.length})</h2>
      <div class="child-grid">
        ${childPlaces.map(c => `
          <a class="child-card" href="?ngib=${esc(c.ngib_id)}">
            <div class="child-name">${esc(c.name)}</div>
            <div class="child-meta">${esc(c.local_type || "")}</div>
            <div class="child-count">${c.entry_count} article${c.entry_count === 1 ? "" : "s"}</div>
          </a>
        `).join("")}
      </div>
    ` : ""}

    ${isMunicipi && minorEnts.length ? `
      <h2 class="section-h">Articles dins el terme sense entrada NGIB pròpia (${minorEnts.length})</h2>
      <div class="child-grid">
        ${minorEnts.map(e => entryCard(e, blobs)).join("")}
      </div>
    ` : ""}

    ${isMunicipi && jurisdictionalEnts.length ? `
      <h2 class="section-h">Articles administratius i jurisdiccionals (${jurisdictionalEnts.length})</h2>
      <p class="section-help">Termes, partits judicials, diòcesis i altres categories administratives sense equivalent NGIB modern.</p>
      <div class="child-grid">
        ${jurisdictionalEnts.map(e => entryCard(e, blobs)).join("")}
      </div>
    ` : ""}
  `;

  $("download-place").addEventListener("click", () => {
    const payload = {
      ngib_id: place.ngib_id,
      name: place.name,
      municipality: place.municipality,
      island: place.island,
      local_type: place.local_type,
      lat: place.lat, lng: place.lng,
      variants: place.variants,
      child_places: childPlaces,
      entries: ents.map(e => ({
        ...e,
        blob: blobs[`${e.source}:${e.source_id}`] || null,
      })),
      minor_entries: minorEnts.map(e => ({
        ...e,
        blob: blobs[`${e.source}:${e.source_id}`] || null,
      })),
      jurisdictional_entries: jurisdictionalEnts.map(e => ({
        ...e,
        blob: blobs[`${e.source}:${e.source_id}`] || null,
      })),
    };
    const url = URL.createObjectURL(new Blob(
      [JSON.stringify(payload, null, 2)],
      { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = dl;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });

  document.querySelectorAll(".back-link[data-goto]").forEach(el => {
    el.addEventListener("click", ev => { ev.preventDefault(); gotoTab("explore"); });
  });
}

// ---------------------- stats -----------------------------------------------
function renderStats() {
  const el = $("stats-content");
  if (el.dataset.rendered === "1") return;

  // 1. Llocs presents en N fonts (compting every source attested in
  // any of the four entry buckets, not just the main timeline).
  const dist = [0, 0, 0, 0, 0, 0];
  for (const p of state.data.places) {
    const n = new Set(allEntriesOf(p).map(e => e.source)).size;
    if (n >= 1 && n <= 5) dist[n] += 1;
  }
  const distMax = Math.max(...dist.slice(1));
  const distHtml = [1, 2, 3, 4, 5].map(n => {
    const w = dist[n] ? Math.max(2, Math.round(dist[n] / distMax * 100)) : 0;
    return `
      <span class="bar-label">${n} font${n === 1 ? "" : "s"}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
      <span class="bar-count">${fmt(dist[n] || 0)}</span>
    `;
  }).join("");

  // 2. Per illa.
  const byIsland = {};
  for (const p of state.data.places) byIsland[p.island || "—"] = (byIsland[p.island || "—"] || 0) + 1;
  const islandMax = Math.max(...Object.values(byIsland));
  const islandHtml = Object.entries(byIsland)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => {
      const w = Math.max(2, Math.round(v / islandMax * 100));
      return `
        <span class="bar-label">${esc(k)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
        <span class="bar-count">${fmt(v)}</span>
      `;
    }).join("");

  // 3. Per font — v2 reports describes_linked + parent_linked instead
  // of a single linked count.
  const t = state.data.totals;
  const srcHtml = `
    <table class="coverage-table">
      <thead><tr>
        <th>Font</th><th>Any</th>
        <th class="num">Articles</th>
        <th class="num">Amb describes</th>
        <th class="num">Amb parent</th>
        <th class="num">% describes</th>
      </tr></thead>
      <tbody>
        ${SOURCE_ORDER.map(s => {
          const d = t.by_source[s] || { total: 0, describes_linked: 0, parent_linked: 0 };
          const dpct = d.total ? (d.describes_linked * 100 / d.total) : 0;
          return `<tr>
            <td><strong>${esc(SOURCE_LABEL[s])}</strong></td>
            <td>${SOURCE_YEAR[s]}</td>
            <td class="num">${fmt(d.total)}</td>
            <td class="num">${fmt(d.describes_linked)}</td>
            <td class="num">${fmt(d.parent_linked)}</td>
            <td class="num">${dpct.toFixed(1)}%</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>
  `;

  el.innerHTML = `
    <div class="stat-block">
      <h3>Llocs atestats en N fonts</h3>
      <div class="bar-chart">${distHtml}</div>
      <p style="margin-top:1rem;color:#52606d;font-size:.9rem">
        Una mateixa entitat pot tenir entrades als cinc nomenclàtors o només a un. La gran cua dreta del corpus són topònims molt locals (caserius, predis) que només apareixen al recompte de 1860.
      </p>
    </div>
    <div class="stat-block">
      <h3>Llocs canònics per illa</h3>
      <div class="bar-chart">${islandHtml}</div>
    </div>
    <div class="stat-block">
      <h3>Cobertura per font</h3>
      ${srcHtml}
      <p style="margin-top:1rem;color:#52606d;font-size:.9rem">
        <em>Describes</em> = l'article descriu una entitat NGIB concreta. <em>Parent</em> = l'article està ubicat dins el terme d'un municipi NGIB. La cua d'articles orfes (cap dels dos enllaços) és visible amb el filtre <em>Sense vincle NGIB</em> de la pestanya Explorar.
      </p>
    </div>
  `;
  el.dataset.rendered = "1";
}

// ---------------------- boot ------------------------------------------------
initTabs();
loadData().catch(err => {
  console.error(err);
  $("results").innerHTML = `<div class="loading" style="color:#a93838">Error carregant les dades. Veure la consola del navegador.</div>`;
});
