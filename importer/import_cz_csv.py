#!/usr/bin/env python3
"""
RUIAN CZ addresses importer.

Downloads the monthly address dump from CUZK, imports it into PostgreSQL/PostGIS
through a staging table using COPY, parallelized per region. Geometry is
materialized in a single pass in the database after all COPY operations finish.

Zero-downtime strategy:
  - The currently live `cz_addresses` table stays online for the entire import.
  - New data is loaded into `cz_addresses_staging`, materialized into
    `cz_addresses_new`, indexed, and analyzed — all without touching the live table.
  - A final atomic swap (DROP + RENAME inside a single transaction) replaces
    the old table with the new one. The exclusive lock is held only for
    milliseconds.
"""
import os
import shutil
import tempfile
import datetime
from calendar import monthrange
from concurrent.futures import ProcessPoolExecutor, as_completed
from zipfile import ZipFile

import requests

from importer.db import connect, ensure_extensions, run_sql

# RUIAN CSV URL template — date is YYYYMMDD of the last day of the month.
# vdp.cuzk.cz drops plain HTTP connections, so HTTPS is required.
DATA_URL = "https://vdp.cuzk.cz/vymenny_format/csv/{}_OB_ADR_csv.zip"

# Number of parallel workers for COPY (one CSV = one region)
MAX_WORKERS = int(os.getenv("RUIAN_WORKERS", "4"))

# How many months back to try if the current dump is not yet published
MAX_MONTHS_FALLBACK = 3

# formatted_address per vyhláška 359/2011 Sb., příloha 1 (vzory 1–6).
# Lokátor = ulice OR cast_obce (when ≠ obec); fallback to "č.p." prefix only
# when neither is shown. "č.ev." is always present before evidence numbers.
# cast_obce on its own line only when a street is also shown AND it differs
# from obec. Praha gets mop_nazev (e.g. "Praha 6") instead of plain obec_nazev
# — and since mop_nazev is only populated for Praha addresses in RUIAN,
# COALESCE(mop_nazev, obec_nazev) cleanly covers both. Stored proper-case; the
# search GIN trigram index is built on lower(formatted_address) so we don't
# need a duplicated column.
#
# Kept as a named constant rather than inlined below so the tests can apply the
# very expression that runs in production instead of a copy of it that would
# quietly drift — the whole of /v1/search is matched against its output.
FORMATTED_ADDRESS_SQL = """
            COALESCE(
                NULLIF(ulice_nazev, '') || ' ',
                CASE WHEN cast_obce_nazev IS NOT NULL
                          AND cast_obce_nazev <> obec_nazev
                     THEN cast_obce_nazev || ' ' END,
                ''
            )
            || CASE
                 WHEN typ_so = 'č.ev.' THEN 'č.ev. '
                 WHEN NULLIF(ulice_nazev, '') IS NULL
                      AND (cast_obce_nazev IS NULL
                           OR cast_obce_nazev = obec_nazev)
                   THEN 'č.p. '
                 ELSE ''
               END
            || COALESCE(cislo_domovni::text, '')
            || COALESCE('/' || cislo_orientacni::text, '')
            || COALESCE(cislo_orientacni_znak, '')
            || CASE WHEN NULLIF(ulice_nazev, '') IS NOT NULL
                         AND cast_obce_nazev IS NOT NULL
                         AND cast_obce_nazev <> obec_nazev
                    THEN ', ' || cast_obce_nazev
                    ELSE '' END
            || ', '
            || COALESCE(psc::text || ' ', '')
            || COALESCE(mop_nazev, obec_nazev)
"""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _candidate_dates():
    """Yield last-day-of-month dates, starting from the previous month going back."""
    now = datetime.datetime.now()
    year, month = now.year, now.month
    for _ in range(MAX_MONTHS_FALLBACK):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        last_day = monthrange(year, month)[1]
        yield f"{year}{month:02d}{last_day}"


