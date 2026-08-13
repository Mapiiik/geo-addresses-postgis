"""SQL query templates and the CZ fallback-ladder lookup logic.

All SQL uses positional/named parameter binding via psycopg — never f-string
interpolation of user input — so no SQL injection risk. The only f-string
substitutions are between trusted constants (column lists, table names,
fallback variant where-clauses).
"""
import re
from typing import Any, NamedTuple

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from api.models import AddressMatch, Geometry

# ---------------------------------------------------------------------------
# Column projections
# ---------------------------------------------------------------------------

CZ_COLUMNS = """
    kod_adm, obec_kod, obec_nazev, momc_kod, momc_nazev,
    mop_kod, mop_nazev, cast_obce_kod, cast_obce_nazev,
    ulice_kod, ulice_nazev, typ_so,
    cislo_domovni, cislo_orientacni, cislo_orientacni_znak,
    psc, plati_od,
    formatted_address,
    ST_X(geometry) AS lon,
    ST_Y(geometry) AS lat
"""

HR_COLUMNS = """
    inspire_id, ogc_fid, gml_id, zgrada_id,
    kucni_broj, broj, podbroja_alfa, podbroj_num, rotacija,
    ulica, ulica_id, ulica_redni_broj,
    naselje, naselje_id,
    postanski_ured, postanski_ured_id, postanski_broj,
    katastarska_opcina, katastarska_opcina_id,
    broj_cestice, ostale_vezane_cestice,
    formatted_address,
    ST_X(geometry) AS lon,
    ST_Y(geometry) AS lat
"""

# ---------------------------------------------------------------------------
# CZ fallback ladder
# ---------------------------------------------------------------------------

# Five variants for CZ lookup, mirroring the original consumer-side fallback
# logic. Each entry is (label, where-clause-fragment). Step 0 = strict; the
# higher the step, the looser the match.
CZ_VARIANTS: list[tuple[str, str]] = [
    (
        "strict (street + obec)",
        """
        ulice_nazev IS NOT DISTINCT FROM %(street)s
        AND obec_nazev IS NOT DISTINCT FROM %(city)s
        """,
    ),
    (
        "city as MOP",
        """
        ulice_nazev IS NOT DISTINCT FROM %(street)s
        AND mop_nazev  IS NOT DISTINCT FROM %(city)s
        """,
    ),
    (
        "city as MOMC",
        """
        ulice_nazev IS NOT DISTINCT FROM %(street)s
        AND momc_nazev IS NOT DISTINCT FROM %(city)s
        """,
    ),
    (
        "city as cast_obce",
        """
        ulice_nazev IS NOT DISTINCT FROM %(street)s
        AND cast_obce_nazev IS NOT DISTINCT FROM %(city)s
        """,
    ),
    (
        "street as cast_obce (no street name)",
        # The COPY import maps empty CSV cells to NULL; defensive OR keeps
        # this step working even if data ever lands as ''.
        """
        (ulice_nazev IS NULL OR ulice_nazev = '')
        AND cast_obce_nazev IS NOT DISTINCT FROM %(street)s
        AND obec_nazev IS NOT DISTINCT FROM %(city)s
        """,
    ),
]

CZ_BASE_FILTER = """
    typ_so = %(typ_so)s
    AND cislo_domovni IS NOT DISTINCT FROM %(cislo_domovni)s
    AND cislo_orientacni IS NOT DISTINCT FROM %(cislo_orientacni)s
    AND cislo_orientacni_znak IS NOT DISTINCT FROM %(cislo_orientacni_znak)s
    AND psc IS NOT DISTINCT FROM %(psc)s
"""

# ---------------------------------------------------------------------------
# CZ number parser ("2186/1b" → (2186, 1, "b"))
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"^(?P<house>\d+)(?:\s*/\s*(?P<orient>\d+)(?P<letter>[a-zA-Z]*))?"
)


def parse_cz_number(raw: str | None) -> tuple[int | None, int | None, str | None]:
    """Parse a CZ composite number. Returns (house, orientation, letter)."""
    if not raw:
        return None, None, None
    m = _NUMBER_RE.match(raw.strip())
    if not m:
        return None, None, None
    house = int(m["house"]) if m["house"] else None
    orient = int(m["orient"]) if m["orient"] else None
    letter = m["letter"] or None
    return house, orient, letter


