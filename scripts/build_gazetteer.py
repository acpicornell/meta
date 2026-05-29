#!/usr/bin/env python3
"""Construct a fuzzy-searchable Balearic gazetteer from NGIB.

Source: NGIB (Nomenclàtor Geogràfic de les Illes Balears) parquets
shipped in ``data/ngib/`` — ~55,500 toponyms with municipality, island,
local_type, lat/lon, plus 1,366 variant spellings.

Output: data/gazetteer.parquet  with rows:
    - id                 NGIB geographic_name_id
    - spelling           original spelling (catalan modern)
    - normalized         article-stripped, accent-stripped, uppercase
    - tokens             space-separated tokens of normalized
    - first_token        first token (after dropping the article)
    - municipality       Catalan modern (e.g. "Pollença")
    - island             Mallorca / Menorca / Eivissa / Formentera / Cabrera
    - local_type         from NGIB taxonomy
    - is_settlement      bool — likely matches a Floridablanca/Miñano/Madoz/1860/Riera entry
    - lon, lat           coordinates
    - source             'ngib' / 'ngib_variants' / 'historical'

The 'historical' rows are hand-curated 19th-century Castilian and
broken-OCR spellings (Mallorea, Pollensa, Iviza, Mahón, Sineu/Sinen,
Manon, Andrach, Santagny, Monacor…) — they bridge the gap between
modern Catalan NGIB and the Castilianised renderings used across
Floridablanca, Miñano, Madoz, the 1860 Nomenclátor and Riera.
"""
from __future__ import annotations

import sys, unicodedata
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
NGIB_DATA = ROOT / 'data' / 'ngib'

# Local types most likely to appear as a headword across the five
# Balearic sources (Floridablanca, Miñano, Madoz, 1860 and Riera).
# (Settlements, religious sites, possessions/finques, prominent natural
#  features. We exclude "paratge / àrea menor" and "carretera" because
#  they swamp the index with non-relevant items.)
SETTLEMENT_TYPES = {
    'Municipi',
    'Capital de municipi',
    'Capital de Municipi',
    'Nucli de població capital de municipi',
    'Entitat de Població',
    'Llogaret, llogarret, ranxo',
    'Altre nucli de població, llogaret',
    'Vila',
    'Barri',
    'Barriada',
    'Urbanització, barriada (aïllat)',
}

# Tipus NGIB ordenats de més a menys autoritatiu per resoldre col·lisions
# d'homònims dins d'una mateixa illa. Quan dos registres NGIB normalitzen al
# mateix nom (p. ex. `sa Pobla` com a municipi al nord de Mallorca i `sa Pobla`
# com a possessió a Llucmajor), el de major prioritat prevaldrà al dedup.
LOCAL_TYPE_PRIORITY = [
    'Municipi',
    'Capital de municipi',
    'Capital de Municipi',
    'Nucli de població capital de municipi',
    'Vila',
    'Entitat de Població',
    'Llogaret, llogarret, ranxo',
    'Altre nucli de població, llogaret',
    'Illa gran',
    'Illa mitjana',
    'Urbanització, barriada (aïllat)',
    'Barriada',
    'Barri',
    'Santuari',
    'Monestir, convent, cartoixa',
    'Església, capella, oratori, ermita',
    'Edifici religiós',
    'Castell, fortalesa',
    'Far',
    'Cim, puig, talaia',
    'Elevació gran',
    'Serra, serral, serralada',
    'Cap, punta, morro mitjà',
    'Cap, punta, morro petit',
    'Estret, cala, badia mitjana',
    'Estret, cala petita, rada',
    'Construcció agroindustrial',
    'Finca, possessió, lloc, casa pagesa, caseta',
    'Accident petit, relleu del fons marí, illot',
    'Monument',
]
def _type_rank(ltype: str | None) -> int:
    try:
        return LOCAL_TYPE_PRIORITY.index(ltype or '')
    except ValueError:
        return len(LOCAL_TYPE_PRIORITY)  # unknown → lowest priority
