"""Ranking behaviour of /v1/search, against a seeded PostGIS.

These are the regressions that motivated the token-based matching. The one at
the top is the original report: searching a municipality plus a house number
returned everything *except* that house, because scoring the query as a single
string let the municipality name alone decide the score — every address in the
village tied, and the alphabetical tie-break picked the winner.
"""
import pytest

from api import queries
from tests.conftest import labels, requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

TARGET = "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou"


@pytest.mark.parametrize(
    "q",
    [
        "Vysoké nad Jizerou 367",
        "367 Vysoké nad Jizerou",
        "Jizerou 367 Vysoké nad",
    ],
    ids=["number-last", "number-first", "shuffled"],
)
async def test_house_number_decides_the_hit(seeded_conn, q):
    """The reported bug: the number has to drive the result, in any word order."""
    rows = await queries.cz_search(seeded_conn, q, limit=10)

    assert labels(rows) == [TARGET]
    assert rows[0]["_score"] == pytest.approx(1.0)


async def test_village_neighbours_do_not_outrank_the_number(seeded_conn):
    """Every label in the village shares the municipality name; only one shares
    the number. Before the fix all of them tied and "Bozkovská" sorted first."""
    rows = await queries.cz_search(seeded_conn, "Vysoké nad Jizerou 367", limit=10)

    assert not [lbl for lbl in labels(rows) if "Bozkovská" in lbl]


@pytest.mark.parametrize(
    "q, expected",
    [
        ("Vysoké nad Jizerou 36", "č.p. 36, 51211 Vysoké nad Jizerou"),
        ("Vysoké nad Jizerou 363", "č.p. 363, 51211 Vysoké nad Jizerou"),
        ("Vysoké nad Jizerou 367", TARGET),
    ],
)
async def test_numbers_are_matched_exactly_not_fuzzily(seeded_conn, q, expected):
    """367 is not "almost" 368: a house number is an identifier, so trigram
    similarity between numbers must never enter the ranking. 36 and 367 live in
    the same village and must not reach each other."""
    rows = await queries.cz_search(seeded_conn, q, limit=10)

    assert labels(rows) == [expected]


async def test_number_does_not_match_a_longer_number(seeded_conn):
    """"248" must find "Karlova 248/19" but not "Karlova 2480" next door."""
    rows = await queries.cz_search(seeded_conn, "Karlova 248 Aš", limit=10)

    assert labels(rows) == ["Karlova 248/19, 35201 Aš"]


@pytest.mark.parametrize("q", ["Karlova 248/19 Aš", "Karlova 19 Aš"])
async def test_both_halves_of_a_composite_number_are_findable(seeded_conn, q):
    """"248/19" is one token, but "/" is not a word character, so the house and
    orientation halves each stand on their own as search terms."""
    rows = await queries.cz_search(seeded_conn, q, limit=10)

    assert labels(rows) == ["Karlova 248/19, 35201 Aš"]


async def test_street_similarity_is_word_aligned(seeded_conn):
    """"Karlova" must not drag in "Křesomyslova". Plain `word_similarity` scores
    that pair at 0.375 — over threshold — purely on the shared "…slova" tail,
    because its extents ignore word boundaries."""
    rows = await queries.cz_search(seeded_conn, "Karlova Praha", limit=10)

    assert labels(rows) == ["Karlova 144/27, Staré Město, 11000 Praha 1"]


async def test_part_of_municipality_is_searchable(seeded_conn):
    """Village addresses have no street; the locator is the part-of-municipality
    and it has to work as a search term (the README's own example)."""
    rows = await queries.cz_search(seeded_conn, "Buřany 33", limit=10)

    assert labels(rows) == ["Buřany 33, 51243 Jablonec nad Jizerou"]


