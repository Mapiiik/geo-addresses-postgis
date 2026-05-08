"""All v1 API endpoints.

Endpoints:
    POST /v1/lookup                          structured lookup w/ CZ fallback ladder
    POST /v1/lookup/batch                    bulk variant of /lookup
    GET  /v1/addresses/{source}/{registry_id}  by-id lookup
    GET  /v1/reverse                         nearest address to coords
    GET  /v1/search                          autocomplete (ILIKE prefix)
    GET  /v1/meta                            dataset metadata
    GET  /v1/health                          DB connectivity check

Optional payload extension is controlled by `?include=raw[,...]`:
    raw  → adds a `raw` dict with native source columns to each match
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response

from api import db, queries
from api.models import (
    AddressMatch,
    BatchLookupRequest,
    BatchLookupResponse,
    Country,
    DatasetMeta,
    HealthResponse,
    LookupRequest,
    LookupResponse,
    MetaResponse,
)
from api.settings import API_KEYS, CACHE_MAX_AGE

router = APIRouter(prefix="/v1")


# ---------------------------------------------------------------------------
# Auth + shared helpers
# ---------------------------------------------------------------------------

async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """No-op when API_KEYS is empty (dev mode); otherwise checks the header."""
    if not API_KEYS:
        return
    if x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key.")


def parse_include(include: str | None) -> set[str]:
    if not include:
        return set()
    return {p.strip() for p in include.split(",") if p.strip()}


def _row_to_match(row: dict, source: Country, include: set[str]) -> AddressMatch:
    raw_wanted = "raw" in include
    if source == "cz":
        return queries.cz_row_to_match(row, raw_wanted)
    return queries.hr_row_to_match(row, raw_wanted)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def _cz_params(req: LookupRequest) -> dict:
    """Resolve the CZ query parameters from a LookupRequest, parsing the raw
    composite number when explicit parts are not supplied."""
    house = req.house_number
    orient = req.orientation_number
    letter = req.orientation_letter
    if house is None and orient is None:
        h, o, l = queries.parse_cz_number(req.number)
        house = h if house is None else house
        orient = o if orient is None else orient
        letter = l if letter is None else letter

    # Normalise empty string → None so IS NOT DISTINCT FROM matches NULL columns.
    def _none_if_empty(v: str | None) -> str | None:
        return v if v else None

    return {
        "street": _none_if_empty(req.street),
        "city": _none_if_empty(req.city),
        "psc": int(req.postal_code) if req.postal_code else None,
        "typ_so": "č.ev." if req.number_type == "registration" else "č.p.",
        "cislo_domovni": house,
        "cislo_orientacni": orient,
        "cislo_orientacni_znak": _none_if_empty(letter),
    }


def _hr_params(req: LookupRequest) -> dict:
    return {
        "street": req.street or None,
        "number": req.number or None,
        "city": req.city or None,
        "postal_code": req.postal_code or None,
    }


async def _lookup_one(req: LookupRequest, include: set[str]) -> LookupResponse:
    async with db.get_conn() as conn:
        if req.country == "cz":
            rows, step = await queries.cz_lookup(conn, _cz_params(req))
            matches = [queries.cz_row_to_match(r, "raw" in include) for r in rows]
            return LookupResponse(
                matches=matches,
                fallback_step=step,
                ambiguous=len(matches) > 1,
            )
        # HR
        rows = await queries.hr_lookup(conn, _hr_params(req))
        matches = [queries.hr_row_to_match(r, "raw" in include) for r in rows]
        return LookupResponse(
            matches=matches,
            fallback_step=0 if matches else None,
            ambiguous=len(matches) > 1,
        )


@router.post(
    "/lookup",
    response_model=LookupResponse,
    dependencies=[Depends(require_api_key)],
    summary="Structured address lookup with optional fallback ladder.",
)
async def lookup(req: LookupRequest, include: str | None = Query(None)) -> LookupResponse:
    return await _lookup_one(req, parse_include(include))


@router.post(
    "/lookup/batch",
    response_model=BatchLookupResponse,
    dependencies=[Depends(require_api_key)],
    summary="Bulk version of /lookup. Each item is processed independently.",
)
async def lookup_batch(
    req: BatchLookupRequest, include: str | None = Query(None)
) -> BatchLookupResponse:
    inc = parse_include(include)
    results = [await _lookup_one(item, inc) for item in req.items]
    return BatchLookupResponse(results=results)


# ---------------------------------------------------------------------------
# By-id
# ---------------------------------------------------------------------------

@router.get(
    "/addresses/{source}/{registry_id}",
    response_model=AddressMatch,
    dependencies=[Depends(require_api_key)],
    summary="Look up a single address by its registry id (CZ kod_adm or HR ogc_fid).",
)
async def address_by_id(
    source: Country,
    registry_id: int,
    response: Response,
    include: str | None = Query(None),
) -> AddressMatch:
    inc = parse_include(include)
    async with db.get_conn() as conn:
        row = (
            await queries.cz_by_id(conn, registry_id)
            if source == "cz"
            else await queries.hr_by_id(conn, registry_id)
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{source.upper()} address {registry_id} not found.")
    response.headers["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"
    return _row_to_match(row, source, inc)


# ---------------------------------------------------------------------------
# Reverse geocoding
# ---------------------------------------------------------------------------

@router.get(
    "/reverse",
    response_model=list[AddressMatch],
    dependencies=[Depends(require_api_key)],
    summary="Find the nearest addresses to a coordinate.",
)
async def reverse(
    country: Country,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(500.0, gt=0, le=50_000),
    limit: int = Query(10, ge=1, le=100),
    include: str | None = Query(None),
) -> list[AddressMatch]:
    inc = parse_include(include)
    async with db.get_conn() as conn:
        rows = (
            await queries.cz_reverse(conn, lon, lat, radius_m, limit)
            if country == "cz"
            else await queries.hr_reverse(conn, lon, lat, radius_m, limit)
        )
    return [_row_to_match(r, country, inc) for r in rows]


# ---------------------------------------------------------------------------
# Search (autocomplete)
# ---------------------------------------------------------------------------

@router.get(
    "/search",
    response_model=list[AddressMatch],
    dependencies=[Depends(require_api_key)],
    summary="Prefix search on street + city, suitable for autocomplete.",
)
async def search(
    country: Country,
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(10, ge=1, le=50),
    include: str | None = Query(None),
) -> list[AddressMatch]:
    inc = parse_include(include)
    async with db.get_conn() as conn:
        rows = (
            await queries.cz_search(conn, q, limit)
            if country == "cz"
            else await queries.hr_search(conn, q, limit)
        )
    return [_row_to_match(r, country, inc) for r in rows]


# ---------------------------------------------------------------------------
# Meta + health
# ---------------------------------------------------------------------------

@router.get("/meta", response_model=MetaResponse, summary="Dataset row counts and freshness.")
async def meta(response: Response) -> MetaResponse:
    async with db.get_conn() as conn:
        cz = await queries.dataset_meta(conn, "cz_addresses")
        hr = await queries.dataset_meta(conn, "hr_addresses")
    response.headers["Cache-Control"] = "public, max-age=300"
    return MetaResponse(
        api_version="v1",
        datasets=[
            DatasetMeta(table="cz_addresses", **cz),
            DatasetMeta(table="hr_addresses", **hr),
        ],
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe + DB ping.")
async def health() -> HealthResponse:
    try:
        async with db.get_conn() as conn:
            ok = await queries.db_ping(conn)
    except Exception:
        return HealthResponse(status="degraded", db="down")
    return HealthResponse(status="ok" if ok else "degraded", db="up" if ok else "down")