POSSESSION_TYPES = {
    'Finca, possessió, lloc, casa pagesa, caseta',
    'Construcció agroindustrial',
}
RELIGIOUS_TYPES = {
    'Edifici religiós',
    'Església, capella, oratori, ermita',
    'Monestir, convent, cartoixa',
    'Santuari',
    'Monument',
}
NATURAL_TYPES = {
    'Cim, puig, talaia',
    'Pic, cim gran (puntual)',
    'Pic, cim petit (puntual)',
    'Elevació petita',
    'Elevació gran',
    'Serra, serral, serralada',
    'Serra petita',
    'Coll petit (puntual)',
    'Pas',
    'Cap, punta, morro principal',
    'Cap, punta, morro mitjà',
    'Cap, punta, morro petit',
    'Estret, cala, badia principal',
    'Estret, cala, badia mitjana',
    'Estret, cala petita, rada',
    'Platja principal',
    'Platja mitjana',
    'Platja petita, platgeta',
    'Moll, dic, pantalà, varador',
    'Cova, balma, avenc',
    'Torrent',
    'Font, surgència',
    'Bassa',
    'Salines',
    "Salt d'aigua",
    'Canal, síquia, aqüeducte',
    'Zona pantanosa, aiguamoll',
    'Illa gran',
    'Illa mitjana',
    'Accident petit, relleu del fons marí, illot',
    'Pla, plana',
    'Castell, fortalesa',
    'Far',
}
ALL_TYPES = SETTLEMENT_TYPES | POSSESSION_TYPES | RELIGIOUS_TYPES | NATURAL_TYPES

# Articles to strip from the leading position (Catalan).
LEADING_ARTICLES = (
    "S'", "s'", "L'", "l'",
    "es ", "Es ", "ES ",
    "sa ", "Sa ", "SA ",
    "el ", "El ", "EL ",
    "la ", "La ", "LA ",
    "els ", "Els ", "ELS ",
    "les ", "Les ", "LES ",
    "ses ", "Ses ", "SES ",
    "sos ", "Sos ", "SOS ",
    "so ", "So ",
    "na ", "Na ",
    "en ", "En ", "n'", "N'",
)

def strip_diacritics(s: str) -> str:
    """NFD then remove combining marks. Keeps ñ → n, ç → c, ü → u."""
    n = unicodedata.normalize('NFD', s)
    return ''.join(c for c in n if unicodedata.category(c) != 'Mn')

def strip_article(s: str) -> str:
    for a in LEADING_ARTICLES:
        if s.startswith(a):
            return s[len(a):]
    return s

def normalize(s: str) -> str:
    """Uppercase, strip accents, strip article, collapse whitespace."""
    if not s:
        return ''
    s = strip_article(s.strip())
    s = strip_diacritics(s)
    s = s.upper()
    # Hyphens and slashes act as word separators in Miñano's typography
    # (e.g. ALCARIA-ROJA, SAN JUAN/SANT JOAN). Converting them to spaces
    # lets the fuzzy matcher line up tokens correctly.
    for sep in ('-', '–', '—', '/'):
        s = s.replace(sep, ' ')
    s = ' '.join(s.split())
    # Drop common punctuation that may obscure matching.
    for ch in '.,;:¡¿!?()[]{}«»"\'`':
        s = s.replace(ch, '')
    return s.strip()


