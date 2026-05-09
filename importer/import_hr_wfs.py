#!/usr/bin/env python3
"""
HR DGU addresses importer (INSPIRE WFS).

Downloads addresses from the Croatian DGU INSPIRE WFS endpoint, loads them
into PostgreSQL/PostGIS, and atomically swaps the result with the live table.

Schema (aligned with the CZ RUIAN importer):
  - geometry         geometry(Point, 4326)   — WGS84, used by most queries
  - geometry_htrs96  geometry(Point, 3765)   — native HTRS96/TM projection,
                                                kept for domestic Croatian
                                                reporting

Strategy:
  - Download via ogr2ogr directly into hr_addresses_new with the native HTRS96
    geometry stored as `geometry_htrs96`. The WGS84 column is added as a
    STORED generated column so it is filled in a single table rewrite, with
    no separate UPDATE pass.
  - Build all indexes after the import on the new table (GIST for spatial,
    btree for attributes).
  - Atomic DROP + RENAME swap so the live hr_addresses table stays online for
    the entire import. The exclusive lock is held only at the very end and
    only for milliseconds.
"""
import datetime
import subprocess

from importer.db import PG_CONN_OGR, connect, ensure_extensions, run_sql

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WFS_URL = "WFS:https://geoportal.dgu.hr/services/inspire/ad/wfs"

# INSPIRE layer → final table name
LAYERS = {
    "ad:AD.Address": "hr_addresses",
}

# Native Croatian SRID (HTRS96 / TM) and the WGS84 SRID we use for queries
NATIVE_SRID = 3765
WGS84_SRID = 4326

# Sanity-check threshold — Croatia has ~1.7M addresses; anything below this
# almost certainly means a truncated WFS response, so we abort before the swap.
MIN_EXPECTED_ROWS = 1_500_000


# ---------------------------------------------------------------------------
# Import steps
# ---------------------------------------------------------------------------

def prepare_workspace(table_name):
    """Drop any leftover _new table/sequence from a previous failed run."""
    working_table = f"{table_name}_new"
    seq_name = f"{working_table}_ogc_fid_seq"
    print(f"Cleaning up any stale workspace for {working_table}…")
    run_sql(f"""
        DROP TABLE IF EXISTS {working_table};
        DROP SEQUENCE IF EXISTS {seq_name};
    """)

def import_layer(layer_name, table_name):
    """Run ogr2ogr to fetch the WFS layer into the working table.

    The live hr_addresses table is not touched — we import into <table>_new
    and swap it in atomically at the end.

    ogr2ogr options:
      - -nln <table>_new            target working table
      - GEOMETRY_NAME=geometry_htrs96
                                    name the native-SRID geometry column to
                                    match the final schema directly, no later
                                    rename needed
      - SPATIAL_INDEX=NONE          skip the auto-built GIST index — we build
                                    indexes ourselves after the import (faster
                                    than maintaining one during inserts)
      - UNLOGGED=ON                 skip WAL during bulk load; we ALTER to
                                    LOGGED before the swap
      - GDAL_HTTP_TIMEOUT=600       10-minute timeout for slow WFS responses
    """
    working_table = f"{table_name}_new"
    print(f"Importing {layer_name} → {working_table} (native SRID: {NATIVE_SRID})")

    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        PG_CONN_OGR,
        WFS_URL,
        layer_name,
        "-nln", working_table,
        "-overwrite",
        "-lco", "GEOMETRY_NAME=geometry_htrs96",
        "-lco", "FID=ogc_fid",
        "-lco", "UNLOGGED=ON",
        "-lco", "SPATIAL_INDEX=NONE",
        "--config", "GDAL_HTTP_TIMEOUT", "600",
        "--config", "OGR_WFS_PAGING_ALLOWED", "YES",
        "--config", "OGR_WFS_PAGE_SIZE", "100000",
    ]
    subprocess.run(cmd, check=True)

def validate_import(table_name):
    """Sanity-check the import before we let it replace the live table."""
    working_table = f"{table_name}_new"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM {working_table};")
        count = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()
    print(f"Imported row count: {count:,}")
    if count < MIN_EXPECTED_ROWS:
        raise RuntimeError(
            f"Import looks incomplete: got {count:,} rows, expected at least "
            f"{MIN_EXPECTED_ROWS:,}. Aborting before swap — live table is untouched."
        )

def add_derived_columns(table_name):
    """Add WGS84 geometry and search_label as STORED generated columns.

    Both are computed once per row at write time and persisted on disk —
    no separate UPDATE pass. Done as a single ALTER TABLE so PostgreSQL
    only rewrites the table once for both columns combined.

    search_label is a lowercased "ulica kucni_broj, postanski_broj naselje"
    composite — feeds the pg_trgm GIN index used by /v1/search for fuzzy
    matching (typo tolerance, partial / out-of-order matches).
    """
    working_table = f"{table_name}_new"
    print(f"Adding WGS84 geometry and search_label as generated columns…")
    run_sql(f"""
        ALTER TABLE {working_table}
            ADD COLUMN geometry geometry(Point, {WGS84_SRID})
                GENERATED ALWAYS AS (ST_Transform(geometry_htrs96, {WGS84_SRID})) STORED,
            ADD COLUMN search_label text
                GENERATED ALWAYS AS (
                    lower(
                        COALESCE(ulica || ' ', '')
                        || COALESCE(kucni_broj::text, '')
                        || ', '
                        || COALESCE(postanski_broj::text || ' ', '')
                        || COALESCE(naselje, '')
                    )
                ) STORED;
    """)


