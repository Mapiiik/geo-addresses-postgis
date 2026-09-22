#!/usr/bin/env python3
"""
HR DGU addresses importer (INSPIRE AD, ATOM download).

Where it comes from:
  - The DGU publishes the addresses both through a WFS and as a pre-packaged
    download. This importer reads the package, because the WFS answers
    "Service WFS is disabled".
  - One zip holds four GML files: the addresses themselves and the three
    things an address points at - the street, the post office and the
    administrative units it lies in. The addresses are read last, with the
    other three already in hand, and each address is resolved as it streams
    past.
  - The identifier is the same one the WFS gave, `HR.DGU.RPJ:KB.0000000135`,
    so everything that already holds an address reference keeps resolving.

What the package does not carry: the building, the cadastral parcel and the
municipality it is in, and the rotation of the house number on the map. Those
columns stay empty - the national model had them, INSPIRE does not. The
county and the municipality are filled from `hr_admin_units` instead, the way
they were before.

Where an identifier is the register's own - the street, the post office - it
is taken from the package. Those are internal numbers and nothing is promised
about them matching what the WFS used to report. Only `inspire_id` is.

Strategy:
  - Stream the addresses into an UNLOGGED working table with COPY, keeping the
    source projection (ETRS89/LAEA) as it comes.
  - WGS84 and the domestic HTRS96 projection are added as STORED generated
    columns, so they are computed in a single table rewrite.
  - Build the indexes afterwards, then an atomic DROP + RENAME swap, so the
    live table stays online for the whole import and is locked only for the
    milliseconds the rename takes.
"""
import datetime
import os
import tempfile
import zipfile
from xml.etree import ElementTree

import requests

from importer.db import connect, ensure_extensions, run_sql

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOWNLOAD_URL = os.getenv(
    "HR_ADDRESSES_URL",
    "https://geoportal.dgu.hr/services/atom/INSPIRE_Addresses_(AD).zip",
)

TABLE = "hr_addresses"

# The settlement, the municipality and the county, as
# importer.import_hr_admin_units builds them.
ADMIN_UNITS_TABLE = "hr_admin_units"

# The projection the package is published in, the native Croatian one (HTRS96
# / TM) kept for domestic reporting, and the WGS84 we answer queries in.
SOURCE_SRID = 3035
NATIVE_SRID = 3765
WGS84_SRID = 4326

# Sanity-check threshold — Croatia has ~1.7M addresses; anything below this
# almost certainly means a truncated download, so we abort before the swap.
MIN_EXPECTED_ROWS = 1_500_000

# A settlement in the words of the INSPIRE code list. The package gives an
# address two administrative units, the settlement and the country, and only
# the first of them says anything an address is filed under.
SETTLEMENT = "4thOrder"

# The display label, "ulica kucni_broj, postanski_broj naselje". Kept as a
# named constant rather than inlined into the ALTER TABLE below so the tests
# can apply the very expression that runs in production — the whole of
# /v1/search is matched against its output, so a silent drift here would move
# search behaviour without touching a line of api/.
FORMATTED_ADDRESS_SQL = """
                    COALESCE(ulica || ' ', '')
                    || COALESCE(kucni_broj::text, '')
                    || ', '
                    || COALESCE(postanski_broj::text || ' ', '')
                    || COALESCE(naselje, '')
"""

NS = {
    "ad": "http://inspire.ec.europa.eu/schemas/ad/4.0",
    "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
    "gn": "http://inspire.ec.europa.eu/schemas/gn/4.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "xlink": "http://www.w3.org/1999/xlink",
}

GML_ID = f"{{{NS['gml']}}}id"
XLINK_HREF = f"{{{NS['xlink']}}}href"

ADDRESS_TAG = f"{{{NS['ad']}}}Address"
STREET_TAG = f"{{{NS['ad']}}}ThoroughfareName"
UNIT_TAG = f"{{{NS['ad']}}}AdminUnitName"
POST_TAG = f"{{{NS['ad']}}}PostalDescriptor"

