"""Shared DB configuration and helpers for all importer scripts.

Centralises:
  - reading PG_CONN_ADDRESSES (and failing loudly if it is unset),
  - building the ogr2ogr-flavoured PG: connection string,
  - the small `connect()` / `run_sql()` helpers used by every script.
"""
import os

import psycopg

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
    """Open a new psycopg connection using the shared connection string."""
    return psycopg.connect(PG_CONN)


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


def ensure_extensions():
    """Self-heal: ensure required PostgreSQL extensions are installed.

    - postgis: geometry types and spatial functions used everywhere.
    - pg_trgm: trigram-based fuzzy matching used by the API /v1/search
               endpoint (typo tolerance, partial / out-of-order matches).

    The postgis/postgis image installs postgis automatically on first DB
    init, but only when the data directory is empty. If you migrated an
    existing volume or added pg_trgm later, the extensions may be missing.
    This call is idempotent and cheap, so we run it at the start of every
    importer.

    Requires superuser privileges, which POSTGRES_USER has by default.
    """
    run_sql(
        """
        CREATE EXTENSION IF NOT EXISTS postgis;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        """
    )