def create_indexes(table_name):
    """Build all indexes on the new table before the swap.

    Important: the previous schema used `USING btree` even for the geometry
    columns, which is wrong — btree on geometry is essentially useless for
    spatial queries (the planner cannot use it for ST_DWithin, &&, etc.).
    This script creates GIST indexes for spatial columns, which is what
    PostGIS spatial queries actually need.
    """
    working_table = f"{table_name}_new"
    print(f"Creating indexes on {working_table}…")
    run_sql(f"""
        -- Spatial indexes (GIST — required for spatial queries)
        CREATE INDEX hr_addr_new_geometry_idx
            ON {working_table} USING GIST (geometry);
        CREATE INDEX hr_addr_new_geometry_htrs96_idx
            ON {working_table} USING GIST (geometry_htrs96);

        -- inspire_id is the stable HR address identifier (DGU INSPIRE format,
        -- e.g. "HR.DGU.RPJ:KB.0000021409"). Unlike ogc_fid — which ogr2ogr
        -- reassigns on every import — inspire_id is anchored in the source
        -- data and survives reimports. The API uses it as registry_ref, so
        -- a fast UNIQUE lookup is mandatory.
        CREATE UNIQUE INDEX hr_addr_new_inspire_id_idx
            ON {working_table} (inspire_id);

        -- Attribute indexes (btree)
        CREATE INDEX hr_addr_new_street_idx     ON {working_table} (ulica);
        CREATE INDEX hr_addr_new_house_idx      ON {working_table} (kucni_broj);
        CREATE INDEX hr_addr_new_settlement_idx ON {working_table} (naselje);
        CREATE INDEX hr_addr_new_postcode_idx   ON {working_table} (postanski_broj);

        -- pg_trgm GIN index powers fuzzy search on the API /v1/search endpoint.
        CREATE INDEX hr_addr_new_search_trgm_idx
            ON {working_table} USING GIN (search_label gin_trgm_ops);
    """)


def make_logged_and_analyze(table_name):
    """Switch the working table from UNLOGGED to LOGGED and analyze it.

    UNLOGGED tables are wiped on crash and not replicated — fine for the
    bulk-load phase, but we want full durability for the live table.
    """
    working_table = f"{table_name}_new"
    print(f"Switching {working_table} to LOGGED and running ANALYZE…")
    run_sql(f"ALTER TABLE {working_table} SET LOGGED;")
    run_sql(f"ANALYZE {working_table};")


def atomic_swap(table_name):
    """Atomically replace the live table with the new one.

    Wrapped in a single transaction so the swap is all-or-nothing. The
    exclusive lock is held only for the DROP + RENAME, which is effectively
    instant.
    """
    working_table = f"{table_name}_new"
    working_seq = f"{working_table}_ogc_fid_seq"
    final_seq = f"{table_name}_ogc_fid_seq"
    print(f"Performing atomic swap: {working_table} → {table_name}…")
    run_sql(f"""
        BEGIN;

        DROP TABLE IF EXISTS {table_name};

        ALTER TABLE {working_table} RENAME TO {table_name};

        ALTER SEQUENCE {working_seq} RENAME TO {final_seq};

        ALTER INDEX hr_addr_new_geometry_idx          RENAME TO hr_addr_geometry_idx;
        ALTER INDEX hr_addr_new_geometry_htrs96_idx   RENAME TO hr_addr_geometry_htrs96_idx;
        ALTER INDEX hr_addr_new_inspire_id_idx        RENAME TO hr_addr_inspire_id_idx;
        ALTER INDEX hr_addr_new_street_idx            RENAME TO hr_addr_street_idx;
        ALTER INDEX hr_addr_new_house_idx             RENAME TO hr_addr_house_idx;
        ALTER INDEX hr_addr_new_settlement_idx        RENAME TO hr_addr_settlement_idx;
        ALTER INDEX hr_addr_new_postcode_idx          RENAME TO hr_addr_postcode_idx;
        ALTER INDEX hr_addr_new_search_trgm_idx       RENAME TO hr_addr_search_trgm_idx;

        COMMIT;
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    started = datetime.datetime.now()

    ensure_extensions()

    for layer, table in LAYERS.items():
        prepare_workspace(table)
        import_layer(layer, table)
        validate_import(table)
        add_derived_columns(table)
        create_indexes(table)
        make_logged_and_analyze(table)
        atomic_swap(table)

    elapsed = datetime.datetime.now() - started
    print(f"HR DGU import completed successfully in {elapsed}.")


if __name__ == "__main__":
    main()