MEMBERS = {
    "streets": "ThoroughfareName.gml",
    "units": "AdminUnitName.gml",
    "post_offices": "PostalDescriptor.gml",
    "addresses": "Address.gml",
}

COLUMNS = (
    "gml_id", "inspire_id",
    "kucni_broj", "broj", "podbroja_alfa", "podbroj_num",
    "ulica", "ulica_id", "ulica_redni_broj",
    "naselje", "naselje_id",
    "postanski_ured", "postanski_ured_id", "postanski_broj",
    "geometry_laea",
)


# ---------------------------------------------------------------------------
# Reading the package
# ---------------------------------------------------------------------------

def download(url, target):
    """Fetch the package, reporting progress as it goes.

    Streamed to disk rather than held in memory: the zip is some 85 MB and the
    addresses inside it 2.6 GB.
    """
    print(f"Downloading {url}…")
    with requests.get(url, stream=True, timeout=600) as response:
        response.raise_for_status()
        written = 0
        with open(target, "wb") as file:
            for chunk in response.iter_content(chunk_size=1 << 20):
                file.write(chunk)
                written += len(chunk)
                if written % (25 << 20) < (1 << 20):
                    print(f"  {written / (1 << 20):,.0f} MB…")
    print(f"Downloaded {written / (1 << 20):,.0f} MB.")


def features(gml_file, tag):
    """Every feature of one kind in the file, one at a time.

    Pulled apart as it streams, each element dropped as soon as it has been
    read and cut off the tree, or a 2.6 GB file would end up in memory.
    """
    context = ElementTree.iterparse(gml_file, events=("start", "end"))
    _, root = next(context)

    for event, element in context:
        if event != "end" or element.tag != tag:
            continue

        yield element

        element.clear()
        root.clear()


def text_of(element, path):
    """The text at a path, or None where the source left it out."""
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None

    return found.text.strip() or None


def local_id(element):
    """The register's own identifier and the namespace it lives in.

    @return tuple of the whole identifier and the number inside it
    """
    whole = text_of(element, "ad:inspireId/base:Identifier/base:localId")
    space = text_of(element, "ad:inspireId/base:Identifier/base:namespace")
    if whole is None:
        return None, None

    number = whole.rsplit(".", 1)[-1]

    return (f"{space}:{whole}" if space else whole), int(number) if number.isdigit() else None


def read_streets(gml_file):
    """The streets, by the identifier the addresses point at them with.

    A street is numbered within its settlement: the identifier the register
    knows it by is the settlement's six-digit code followed by the street's
    own four, which is where `ulica_redni_broj` comes from.

    @return dict of gml id → (name, id, number within the settlement)
    """
    streets = {}
    for element in features(gml_file, STREET_TAG):
        _, number = local_id(element)
        within = text_of(element, "ad:alternativeIdentifier")
        streets[element.get(GML_ID, "")] = (
            text_of(
                element,
                "ad:name/ad:ThoroughfareNameValue/ad:name/gn:GeographicalName"
                "/gn:spelling/gn:SpellingOfName/gn:text",
            ),
            number,
            int(within[6:]) if within and within[6:].isdigit() else None,
        )

    return streets


def read_settlements(gml_file):
    """The settlements, by the identifier the addresses point at them with.

    Only the settlements: the package gives an address its settlement and the
    country, and the chain in between - the municipality and the county - is
    not in this file at all. `alternativeIdentifier` is the national code,
    which is what an address is filed under as `naselje_id`.

    @return dict of gml id → (name, national code)
    """
    settlements = {}
    for element in features(gml_file, UNIT_TAG):
        level = element.find("ad:level", NS)
        if level is None or not level.get(XLINK_HREF, "").endswith(SETTLEMENT):
            continue

        code = text_of(element, "ad:alternativeIdentifier")
        settlements[element.get(GML_ID, "")] = (
            text_of(element, "ad:name/gn:GeographicalName/gn:spelling/gn:SpellingOfName/gn:text"),
            int(code) if code and code.isdigit() else None,
        )

    return settlements


