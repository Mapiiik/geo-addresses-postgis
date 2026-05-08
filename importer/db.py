"""Shared DB configuration and helpers for all importer scripts.

Centralises:
  - reading PG_CONN_ADDRESSES (and failing loudly if it is unset),
  - building the ogr2ogr-flavoured PG: connection string,
  - the small `connect()` / `run_sql()` helpers used by every script.
"""
import os

import psycopg2

try:
    PG_CONN = os.environ["PG_CONN_ADDRESSES"]
except KeyError as exc:
    raise SystemExit(
        "PG_CONN_ADDRESSES is not set. "
        "Define it in your .env file (see .env.example) before running the importer."
    ) from exc

# ogr2ogr expects the libpq connection string prefixed with "PG:"
PG_CONN_OGR = "PG:" + PG_CONN


def connect():
    """Open a new psycopg2 connection using the shared connection string."""
    return psycopg2.connect(PG_CONN)


def run_sql(sql):
    """Execute a SQL block in its own connection and commit."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close()
    finally:
        conn.close()