# ---------------------------------------------------------------------------
# Row → AddressMatch conversion
# ---------------------------------------------------------------------------

def _cz_house_number_str(row: dict[str, Any]) -> str | None:
    """Reconstruct the human-readable composite number from CZ columns."""
    if row.get("cislo_domovni") is None:
        return None
    parts = [str(row["cislo_domovni"])]
    if row.get("cislo_orientacni") is not None:
        parts.append("/" + str(row["cislo_orientacni"]))
        if row.get("cislo_orientacni_znak"):
            parts.append(row["cislo_orientacni_znak"])
    return "".join(parts)


def _cz_number_type(typ_so: str | None) -> str | None:
    """Map RUIAN typ_so column to the API's number_type enum value."""
    if typ_so == "č.ev.":
        return "registration"
    if typ_so == "č.p.":
        return "house"
    return None


def cz_row_to_match(row: dict[str, Any], include_raw: bool) -> AddressMatch:
    raw = None
    if include_raw:
        # Drop helper columns from ST_X/ST_Y and the search _score (latter is
        # computed at query time, not a real DB column); keep the rest verbatim.
        raw = {k: v for k, v in row.items() if k not in ("lon", "lat", "_score")}

    # Line-1 locator per vyhláška 359/2011 Sb.: ulice_nazev when present,
    # otherwise cast_obce_nazev for village addresses without streets (vzor
    # 5). Mirrors the formatted_address composition. Stays null only for
    # vzor 6 ("č.p. <num>, <obec>") where neither exists.
    street = row.get("ulice_nazev")
    if not street:
        cast_obce = row.get("cast_obce_nazev")
        if cast_obce and cast_obce != row.get("obec_nazev"):
            street = cast_obce

    # Postal city per vyhláška § 6 (2) c): for Praha the district number is
    # appended ("Praha 6") via mop_nazev; for other obce mop_nazev is NULL
    # and obec_nazev is the city. Single COALESCE covers both.
    city = row.get("mop_nazev") or row.get("obec_nazev")

    return AddressMatch(
        registry_ref=str(row["kod_adm"]),
        source="cz",
        street=street,
        house_number=_cz_house_number_str(row),
        number_type=_cz_number_type(row.get("typ_so")),
        city=city,
        postal_code=str(row["psc"]) if row.get("psc") is not None else None,
        formatted_address=row.get("formatted_address"),
        geometry=Geometry(coordinates=(row["lon"], row["lat"])),
        distance_m=row.get("distance_m"),
        score=row.get("_score"),
        raw=raw,
    )


def _str_or_none(v: Any) -> str | None:
    """Coerce DB values to str for the response envelope. ogr2ogr's WFS import
    autodetects column types, so HR fields that look numeric (postanski_broj,
    kucni_broj for purely numeric numbers) come back as int. The API contract
    is uniformly string for these fields."""
    return None if v is None else str(v)


