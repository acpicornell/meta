-- meta — cross-nomenclator schema (v2 dual-link model).
--
-- Three tables:
--   - places       : one canonical entity per NGIB geographic_name_id.
--                    Includes Municipi rows AND sub-features (Port de
--                    Sóller, Biniaraix, s'Arracó, Penyes d'Alaior, …)
--                    that have their own NGIB id and are referenced by
--                    at least one sibling article.
--   - entries      : one row per place mention across the five sources.
--                    Each entry carries TWO NGIB references:
--                      describes_ngib_id  — what the article is about
--                      parent_ngib_id     — the municipality where it sits
--                    plus an entry_kind tag classifying the article into
--                    one of four conceptual buckets.
--   - entry_resolution_log : auditable matcher decision log per entry,
--                    with candidates considered for both the parent and
--                    the describes resolutions.

DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS entry_resolution_log;
DROP TABLE IF EXISTS place_links;          -- legacy v1 audit table
DROP TABLE IF EXISTS places;

CREATE TABLE places (
    ngib_id        VARCHAR PRIMARY KEY,            -- NGIB geographic_name_id
    name_catalan   TEXT NOT NULL,
    municipality   TEXT,
    island         TEXT,
    local_type     TEXT,
    lat            DOUBLE,
    lng            DOUBLE
);
CREATE INDEX idx_places_island        ON places(island);
CREATE INDEX idx_places_municipality  ON places(municipality);
CREATE INDEX idx_places_local_type    ON places(local_type);

CREATE TABLE entries (
    id                    INTEGER PRIMARY KEY,
    source_project        TEXT NOT NULL,             -- floridablanca|minano|madoz|nomenclator_1860|riera
    source_id             TEXT NOT NULL,
    source_year           INTEGER NOT NULL,          -- 1787|1826|1845|1860|1881
    title                 TEXT NOT NULL,
    title_norm            TEXT NOT NULL,
    place_type            TEXT,
    island                TEXT,
    municipality          TEXT,                      -- raw from the sibling, if present
    source_url            TEXT,
    is_supplement         BOOLEAN DEFAULT FALSE,     -- Miñano Tom XI, Madoz Tom XVI, Riera (addicional)

    -- Dual NGIB linking.
    entry_kind            TEXT NOT NULL,             -- municipality | feature_with_ngib
                                                     -- | feature_no_ngib | jurisdictional
    describes_ngib_id     VARCHAR,                   -- the entity the article describes
                                                     -- (NULL for feature_no_ngib + jurisdictional)
    parent_ngib_id        VARCHAR,                   -- the municipality the article sits in
                                                     -- (may equal describes_ngib_id when kind=municipality)
    describes_method      TEXT,                      -- is_municipality | historical_curated |
                                                     -- exact_norm | fuzzy_wratio | fuzzy_token_set |
                                                     -- llm_describes | jurisdictional_no_equivalent | none
    parent_method         TEXT,                      -- hint_explicit | cross_reference |
                                                     -- self_municipality | llm_parent | unresolved
    describes_confidence  DOUBLE,
    parent_confidence     DOUBLE,

    blob                  JSON NOT NULL
);
CREATE INDEX idx_entries_describes ON entries(describes_ngib_id);
CREATE INDEX idx_entries_parent    ON entries(parent_ngib_id);
CREATE INDEX idx_entries_source    ON entries(source_project);
CREATE INDEX idx_entries_kind      ON entries(entry_kind);
CREATE INDEX idx_entries_title_norm ON entries(title_norm);

CREATE TABLE entry_resolution_log (
    source_project              TEXT NOT NULL,
    source_id                   TEXT NOT NULL,
    -- parent resolution
    parent_ngib_id              VARCHAR,
    parent_method               TEXT,
    parent_confidence           DOUBLE,
    parent_candidate_ids        VARCHAR[],
    parent_candidate_scores     DOUBLE[],
    -- describes resolution
    describes_ngib_id           VARCHAR,
    describes_method            TEXT,
    describes_confidence        DOUBLE,
    describes_candidate_ids     VARCHAR[],
    describes_candidate_scores  DOUBLE[],
    entry_kind                  TEXT,
    notes                       TEXT,                -- free text from the resolver (e.g. why unresolved)
    PRIMARY KEY (source_project, source_id)
);
