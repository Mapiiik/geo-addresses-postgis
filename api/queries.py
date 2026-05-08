"""SQL query templates and the CZ fallback-ladder lookup logic.

All SQL uses positional/named parameter binding via psycopg — never f-string
interpolation of user input — so no SQL injection risk. The only f-string
substitutions are between trusted constants (column lists, table names,
fallback variant where-clauses).
"""
import re
from typing import Any

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
    ST_X(geometry) AS lon,
    ST_Y(geometry) AS lat
"""

HR_COLUMNS = """
    ogc_fid, ulica, kucni_broj, naselje, postanski_broj,
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


def cz_row_to_match(row: dict[str, Any], include_raw: bool) -> AddressMatch:
    raw = None
    if include_raw:
        # Drop helper columns from ST_X/ST_Y and the search _score (latter is
        # computed at query time, not a real DB column); keep the rest verbatim.
        raw = {k: v for k, v in row.items() if k not in ("lon", "lat", "_score")}
    return AddressMatch(
        registry_ref=str(row["kod_adm"]),
        source="cz",
        street=row.get("ulice_nazev"),
        house_number=_cz_house_number_str(row),
        city=row.get("obec_nazev"),
        postal_code=str(row["psc"]) if row.get("psc") is not None else None,
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
        registry_ref=str(row["ogc_fid"]),
        source="hr",
        street=_str_or_none(row.get("ulica")),
        house_number=_str_or_none(row.get("kucni_broj")),
        city=_str_or_none(row.get("naselje")),
        postal_code=_str_or_none(row.get("postanski_broj")),
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


async def hr_by_id(conn: AsyncConnection, ogc_fid: int) -> dict | None:
    sql = f"SELECT {HR_COLUMNS} FROM hr_addresses WHERE ogc_fid = %s"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (ogc_fid,))
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


async def hr_by_ids(conn: AsyncConnection, ogc_fids: list[int]) -> list[dict]:
    if not ogc_fids:
        return []
    sql = f"SELECT {HR_COLUMNS} FROM hr_addresses WHERE ogc_fid = ANY(%s)"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, (ogc_fids,))
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


async def _trgm_search(
    conn: AsyncConnection, table: str, columns: str, q: str, limit: int
) -> list[dict]:
    """Fuzzy autocomplete via pg_trgm against the precomputed `search_label`.

    Uses the `<%` (word similarity) operator + GIN trigram index built by
    the importer. Word similarity finds the best-matching extent of the
    label, which is exactly the autocomplete behaviour users expect:
    typing "Stjepana" matches "stjepana ivičevića 1, makarska 21300"
    even though only a small portion of the label aligns with the query.

    Tokens can appear in any order; minor typos are tolerated; the result
    is ranked by `word_similarity` score so the best hit comes first.
    """
    # search_label is stored lowercased; lowercase the query to match.
    q_lower = q.lower().strip()

    sql = f"""
        SELECT {columns},
               word_similarity(%(q)s, search_label) AS _score,
               search_label
        FROM {table}
        WHERE %(q)s <%% search_label
        ORDER BY _score DESC, search_label
        LIMIT %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        # SET LOCAL keeps the threshold change scoped to this transaction so
        # other code paths reusing the pooled connection are unaffected.
        # 0.3 is permissive enough for partial words and small typos.
        await cur.execute("SET LOCAL pg_trgm.word_similarity_threshold = 0.3")
        await cur.execute(sql, {"q": q_lower, "limit": limit})
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
