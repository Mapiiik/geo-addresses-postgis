"""Runtime configuration loaded from environment variables."""
import os

try:
    PG_CONN_API = os.environ["PG_CONN_API"]
except KeyError as exc:
    raise SystemExit(
        "PG_CONN_API is not set. The API service needs a libpq connection "
        "string for the read-only DB role (typically constructed by compose)."
    ) from exc

# Connection pool sizing
PG_POOL_MIN = int(os.getenv("PG_POOL_MIN", "2"))
PG_POOL_MAX = int(os.getenv("PG_POOL_MAX", "10"))

# API key auth (comma-separated list). Empty = open access (dev mode).
_keys = os.getenv("API_KEYS", "").strip()
API_KEYS: set[str] = {k.strip() for k in _keys.split(",") if k.strip()}

# Default cache TTL for GET responses (seconds). Data refreshes monthly,
# so a multi-hour cache is safe even with conservative settings.
CACHE_MAX_AGE = int(os.getenv("CACHE_MAX_AGE", "3600"))