def hr_row_to_match(row: dict[str, Any], include_raw: bool) -> AddressMatch:
    raw = None
    if include_raw:
        raw = {k: v for k, v in row.items() if k not in ("lon", "lat", "_score")}
    return AddressMatch(
        # Use inspire_id (e.g. "HR.DGU.RPJ:KB.0000021409") rather than ogc_fid
        # — ogc_fid is reassigned by ogr2ogr on every import, inspire_id is
        # the stable DGU identifier anchored in the source data.
        registry_ref=row["inspire_id"],
        source="hr",
        street=_str_or_none(row.get("ulica")),
        house_number=_str_or_none(row.get("kucni_broj")),
        # DGU does not distinguish house vs. registration numbers like CZ
        # does (č.p. / č.ev.). Default to "house" since semantically that's
        # what kucni_broj is — keeps the field non-null for clients that
        # don't want to special-case sources without the distinction.
        number_type="house",
        city=_str_or_none(row.get("naselje")),
        postal_code=_str_or_none(row.get("postanski_broj")),
        formatted_address=_str_or_none(row.get("formatted_address")),
        geometry=Geometry(coordinates=(row["lon"], row["lat"])),
        distance_m=row.get("distance_m"),
        score=row.get("_score"),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

async def cz_lookup(conn: AsyncConnection, params: dict[str, Any]) -> tuple[list[dict], int | None]:
    """Run the CZ fallback ladder. Returns (rows, step_used). step_used is
    0..4 for the variant that produced results, or None if no variant matched."""
    async with conn.cursor(row_factory=dict_row) as cur:
        for step, (_label, where_extra) in enumerate(CZ_VARIANTS):
            sql = (
                f"SELECT {CZ_COLUMNS} FROM cz_addresses "
                f"WHERE {CZ_BASE_FILTER} AND ({where_extra})"
            )
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            if rows:
                return rows, step
    return [], None


async def hr_lookup(conn: AsyncConnection, params: dict[str, Any]) -> list[dict]:
    sql = f"""
        SELECT {HR_COLUMNS} FROM hr_addresses
        WHERE ulica          IS NOT DISTINCT FROM %(street)s
          AND kucni_broj     IS NOT DISTINCT FROM %(number)s
          AND naselje        IS NOT DISTINCT FROM %(city)s
          AND postanski_broj IS NOT DISTINCT FROM %(postal_code)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


# ---------------------------------------------------------------------------
# By-id, reverse, search
# ---------------------------------------------------------------------------

async def cz_by_id(conn: AsyncConnection, kod_adm: int) -> dict | None:
    sql = f"SELECT {CZ_COLUMNS} FROM cz_addresses WHERE kod_adm = %s"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (kod_adm,))
        return await cur.fetchone()


async def hr_by_id(conn: AsyncConnection, inspire_id: str) -> dict | None:
    sql = f"SELECT {HR_COLUMNS} FROM hr_addresses WHERE inspire_id = %s"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (inspire_id,))
        return await cur.fetchone()


async def cz_by_ids(conn: AsyncConnection, kod_adms: list[int]) -> list[dict]:
    """Bulk by-id lookup for CZ. psycopg adapts the Python list to a Postgres
    array, so `= ANY(%s)` performs a single index lookup over the set."""
    if not kod_adms:
        return []
    sql = f"SELECT {CZ_COLUMNS} FROM cz_addresses WHERE kod_adm = ANY(%s)"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (kod_adms,))
        return await cur.fetchall()


async def hr_by_ids(conn: AsyncConnection, inspire_ids: list[str]) -> list[dict]:
    if not inspire_ids:
        return []
    sql = f"SELECT {HR_COLUMNS} FROM hr_addresses WHERE inspire_id = ANY(%s)"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (inspire_ids,))
        return await cur.fetchall()


async def cz_reverse(
    conn: AsyncConnection, lon: float, lat: float, radius_m: float, limit: int
) -> list[dict]:
    sql = f"""
        SELECT {CZ_COLUMNS},
               ST_Distance(
                   geometry::geography,
                   ST_MakePoint(%(lon)s, %(lat)s)::geography
               ) AS distance_m
        FROM cz_addresses
        WHERE ST_DWithin(
                  geometry::geography,
                  ST_MakePoint(%(lon)s, %(lat)s)::geography,
                  %(radius_m)s
              )
        ORDER BY distance_m
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, {"lon": lon, "lat": lat, "radius_m": radius_m, "limit": limit})
        return await cur.fetchall()


