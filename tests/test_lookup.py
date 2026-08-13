"""The CZ fallback ladder and the remaining DB-backed helpers in queries.py.

The ladder exists because "city" means different things to different callers:
a CRM may hold the municipality, the Praha city borough, the city district or
the part-of-municipality, and RUIAN keeps all four in separate columns. Each
rung tries one of them and the first that matches wins, so what the tests pin
down is not just *whether* a lookup resolves but *at which rung* — a row that
starts resolving one rung earlier or later is a silent behaviour change.
"""
import pytest

from api import queries
from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


def cz_params(street=None, city=None, psc=None, house=None, orient=None,
              letter=None, typ_so="č.p."):
    """The parameter dict cz_lookup expects, with everything defaulting to NULL
    so each test only states the fields it cares about."""
    return {
        "street": street,
        "city": city,
        "psc": psc,
        "typ_so": typ_so,
        "cislo_domovni": house,
        "cislo_orientacni": orient,
        "cislo_orientacni_znak": letter,
    }


# --- the ladder, rung by rung --------------------------------------------

async def test_rung_0_street_and_municipality(seeded_conn):
    rows, step = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Karlova", city="Aš", psc=35201, house=248, orient=19),
    )

    assert step == 0
    assert rows[0]["formatted_address"] == "Karlova 248/19, 35201 Aš"


async def test_rung_1_city_given_as_praha_borough(seeded_conn):
    """"Praha 1" is a městský obvod, not a municipality — obec_nazev is "Praha"."""
    rows, step = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Karlova", city="Praha 1", psc=11000, house=144, orient=27),
    )

    assert step == 1
    assert rows[0]["formatted_address"] == "Karlova 144/27, Staré Město, 11000 Praha 1"


async def test_rung_2_city_given_as_city_district(seeded_conn):
    """Reachable only outside Praha: Brno has a MOMC but no MOP, so the MOP rung
    cannot answer first."""
    rows, step = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Masarykova", city="Brno-střed", psc=60200, house=307, orient=30),
    )

    assert step == 2
    assert rows[0]["formatted_address"] == "Masarykova 307/30, Brno-město, 60200 Brno"


async def test_rung_3_city_given_as_part_of_municipality(seeded_conn):
    rows, step = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Karlova", city="Staré Město", psc=11000, house=144, orient=27),
    )

    assert step == 3
    assert rows[0]["formatted_address"] == "Karlova 144/27, Staré Město, 11000 Praha 1"


async def test_rung_4_part_of_municipality_given_as_street(seeded_conn):
    """Village addresses have no street at all; callers put the part-of-
    municipality in the street field because that is what is on the envelope."""
    rows, step = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Buřany", city="Jablonec nad Jizerou", psc=51243, house=33),
    )

    assert step == 4
    assert rows[0]["formatted_address"] == "Buřany 33, 51243 Jablonec nad Jizerou"


async def test_no_rung_matches(seeded_conn):
    rows, step = await queries.cz_lookup(
        seeded_conn, cz_params(street="Karlova", city="Ostrava", psc=70200, house=1)
    )

    assert (rows, step) == ([], None)


# --- the base filter ------------------------------------------------------

async def test_evidence_numbers_are_a_separate_series(seeded_conn):
    """č.p. 31 and č.ev. 31 are different addresses; typ_so is what separates
    them, and it is an equality test rather than a fallback rung."""
    params = cz_params(street="Helkovice", city="Vysoké nad Jizerou", psc=51301, house=31)

    as_house, _ = await queries.cz_lookup(seeded_conn, params)
    as_registration, step = await queries.cz_lookup(
        seeded_conn, params | {"typ_so": "č.ev."}
    )

    assert as_house == []
    assert step == 4
    assert as_registration[0]["formatted_address"] == (
        "Helkovice č.ev. 31, 51301 Vysoké nad Jizerou"
    )


async def test_orientation_letter_is_part_of_the_match(seeded_conn):
    """1903/14a and 1903/14b are distinct addresses; dropping the letter must
    not silently match either of them."""
    found, _ = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Studentská", city="Praha 6", psc=16000,
                  house=1903, orient=14, letter="a"),
    )
    without_letter, _ = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Studentská", city="Praha 6", psc=16000, house=1903, orient=14),
    )

    assert found[0]["formatted_address"] == "Studentská 1903/14a, Dejvice, 16000 Praha 6"
    assert without_letter == []


