"""Pure-Python parts of api/queries.py — no database needed.

These cover the SQL *construction* and the row → envelope mapping. They are
deliberately thin: the interesting behaviour of /v1/search lives in what
Postgres does with the clauses built here, and that is covered by
test_search.py.
"""
import pytest

from api.queries import (
    _TOKEN_SPLIT_RE,
    _token_match,
    cz_row_to_match,
    hr_row_to_match,
    parse_cz_number,
)


def tokens(q: str) -> list[str]:
    return _TOKEN_SPLIT_RE.split(q.lower())


# --- tokenisation ---------------------------------------------------------

@pytest.mark.parametrize(
    "q, expected",
    [
        ("Vysoké nad Jizerou 367", ["vysoké", "nad", "jizerou", "367"]),
        # "/" is kept so a composite number survives as one token…
        ("Karlova 248/19", ["karlova", "248/19"]),
        # …while commas and the "č.p." dots are separators.
        ("Karlova 144/27, Praha", ["karlova", "144/27", "praha"]),
        ("č.p. 33, Tuřany", ["č", "p", "33", "tuřany"]),
    ],
)
def test_tokenisation(q, expected):
    assert [t for t in tokens(q) if t] == expected


# --- clause construction --------------------------------------------------

def test_numeric_tokens_are_matched_as_whole_words():
    """Both tiers pin a number down literally, anchored at both ends so it
    cannot slide onto a postcode, with a letter suffix still allowed."""
    m = _token_match(tokens("ilica 100"))

    assert m.params["w1"] == r"\m100[a-z]*\M"
    # The numeric condition is identical in both variants — a number is never
    # fuzzy, so there is nothing for the looser tier to relax.
    assert "%(w1)s" in m.exact and "%(w1)s" in m.fuzzy


def test_text_tokens_relax_only_in_the_fuzzy_variant():
    m = _token_match(tokens("karlova praha"))

    assert m.exact == (
        "lower(formatted_address) ~ %(w0)s AND lower(formatted_address) ~ %(w1)s"
    )
    assert m.fuzzy == (
        "lower(formatted_address) %%>> %(t0)s AND lower(formatted_address) %%>> %(t1)s"
    )
    assert m.params["w0"] == r"\mkarlova\M"
    assert m.params["t0"] == "karlova"


def test_score_is_the_mean_over_tokens():
    m = _token_match(tokens("vysoké nad jizerou 367"))

    assert m.score.endswith("/ 4")
    assert m.score.count("strict_word_similarity") == 4


def test_single_character_text_tokens_are_dropped():
    """"č" and "p" fall out of "č.p." and carry no trigram information, but a
    one-digit house number is still a real constraint and must survive.

    Placeholders are numbered by position in the query, so dropping the first
    two tokens leaves the survivors at w2/w3 — the names only have to be
    unique and to line up with the params dict."""
    m = _token_match(tokens("č.p. 7 Vysoké"))

    assert m.score.count("strict_word_similarity") == 2
    assert m.params["w2"] == r"\m7[a-z]*\M"
    assert m.params["w3"] == r"\mvysoké\M"
    assert "w0" not in m.params and "w1" not in m.params


def test_query_without_usable_tokens():
    """Nothing to build a clause from — the caller falls back to whole-query."""
    assert _token_match(tokens("a b")) is None
    assert _token_match([""]) is None


def test_tokens_cannot_carry_regex_metacharacters():
    """Tokens are interpolated into a regex *value*, so the split must not let
    metacharacters through. Anything but word characters and "/" separates."""
    for tok in tokens("a.*b (c) [d] 12$"):
        assert not (set(tok) - set("/")) & set(".*+?()[]{}^$|\\")


# --- row → response envelope ---------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2186/1b", (2186, 1, "b")),
        ("248/19", (248, 19, None)),
        ("367", (367, None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
        ("abc", (None, None, None)),
    ],
)
def test_parse_cz_number(raw, expected):
    assert parse_cz_number(raw) == expected


def cz_row(**over):
    row = {
        "kod_adm": 25510878,
        "obec_nazev": "Vysoké nad Jizerou",
        "mop_nazev": None,
        "cast_obce_nazev": "Vysoké nad Jizerou",
        "ulice_nazev": "Věnceslava Metelky",
        "typ_so": "č.p.",
        "cislo_domovni": 367,
        "cislo_orientacni": None,
        "cislo_orientacni_znak": None,
        "psc": 51211,
        "formatted_address": "Věnceslava Metelky 367, 51211 Vysoké nad Jizerou",
        "lon": 15.39923,
        "lat": 50.68743,
        "_score": 1.0,
    }
    return row | over


def test_cz_row_to_match_carries_the_search_score():
    m = cz_row_to_match(cz_row(), include_raw=False)

    assert m.registry_ref == "25510878"
    assert m.house_number == "367"
    assert m.score == 1.0
    assert m.raw is None


def test_cz_row_to_match_drops_helper_columns_from_raw():
    """lon/lat/_score are query-time artefacts, not source columns."""
    m = cz_row_to_match(cz_row(), include_raw=True)

    assert set(m.raw) & {"lon", "lat", "_score"} == set()
    assert m.raw["kod_adm"] == 25510878


def test_hr_row_to_match_stringifies_numeric_source_columns():
    """ogr2ogr types purely numeric HR columns as int; the envelope is strings."""
    m = hr_row_to_match(
        {
            "inspire_id": "HR.DGU.RPJ:KB.0022072614",
            "ulica": "Ilica",
            "kucni_broj": 100,
            "naselje": "Zagreb",
            "postanski_broj": 10000,
            "formatted_address": "Ilica 100, 10000 Zagreb",
            "lon": 15.96335,
            "lat": 45.81237,
            "_score": 1.0,
        },
        include_raw=False,
    )

    assert m.registry_ref == "HR.DGU.RPJ:KB.0022072614"
    assert m.house_number == "100"
    assert m.postal_code == "10000"
