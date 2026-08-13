"""Test fixtures: a seeded, throwaway copy of the two address tables.

The behaviour under test is pg_trgm's, not Python's — word similarity, operator
thresholds, what a regex does at a word boundary — so the tests run against a
real PostGIS. `TEST_PG_CONN` points at one (CI starts a `postgis/postgis`
service container); without it the DB-backed tests skip rather than fail, so
`pytest` stays usable on a machine with no database.

Everything is created in a schema of its own and dropped afterwards, and
`search_path` makes the unqualified table names in api/queries.py resolve to
it. Pointing `TEST_PG_CONN` at a populated database is therefore safe: the
fixtures never touch `public`.

The seed is deliberately tiny, which means the planner will pick sequential
scans over the trigram index no matter what. That does not weaken the
assertions — `%>>` and `~` return the same rows either way, the index is only
an access path — but it does mean index usage itself cannot be tested here.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from psycopg import AsyncConnection

FIXTURES = Path(__file__).parent / "fixtures"
TEST_SCHEMA = "geo_addresses_test"

TEST_PG_CONN = os.environ.get("TEST_PG_CONN")

# api.settings refuses to import without a connection string — deliberately, so
# a misconfigured container fails at startup instead of on the first request.
# The route tests never open the pool (they substitute db.get_conn), so the
# value only has to exist.
os.environ.setdefault("PG_CONN_API", TEST_PG_CONN or "host=localhost dbname=unused")

requires_db = pytest.mark.skipif(
    not TEST_PG_CONN, reason="TEST_PG_CONN not set — skipping DB-backed tests"
)


@pytest_asyncio.fixture
async def seeded_conn():
    """Connection whose search_path points at a freshly seeded test schema.

    Function-scoped: re-seeding ~20 rows per test costs little and keeps the
    fixture free of the event-loop scoping that a session-scoped async fixture
    would drag in.
    """
    conn = await AsyncConnection.connect(TEST_PG_CONN, autocommit=True)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        # public stays on the path so the fixtures can reach the postgis and
        # pg_trgm functions, but the test schema shadows it for table names.
        await conn.execute(f"SET search_path TO {TEST_SCHEMA}, public")
        for name in ("schema.sql", "data.sql"):
            await conn.execute((FIXTURES / name).read_text(encoding="utf-8"))
        # queries.py relies on SET LOCAL, which needs a transaction; switching
        # autocommit off here makes the connection behave as it does under the
        # API's pool.
        await conn.set_autocommit(False)
        yield conn
    finally:
        await conn.rollback()
        await conn.set_autocommit(True)
        await conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        await conn.close()


@pytest_asyncio.fixture
async def client(seeded_conn, monkeypatch):
    """The real ASGI app, driven in-process, talking to the seeded schema.

    `db.get_conn` is substituted rather than the pool opened, so no lifespan
    runs and no sockets are involved — the routes still go through the genuine
    dependency, validation and serialisation path.
    """
    import httpx

    from api import db
    from api.main import app

    @asynccontextmanager
    async def _conn():
        yield seeded_conn

    monkeypatch.setattr(db, "get_conn", _conn)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def labels(rows) -> list[str]:
    """Search rows → their display labels, for readable assertions."""
    return [r["formatted_address"] for r in rows]
