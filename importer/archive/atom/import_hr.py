#!/usr/bin/env python3
import requests
import zipfile
import io
import subprocess
import os
import xml.etree.ElementTree as ET
import psycopg2

ATOM_URL = "https://geoportal.dgu.hr/services/atom/ad/xml"

PG_CONN = os.getenv(
    "PG_CONN_ADDRESSES",
    "host=localhost dbname=addresses user=ruian password=xxx"
)

PG_CONN_OGR = "PG:" + PG_CONN # ogr2ogr
PG_CONN_SQL = PG_CONN         # psycopg2

TABLES = {
    "Address.gml": "hr_addresses",
    "ThoroughfareName.gml": "hr_streets",
    "AdminUnitName.gml": "hr_admin_units",
    "PostalDescriptor.gml": "hr_postcodes",
}


def run_sql(sql):
    conn = psycopg2.connect(PG_CONN_SQL)
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()


def get_zip_url():
    print("Downloading ATOM feed…")
    response = requests.get(ATOM_URL)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    for entry in root.findall("atom:entry", ns):
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("type") == "application/x-gmz":
                return link.attrib["href"]

    raise Exception("ZIP URL not found in ATOM feed")


def download_and_extract(zip_url):
    print(f"Downloading ZIP: {zip_url}")
    response = requests.get(zip_url)
    response.raise_for_status()

    if not os.path.exists("data"):
        os.makedirs("data")

    z = zipfile.ZipFile(io.BytesIO(response.content))
    z.extractall("data")

    return "data"


def import_gml(gml_path, table_name):
    print(f"Importing {gml_path} → {table_name}")

    subprocess.run([
        "ogr2ogr",
        "-f", "PostgreSQL",
        PG_CONN_OGR,
        gml_path,
        "-nln", table_name,
        "-overwrite",
        "-lco", "GEOMETRY_NAME=geom"
    ], check=True)


def create_indexes():
    print("Creating indexes…")

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_addresses_geom_idx
            ON hr_addresses USING GIST (geom);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_addresses_thoroughfare_idx
            ON hr_addresses(thoroughfarename_localid);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_addresses_adminunit_idx
            ON hr_addresses(adminunitname_localid);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_addresses_postcode_idx
            ON hr_addresses(postaldescriptor_localid);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_streets_text_idx
            ON hr_streets(text);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_admin_units_text_idx
            ON hr_admin_units(text);
    """)

    run_sql("""
        CREATE INDEX IF NOT EXISTS hr_postcodes_postcode_idx
            ON hr_postcodes(postcode);
    """)


def create_view():
    print("Creating view hr_addresses_full…")

    run_sql("""
        CREATE OR REPLACE VIEW hr_addresses_full AS
        SELECT
            a.ogc_fid,
            a.geom,
            s.text AS street,
            a.designator AS house_number,
            u.text AS settlement,
            p.postcode AS postcode,
            a.alternativeidentifier AS full_address,
            a.localid AS address_localid,
            a.gml_id AS address_gml_id
        FROM hr_addresses a
        LEFT JOIN hr_streets s
            ON s.localid = a.thoroughfarename_localid
        LEFT JOIN hr_admin_units u
            ON u.localid = a.adminunitname_localid
        LEFT JOIN hr_postcodes p
            ON p.localid = a.postaldescriptor_localid;
    """)


def integrity_check():
    print("Running integrity check…")

    run_sql("""
        ANALYZE hr_addresses;
        ANALYZE hr_streets;
        ANALYZE hr_admin_units;
        ANALYZE hr_postcodes;
    """)

    print("Integrity check completed.")


if __name__ == "__main__":
    zip_url = get_zip_url()
    data_dir = download_and_extract(zip_url)

    for gml_file, table in TABLES.items():
        full_path = os.path.join(data_dir, gml_file)
        if os.path.exists(full_path):
            import_gml(full_path, table)
        else:
            print(f"WARNING: {gml_file} not found in ZIP")

    create_indexes()
    create_view()
    integrity_check()

    print("All imports and SQL operations completed successfully.")
