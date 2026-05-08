"""FastAPI application for the geo-addresses-postgis REST API.

Generic address service over the cz_addresses + hr_addresses tables maintained
by this project's importer. The API itself is stateless and read-only — see
api/routes.py for the endpoint catalogue.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import db
from api.routes import router as v1_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="geo-addresses-postgis",
    version="1.0.0",
    description=(
        "REST API on top of the CZ RUIAN + HR DGU address database. "
        "Generic — not tied to any specific consumer."
    ),
    lifespan=lifespan,
    # OpenAPI spec auto-served at /openapi.json, Swagger UI at /docs.
)

app.include_router(v1_router)
