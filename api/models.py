"""Pydantic models for request bodies and response payloads."""
from datetime import date, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

Country = Literal["cz", "hr"]
NumberType = Literal["house", "registration"]

# Single source of truth for which country codes the API accepts. Derived from
# the Country Literal via typing.get_args, so adding a new country is a single
# edit to the Literal above — this list and the /v1/meta response update
# automatically.
SUPPORTED_COUNTRIES: list[str] = list(get_args(Country))


class Geometry(BaseModel):
    """GeoJSON-style point geometry in WGS84 (EPSG:4326), order [lon, lat]."""

    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]


class AddressMatch(BaseModel):
    """One matched address. Top-level fields are normalised; native columns
    are returned in `raw` only when the caller passes `?include=raw`."""

    registry_ref: str = Field(description="Source-specific stable id (RUIAN kod_adm or DGU ogc_fid)")
    source: Country
    street: str | None = None
    house_number: str | None = None
    number_type: NumberType | None = Field(
        default=None,
        description=(
            "Type of house number. 'registration' is CZ-specific (č.ev.); all "
            "other addresses default to 'house' — including HR DGU and any "
            "other source that doesn't distinguish. Null only when no number "
            "is present at all (rare)."
        ),
    )
    city: str | None = None
    postal_code: str | None = None
    formatted_address: str | None = Field(
        default=None,
        description=(
            "Display-ready address in the country's national format. CZ follows "
            "vyhláška 359/2011 Sb. § 6 (e.g. 'Studentská 1903/14a, Dejvice, "
            "16000 Praha 6'); HR uses 'ulica kucni_broj, postanski_broj naselje'. "
            "Splitting on ', ' yields 2-3 lines depending on the address kind."
        ),
    )
    geometry: Geometry
    distance_m: float | None = Field(
        default=None,
        description="Distance from the query point in metres (only set by /reverse).",
    )
    score: float | None = Field(
        default=None,
        description=(
            "Match score in the range 0-1, higher = better. "
            "Set by /search (pg_trgm word_similarity against the formatted "
            "address label). Useful for client-side thresholding so callers "
            "can ignore weak matches. None for endpoints that don't compute it."
        ),
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Native source columns. Populated only when `?include=raw`.",
    )


class LookupRequest(BaseModel):
    """Structured address lookup. The CZ side runs a 5-variant fallback ladder
    that mirrors the original consumer-side logic; HR runs a single match."""

    country: Country
    street: str | None = None
    # Either the raw composite number ("76/3a") OR the parsed parts. If both
    # are given, parsed parts win.
    number: str | None = Field(default=None, description="Raw number string (CZ format e.g. '2186/1b')")
    house_number: int | None = None
    orientation_number: int | None = None
    orientation_letter: str | None = None
    number_type: NumberType = Field(default="house", description="CZ-only: č.p. (house) or č.ev. (registration)")
    city: str | None = None
    postal_code: str | None = None


class LookupResponse(BaseModel):
    matches: list[AddressMatch]
    fallback_step: int | None = Field(
        default=None,
        description=(
            "Which lookup variant produced the result. "
            "0 = strict match, 1-4 = CZ fallback variants, null = no match found."
        ),
    )
    ambiguous: bool = Field(
        default=False,
        description="True if more than one address matched at the chosen step.",
    )


class BatchLookupRequest(BaseModel):
    items: list[LookupRequest]


class BatchLookupResponse(BaseModel):
    results: list[LookupResponse]


class BatchByIdItem(BaseModel):
    """One entry in a /v1/addresses/batch request — a (source, id) pair.

    `registry_id` is a string because identifier shape varies per source:
      - CZ: numeric kod_adm, e.g. "11855321"
      - HR: full INSPIRE id, e.g. "HR.DGU.RPJ:KB.0000021409"
    Pydantic coerces incoming JSON numbers to strings transparently, so a
    caller may pass `12345` or `"12345"` for CZ — both end up as "12345".
    """

    source: Country
    registry_id: str


class BatchByIdRequest(BaseModel):
    items: list[BatchByIdItem]


class BatchByIdResponse(BaseModel):
    matches: list[AddressMatch] = Field(
        description="Found addresses, in arbitrary order. Mix of CZ + HR if both were requested.",
    )
    not_found: list[BatchByIdItem] = Field(
        description="Items from the request that did not match anything in the DB.",
    )


class DatasetMeta(BaseModel):
    table: str
    row_count: int
    last_analyzed: datetime | None = Field(
        default=None,
        description=(
            "Timestamp of the last ANALYZE run on the table. The importer runs "
            "ANALYZE as the final step of every successful import, so this is "
            "a reliable proxy for 'last refreshed'."
        ),
    )


class MetaResponse(BaseModel):
    api_version: str
    supported_countries: list[str] = Field(
        description=(
            "ISO 3166-1 alpha-2 lowercase codes of the countries this API "
            "instance can serve (i.e. has a loaded dataset for). Use this "
            "to drive UI dropdowns and validation in clients without "
            "hardcoding the list."
        ),
    )
    datasets: list[DatasetMeta]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["up", "down"]
