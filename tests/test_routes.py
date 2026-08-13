"""The HTTP layer: status codes, validation and the response envelope.

These run the real ASGI app against the seeded schema, so they cover what the
query-level tests cannot — request parsing, the number parser that turns
"248/19" into its parts, the include= switch, the API-key dependency and the
error contract clients code against.
"""
from contextlib import asynccontextmanager

import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

CZ_ID = 25510878  # Věnceslava Metelky 367
HR_ID = "HR.DGU.RPJ:KB.0022072614"  # Ilica 100


# --- health / meta --------------------------------------------------------

async def test_health(client):
    r = await client.get("/v1/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "up"}


async def test_health_reports_a_dead_database(client, monkeypatch):
    """The probe must answer 200 with a degraded body rather than raise — an
    orchestrator reads the payload, and a 500 here looks like a broken app
    instead of a broken dependency."""
    from api import db

    @asynccontextmanager
    async def _boom():
        raise OSError("connection refused")
        yield  # pragma: no cover — unreachable, keeps this an async generator

    monkeypatch.setattr(db, "get_conn", _boom)
    r = await client.get("/v1/health")

    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "db": "down"}


async def test_meta(client):
    r = await client.get("/v1/meta")
    body = r.json()

    assert r.status_code == 200
    assert body["api_version"] == "v1"
    assert body["supported_countries"] == ["cz", "hr"]
    assert {d["table"] for d in body["datasets"]} == {"cz_addresses", "hr_addresses"}
    assert r.headers["cache-control"] == "public, max-age=300"


# --- search ---------------------------------------------------------------

async def test_search_envelope(client):
    r = await client.get("/v1/search", params={"country": "cz", "q": "Vysoké nad Jizerou 367"})
    [match] = r.json()

    assert r.status_code == 200
    assert match["registry_ref"] == str(CZ_ID)
    assert match["source"] == "cz"
    assert match["street"] == "Věnceslava Metelky"
    assert match["house_number"] == "367"
    assert match["number_type"] == "house"
    assert match["city"] == "Vysoké nad Jizerou"
    assert match["postal_code"] == "51211"
    assert match["formatted_address"] == "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou"
    assert match["geometry"] == {"type": "Point", "coordinates": [15.39923, 50.68743]}
    assert match["score"] == pytest.approx(1.0)
    assert match["distance_m"] is None
    assert match["raw"] is None


async def test_search_include_raw_adds_source_columns(client):
    r = await client.get(
        "/v1/search",
        params={"country": "cz", "q": "Vysoké nad Jizerou 367", "include": "raw"},
    )
    [match] = r.json()

    assert match["raw"]["kod_adm"] == CZ_ID
    assert match["raw"]["cast_obce_nazev"] == "Vysoké nad Jizerou"
    # Query-time artefacts must not leak into the "native columns" dict.
    assert not {"lon", "lat", "_score"} & set(match["raw"])


async def test_search_unknown_include_value_is_ignored(client):
    """include= is forward-compatible: an unknown token must not 400, so a
    client asking for a field a newer server has stays working against an
    older one."""
    r = await client.get(
        "/v1/search",
        params={"country": "cz", "q": "Vysoké nad Jizerou 367", "include": "geometry_native"},
    )

    assert r.status_code == 200
    assert r.json()[0]["raw"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"country": "xx", "q": "Praha"},
        {"country": "cz", "q": "a"},
        {"country": "cz", "q": "Praha", "limit": 0},
        {"country": "cz", "q": "Praha", "limit": 51},
        {"q": "Praha"},
    ],
    ids=["bad-country", "q-too-short", "limit-low", "limit-high", "no-country"],
)
async def test_search_validation(client, params):
    assert (await client.get("/v1/search", params=params)).status_code == 422


# --- by id ----------------------------------------------------------------

async def test_address_by_id(client):
    r = await client.get(f"/v1/addresses/cz/{CZ_ID}")

    assert r.status_code == 200
    assert r.json()["formatted_address"] == (
        "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou"
    )
    assert r.headers["cache-control"] == "public, max-age=3600"