def download_ruian(tmpdir):
    """Download and extract the RUIAN CSV dump. Returns path to the CSV directory."""
    last_error = None
    for date_name in _candidate_dates():
        url = DATA_URL.format(date_name)
        print(f"Trying: {url}")
        try:
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            last_error = e
            print(f"  not available ({e}), trying older month…")
            continue

        zip_path = os.path.join(tmpdir, "ruian.zip")
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)

        print(f"  downloaded {os.path.getsize(zip_path) / 1024 / 1024:.1f} MB, extracting…")
        with ZipFile(zip_path, "r") as z:
            z.extractall(tmpdir)

        csv_dir = os.path.join(tmpdir, "CSV")
        if not os.path.isdir(csv_dir):
            # Some dump versions extract straight into tmpdir without a CSV/ subdir
            csv_dir = tmpdir
        return csv_dir

    raise RuntimeError(
        f"Could not find RUIAN dump in last {MAX_MONTHS_FALLBACK} months. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def prepare_staging():
    """Create a fresh staging table. Does NOT touch the live cz_addresses table.

    Also drops any leftover cz_addresses_new from a previous failed run.
    """
    print("Preparing staging table…")
    run_sql("""
        DROP TABLE IF EXISTS cz_addresses_staging;
        DROP TABLE IF EXISTS cz_addresses_new;

        -- UNLOGGED skips WAL writes, which is significantly faster.
        -- The staging table mirrors the CSV layout 1:1 (y, x order as in the file).
        CREATE UNLOGGED TABLE cz_addresses_staging (
            kod_adm INTEGER,
            obec_kod INTEGER,
            obec_nazev VARCHAR,
            momc_kod INTEGER,
            momc_nazev VARCHAR,
            mop_kod INTEGER,
            mop_nazev VARCHAR,
            cast_obce_kod INTEGER,
            cast_obce_nazev VARCHAR,
            ulice_kod INTEGER,
            ulice_nazev VARCHAR,
            typ_so VARCHAR,
            cislo_domovni INTEGER,
            cislo_orientacni INTEGER,
            cislo_orientacni_znak VARCHAR,
            psc INTEGER,
            y DOUBLE PRECISION,
            x DOUBLE PRECISION,
            plati_od DATE
        );
    """)


# ---------------------------------------------------------------------------
# COPY worker (runs in a subprocess)
# ---------------------------------------------------------------------------

def copy_file_to_staging(path):
    """Worker: stream a single CSV file into the staging table via COPY."""
    conn = connect()
    try:
        cur = conn.cursor()

        # Per-session tuning for faster writes (no global server changes)
        cur.execute("SET LOCAL synchronous_commit = OFF;")
        cur.execute("SET LOCAL maintenance_work_mem = '512MB';")

        copy_sql = """
            COPY cz_addresses_staging (
                kod_adm, obec_kod, obec_nazev, momc_kod, momc_nazev,
                mop_kod, mop_nazev, cast_obce_kod, cast_obce_nazev,
                ulice_kod, ulice_nazev, typ_so, cislo_domovni,
                cislo_orientacni, cislo_orientacni_znak, psc,
                y, x, plati_od
            ) FROM STDIN WITH (FORMAT csv, DELIMITER ';', NULL '')
        """

        with open(path, encoding="windows-1250", errors="replace") as f:
            # Skip the header row (RUIAN CSV files have a column-name header)
            next(f)
            with cur.copy(copy_sql) as copy:
                # Stream line by line. psycopg3 buffers internally, so this is
                # as fast as feeding the whole file in one chunk; the str→bytes
                # encoding from windows-1250 to client_encoding (UTF-8) happens
                # transparently because the file was opened in text mode.
                for line in f:
                    copy.write(line)

        conn.commit()
        cur.close()
    finally:
        conn.close()
    return os.path.basename(path)


# ---------------------------------------------------------------------------
# Import orchestration
# ---------------------------------------------------------------------------

def import_ruian(src_dir):
    files = sorted(
        os.path.join(src_dir, f)
        for f in os.listdir(src_dir)
        if f.lower().endswith(".csv")
    )
    if not files:
        raise RuntimeError(f"No CSV files found in {src_dir}")

    print(f"Loading {len(files)} CSV file(s) into staging via COPY "
          f"(parallel, {MAX_WORKERS} workers)…")

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(copy_file_to_staging, p): p for p in files}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                done = fut.result()
                print(f"  ✓ {done}")
            except Exception as e:
                print(f"  ✗ {os.path.basename(name)} FAILED: {e}")
                raise


