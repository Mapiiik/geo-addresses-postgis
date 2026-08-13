"""The display label the importers compose, per vyhláška 359/2011 Sb.

`formatted_address` is not just what clients render — it is the single column
/v1/search matches against, so its layout decides what is findable. A change
here moves search behaviour without touching a line of api/, which is why the
expression is asserted directly rather than through the search endpoint.

Both importers expose their SQL as `FORMATTED_ADDRESS_SQL`; the tests apply
that very expression, so a copy cannot drift out from under them. The expected
strings are the labels a real import produced for these exact rows.
"""
import pytest
from psycopg.rows import dict_row

from importer.import_cz_csv import FORMATTED_ADDRESS_SQL as CZ_SQL
from importer.import_hr_wfs import FORMATTED_ADDRESS_SQL as HR_SQL
from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

CZ_COLUMNS = (
    "obec_nazev", "mop_nazev", "cast_obce_nazev", "ulice_nazev", "typ_so",
    "cislo_domovni", "cislo_orientacni", "cislo_orientacni_znak", "psc",
)


async def cz_label(conn, **row) -> str:
    """Run the importer's own expression over one hypothetical staging row."""
    values = {c: row.get(c) for c in CZ_COLUMNS}
    selected = ", ".join(f"%({c})s AS {c}" for c in CZ_COLUMNS)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {CZ_SQL} AS label FROM (SELECT {selected}) AS staging", values
        )
        return (await cur.fetchone())["label"]


async def hr_label(conn, **row) -> str:
    values = {c: row.get(c) for c in ("ulica", "kucni_broj", "postanski_broj", "naselje")}
    selected = ", ".join(f"%({c})s AS {c}" for c in values)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {HR_SQL} AS label FROM (SELECT {selected}) AS staging", values
        )
        return (await cur.fetchone())["label"]


# --- CZ, vyhláška 359/2011 Sb. příloha 1 ----------------------------------

async def test_street_and_house_number(seeded_conn):
    """Vzor 1: street with a composite number, part-of-municipality equal to
    the municipality so it earns no line of its own."""
    assert await cz_label(
        seeded_conn, obec_nazev="Aš", cast_obce_nazev="Aš", ulice_nazev="Karlova",
        typ_so="č.p.", cislo_domovni=248, cislo_orientacni=19, psc=35201,
    ) == "Karlova 248/19, 35201 Aš"


async def test_part_of_municipality_gets_its_own_line(seeded_conn):
    """A street *and* a part-of-municipality that differs from the municipality
    means both are shown, the latter after the number."""
    assert await cz_label(
        seeded_conn, obec_nazev="Praha", mop_nazev="Praha 1",
        cast_obce_nazev="Staré Město", ulice_nazev="Karlova", typ_so="č.p.",
        cislo_domovni=144, cislo_orientacni=27, psc=11000,
    ) == "Karlova 144/27, Staré Město, 11000 Praha 1"


async def test_orientation_letter(seeded_conn):
    assert await cz_label(
        seeded_conn, obec_nazev="Praha", mop_nazev="Praha 6",
        cast_obce_nazev="Dejvice", ulice_nazev="Studentská", typ_so="č.p.",
        cislo_domovni=1903, cislo_orientacni=14, cislo_orientacni_znak="a", psc=16000,
    ) == "Studentská 1903/14a, Dejvice, 16000 Praha 6"


async def test_praha_borough_replaces_the_municipality(seeded_conn):
    """§ 6 (2) c): the postal city for Praha is the borough. mop_nazev is
    populated only for Praha, so the same COALESCE serves every other town."""
    in_praha = await cz_label(
        seeded_conn, obec_nazev="Praha", mop_nazev="Praha 6", cast_obce_nazev="Dejvice",
        ulice_nazev="Studentská", typ_so="č.p.", cislo_domovni=1903, psc=16000,
    )
    elsewhere = await cz_label(
        seeded_conn, obec_nazev="Aš", cast_obce_nazev="Aš", ulice_nazev="Karlova",
        typ_so="č.p.", cislo_domovni=248, psc=35201,
    )

    assert in_praha.endswith("16000 Praha 6")
    assert elsewhere.endswith("35201 Aš")


