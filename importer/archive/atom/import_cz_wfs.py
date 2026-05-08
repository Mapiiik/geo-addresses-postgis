#!/usr/bin/env python3
import subprocess
import psycopg2
import os

# Czech INSPIRE Address WFS endpoint
WFS_URL = "WFS:https://services.cuzk.cz/wfs/inspire-ad-wfs.asp"

# PostgreSQL connection
PG_CONN = os.getenv(
    "PG_CONN_ADDRESSES",
    "host=localhost dbname=addresses user=ruian password=xxx"
)

PG_CONN_OGR = "PG:" + PG_CONN
PG_CONN_SQL = PG_CONN

# Keep LAYERS structure, but only one layer for now
LAYERS = {
    "ad:Address": "cz_addresses",
}

def run_sql(sql):
    conn = psycopg2.connect(PG_CONN_SQL)
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()

def import_layer(layer_name, table_name):
    print(f"Importing {layer_name} → {table_name}")

    subprocess.run([
        "ogr2ogr",
        "-f", "PostgreSQL",
        PG_CONN_OGR,
        WFS_URL,
        layer_name,
        "-nln", table_name,
        "-overwrite",
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "FID=ogc_fid"
    ], check=True)

def add_wgs84_geometry():
    print("Adding WGS84 geometry column…")

    run_sql("""
        ALTER TABLE cz_addresses
        ADD COLUMN IF NOT EXISTS geom_wgs geometry(Point, 4326);
    """)

    print("Transforming geometries to EPSG:4326…")

    run_sql("""
        UPDATE cz_addresses
        SET geom_wgs = ST_Transform(geom, 4326)
        WHERE geom IS NOT NULL;
    """)

def create_indexes():
    print("Creating indexes…")

    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_geom_idx ON cz_addresses USING GIST (geom);")
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_geom_wgs_idx ON cz_addresses USING GIST (geom_wgs);")

    # INSPIRE Address fields
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_locator_idx ON cz_addresses(locatorDesignator);")
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_locatorname_idx ON cz_addresses(locatorName);")
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_street_idx ON cz_addresses(thoroughfareName_href);")
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_adminunit_idx ON cz_addresses(adminUnitName_href);")
    run_sql("CREATE INDEX IF NOT EXISTS cz_addr_postcode_idx ON cz_addresses(postCode_href);")

def analyze():
    print("Running ANALYZE…")
    run_sql("ANALYZE cz_addresses;")

if __name__ == "__main__":
    for layer, table in LAYERS.items():
        import_layer(layer, table)

    add_wgs84_geometry()
    create_indexes()
    analyze()

    print("CZ WFS import completed successfully.")