def materialize_new_table():
    """Build cz_addresses_new with geometry and search_label from staging.

    The live cz_addresses table is untouched during this step.

    Notes on coordinates:
      - The CSV column order is (Y, X), so the staging table also stores
        them as (y, x) — column name matches the CSV header.
      - RUIAN publishes the values as positive numbers, but the actual
        S-JTSK values are negative. We negate both before building the
        geometry.
      - EPSG:5514 (S-JTSK / Křovák East-North) expects axis order
        (easting, northing). After negation, `-y` is the easting and
        `-x` is the northing — hence ST_MakePoint(-y, -x), NOT (-x, -y).
        Getting the order wrong shifts results by hundreds of kilometres.

    The formatted_address column is built per Czech vyhláška 359/2011 Sb. § 6
    (rules for composing an address from RUIAN). It is both display-ready
    (proper case) and the basis of fuzzy search — the trigram GIN index is
    built on lower(formatted_address), so a single stored column powers both
    /v1/search and the formatted_address response field.
    """
    print("Materializing cz_addresses_new with geometry and formatted_address…")
    run_sql(f"""
        CREATE TABLE cz_addresses_new AS
        SELECT
            kod_adm,
            obec_kod, obec_nazev,
            momc_kod, momc_nazev,
            mop_kod, mop_nazev,
            cast_obce_kod, cast_obce_nazev,
            ulice_kod, ulice_nazev,
            typ_so,
            cislo_domovni, cislo_orientacni, cislo_orientacni_znak,
            psc,
            plati_od,
            ST_SetSRID(ST_MakePoint(-y, -x), 5514)                       AS geometry_jtsk,
            ST_Transform(ST_SetSRID(ST_MakePoint(-y, -x), 5514), 4326)   AS geometry,
            {FORMATTED_ADDRESS_SQL} AS formatted_address
        FROM cz_addresses_staging
        WHERE x IS NOT NULL AND y IS NOT NULL;

        ALTER TABLE cz_addresses_new ADD PRIMARY KEY (kod_adm);

        -- Enforce strict geometry types (CREATE TABLE AS does not preserve them)
        ALTER TABLE cz_addresses_new
            ALTER COLUMN geometry_jtsk TYPE geometry(Point, 5514)
                USING geometry_jtsk,
            ALTER COLUMN geometry TYPE geometry(Point, 4326)
                USING geometry;

        DROP TABLE cz_addresses_staging;
    """)


def create_indexes_on_new():
    """Build indexes on cz_addresses_new. The live table is unaffected."""
    print("Creating indexes on cz_addresses_new…")
    run_sql("""
        CREATE INDEX cz_addr_new_geometry_idx       ON cz_addresses_new USING GIST (geometry);
        CREATE INDEX cz_addr_new_geometry_jtsk_idx  ON cz_addresses_new USING GIST (geometry_jtsk);
        CREATE INDEX cz_addr_new_street_idx         ON cz_addresses_new (ulice_nazev);
        CREATE INDEX cz_addr_new_city_idx           ON cz_addresses_new (obec_nazev);
        CREATE INDEX cz_addr_new_psc_idx            ON cz_addresses_new (psc);
        -- pg_trgm GIN index on lower(formatted_address) powers fuzzy search
        -- on the API /v1/search endpoint. Functional index lets one stored
        -- column serve both the display label and the case-insensitive search.
        CREATE INDEX cz_addr_new_search_trgm_idx
            ON cz_addresses_new USING GIN (lower(formatted_address) gin_trgm_ops);
    """)


def analyze_new():
    print("Running ANALYZE on cz_addresses_new…")
    run_sql("ANALYZE cz_addresses_new;")


def atomic_swap():
    """Atomically replace the live cz_addresses table with cz_addresses_new.

    Wrapped in a single transaction so the swap is all-or-nothing. The
    exclusive lock is held only for the duration of the DROP + RENAME
    operations, which is effectively instant.

    Index names are also renamed back to their canonical form so subsequent
    runs find them under the expected names.
    """
    print("Performing atomic swap…")
    run_sql("""
        BEGIN;

        DROP TABLE IF EXISTS cz_addresses;

        ALTER TABLE cz_addresses_new RENAME TO cz_addresses;

        ALTER INDEX cz_addr_new_geometry_idx       RENAME TO cz_addr_geometry_idx;
        ALTER INDEX cz_addr_new_geometry_jtsk_idx  RENAME TO cz_addr_geometry_jtsk_idx;
        ALTER INDEX cz_addr_new_street_idx         RENAME TO cz_addr_street_idx;
        ALTER INDEX cz_addr_new_city_idx           RENAME TO cz_addr_city_idx;
        ALTER INDEX cz_addr_new_psc_idx            RENAME TO cz_addr_psc_idx;
        ALTER INDEX cz_addr_new_search_trgm_idx    RENAME TO cz_addr_search_trgm_idx;

        COMMIT;
    """)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    started = datetime.datetime.now()
    tmpdir = tempfile.mkdtemp(prefix="ruian_")
    print(f"Temp dir: {tmpdir}")

    try:
        ensure_extensions()
        src_dir = download_ruian(tmpdir)
        prepare_staging()
        import_ruian(src_dir)
        materialize_new_table()
        create_indexes_on_new()
        analyze_new()
        atomic_swap()
    finally:
        # Always clean up the temp directory, even on failure
        shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = datetime.datetime.now() - started
    print(f"CZ RUIAN import completed successfully in {elapsed}.")


if __name__ == "__main__":
    main()