async def test_no_street_uses_the_part_of_municipality_as_locator(seeded_conn):
    """Vzor 5: villages have no streets, so the part-of-municipality takes the
    locator slot — and then no "č.p." prefix is written."""
    assert await cz_label(
        seeded_conn, obec_nazev="Jablonec nad Jizerou", cast_obce_nazev="Buřany",
        typ_so="č.p.", cislo_domovni=33, psc=51243,
    ) == "Buřany 33, 51243 Jablonec nad Jizerou"


async def test_no_locator_at_all_falls_back_to_the_cp_prefix(seeded_conn):
    """Vzor 6: no street, and a part-of-municipality identical to the
    municipality. With nothing to put in front of the number, the number needs
    labelling or "36, 51211 Vysoké nad Jizerou" would read as nonsense."""
    assert await cz_label(
        seeded_conn, obec_nazev="Vysoké nad Jizerou",
        cast_obce_nazev="Vysoké nad Jizerou", typ_so="č.p.",
        cislo_domovni=36, psc=51211,
    ) == "č.p. 36, 51211 Vysoké nad Jizerou"


async def test_evidence_number_is_always_labelled(seeded_conn):
    """č.ev. is never implicit: it marks a different number series, so it is
    written even when a locator is present."""
    with_locator = await cz_label(
        seeded_conn, obec_nazev="Vysoké nad Jizerou", cast_obce_nazev="Helkovice",
        typ_so="č.ev.", cislo_domovni=31, psc=51301,
    )
    without_locator = await cz_label(
        seeded_conn, obec_nazev="Vysoké nad Jizerou",
        cast_obce_nazev="Vysoké nad Jizerou", typ_so="č.ev.",
        cislo_domovni=1, psc=51211,
    )

    assert with_locator == "Helkovice č.ev. 31, 51301 Vysoké nad Jizerou"
    assert without_locator == "č.ev. 1, 51211 Vysoké nad Jizerou"


async def test_empty_street_is_treated_as_absent(seeded_conn):
    """The COPY import maps empty CSV cells to NULL, but the expression guards
    with NULLIF so an empty string cannot produce a leading space."""
    assert await cz_label(
        seeded_conn, obec_nazev="Jablonec nad Jizerou", cast_obce_nazev="Buřany",
        ulice_nazev="", typ_so="č.p.", cislo_domovni=33, psc=51243,
    ) == "Buřany 33, 51243 Jablonec nad Jizerou"


async def test_missing_postcode_leaves_no_double_space(seeded_conn):
    """RUIAN does occasionally ship an address without a PSČ; the label has to
    stay well-formed rather than gain a stray gap."""
    assert await cz_label(
        seeded_conn, obec_nazev="Aš", cast_obce_nazev="Aš", ulice_nazev="Karlova",
        typ_so="č.p.", cislo_domovni=248, cislo_orientacni=19,
    ) == "Karlova 248/19, Aš"


# --- HR -------------------------------------------------------------------

async def test_hr_label(seeded_conn):
    assert await hr_label(
        seeded_conn, ulica="Ilica", kucni_broj="100", postanski_broj=10000,
        naselje="Zagreb",
    ) == "Ilica 100, 10000 Zagreb"


async def test_hr_label_with_a_letter_suffix(seeded_conn):
    assert await hr_label(
        seeded_conn, ulica="Ilica", kucni_broj="1A", postanski_broj=10000,
        naselje="Zagreb",
    ) == "Ilica 1A, 10000 Zagreb"


async def test_hr_label_without_a_street(seeded_conn):
    """Not every DGU record carries a street; the label must not start with a
    stray space."""
    assert await hr_label(
        seeded_conn, kucni_broj="5", postanski_broj=21300, naselje="Makarska",
    ) == "5, 21300 Makarska"


async def test_fixture_labels_match_the_importer_expression(seeded_conn):
    """The CZ seed carries formatted_address as literal text, copied from a real
    import. This recomputes it from the importer's expression, so the seed
    cannot quietly describe a format production no longer produces."""
    async with seeded_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT formatted_address, {CZ_SQL} AS recomputed FROM cz_addresses"
        )
        rows = await cur.fetchall()

    assert rows
    assert [r for r in rows if r["formatted_address"] != r["recomputed"]] == []