@pytest.mark.parametrize(
    "q",
    ["Vysoke nad Jizerou 367", "Vysoke nad Jyzerou 367", "Vysoké nad Jizer 367"],
    ids=["no-diacritics", "typo", "half-typed"],
)
async def test_names_stay_fuzzy(seeded_conn, q):
    """Numbers are exact, but names must still absorb missing diacritics, typos
    and half-typed words — that is the autocomplete half of the contract."""
    rows = await queries.cz_search(seeded_conn, q, limit=10)

    assert labels(rows)[0] == TARGET
    assert rows[0]["_score"] < 1.0, "a fuzzy match should not score as exact"


async def test_every_token_must_be_covered(seeded_conn):
    """A token no address satisfies must not be silently ignored: "Bozkovská"
    and "367" never co-occur, so nothing may come back as a full-score match."""
    rows = await queries.cz_search(seeded_conn, "Bozkovská 367 Vysoké", limit=10)

    assert all(r["_score"] < 1.0 for r in rows)


async def test_falls_back_instead_of_returning_nothing(seeded_conn):
    """A mistyped number leaves no label covering every token. Rather than an
    empty page, the whole query is matched loosely for closest guesses."""
    rows = await queries.cz_search(seeded_conn, "Vysoké nad Jizerou 99999", limit=10)

    assert rows, "expected the fallback tier to answer"
    # The looser tier drops the unsatisfiable number and ranks on what is left,
    # so the municipality still leads — but it no longer claims a full score.
    assert "Vysoké nad Jizerou" in labels(rows)[0]
    assert all(r["_score"] < 1.0 for r in rows)


async def test_limit_is_honoured(seeded_conn):
    rows = await queries.cz_search(seeded_conn, "Vysoké nad Jizerou", limit=3)

    assert len(rows) == 3


# --- HR -------------------------------------------------------------------

async def test_number_does_not_match_the_postcode(seeded_conn):
    """Every Zagreb label carries the postcode 10000, so a house number anchored
    only at its start matches "100" against it and buries the real hit."""
    rows = await queries.hr_search(seeded_conn, "Ilica 100 Zagreb", limit=10)

    assert labels(rows)[0] == "Ilica 100, 10000 Zagreb"
    assert "Ilica 45/1, 10000 Zagreb" not in labels(rows)


async def test_letter_suffixed_house_numbers(seeded_conn):
    """Anchoring the end of the number must still admit a letter suffix, or
    "Ilica 1" would never reach "Ilica 1A" — but only as a weaker match than a
    house actually numbered 1.

    "Ilica 45/1" is a full-strength hit here, and legitimately so: "/" is not a
    word character, so the "1" in "45/1" is a whole word — the same rule that
    lets "19" find "248/19"."""
    rows = await queries.hr_search(seeded_conn, "Ilica 1 Zagreb", limit=10)
    by_label = {r["formatted_address"]: r["_score"] for r in rows}

    assert labels(rows)[0] == "Ilica 1, 10000 Zagreb"
    assert by_label["Ilica 1, 10000 Zagreb"] == pytest.approx(1.0)
    assert by_label["Ilica 45/1, 10000 Zagreb"] == pytest.approx(1.0)
    assert by_label["Ilica 1A, 10000 Zagreb"] < 1.0


async def test_common_word_near_match_does_not_win(seeded_conn):
    """"ulica" is Croatian for "street" and is in a large share of all labels, so
    it is the obvious fuzzy near-match for "Ilica" — and must lose to it."""
    rows = await queries.hr_search(seeded_conn, "Ilica 100 Zagreb", limit=10)

    assert labels(rows)[0] == "Ilica 100, 10000 Zagreb"
    assert rows[0]["_score"] > max(r["_score"] for r in rows[1:])


async def test_diacritics_heavy_street(seeded_conn):
    rows = await queries.hr_search(seeded_conn, "Stjepana Ivičevića 7 Makarska", limit=10)

    assert labels(rows) == ["Stjepana Ivičevića 7, 21300 Makarska"]
    assert rows[0]["_score"] == pytest.approx(1.0)