# Hand-curated 19th-century Castilian spellings seen in Miñano's text
# that won't appear in NGIB. Format: (historical_spelling, modern_form_for_lookup)
HISTORICAL_VARIANTS = [
    # Major islands & capitals — Castilian-Spanish forms common in early
    # 19th-century print. The Catalan modern form lives in NGIB; we add
    # the Castilianised + occasionally-mangled forms here.
    ("Mallorca",   "Mallorca",   "Mallorca"),
    ("Mallorea",   "Mallorca",   "Mallorca"),    # OCR variant (e↔c)
    ("Maiorca",    "Mallorca",   "Mallorca"),    # antique
    ("Mahón",      "Maó",        "Menorca"),     # standard Castilian
    ("Mahon",      "Maó",        "Menorca"),
    ("Iviza",      "Eivissa",    "Eivissa"),     # Miñano's preferred form
    ("Ibiza",      "Eivissa",    "Eivissa"),     # Castilian
    ("Pollensa",   "Pollença",   "Mallorca"),    # Castilian
    ("Pollenza",   "Pollença",   "Mallorca"),
    ("Pollentia",  "Pollença",   "Mallorca"),    # Latin/historic
    ("Felanitx",   "Felanitx",   "Mallorca"),
    ("Lluchmayor", "Llucmajor",  "Mallorca"),
    ("Llumayor",   "Llucmajor",  "Mallorca"),
    ("Llucmajor",  "Llucmajor",  "Mallorca"),
    ("Andraitx",   "Andratx",    "Mallorca"),
    ("Andraix",    "Andratx",    "Mallorca"),
    ("Andraig",    "Andratx",    "Mallorca"),
    ("Bunyola",    "Bunyola",    "Mallorca"),
    ("Buñola",     "Bunyola",    "Mallorca"),
    ("Bañola",     "Bunyola",    "Mallorca"),
    ("Sineu",      "Sineu",      "Mallorca"),
    ("Sinen",      "Sineu",      "Mallorca"),    # OCR variant (u↔n)
    ("Manacor",    "Manacor",    "Mallorca"),
    ("Selva",      "Selva",      "Mallorca"),
    ("Inca",       "Inca",       "Mallorca"),
    ("Soller",     "Sóller",     "Mallorca"),
    ("Söller",     "Sóller",     "Mallorca"),
    ("Esporlas",   "Esporles",   "Mallorca"),
    ("Esporles",   "Esporles",   "Mallorca"),
    ("Valldemosa", "Valldemossa","Mallorca"),
    ("Valldemusa", "Valldemossa","Mallorca"),
    ("Valdemosa",  "Valldemossa","Mallorca"),
    ("Estellenchs","Estellencs", "Mallorca"),
    ("Establiments","Establiments","Mallorca"),
    ("Establimens","Establiments","Mallorca"),
    ("Puigpunyent","Puigpunyent","Mallorca"),
    ("Puigpuñent", "Puigpunyent","Mallorca"),
    ("Puigpuñer",  "Puigpunyent","Mallorca"),
    ("Marratchi",  "Marratxí",   "Mallorca"),
    ("Marratxi",   "Marratxí",   "Mallorca"),
    ("Santañy",    "Santanyí",   "Mallorca"),
    ("Santany",    "Santanyí",   "Mallorca"),
    ("Santanyí",   "Santanyí",   "Mallorca"),
    ("Calviá",     "Calvià",     "Mallorca"),
    ("Calvià",     "Calvià",     "Mallorca"),
    ("Banyalbufar","Banyalbufar","Mallorca"),
    ("Bañalbufar", "Banyalbufar","Mallorca"),
    ("Deyá",       "Deià",       "Mallorca"),
    ("Deyà",       "Deià",       "Mallorca"),
    ("Llubin",     "Llubí",      "Mallorca"),
    ("Llubí",      "Llubí",      "Mallorca"),
    ("Lloseta",    "Lloseta",    "Mallorca"),
    ("Llorito",    "Lloret de Vistalegre","Mallorca"),
    ("Lloret",     "Lloret de Vistalegre","Mallorca"),
    ("Algaida",    "Algaida",    "Mallorca"),
    ("Algayda",    "Algaida",    "Mallorca"),
    ("Caymari",    "Caimari",    "Mallorca"),
    ("Caimari",    "Caimari",    "Mallorca"),
    ("Caimary",    "Caimari",    "Mallorca"),    # Miñano Y/I variant
    ("Mancor",     "Mancor de la Vall","Mallorca"),
    ("Mancor del Valle","Mancor de la Vall","Mallorca"),    # Floridablanca
    ("Moscari",    "Moscari",    "Mallorca"),
    ("Moscarí",    "Moscari",    "Mallorca"),
    ("Biniamar",   "Biniamar",   "Mallorca"),
    ("Binisalem",  "Binissalem", "Mallorca"),
    ("Bisanlem",   "Binissalem", "Mallorca"),
    ("Búger",      "Búger",      "Mallorca"),
    ("Buger",      "Búger",      "Mallorca"),
    ("Bugeu",      "Búger",      "Mallorca"),    # documented OCR mangle
    # NOTE: NGIB does not have a stand-alone "Bellver" row; the
    # only Bellver entity is the Castell de Bellver monument in
    # Palma. Don't curate "Belver" / "Bellver" against a missing
    # target — the explicit Castell variants below cover the cases.
    ("Belver Castillo","Castell de Bellver","Mallorca"),
    ("Ariañy",     "Ariany",     "Mallorca"),
    ("Ariany",     "Ariany",     "Mallorca"),
    ("Costiche",   "Costitx",    "Mallorca"),
    ("Costítx",    "Costitx",    "Mallorca"),
    ("Costitx",    "Costitx",    "Mallorca"),
    ("Consell",    "Consell",    "Mallorca"),
    ("Petra",      "Petra",      "Mallorca"),
    ("Sancellas",  "Sencelles",  "Mallorca"),
    ("Sencelles",  "Sencelles",  "Mallorca"),
    ("Sansellas",  "Sencelles",  "Mallorca"),
    ("Sancelles",  "Sencelles",  "Mallorca"),
    ("Vilafranca", "Vilafranca de Bonany","Mallorca"),
    ("Villafranca","Vilafranca de Bonany","Mallorca"),
    ("Vila Franca","Vilafranca de Bonany","Mallorca"),    # Floridablanca name_1787
    ("Villa Franca","Vilafranca de Bonany","Mallorca"),
    ("Villafranca de Bonany","Vilafranca de Bonany","Mallorca"),
    ("Vilafranca de Bonany","Vilafranca de Bonany","Mallorca"),
    ("Sa Pobla",   "sa Pobla",   "Mallorca"),
    ("La Puebla",  "sa Pobla",   "Mallorca"),
    ("Pobla",      "sa Pobla",   "Mallorca"),
    ("Sant Joan",  "Sant Joan",  "Mallorca"),
    ("San Joan",   "Sant Joan",  "Mallorca"),
    ("Son Servera","Son Servera","Mallorca"),
    ("Sonservera", "Son Servera","Mallorca"),
    ("Sant Llorenç","Sant Llorenç des Cardassar","Mallorca"),
    ("Sant Llorens","Sant Llorenç des Cardassar","Mallorca"),
    ("San Lorenzo","Sant Llorenç des Cardassar","Mallorca"),
    ("San Lorenzo del Cardasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Llorens d'el Cordasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Llorens d' el Cordasar","Sant Llorenç des Cardassar","Mallorca"),  # Riera OCR space
    ("San Llorens del Cordasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Llorens del Cardasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Llorens Descardasar","Sant Llorenç des Cardassar","Mallorca"),     # Miñano joined
    ("San Lorens Descardasar","Sant Llorenç des Cardassar","Mallorca"),
    ("San Lorens des Cardesar","Sant Llorenç des Cardassar","Mallorca"),     # CARDESAR variant
    ("San Llorens des Cardesar","Sant Llorenç des Cardassar","Mallorca"),
    ("Capdepera",  "Capdepera",  "Mallorca"),
    ("Cap de Pera","Capdepera",  "Mallorca"),
    # Miñano writes 'Alcaria' for what NGIB records as 'Alqueria'
    # (same Arabic etymology, al-qarya). Curated mappings to the actual
    # modern Catalan toponyms.
    ("Alcaria-Roja",   "Alqueria Roja",      "Mallorca"),
    ("Alcaria Roja",   "Alqueria Roja",      "Mallorca"),
    ("Alcaria-Blanca", "s'Alqueria Blanca",  "Mallorca"),
    ("Alcaria Blanca", "s'Alqueria Blanca",  "Mallorca"),
    ("Artá",       "Artà",       "Mallorca"),
    ("Artà",       "Artà",       "Mallorca"),
    ("Porreras",   "Porreres",   "Mallorca"),
    ("Porreres",   "Porreres",   "Mallorca"),
    ("Alcudia",    "Alcúdia",    "Mallorca"),
    ("Alcúdia",    "Alcúdia",    "Mallorca"),
    ("Alcudía",    "Alcúdia",    "Mallorca"),
    ("Montuiri",   "Montuïri",   "Mallorca"),
    ("Muro",       "Muro",       "Mallorca"),
    ("María",      "Maria de la Salut","Mallorca"),
    ("Santa María","Santa Maria del Camí","Mallorca"),
    ("Santa Maria","Santa Maria del Camí","Mallorca"),
    ("Santa Margarita","Santa Margalida","Mallorca"),
    ("Santa Margalida","Santa Margalida","Mallorca"),
    ("Santa Eugenia","Santa Eugènia","Mallorca"),
    ("Felanitx",   "Felanitx",   "Mallorca"),
    ("Fornalutx",  "Fornalutx",  "Mallorca"),
    ("Fornaluche", "Fornalutx",  "Mallorca"),
    ("Llucalcari", "Llucalcari", "Mallorca"),
    ("Lluch",      "Lluc",       "Mallorca"),
    ("Lluc",       "Lluc",       "Mallorca"),
    ("Randa",      "Randa",      "Mallorca"),
    ("Llumdeneva", "Lloc d'en Eva", "Mallorca"),
    # Menorca
    ("Mahón",      "Maó",        "Menorca"),
    ("Maó",        "Maó",        "Menorca"),
    ("Ciudadela",  "Ciutadella de Menorca","Menorca"),
    ("Ciutadella", "Ciutadella de Menorca","Menorca"),
    ("Alaior",     "Alaior",     "Menorca"),
    ("Alayor",     "Alaior",     "Menorca"),
    ("Mercadal",   "es Mercadal","Menorca"),
    ("Ferrerías",  "Ferreries",  "Menorca"),
    ("Ferreries",  "Ferreries",  "Menorca"),
    ("Perrerías",  "Ferreries",  "Menorca"),  # OCR P↔F
    ("Fornells",   "Fornells",   "Menorca"),
    ("San Cristóbal","es Migjorn Gran","Menorca"),
    ("San Cristobal","es Migjorn Gran","Menorca"),
    ("San Climent","Sant Climent","Menorca"),
    ("San Clemente","Sant Climent","Menorca"),
    ("San Luis",   "Sant Lluís", "Menorca"),
    ("Sant Lluís", "Sant Lluís", "Menorca"),
    ("Villacarlos","es Castell", "Menorca"),
    ("Villa Carlos","es Castell","Menorca"),
    ("Es Castell", "es Castell", "Menorca"),
    ("Adaya",      "Addaia",     "Menorca"),
    # Eivissa / Ibiza
    ("Iviza",      "Eivissa",    "Eivissa"),
    ("Ibiza",      "Eivissa",    "Eivissa"),
    ("Eivissa",    "Eivissa",    "Eivissa"),
    ("Sant Antoni","Sant Antoni de Portmany","Eivissa"),
    ("San Antonio","Sant Antoni de Portmany","Eivissa"),
    ("Pormany",    "Sant Antoni de Portmany","Eivissa"),
    ("Portmany",   "Sant Antoni de Portmany","Eivissa"),
    ("Sant Josep", "Sant Josep de sa Talaia","Eivissa"),
    ("San José",   "Sant Josep de sa Talaia","Eivissa"),
    ("Sant Joan",  "Sant Joan de Labritja","Eivissa"),
    ("San Juan Bautista","Sant Joan de Labritja","Eivissa"),
    ("Sant Carles","Sant Carles de Peralta","Eivissa"),
    ("San Carlos", "Sant Carles de Peralta","Eivissa"),
    ("Santa Eulalia","Santa Eulària des Riu","Eivissa"),
    ("Santa Eulària","Santa Eulària des Riu","Eivissa"),
    ("Sant Llorenç","Sant Llorenç de Balàfia","Eivissa"),
    ("San Lorenzo","Sant Llorenç de Balàfia","Eivissa"),
    ("Sant Rafel", "Sant Rafel de sa Creu","Eivissa"),
    ("San Rafael", "Sant Rafel de sa Creu","Eivissa"),
    ("Santa Gertrudis","Santa Gertrudis de Fruitera","Eivissa"),
    ("Santa Inés", "Santa Agnès de Corona","Eivissa"),
    ("Santa Agnès","Santa Agnès de Corona","Eivissa"),
    ("Sant Jordi", "Sant Jordi de Ses Salines","Eivissa"),
    ("San Jorge",  "Sant Jordi de Ses Salines","Eivissa"),
    ("Jesús",      "Jesús",      "Eivissa"),
    ("Balanzat",   "Sant Miquel de Balansat","Eivissa"),
    ("Balansat",   "Sant Miquel de Balansat","Eivissa"),
    # Formentera. NGIB names the capital "Sant Francesc de Formentera"
    # (NOT "Sant Francesc Xavier"), so the historical variants point at
    # the spelling NGIB actually uses.
    ("Formentera", "Formentera", "Formentera"),
    ("San Francisco Javier","Sant Francesc de Formentera","Formentera"),
    ("Sant Francesc Xavier","Sant Francesc de Formentera","Formentera"),
    ("Sant Francesc","Sant Francesc de Formentera","Formentera"),
    ("San Fernando","Sant Ferran de ses Roques","Formentera"),
    ("Sant Ferran","Sant Ferran de ses Roques","Formentera"),
    ("Pilar de la Mola","el Pilar de la Mola","Formentera"),
    ("Nuestra Señora del Pilar de la Mola","el Pilar de la Mola","Formentera"),
    # Cabrera
    ("Cabrera",    "Cabrera",    "Cabrera"),

    # Llogarets / sub-municipalities with Miñano/Madoz orthographic
    # mangles that are hard to fuzzy-match. Each maps to its modern
    # canonical NGIB form so the entry resolves to its own place
    # instead of falling back to the parent municipality.
    ("Sarrecó",    "s'Arracó",   "Mallorca"),     # llogaret d'Andratx
    ("Serrecó",    "s'Arracó",   "Mallorca"),
    ("Arracó",     "s'Arracó",   "Mallorca"),

    # Madoz-specific spellings and OCR artifacts (extras from
    # madoz/scripts/build_gazetteer.py, merged in for the meta project).
    ("Manon",      "Maó",        "Menorca"),     # OCR of Mahón (M↔M, h missing)
    ("Andrach",    "Andratx",    "Mallorca"),    # Madoz spelling
    ("Andrache",   "Andratx",    "Mallorca"),
    ("Monacor",    "Manacor",    "Mallorca"),    # Madoz typo
    ("Santagny",   "Santanyí",   "Mallorca"),    # Madoz typo
    ("Benisalem",  "Binissalem", "Mallorca"),    # Madoz uses BENISALEM
    ("Puebla",     "sa Pobla",   "Mallorca"),
    ("San Juan",   "Sant Joan",  "Mallorca"),
    ("San Lorenzo ó Llorens Descardasar",
                   "Sant Llorenç des Cardassar","Mallorca"),
    ("Maria de la Salud","Maria de la Salut","Mallorca"),
    # Madoz OCR variants where the canonical second form is dropped
    # by clean_title's «X ó Y» trailing strip. Curate the first form
    # explicitly.
    ("Vañalbufar", "Banyalbufar","Mallorca"),    # Madoz V/B OCR
    ("Fornaluig",  "Fornalutx",  "Mallorca"),    # Madoz spelling
    # 1860 nomenclator spellings.
    ("Llummayor",  "Llucmajor",  "Mallorca"),
    ("Lluch-mayor","Llucmajor",  "Mallorca"),
    ("Llucmayor",  "Llucmajor",  "Mallorca"),    # Miñano single-C variant
    # Floridablanca / 1860 compounded toponym ("Campos del Puerto"
    # = the modern Campos villa).
    ("Campos del Puerto","Campos","Mallorca"),
    ("Campos del Puerto Real","Campos","Mallorca"),
    # Miñano OCR mangles / spellings of common sub-toponyms.
    ("Ascorca",    "Escorca",    "Mallorca"),    # Miñano A/E OCR
    ("Mansanella", "Mancor de la Vall","Mallorca"),  # Miñano spelling
    # Floridablanca aldea spellings.
    ("Esglayeta",  "s'Esgleieta","Mallorca"),
    ("Esglayeta, La","s'Esgleieta","Mallorca"),
    ("La Esglayeta","s'Esgleieta","Mallorca"),
    # Castell de Bellver (the Palma castle, often titled BELVER as a
    # standalone «castillo» article in Miñano/Madoz).
    ("Belver Castell","Castell de Bellver","Mallorca"),
    ("Castillo de Belver","Castell de Bellver","Mallorca"),
    # Eivissa parròquies that pre-date the modern Municipis. These
    # land on the historical hamlet of the same name.
    ("San Mateo",  "Sant Mateu d'Albarca","Eivissa"),
    ("San Agustín","Sant Agustí des Vedrà","Eivissa"),
    ("San Agustin","Sant Agustí des Vedrà","Eivissa"),
    # Miñano santuari de Lluc.
    ("Nuestra Señora de Lluch","Lluc","Mallorca"),
    # Mallorca salines (the village ses Salines + the historic
    # salt flats). The Miñano "SALINAS (las)" loses the article when
    # parens are stripped, so curate the bare form too.
    ("Las Salinas","ses Salines","Mallorca"),
    ("Salinas Las","ses Salines","Mallorca"),
    ("Salinas",    "ses Salines","Mallorca"),
    # The Palma castle (often a stand-alone «castillo» article in
    # Miñano under the bare toponym).
    ("Belver",     "Castell de Bellver","Mallorca"),  # OCR variant
    ("Castell de Belver","Castell de Bellver","Mallorca"),

    # Floridablanca-specific Castilianised forms (1787, INE re-typed).
    # These show up in the floridablanca/web/data.json name_current
    # field; most are also covered above but a couple of long forms
    # come straight from the INE transcription.
    ("Villa de Mahón",          "Maó",                "Menorca"),
    ("Villa de Ciudadela",      "Ciutadella de Menorca","Menorca"),
    ("Ciudad de Palma",         "Palma",              "Mallorca"),
    ("Villa de Inca",           "Inca",               "Mallorca"),
    ("Villa de Felanitx",       "Felanitx",           "Mallorca"),
]