async def test_address_by_id_hr(client):
    r = await client.get(f"/v1/addresses/hr/{HR_ID}")

    assert r.status_code == 200
    assert r.json()["formatted_address"] == "Ilica 100, 10000 Zagreb"


async def test_address_by_id_not_found(client):
    r = await client.get("/v1/addresses/cz/1")

    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


async def test_cz_id_must_be_numeric(client):
    """CZ ids are kod_adm; a non-numeric one is a client mistake, not a miss."""
    r = await client.get("/v1/addresses/cz/abc")

    assert r.status_code == 422


async def test_batch_by_id_reports_misses(client):
    r = await client.post(
        "/v1/addresses/batch",
        json={
            "items": [
                {"source": "cz", "registry_id": str(CZ_ID)},
                {"source": "hr", "registry_id": HR_ID},
                {"source": "cz", "registry_id": "1"},
            ]
        },
    )
    body = r.json()

    assert r.status_code == 200
    assert {m["registry_ref"] for m in body["matches"]} == {str(CZ_ID), HR_ID}
    assert body["not_found"] == [{"source": "cz", "registry_id": "1"}]


async def test_batch_by_id_reports_hr_misses(client):
    """Misses are tracked per source — an unknown INSPIRE id has to come back
    in not_found just like an unknown kod_adm."""
    r = await client.post(
        "/v1/addresses/batch",
        json={
            "items": [
                {"source": "hr", "registry_id": HR_ID},
                {"source": "hr", "registry_id": "HR.DGU.RPJ:KB.9999999999"},
            ]
        },
    )
    body = r.json()

    assert [m["registry_ref"] for m in body["matches"]] == [HR_ID]
    assert body["not_found"] == [
        {"source": "hr", "registry_id": "HR.DGU.RPJ:KB.9999999999"}
    ]


async def test_batch_by_id_dedupes_misses(client):
    """A duplicated id in the request must not produce a duplicated miss."""
    r = await client.post(
        "/v1/addresses/batch",
        json={"items": [{"source": "cz", "registry_id": "1"}] * 3},
    )

    assert r.json()["not_found"] == [{"source": "cz", "registry_id": "1"}]


async def test_batch_by_id_rejects_non_numeric_cz_id(client):
    r = await client.post(
        "/v1/addresses/batch",
        json={"items": [{"source": "cz", "registry_id": "abc"}]},
    )

    assert r.status_code == 422


# --- lookup ---------------------------------------------------------------

async def test_lookup_parses_the_composite_number(client):
    """The caller sends "248/19" as written on the envelope; splitting it into
    cislo_domovni / cislo_orientacni is the server's job."""
    r = await client.post(
        "/v1/lookup",
        json={"country": "cz", "street": "Karlova", "number": "248/19",
              "city": "Aš", "postal_code": "35201"},
    )
    body = r.json()

    assert r.status_code == 200
    assert body["fallback_step"] == 0
    assert body["ambiguous"] is False
    assert body["matches"][0]["formatted_address"] == "Karlova 248/19, 35201 Aš"


async def test_lookup_reports_the_rung_that_answered(client):
    r = await client.post(
        "/v1/lookup",
        json={"country": "cz", "street": "Karlova", "number": "144/27",
              "city": "Staré Město", "postal_code": "11000"},
    )

    assert r.json()["fallback_step"] == 3


async def test_lookup_explicit_number_parts_win_over_the_raw_string(client):
    r = await client.post(
        "/v1/lookup",
        json={"country": "cz", "street": "Studentská", "number": "ignored",
              "house_number": 1903, "orientation_number": 14,
              "orientation_letter": "a", "city": "Praha 6", "postal_code": "16000"},
    )

    assert r.json()["matches"][0]["formatted_address"] == (
        "Studentská 1903/14a, Dejvice, 16000 Praha 6"
    )