def read_post_offices(gml_file):
    """The post offices, by the identifier the addresses point at them with.

    @return dict of gml id → (name, id, postcode)
    """
    post_offices = {}
    for element in features(gml_file, POST_TAG):
        _, number = local_id(element)
        code = text_of(element, "ad:postCode")
        post_offices[element.get(GML_ID, "")] = (
            text_of(element, "ad:postName/gn:GeographicalName/gn:spelling/gn:SpellingOfName/gn:text"),
            number,
            int(code) if code and code.isdigit() else None,
        )

    return post_offices


def house_number(element):
    """The house number as the register writes it, and its three parts.

    INSPIRE keeps the number, the letter after it and the sub-number apart,
    and the register's own label for the address puts them back together as
    "105A/1". That is what the number column has always held.

    @return tuple of the whole number, the number, the letter and the sub-number
    """
    parts = {}
    for locator in element.findall(
        "ad:locator/ad:AddressLocator/ad:designator/ad:LocatorDesignator", NS
    ):
        kind = locator.find("ad:type", NS)
        value = text_of(locator, "ad:designator")
        if kind is not None and value is not None:
            parts[kind.get(XLINK_HREF, "").rsplit("/", 1)[-1]] = value

    number = parts.get("addressNumber")
    letter = parts.get("addressNumberExtension")
    sub = parts.get("addressNumber2ndExtension")

    whole = (number or "") + (letter or "") + (f"/{sub}" if sub else "")

    return (
        whole or None,
        int(number) if number and number.isdigit() else None,
        letter,
        int(sub) if sub and sub.isdigit() else None,
    )


def point(element):
    """The address point as PostGIS reads it.

    The package is published in ETRS89/LAEA, whose axes run northing first.
    """
    position = text_of(
        element, "ad:position/ad:GeographicPosition/ad:geometry/gml:Point/gml:pos"
    )
    if position is None:
        return None

    northing, easting = position.split()[:2]

    return f"SRID={SOURCE_SRID};POINT({easting} {northing})"


def read_addresses(gml_file, streets, settlements, post_offices):
    """Every address in the file, resolved against the three lookups.

    An address points at its street, its post office and its administrative
    units by the identifiers those carry in the other three files. Anything
    the package leaves out - and a component pointing at something not in the
    package would be one - is left empty rather than dropping the address:
    a house with no street is an address all the same.

    @return generator of rows for the table
    """
    for element in features(gml_file, ADDRESS_TAG):
        street = post_office = (None, None, None)
        settlement = (None, None)

        for component in element.findall("ad:component", NS):
            reference = component.get(XLINK_HREF, "").lstrip("#")
            if reference in streets:
                street = streets[reference]
            elif reference in settlements:
                settlement = settlements[reference]
            elif reference in post_offices:
                post_office = post_offices[reference]

        whole, number, letter, sub = house_number(element)
        reference, _ = local_id(element)

        yield (
            element.get(GML_ID), reference,
            whole, number, letter, sub,
            street[0], street[1], street[2],
            settlement[0], settlement[1],
            post_office[0], post_office[1], post_office[2],
            point(element),
        )


# ---------------------------------------------------------------------------
# Import steps
# ---------------------------------------------------------------------------

def prepare_workspace(table_name):
    """Drop any leftover _new table/sequence from a previous failed run, and
    create the working table the addresses are streamed into.

    UNLOGGED skips the WAL for the bulk load — the table is switched to LOGGED
    before the swap. The columns the package does not carry are created all
    the same, because the API selects them by name.
    """
    working_table = f"{table_name}_new"
    print(f"Preparing {working_table}…")
    run_sql(f"""
        DROP TABLE IF EXISTS {working_table};
        DROP SEQUENCE IF EXISTS {working_table}_ogc_fid_seq;

        CREATE UNLOGGED TABLE {working_table} (
            ogc_fid               serial,
            gml_id                character varying,
            inspire_id            character varying,
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
            geometry_laea         geometry(Point, {SOURCE_SRID})
        );
    """)