def main():
    con = duckdb.connect(':memory:')
    # Escape SQL apostrophes (e.g. "Salt d'aigua").
    types_sql = ', '.join(f"'{t.replace(chr(39), chr(39)*2)}'" for t in sorted(ALL_TYPES))

    print(f'Loading NGIB main toponyms (types: {len(ALL_TYPES)})…', file=sys.stderr)
    ngib_rows = con.sql(f"""
        SELECT
          spelling, municipality, island, local_type_name,
          geographic_name_id, lon, lat
        FROM read_parquet('{NGIB_DATA}/ngib.parquet')
        WHERE local_type_name IN ({types_sql})
          AND spelling IS NOT NULL
          AND length(spelling) >= 3
          AND status = 'vigent'
    """).fetchall()
    print(f'  → {len(ngib_rows):,} primary toponyms', file=sys.stderr)

    print('Loading NGIB variants…', file=sys.stderr)
    variant_rows = con.sql(f"""
        SELECT v.spelling, v.municipality, n.island, n.local_type_name,
               v.geographic_name_id, n.lon, n.lat
        FROM read_parquet('{NGIB_DATA}/ngib_variants.parquet') v
        LEFT JOIN read_parquet('{NGIB_DATA}/ngib.parquet') n
          ON v.geographic_name_id = n.geographic_name_id
        WHERE v.spelling IS NOT NULL AND length(v.spelling) >= 3
    """).fetchall()
    print(f'  → {len(variant_rows):,} variant spellings', file=sys.stderr)

    settlement_set = SETTLEMENT_TYPES | POSSESSION_TYPES

    # Sort the combined rows so that, within each (normalized, island)
    # group, the most authoritative local type comes first. The dedup loop
    # below then picks that one and drops the rest. Without this step the
    # dedup is order-dependent and can promote a possessió over the
    # actual village of the same name (`sa Pobla` farm in Llucmajor was
    # winning over `sa Pobla` municipality in the north of Mallorca).
    all_rows = ngib_rows + variant_rows
    all_rows.sort(key=lambda r: (
        normalize(r[0] or '') or '~',  # group by normalized name
        r[2] or '',                    # then by island
        _type_rank(r[3]),              # then most authoritative type first
    ))

    out_rows = []
    seen_norm = set()  # dedupe by (normalized, island)

    for (spelling, mun, isl, ltype, gn_id, lon, lat) in all_rows:
        if not spelling or not isl:
            continue
        norm = normalize(spelling)
        if not norm or len(norm) < 3:
            continue
        key = (norm, isl)
        if key in seen_norm:
            continue
        seen_norm.add(key)
        tokens = norm.split()
        first_tok = tokens[0] if tokens else ''
        is_settlement = (ltype in settlement_set)
        out_rows.append({
            'id': str(gn_id) if gn_id else '',
            'spelling': spelling,
            'normalized': norm,
            'tokens': ' '.join(tokens),
            'first_token': first_tok,
            'municipality': mun or '',
            'island': isl,
            'local_type': ltype or '',
            'is_settlement': is_settlement,
            'lon': float(lon) if lon and lon != 'None' else None,
            'lat': float(lat) if lat and lat != 'None' else None,
            'source': 'ngib',
        })

    print(f'After dedupe: {len(out_rows):,} NGIB rows', file=sys.stderr)

    # Append historical Castilian/OCR-variant spellings.
    for (hist, modern, isl) in HISTORICAL_VARIANTS:
        norm = normalize(hist)
        if not norm or len(norm) < 3:
            continue
        if (norm, isl) in seen_norm:
            continue
        seen_norm.add((norm, isl))
        tokens = norm.split()
        out_rows.append({
            'id': f'hist:{hist}',
            'spelling': hist,
            'normalized': norm,
            'tokens': ' '.join(tokens),
            'first_token': tokens[0] if tokens else '',
            'municipality': modern,
            'island': isl,
            'local_type': 'Variant històrica',
            'is_settlement': True,
            'lon': None, 'lat': None,
            'source': 'historical',
        })

    print(f'Total gazetteer entries: {len(out_rows):,}', file=sys.stderr)

    # Save as parquet via duckdb (CREATE TABLE + INSERT — register() can't
    # take a Python list-of-dicts directly).
    out_path = ROOT / 'data' / 'gazetteer.parquet'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute("""
        CREATE TABLE out (
            id              VARCHAR,
            spelling        VARCHAR,
            normalized      VARCHAR,
            tokens          VARCHAR,
            first_token     VARCHAR,
            municipality    VARCHAR,
            island          VARCHAR,
            local_type      VARCHAR,
            is_settlement   BOOLEAN,
            lon             DOUBLE,
            lat             DOUBLE,
            source          VARCHAR
        )
    """)
    cols = ['id','spelling','normalized','tokens','first_token','municipality',
            'island','local_type','is_settlement','lon','lat','source']
    con.executemany(
        f"INSERT INTO out VALUES ({', '.join('?' * len(cols))})",
        [[r[c] for c in cols] for r in out_rows],
    )
    con.sql(f"COPY (SELECT * FROM out) TO '{out_path}' (FORMAT PARQUET)")
    print(f'Wrote {out_path}', file=sys.stderr)

    # Stats
    print('\n=== gazetteer breakdown ===', file=sys.stderr)
    s = con.sql("""
        SELECT island, count(*) FROM out GROUP BY island ORDER BY count(*) DESC
    """).fetchall()
    for isl, n in s:
        print(f'  {isl or "(unknown)":<14} {n:>6,}', file=sys.stderr)
    print(file=sys.stderr)
    s = con.sql("""
        SELECT source, count(*) FROM out GROUP BY source
    """).fetchall()
    for src, n in s:
        print(f'  source={src:<12} {n:>6,}', file=sys.stderr)
    print(file=sys.stderr)
    s = con.sql("""
        SELECT is_settlement, count(*) FROM out GROUP BY is_settlement
    """).fetchall()
    for flag, n in s:
        print(f'  is_settlement={str(flag):<6} {n:>6,}', file=sys.stderr)

if __name__ == '__main__':
    main()