async def test_postal_code_narrows_the_match(seeded_conn):
    rows, _ = await queries.cz_lookup(
        seeded_conn,
        cz_params(street="Karlova", city="Aš", psc=99999, house=248, orient=19),
    )

    assert rows == []


# --- HR -------------------------------------------------------------------

async def test_hr_lookup_is_exact_on_all_four_columns(seeded_conn):
    rows = await queries.hr_lookup(
        seeded_conn,
        {"street": "Ilica", "number": "100", "city": "Zagreb", "postal_code": 10000},
    )

    assert len(rows) == 1
    assert rows[0]["formatted_address"] == "Ilica 100, 10000 Zagreb"


async def test_hr_lookup_without_a_match(seeded_conn):
    rows = await queries.hr_lookup(
        seeded_conn,
        {"street": "Ilica", "number": "999", "city": "Zagreb", "postal_code": 10000},
    )

    assert rows == []


# --- by id ----------------------------------------------------------------

async def test_cz_by_id(seeded_conn):
    row = await queries.cz_by_id(seeded_conn, 25510878)

    assert row["formatted_address"] == "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou"
    assert await queries.cz_by_id(seeded_conn, 1) is None


async def test_hr_by_id_uses_the_stable_inspire_id(seeded_conn):
    row = await queries.hr_by_id(seeded_conn, "HR.DGU.RPJ:KB.0022072614")

    assert row["formatted_address"] == "Ilica 100, 10000 Zagreb"
    assert await queries.hr_by_id(seeded_conn, "HR.DGU.RPJ:KB.9999999999") is None


async def test_batch_by_id_returns_only_what_exists(seeded_conn):
    rows = await queries.cz_by_ids(seeded_conn, [25510878, 11855321, 1])

    assert {r["kod_adm"] for r in rows} == {25510878, 11855321}


@pytest.mark.parametrize("fn", [queries.cz_by_ids, queries.hr_by_ids])
async def test_batch_by_id_short_circuits_on_an_empty_list(seeded_conn, fn):
    """No ids means no query — `= ANY('{}')` would be a pointless round trip."""
    assert await fn(seeded_conn, []) == []


async def test_hr_batch_by_id(seeded_conn):
    rows = await queries.hr_by_ids(
        seeded_conn, ["HR.DGU.RPJ:KB.0022072614", "HR.DGU.RPJ:KB.9999999999"]
    )

    assert [r["inspire_id"] for r in rows] == ["HR.DGU.RPJ:KB.0022072614"]


# --- reverse geocoding ----------------------------------------------------

# Věnceslava Metelky 367; its nearest fixture neighbour is ~300 m away.
TARGET_LON, TARGET_LAT = 15.39923, 50.68743


async def test_reverse_orders_by_distance(seeded_conn):
    rows = await queries.cz_reverse(
        seeded_conn, TARGET_LON, TARGET_LAT, radius_m=5000, limit=10
    )

    assert rows[0]["formatted_address"] == (
        "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou"
    )
    assert rows[0]["distance_m"] == pytest.approx(0.0, abs=0.5)
    assert [r["distance_m"] for r in rows] == sorted(r["distance_m"] for r in rows)


async def test_reverse_respects_the_radius(seeded_conn):
    """A 50 m circle around the target holds nothing else."""
    rows = await queries.cz_reverse(
        seeded_conn, TARGET_LON, TARGET_LAT, radius_m=50, limit=10
    )

    assert len(rows) == 1


async def test_reverse_respects_the_limit(seeded_conn):
    rows = await queries.cz_reverse(
        seeded_conn, TARGET_LON, TARGET_LAT, radius_m=5000, limit=2
    )

    assert len(rows) == 2


async def test_hr_reverse(seeded_conn):
    rows = await queries.hr_reverse(seeded_conn, 15.96335, 45.81237, radius_m=100, limit=5)

    assert rows[0]["formatted_address"] == "Ilica 100, 10000 Zagreb"


# --- meta / health --------------------------------------------------------

async def test_db_ping(seeded_conn):
    assert await queries.db_ping(seeded_conn) is True


async def test_dataset_meta_counts_rows(seeded_conn):
    """reltuples is an estimate, but the fixture runs ANALYZE, so it is exact
    here — which is also what makes it trustworthy in production."""
    meta = await queries.dataset_meta(seeded_conn, "cz_addresses")

    assert meta["row_count"] == 15
    assert meta["last_analyzed"] is not None


async def test_dataset_meta_for_a_missing_table(seeded_conn):
    assert await queries.dataset_meta(seeded_conn, "no_such_table") == {
        "row_count": 0,
        "last_analyzed": None,
    }