def store(table_name, rows):
    """Stream the addresses into the working table.

    @return how many were written
    """
    working_table = f"{table_name}_new"
    print(f"Writing addresses into {working_table}…")

    written = 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            with cur.copy(
                f"COPY {working_table} ({', '.join(COLUMNS)}) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
                    written += 1
                    if written % 200_000 == 0:
                        print(f"  {written:,}…")
        conn.commit()
    finally:
        conn.close()

    print(f"Wrote {written:,} addresses.")

    return written


def drop_duplicates(table_name):
    """Keep one row per address identifier.

    The package hands the same identifier out twice here and there - two
    features, the same house, every field alike. The identifier is what the
    API answers by and what everything holding an address has stored, so it
    has to be unique, and the extra row is dropped rather than the import
    failing on its unique index. How many were dropped is printed, because a
    source that suddenly repeats itself in thousands of places is worth
    seeing.
    """
    working_table = f"{table_name}_new"
    run_sql(f"""
        DELETE FROM {working_table}
        WHERE ogc_fid IN (
            SELECT ogc_fid
            FROM (
                SELECT ogc_fid,
                       row_number() OVER (PARTITION BY inspire_id ORDER BY ogc_fid) AS repeat
                FROM {working_table}
            ) AS numbered
            WHERE repeat > 1
        );
    """)

    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT count(*) FROM {working_table};")
        print(f"Kept {cur.fetchone()[0]:,} addresses after dropping repeated identifiers.")
        cur.close()
    finally:
        conn.close()


def has_table(table_name):
    """Whether a table is there to be read."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
        exists = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()

    return bool(exists)


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
    """Add the two projections and formatted_address as STORED generated columns.

    All three are computed once per row at write time and persisted on disk —
    no separate UPDATE pass. Done as a single ALTER TABLE so PostgreSQL only
    rewrites the table once for all of them.

    The package is published in ETRS89/LAEA, which is the one projection
    nothing here wants to answer in: queries run in WGS84 and domestic
    reporting in HTRS96. Both are a change of projection on the same datum, so
    the round trip costs no accuracy worth speaking of.

    formatted_address is a proper-case display label
    "ulica kucni_broj, postanski_broj naselje". The same column also feeds
    the pg_trgm GIN index used by /v1/search via a functional index on
    lower(formatted_address), so we don't need a duplicated lowercase column.
    """
    working_table = f"{table_name}_new"
    print("Adding the WGS84 and HTRS96 geometries and formatted_address…")
    run_sql(f"""
        ALTER TABLE {working_table}
            ADD COLUMN geometry geometry(Point, {WGS84_SRID})
                GENERATED ALWAYS AS (ST_Transform(geometry_laea, {WGS84_SRID})) STORED,
            ADD COLUMN geometry_htrs96 geometry(Point, {NATIVE_SRID})
                GENERATED ALWAYS AS (ST_Transform(geometry_laea, {NATIVE_SRID})) STORED,
            ADD COLUMN formatted_address text
                GENERATED ALWAYS AS ({FORMATTED_ADDRESS_SQL}) STORED;
    """)


def add_administrative_units(table_name):
    """Say which municipality and which county each address is in.

    Joined on the settlement the address carries, which is the very number the
    register keeps the settlement under. The columns are added whether or not
    the lookup is there, because the API selects them: an installation that has
    not imported the administrative units yet answers with nulls rather than
    with an error, and the next import fills them in.

    Done here rather than by a view so that a lookup rebuilt between address
    imports cannot change what an address answered halfway through a run.
    """
    working_table = f"{table_name}_new"
    print(f"Adding zupanija and jls to {working_table}…")
    run_sql(f"""
        ALTER TABLE {working_table}
            ADD COLUMN zupanija character varying,
            ADD COLUMN jls character varying;
    """)

    if not has_table(ADMIN_UNITS_TABLE):
        print(
            f"  {ADMIN_UNITS_TABLE} is not there, leaving them empty — "
            "run importer.import_hr_admin_units and import the addresses again."
        )

        return

    run_sql(f"""
        UPDATE {working_table} AS addresses
        SET zupanija = units.zupanija,
            jls = units.jls
        FROM {ADMIN_UNITS_TABLE} AS units
        WHERE units.naselje_id = addresses.naselje_id;
    """)


def create_indexes(table_name):
    """Build all indexes on the new table before the swap."""
    working_table = f"{table_name}_new"
    print(f"Creating indexes on {working_table}…")
    run_sql(f"""
        -- Spatial indexes (GIST — required for spatial queries)
        CREATE INDEX hr_addr_new_geometry_idx
            ON {working_table} USING GIST (geometry);
        CREATE INDEX hr_addr_new_geometry_htrs96_idx
            ON {working_table} USING GIST (geometry_htrs96);

        -- Reverse geocoding asks in metres, so its ST_DWithin runs against
        -- geometry::geography. An index on the geometry column cannot serve a
        -- predicate on that expression, and without this one the query reads
        -- every row in the table.
        CREATE INDEX hr_addr_new_geography_idx
            ON {working_table} USING GIST ((geometry::geography));

        -- inspire_id is the stable HR address identifier (DGU INSPIRE format,
        -- e.g. "HR.DGU.RPJ:KB.0000021409"). Unlike ogc_fid — which is handed
        -- out afresh on every import — inspire_id is anchored in the source
        -- data and survives reimports. The API uses it as registry_ref, so
        -- a fast UNIQUE lookup is mandatory.
        CREATE UNIQUE INDEX hr_addr_new_inspire_id_idx
            ON {working_table} (inspire_id);

        -- Attribute indexes (btree)
        CREATE INDEX hr_addr_new_street_idx     ON {working_table} (ulica);
        CREATE INDEX hr_addr_new_house_idx      ON {working_table} (kucni_broj);
        CREATE INDEX hr_addr_new_settlement_idx ON {working_table} (naselje);
        CREATE INDEX hr_addr_new_postcode_idx   ON {working_table} (postanski_broj);

        -- pg_trgm GIN index on lower(formatted_address) powers fuzzy search
        -- on the API /v1/search endpoint. Functional index lets one stored
        -- column serve both the display label and case-insensitive search.
        CREATE INDEX hr_addr_new_search_trgm_idx
            ON {working_table} USING GIN (lower(formatted_address) gin_trgm_ops);
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
        ALTER INDEX hr_addr_new_geography_idx         RENAME TO hr_addr_geography_idx;
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

    with tempfile.TemporaryDirectory() as workspace:
        archive = os.path.join(workspace, "ad.zip")
        download(DOWNLOAD_URL, archive)

        with zipfile.ZipFile(archive) as package:
            with package.open(MEMBERS["streets"]) as gml:
                streets = read_streets(gml)
            print(f"Read {len(streets):,} streets.")

            with package.open(MEMBERS["units"]) as gml:
                settlements = read_settlements(gml)
            print(f"Read {len(settlements):,} settlements.")

            with package.open(MEMBERS["post_offices"]) as gml:
                post_offices = read_post_offices(gml)
            print(f"Read {len(post_offices):,} post offices.")

            prepare_workspace(TABLE)

            with package.open(MEMBERS["addresses"]) as gml:
                store(TABLE, read_addresses(gml, streets, settlements, post_offices))

    drop_duplicates(TABLE)
    validate_import(TABLE)
    add_derived_columns(TABLE)
    add_administrative_units(TABLE)
    create_indexes(TABLE)
    make_logged_and_analyze(TABLE)
    atomic_swap(TABLE)

    elapsed = datetime.datetime.now() - started
    print(f"HR DGU address import completed successfully in {elapsed}.")


if __name__ == "__main__":
    main()
