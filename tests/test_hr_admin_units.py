"""Reading the administrative units out of the INSPIRE download.

The chain a settlement hangs on - settlement, municipality, county - is what
the Croatian regulator's address returns are filed by, and it is the only
thing this import takes out of a 600 MB file of boundaries. These tests hold
the reading to the source's own shape, taken from the real download.
"""
from pathlib import Path

from importer.import_hr_admin_units import read_units, settlements

FIXTURE = Path(__file__).parent / "fixtures" / "administrative_units.gml"


def read():
    with FIXTURE.open("rb") as gml:
        return read_units(gml)


def test_a_settlement_carries_the_municipality_and_the_county():
    """The national code of a settlement is what the addresses carry as
    naselje_id, which is what makes the two joinable at all."""
    rows = {row[0]: row for row in settlements(read())}

    assert rows[37591] == (
        37591,
        "Makarska",
        "02496",
        "Makarska",
        "17",
        "Splitsko-dalmatinska županija",
    )


def test_a_settlement_whose_municipality_is_missing_is_still_read():
    """One unit of a chain absent from the file leaves the rest of it standing,
    so an address in that settlement still resolves to its name."""
    rows = {row[0]: row for row in settlements(read())}

    assert rows[19] == (19, "Ada", None, None, None, None)


def test_only_the_settlements_become_rows():
    """The municipalities and the counties are read for the chain's sake, not
    to be stored: a row per settlement is what the addresses join against."""
    units = read()

    assert len(units) == 4
    assert len(settlements(units)) == 2
