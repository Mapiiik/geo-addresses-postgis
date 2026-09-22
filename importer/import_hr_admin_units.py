"""
HR administrative units importer (INSPIRE AU, ATOM download).

Builds the lookup that says which municipality and which county a settlement
belongs to, so that Croatian addresses can be reported the way the regulator
asks for them - HAKOM's address returns name the county (županija) and the
local self-government unit (grad/općina), and the address dataset carries
neither.

Where it comes from:
  - The DGU publishes INSPIRE Administrative Units as a pre-packaged download
    rather than only through the WFS, which is how this importer gets at it:
    one zip holding one GML of some 50 000 units, all levels together.
  - A settlement is a 4th order unit, a municipality a 3rd, a county a 2nd,
    and each unit points at the one above it. That chain is the whole import.
  - The key that ties it to an address is `nationalCode`: for a settlement it
    is the very number the addresses carry as `naselje_id`.

What is kept:
  - One row per settlement, with the names and codes of the municipality and
    the county above it. Nothing else - the geometry, which is all but one per
    cent of the download, is thrown away as it is read.
"""
import datetime
import os
import tempfile
import zipfile
from xml.etree import ElementTree

import requests

from importer.db import connect, ensure_extensions, run_sql

DOWNLOAD_URL = os.getenv(
    "HR_ADMIN_UNITS_URL",
    "https://geoportal.dgu.hr/services/atom/INSPIRE_Administrative_Units_(AU).zip",
)

TABLE = "hr_admin_units"

# The levels the register uses, in the words of the INSPIRE code list.
SETTLEMENT = "4thOrder"
MUNICIPALITY = "3rdOrder"
COUNTY = "2ndOrder"

# Croatia has 21 counties (20 and the City of Zagreb) and some 550 towns and
# municipalities, so anything far below that means a download that was cut
# short rather than a country that was reorganised.
MIN_EXPECTED_SETTLEMENTS = 6_000

NS = {
    "au": "http://inspire.ec.europa.eu/schemas/au/4.0",
    "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
    "gn": "http://inspire.ec.europa.eu/schemas/gn/4.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "xlink": "http://www.w3.org/1999/xlink",
}

UNIT_TAG = f"{{{NS['au']}}}AdministrativeUnit"


def download(url, target):
    """Fetch the package, reporting progress as it goes.

    Streamed to disk rather than held in memory: the zip is some 200 MB and
    the GML inside it around 600 MB.
    """
    print(f"Downloading {url}…")
    with requests.get(url, stream=True, timeout=600) as response:
        response.raise_for_status()
        written = 0
        with open(target, "wb") as file:
            for chunk in response.iter_content(chunk_size=1 << 20):
                file.write(chunk)
                written += len(chunk)
                if written % (50 << 20) < (1 << 20):
                    print(f"  {written / (1 << 20):,.0f} MB…")
    print(f"Downloaded {written / (1 << 20):,.0f} MB.")


def read_units(gml_file):
    """Read every unit out of the GML, without keeping the geometry.

    Pulled apart as it streams: each unit is dropped as soon as it has been
    read, and the elements already handled are cut off the tree, or the
    geometry of the whole country would end up in memory at once.

    @return dict of gml id → unit
    """
    units = {}
    context = ElementTree.iterparse(gml_file, events=("start", "end"))
    _, root = next(context)

    for event, element in context:
        if event != "end" or element.tag != UNIT_TAG:
            continue

        level = element.find("au:nationalLevel", NS)
        upper = element.find("au:upperLevelUnit", NS)
        name = element.find("au:name/gn:GeographicalName/gn:spelling/gn:SpellingOfName/gn:text", NS)
        code = element.find("au:nationalCode", NS)

        units[element.get(f"{{{NS['gml']}}}id", "")] = {
            "level": (level.get(f"{{{NS['xlink']}}}href", "").rsplit("/", 1)[-1] if level is not None else ""),
            "code": (code.text or "").strip() if code is not None else "",
            "name": (name.text or "").strip() if name is not None else "",
            "upper": (upper.get(f"{{{NS['xlink']}}}href", "").lstrip("#") if upper is not None else ""),
        }

        element.clear()
        root.clear()

    return units


def settlements(units):
    """The settlements with the municipality and the county above them.

    A settlement whose chain is broken - a municipality the file does not
    hold, or one that points nowhere - is kept with whatever is known, so that
    an address in it still resolves to something.

    @return list of rows for the table
    """
    rows = []
    for unit in units.values():
        if unit["level"] != SETTLEMENT or not unit["code"]:
            continue

        municipality = units.get(unit["upper"], {})
        county = units.get(municipality.get("upper", ""), {})

        rows.append((
            int(unit["code"]),
            unit["name"],
            municipality.get("code") or None,
            municipality.get("name") or None,
            county.get("code") or None,
            county.get("name") or None,
        ))

    return rows


def store(rows):
    """Write the lookup into a working table and swap it in.

    The same shape the address importers use: the live table stays readable
    while the new one is filled, and the swap itself is a rename.
    """
    working = f"{TABLE}_new"
    print(f"Writing {len(rows):,} settlements into {working}…")

    run_sql(f"""
        DROP TABLE IF EXISTS {working};
        CREATE TABLE {working} (
            naselje_id     bigint PRIMARY KEY,
            naselje        character varying,
            jls_code       character varying,
            jls            character varying,
            zupanija_code  character varying,
            zupanija       character varying
        );
    """)

    conn = connect()
    try:
        with conn.cursor() as cur:
            with cur.copy(
                f"COPY {working} (naselje_id, naselje, jls_code, jls, zupanija_code, zupanija) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
        conn.commit()
    finally:
        conn.close()

    run_sql(f"""
        BEGIN;
        DROP TABLE IF EXISTS {TABLE};
        ALTER TABLE {working} RENAME TO {TABLE};
        ALTER INDEX {working}_pkey RENAME TO {TABLE}_pkey;
        COMMIT;
    """)
    run_sql(f"ANALYZE {TABLE};")


def main():
    started = datetime.datetime.now()

    ensure_extensions()

    with tempfile.TemporaryDirectory() as workspace:
        archive = os.path.join(workspace, "au.zip")
        download(DOWNLOAD_URL, archive)

        with zipfile.ZipFile(archive) as package:
            member = next(name for name in package.namelist() if name.lower().endswith(".gml"))
            print(f"Reading {member}…")
            with package.open(member) as gml:
                units = read_units(gml)

    print(f"Read {len(units):,} administrative units.")
    rows = settlements(units)

    if len(rows) < MIN_EXPECTED_SETTLEMENTS:
        raise RuntimeError(
            f"Import looks incomplete: got {len(rows):,} settlements, expected at least "
            f"{MIN_EXPECTED_SETTLEMENTS:,}. Aborting before swap — live table is untouched."
        )

    store(rows)

    elapsed = datetime.datetime.now() - started
    print(f"HR administrative units import completed successfully in {elapsed}.")


if __name__ == "__main__":
    main()