async def hr_reverse(
    conn: AsyncConnection, lon: float, lat: float, radius_m: float, limit: int
) -> list[dict]:
    sql = f"""
        SELECT {HR_COLUMNS},
               ST_Distance(
                   geometry::geography,
                   ST_MakePoint(%(lon)s, %(lat)s)::geography
               ) AS distance_m
        FROM hr_addresses
        WHERE ST_DWithin(
                  geometry::geography,
                  ST_MakePoint(%(lon)s, %(lat)s)::geography,
                  %(radius_m)s
              )
        ORDER BY distance_m
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, {"lon": lon, "lat": lat, "radius_m": radius_m, "limit": limit})
        return await cur.fetchall()


# Split a query into search tokens. Everything that is not a letter, digit or
# `/` separates tokens — `/` is kept so a CZ composite number ("248/19") stays
# one token. Because the split only ever yields word characters and slashes,
# tokens are safe to embed in a regex without escaping.
_TOKEN_SPLIT_RE = re.compile(r"[^\w/]+", re.UNICODE)
_HAS_DIGIT_RE = re.compile(r"\d")

# Ranking uses the average per-token word similarity, so a token that is
# absent from the label drags the score down. Ordering is stabilised by
# preferring the shortest label among equal scores — the shortest label that
# still contains every token is the least padded with unrelated words.
_TIE_BREAK = "length(formatted_address), formatted_address"
_ORDER_BY = f"_score DESC, {_TIE_BREAK}"


class _TokenMatch(NamedTuple):
    """The two WHERE variants and the scoring expression for one query."""

    exact: str
    fuzzy: str
    score: str
    params: dict[str, Any]


def _token_match(tokens: list[str]) -> _TokenMatch | None:
    """Build the per-token WHERE clauses and scoring expression.

    Returns None when no token is usable, in which case the caller falls back
    to the whole-query search.

    Each token contributes one AND-ed condition, so *every* token has to be
    present in the label — this is what makes "Vysoké nad Jizerou 367" find
    the house rather than the village. Two variants are produced:

    `exact` requires every token verbatim as a whole word, `fuzzy` allows
    typos in the non-numeric ones. Per token:

      * tokens containing a digit are matched literally in both variants, as
        the whole word `~ '\\m<tok>[a-z]*\\M'`. House numbers are identifiers,
        not prose — 367 is not "almost" 368 — and trigram similarity between
        short numbers is meaningless. Anchoring both ends is what keeps "100"
        off the postcode in "Ilica 45/1, 10000 Zagreb"; the trailing `[a-z]*`
        still admits a letter suffix ("5" finds "5a"). Since `/` is not a word
        character, "248" and "19" each still match the halves of "248/19".
      * other tokens are matched with `%>>`, the *strict* word-similarity
        operator, which still tolerates typos, missing diacritics and partial
        words but aligns its comparison to whole words. Plain `%>` compares
        against any extent of the label, word boundaries included, which is far
        too loose for street names: it rates "karlova" against "křesomyslova"
        at 0.375 — above threshold — purely on the shared "…slova" tail.
      * single-character non-numeric tokens are dropped entirely: they carry
        no trigram information and only come from noise like the "č.p." prefix.

    All conditions run against `lower(formatted_address)`, so the functional
    GIN trigram index serves them as a single bitmap AND.
    """
    exact: list[str] = []
    fuzzy: list[str] = []
    score: list[str] = []
    params: dict[str, Any] = {}

    for i, tok in enumerate(tokens):
        key, word = f"t{i}", f"w{i}"
        literal = f"lower(formatted_address) ~ %({word})s"
        if _HAS_DIGIT_RE.search(tok):
            params[word] = rf"\m{tok}[a-z]*\M"
            exact.append(literal)
            fuzzy.append(literal)
        elif len(tok) >= 2:
            params[word] = rf"\m{tok}\M"
            exact.append(literal)
            fuzzy.append(f"lower(formatted_address) %%>> %({key})s")
        else:
            continue
        params[key] = tok
        score.append(f"strict_word_similarity(%({key})s, lower(formatted_address))")

    if not fuzzy:
        return None
    return _TokenMatch(
        exact=" AND ".join(exact),
        fuzzy=" AND ".join(fuzzy),
        score=f"({' + '.join(score)}) / {len(score)}",
        params=params,
    )


async def _trgm_search(
    conn: AsyncConnection, table: str, columns: str, q: str, limit: int
) -> list[dict]:
    """Fuzzy autocomplete via pg_trgm against the precomputed
    `formatted_address`, matched token by token.

    Every query token must be present in the label, and the score is the mean
    per-token similarity — so tokens the label does not cover cost it rank.
    Scoring the query as one string does not work here: `word_similarity`
    returns the best-matching *extent* of the label, so for "Vysoké nad Jizerou
    367" the extent "vysoké nad jizerou" alone already scores 0.83 and every
    address in the village ties at that value, leaving the house number with no
    influence at all on the result order.

    The search runs in three tiers, stopping at the first that can answer:

    1. every token verbatim as a whole word. Such a label scores 1.0 on every
       token, so once this tier fills the page there is nothing a looser match
       could add — and it is the tier that keeps broad queries cheap, since it
       never evaluates a similarity function ("Praha 1" matches 161k labels:
       264 ms here against 1.3 s of scoring in tier 2).
    2. the same tokens, fuzzy on the non-numeric ones, ranked by score. This is
       what absorbs typos, missing diacritics and half-typed words.
    3. no label covers every token — a mistyped number, a word that is not part
       of the address — so the whole query is matched loosely with `<%` and the
       endpoint answers with its closest guesses rather than nothing.
    """
    # formatted_address is stored proper-case; lowercase both sides for the
    # case-insensitive trigram match. The functional GIN index on
    # lower(formatted_address) is what the planner uses for the lookups.
    q_lower = q.lower().strip()
    match = _token_match(_TOKEN_SPLIT_RE.split(q_lower))

    async with conn.cursor(row_factory=dict_row) as cur:
        # SET LOCAL keeps the threshold changes scoped to this transaction so
        # other code paths reusing the pooled connection are unaffected. 0.3 is
        # permissive enough for partial words and small typos in both tiers —
        # the strict threshold drives the per-token pass, the plain one the
        # whole-query fallback below.
        await cur.execute(
            "SET LOCAL pg_trgm.strict_word_similarity_threshold = 0.3"
        )
        await cur.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.3")

        if match is not None:
            await cur.execute(
                f"""
                SELECT {columns}, 1.0::float8 AS _score
                FROM {table}
                WHERE {match.exact}
                ORDER BY {_TIE_BREAK}
                LIMIT %(limit)s
                """,
                match.params | {"limit": limit},
            )
            rows = await cur.fetchall()
            # A short page of exact hits is still worth re-running as tier 2:
            # tier 1 is a subset of it, so nothing is lost and the remaining
            # slots get filled with near misses.
            if len(rows) >= limit:
                return rows

            await cur.execute(
                f"""
                SELECT {columns}, {match.score} AS _score
                FROM {table}
                WHERE {match.fuzzy}
                ORDER BY {_ORDER_BY}
                LIMIT %(limit)s
                """,
                match.params | {"limit": limit},
            )
            rows = await cur.fetchall()
            if rows:
                return rows

        await cur.execute(
            f"""
            SELECT {columns},
                   word_similarity(%(q)s, lower(formatted_address)) AS _score
            FROM {table}
            WHERE %(q)s <%% lower(formatted_address)
            ORDER BY {_ORDER_BY}
            LIMIT %(limit)s
            """,
            {"q": q_lower, "limit": limit},
        )
        return await cur.fetchall()


async def cz_search(conn: AsyncConnection, q: str, limit: int) -> list[dict]:
    """Fuzzy autocomplete on `cz_addresses.search_label` (vyhláška-formatted)."""
    return await _trgm_search(conn, "cz_addresses", CZ_COLUMNS, q, limit)


async def hr_search(conn: AsyncConnection, q: str, limit: int) -> list[dict]:
    """Fuzzy autocomplete on `hr_addresses.search_label`."""
    return await _trgm_search(conn, "hr_addresses", HR_COLUMNS, q, limit)


# ---------------------------------------------------------------------------
# Meta / health
# ---------------------------------------------------------------------------

async def dataset_meta(conn: AsyncConnection, table: str) -> dict:
    """Return row count + last ANALYZE timestamp (proxy for last refresh)."""
    async with conn.cursor() as cur:
        # Row count via pg_class.reltuples is approximate but instant; for a
        # small repo where the importer runs ANALYZE at the end of each load,
        # it is accurate to within a few rows. Avoids a full table scan.
        await cur.execute(
            "SELECT reltuples::bigint, "
            "       pg_stat_get_last_analyze_time(oid) "
            "FROM pg_class WHERE relname = %s AND relkind = 'r'",
            (table,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"row_count": 0, "last_analyzed": None}
        return {"row_count": int(row[0]), "last_analyzed": row[1]}


async def db_ping(conn: AsyncConnection) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1")
        return (await cur.fetchone())[0] == 1
