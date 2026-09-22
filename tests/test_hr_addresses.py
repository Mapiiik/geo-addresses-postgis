"""Reading the addresses out of the INSPIRE download.

An address in the package is four files deep: the house itself, and the
street, the post office and the administrative units it points at. These
tests hold the reading to the source's own shape, taken from the real
download - the two houses below are genuine, down to the coordinates.
"""
from pathlib import Path

from importer.import_hr_addresses import (
    COLUMNS,
    MEMBERS,
    read_addresses,
    read_post_offices,
    read_settlements,
    read_streets,
)

FIXTURES = Path(__file__).parent / "fixtures" / "addresses"


def lookups():
    with (FIXTURES / MEMBERS["streets"]).open("rb") as gml:
        streets = read_streets(gml)
    with (FIXTURES / MEMBERS["units"]).open("rb") as gml:
        settlements = read_settlements(gml)
    with (FIXTURES / MEMBERS["post_offices"]).open("rb") as gml:
        post_offices = read_post_offices(gml)

    return streets, settlements, post_offices


def read(**without):
    """The addresses as rows, resolved against the package's other three files.

    A lookup passed here replaces the one the fixtures hold, which is how the
    tests ask what becomes of an address pointing at something absent.
    """
    streets, settlements, post_offices = lookups()
    found = {"streets": streets, "settlements": settlements, "post_offices": post_offices}
    found.update(without)

    with (FIXTURES / MEMBERS["addresses"]).open("rb") as gml:
        return list(read_addresses(
            gml, found["streets"], found["settlements"], found["post_offices"]
        ))


def row_of(rows, reference):
    return dict(zip(COLUMNS, next(row for row in rows if row[1] == reference)))


def test_an_address_carries_everything_the_table_holds():
    """What the four files say about one house, put back together as the one
    row the table keeps it in."""
    assert row_of(read(), "HR.DGU.RPJ:KB.0000000135") == {
        "gml_id": "Address.2010882620",
        "inspire_id": "HR.DGU.RPJ:KB.0000000135",
        "kucni_broj": "33",
        "broj": 33,
        "podbroja_alfa": None,
        "podbroj_num": None,
        "ulica": "Brežna ulica",
        "ulica_id": 2024021,
        "ulica_redni_broj": 4,
        "naselje": "Andraševec",
        "naselje_id": 132,
        "postanski_ured": "Oroslavje",
        "postanski_ured_id": 2108936947,
        "postanski_broj": 49243,
        "geometry_laea": "SRID=3035;POINT(4781765.61686003 2561799.19395499)",
    }


def test_the_house_number_is_written_the_way_the_register_writes_it():
    """INSPIRE keeps the number, the letter and the sub-number apart, and the
    register's own label puts them back together as "105A/1"."""
    address = row_of(read(), "HR.DGU.RPJ:KB.0000334806")

    assert (address["kucni_broj"], address["broj"], address["podbroja_alfa"], address["podbroj_num"]) \
        == ("105A/1", 105, "A", 1)


def test_the_point_comes_out_easting_first():
    """The package is published in ETRS89/LAEA, whose axes run northing first,
    and PostGIS reads a point the other way round. Croatia lies east and south
    of that projection's origin, so the easting is the larger of the two."""
    address = row_of(read(), "HR.DGU.RPJ:KB.0000334806")

    assert address["geometry_laea"] == "SRID=3035;POINT(4747668.31363779 2396746.36464498)"


def test_a_street_is_numbered_within_its_settlement():
    """The identifier the register knows a street by is the settlement's code
    followed by the street's own number in it, which is the only place the
    number comes from."""
    streets, _, _ = lookups()

    assert streets["ThoroughfareName.2156774419"] == ("Brežna ulica", 2024021, 4)


def test_only_the_settlement_is_read_from_the_administrative_units():
    """An address is given two units, the settlement and the country, and only
    the settlement says anything it is filed under."""
    _, settlements, _ = lookups()

    assert set(settlements.values()) == {("Andraševec", 132), ("Žirje", 74276), ("Gospić", 21474)}


def test_an_address_survives_a_component_the_package_left_out():
    """A house whose street is not in the package is still an address, and is
    read with the street left empty rather than dropped."""
    address = row_of(read(streets={}), "HR.DGU.RPJ:KB.0000000135")

    assert (address["ulica"], address["ulica_id"], address["ulica_redni_broj"]) == (None, None, None)
    assert address["naselje"] == "Andraševec"


def test_an_identifier_the_package_repeats_is_read_twice():
    """The source hands the same identifier out twice here and there. Reading
    keeps both - which one to drop is settled in the database, where the
    unique index is."""
    rows = read()

    assert [row[0] for row in rows if row[1] == "HR.DGU.RPJ:KB.0014001190"] \
        == ["Address.2196941295", "Address.2196941297"]