async def test_lookup_registration_number(client):
    r = await client.post(
        "/v1/lookup",
        json={"country": "cz", "street": "Helkovice", "number": "31",
              "number_type": "registration", "city": "Vysoké nad Jizerou",
              "postal_code": "51301"},
    )

    assert r.json()["matches"][0]["number_type"] == "registration"


async def test_lookup_without_a_match(client):
    r = await client.post(
        "/v1/lookup",
        json={"country": "cz", "street": "Karlova", "number": "1", "city": "Ostrava"},
    )
    body = r.json()

    assert body["matches"] == []
    assert body["fallback_step"] is None


async def test_lookup_hr(client):
    r = await client.post(
        "/v1/lookup",
        json={"country": "hr", "street": "Ilica", "number": "100",
              "city": "Zagreb", "postal_code": "10000"},
    )

    assert r.json()["matches"][0]["registry_ref"] == HR_ID


async def test_lookup_batch_keeps_item_order(client):
    r = await client.post(
        "/v1/lookup/batch",
        json={"items": [
            {"country": "cz", "street": "Karlova", "number": "248/19",
             "city": "Aš", "postal_code": "35201"},
            {"country": "cz", "street": "Karlova", "number": "1", "city": "Ostrava"},
        ]},
    )
    results = r.json()["results"]

    assert len(results) == 2
    assert results[0]["matches"][0]["formatted_address"] == "Karlova 248/19, 35201 Aš"
    assert results[1]["matches"] == []


# --- reverse --------------------------------------------------------------

async def test_reverse(client):
    r = await client.get(
        "/v1/reverse",
        params={"country": "cz", "lat": 50.68743, "lon": 15.39923, "radius_m": 50},
    )
    [match] = r.json()

    assert r.status_code == 200
    assert match["registry_ref"] == str(CZ_ID)
    assert match["distance_m"] == pytest.approx(0.0, abs=0.5)


@pytest.mark.parametrize(
    "params",
    [
        {"country": "cz", "lat": 91, "lon": 0},
        {"country": "cz", "lat": 0, "lon": 181},
        {"country": "cz", "lat": 50, "lon": 15, "radius_m": 0},
        {"country": "cz", "lat": 50, "lon": 15, "radius_m": 50001},
    ],
    ids=["lat-range", "lon-range", "radius-zero", "radius-too-big"],
)
async def test_reverse_validation(client, params):
    assert (await client.get("/v1/reverse", params=params)).status_code == 422


# --- auth -----------------------------------------------------------------

async def test_api_key_is_optional_when_none_configured(client):
    """Empty API_KEYS is the documented dev default: everything passes."""
    assert (await client.get("/v1/health")).status_code == 200


async def test_api_key_required_when_configured(client, monkeypatch):
    monkeypatch.setattr("api.routes.API_KEYS", {"watcher-crm"})
    params = {"country": "cz", "q": "Vysoké nad Jizerou 367"}

    missing = await client.get("/v1/search", params=params)
    wrong = await client.get("/v1/search", params=params, headers={"X-API-Key": "nope"})
    right = await client.get(
        "/v1/search", params=params, headers={"X-API-Key": "watcher-crm"}
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert right.status_code == 200


async def test_meta_stays_open_for_probes(client, monkeypatch):
    """/meta and /health carry no address data and are what a monitoring probe
    hits, so they are deliberately outside the API-key dependency."""
    monkeypatch.setattr("api.routes.API_KEYS", {"watcher-crm"})

    assert (await client.get("/v1/meta")).status_code == 200
    assert (await client.get("/v1/health")).status_code == 200


# --- generated docs -------------------------------------------------------

async def test_openapi_spec_lists_every_endpoint(client):
    spec = (await client.get("/openapi.json")).json()

    assert spec["openapi"].startswith("3.1")
    assert set(spec["paths"]) == {
        "/v1/lookup",
        "/v1/lookup/batch",
        "/v1/addresses/{source}/{registry_id}",
        "/v1/addresses/batch",
        "/v1/reverse",
        "/v1/search",
        "/v1/meta",
        "/v1/health",
    }


async def test_swagger_ui_is_served(client):
    assert (await client.get("/docs")).status_code == 200
