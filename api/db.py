"""Async psycopg connection pool used by all route handlers."""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from api.settings import PG_CONN_API, PG_POOL_MAX, PG_POOL_MIN

_pool: AsyncConnectionPool | None = None


async def open_pool() -> None:
    """Open the global connection pool. Called once from FastAPI lifespan."""
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=PG_CONN_API,
        min_size=PG_POOL_MIN,
        max_size=PG_POOL_MAX,
        open=False,
    )
    await _pool.open()


async def close_pool() -> None:
    """Close the global pool. Called from FastAPI lifespan on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def get_conn() -> AsyncIterator[AsyncConnection]:
    """Yield a connection from the pool for the duration of one request."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised")
    async with _pool.connection() as conn:
        yield conn
