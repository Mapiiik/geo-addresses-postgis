-- Stand-ins for the two live tables, created in a throwaway schema.
--
-- Column names and types are copied from what the importers produce
-- (`pg_dump --schema-only -t cz_addresses -t hr_addresses`), and the full set
-- projected by CZ_COLUMNS / HR_COLUMNS is present: a column the API selects but
-- the importer stopped producing makes these tests fail, which is the point.
--
-- The functional GIN trigram index matters just as much as the columns — every
-- /v1/search condition is written against `lower(formatted_address)` so the
-- planner can serve them from it, and a fixture without it would still pass
-- while silently testing sequential scans.
--
-- hr_addresses keeps formatted_address as a GENERATED column, but the
-- expression is not written here: conftest fills the placeholder below in from
-- importer.import_hr_wfs.FORMATTED_ADDRESS_SQL, so the fixture composes its
-- labels exactly the way the importer does and cannot drift from it. (Do not
-- name that placeholder anywhere else in this file — the substitution is a
-- plain string replace and would splice SQL into a comment.)
--
-- The CZ importer builds its column inside a CREATE TABLE AS over staging
-- columns this table does not carry, so there the authentic strings are
-- inserted, and the expression itself is covered by test_formatted_address.py.
--
-- The native-projection geometries (geometry_jtsk / geometry_htrs96) are left
-- out: nothing in api/ reads them, and dropping them keeps the seed data in
-- plain WGS84.

CREATE TABLE cz_addresses (
    kod_adm               integer PRIMARY KEY,
    obec_kod              integer,
    obec_nazev            character varying,
    momc_kod              integer,
    momc_nazev            character varying,
    mop_kod               integer,
    mop_nazev             character varying,
    cast_obce_kod         integer,
    cast_obce_nazev       character varying,
    ulice_kod             integer,
    ulice_nazev           character varying,
    typ_so                character varying,
    cislo_domovni         integer,
    cislo_orientacni      integer,
    cislo_orientacni_znak character varying,
    psc                   integer,
    plati_od              date,
    geometry              geometry(Point, 4326),
    formatted_address     text
);

CREATE TABLE hr_addresses (
    ogc_fid               integer,
    gml_id                character varying,
    inspire_id            character varying PRIMARY KEY,
    zgrada_id             bigint,
    kucni_broj            character varying,
    broj                  integer,
    podbroja_alfa         character varying,
    podbroj_num           integer,
    rotacija              double precision,
    broj_cestice          character varying,
    ostale_vezane_cestice character varying,
    katastarska_opcina    character varying,
    ulica                 character varying,
    naselje               character varying,
    postanski_ured        character varying,
    naselje_id            bigint,
    ulica_id              bigint,
    postanski_ured_id     bigint,
    katastarska_opcina_id bigint,
    ulica_redni_broj      bigint,
    postanski_broj        integer,
    geometry              geometry(Point, 4326),
    formatted_address     text GENERATED ALWAYS AS ({hr_formatted_address}) STORED
);

CREATE INDEX cz_addr_search_trgm_idx
    ON cz_addresses USING GIN (lower(formatted_address) gin_trgm_ops);
CREATE INDEX hr_addr_search_trgm_idx
    ON hr_addresses USING GIN (lower(formatted_address) gin_trgm_ops);
