# geo-addresses-postgis

PostGIS-based address database for the Czech Republic and Croatia, kept up to
date by automated monthly imports from the official open-data sources:

- **CZ** — RUIAN address dump from [ČÚZK](https://vdp.cuzk.cz/) (CSV, monthly)
- **HR** — INSPIRE Address WFS from [DGU geoportal](https://geoportal.dgu.hr/)

The database is intended as a backend for downstream applications (e.g. CRM
systems, geocoding, address validation), either via direct DB connection or
via the bundled REST API service.

## Features

- **Zero-downtime updates** — every import goes into a `_new` working table
  and is swapped in atomically; live tables stay queryable for the entire
  duration of the import.
- **Scheduled** — a small Python daemon wakes up once a month and refreshes
  both data sources. No external cron required.
- **Parallel COPY** — the CZ importer streams per-region CSVs into a staging
  table in parallel (one process per CSV file).
- **Both projections** — every address has both the **native projection**
  geometry (S-JTSK / EPSG:5514 for CZ, HTRS96/TM / EPSG:3765 for HR) and a
  **WGS84 / EPSG:4326** geometry for general use, with GIST indexes on both.
- **REST API** — FastAPI service with structured lookup (incl. CZ fallback
  ladder), by-id, reverse geocoding, fuzzy autocomplete, and dataset metadata.
  Auto-generated OpenAPI / Swagger docs at `/docs`.
- **Fuzzy search** — every address has a precomputed `search_label`
  (composed per Czech vyhláška 359/2011 Sb. for CZ; "ulica kucni_broj,
  postanski_broj naselje" for HR), indexed with a `pg_trgm` GIN index.
  Tolerates typos, partial words, and out-of-order tokens.
- **Automatic HTTPS** — bundled Caddy reverse proxy auto-fetches a
  Let's Encrypt certificate when `DOMAIN` is a public hostname, or
  uses an internal CA for local dev. No manual cert management.
- **Self-contained Docker stack** — `compose.production.yaml` spins up
  PostGIS + the importer + the API + Caddy with one command.

## Architecture

```
                       ┌──────────────────────────┐
   HTTPS clients ──────►   caddy                   │  ports 80 / 443
                       │   automatic Let's Encrypt │  cert state in
                       │   (or internal-CA for     │  caddy_data volume
                       │    localhost)             │
                       └─────────────┬────────────┘
                                     ▼
                       ┌────────────────────────┐
                       │   addresses_api         │  port 8000 closed by
                       │   (FastAPI)             │  default; only Caddy
                       └────────────┬───────────┘  reaches it internally
                                    │ read-only role
                                    ▼
┌────────────────────────┐        ┌──────────────────────────┐
│  postgis  (PostGIS 18) │◄──────►│  addresses_importer       │
│                        │        │  • importer.scheduler    │
│  cz_addresses          │        │    (monthly cron loop)   │
│  hr_addresses          │        │  • import_cz_csv         │
│  postgis_data (volume) │        │  • import_hr_wfs         │
└────────────────────────┘        └──────────────────────────┘
                                          │
                                          ├── HTTPS → vdp.cuzk.cz (CZ)
                                          └── WFS   → geoportal.dgu.hr (HR)
```

All four services share the default Compose network; the importer and API
reach the DB by service name (`host=postgis`), Caddy reaches the API at
`http://addresses_api:8000`. The API uses a read-only DB role (default
name `addresses_api`, see `API_DB_USER`) with `SELECT`-only privileges —
it cannot modify data or interfere with the importer's atomic-swap.

## Quick start

```bash
cp .env.example .env
# (edit .env — at minimum, change POSTGRES_PASSWORD)

docker compose -f compose.production.yaml up -d
```

The PostGIS service comes up immediately; the importer scheduler waits until
the next scheduled run (default: 5th of each month at 03:00). To populate the
DB right away, trigger a one-shot run:

```bash
docker compose -f compose.production.yaml run --rm addresses_importer \
    python3 -m importer.import_cz_csv

docker compose -f compose.production.yaml run --rm addresses_importer \
    python3 -m importer.import_hr_wfs
```

Indicative import times (fast SSD, your mileage will vary with network and disk):

- **CZ**: ~1 minute end-to-end (download ~60 MB ZIP, parallel COPY of ~6 200
  per-region CSVs into staging, materialise ~3M rows, build all indexes,
  atomic swap).
- **HR**: ~3-4 minutes end-to-end. The WFS server is the bottleneck —
  expect ~2-3 minutes streaming ~1.7M rows over the wire, plus ~1 minute
  for the post-import column rewrites and indexing.

## Configuration

All configuration is via environment variables, set in `.env` (see
`.env.example` for the template).

| Variable            | Default       | Used by         | Purpose                                                       |
|---------------------|---------------|-----------------|---------------------------------------------------------------|
| `POSTGRES_USER`     | `addresses`   | postgis service | DB superuser created on first start                           |
| `POSTGRES_PASSWORD` | `postgis`     | postgis service | DB password — **change before deploying anywhere reachable**  |
| `POSTGRES_DB`       | `addresses`   | postgis service | Database name                                                 |
| `API_DB_USER`       | `addresses_api` | postgis + API | Username of the read-only DB role created for the API         |
| `API_DB_PASSWORD`   | `apipassword` | postgis + API   | Password for the read-only API role                           |
| `API_KEYS`          | (empty)       | API             | Comma-separated allowlist of `X-API-Key` values; empty = open |
| `API_PORT`          | `8000`        | API + caddy     | Port the API listens on; Caddy proxies to it on the compose network. Not published to the host by default — uncomment the `ports:` block in `compose.production.yaml` to expose it for direct HTTP debugging. |
| `SERVER_NAME`       | `localhost`   | caddy           | Hostname(s) Caddy serves; `localhost` → internal CA, real name → Let's Encrypt. Space-separated for SAN. |
| `HTTP_PORT`         | `80`          | caddy           | Host port for plain HTTP. **Must stay `80` for Let's Encrypt** (ACME HTTP-01 challenge); override only in dev. |
| `HTTPS_PORT`        | `443`         | caddy           | Host port for HTTPS. **Must stay `443` for Let's Encrypt** (TLS-ALPN-01 challenge); override only in dev. |
| `PG_POOL_MIN`       | `2`           | API             | Minimum connections in the API's psycopg pool                 |
| `PG_POOL_MAX`       | `10`          | API             | Maximum connections in the API's psycopg pool                 |
| `CACHE_MAX_AGE`     | `3600`        | API             | Default `Cache-Control: max-age` (seconds) on cacheable GETs  |
| `PG_CONN_ADDRESSES` | (constructed) | importer        | libpq connection string; built from `POSTGRES_*` by Compose   |
| `PG_CONN_API`       | (constructed) | API             | libpq connection string for the read-only role                |
| `SCHEDULE_DAY`      | `5`           | scheduler       | Day of month (1–28) when the monthly run fires                |
| `SCHEDULE_HOUR`     | `3`           | scheduler       | Hour (0–23) of the run                                        |
| `SCHEDULE_MINUTE`   | `0`           | scheduler       | Minute (0–59) of the run                                      |
| `RUN_ON_START`      | `0`           | scheduler       | If `1`, run all imports immediately on container start        |
| `RUIAN_WORKERS`     | `4`           | CZ importer     | Parallel COPY workers (one per CSV file = one region)         |

The default schedule (5th of each month at 03:00) gives ČÚZK a few days of
slack — RUIAN dumps are usually published within the first few days of the
new month.

## Output schema

After a successful import, the live tables are:

### `cz_addresses`

| Column                  | Type                    | Notes                          |
|-------------------------|-------------------------|--------------------------------|
| `kod_adm`               | `integer` PRIMARY KEY   | RUIAN address code             |
| `obec_kod`, `obec_nazev` | `integer`, `varchar`   | Municipality                   |
| `momc_kod`, `momc_nazev` | …                      | City district (MOMC)           |
| `mop_kod`, `mop_nazev`   | …                      | City borough (MOP)             |
| `cast_obce_kod`, `cast_obce_nazev` | …            | Part of municipality           |
| `ulice_kod`, `ulice_nazev` | …                    | Street                         |
| `typ_so`                | `varchar`               | Building type (č.p. / č.ev.)   |
| `cislo_domovni`         | `integer`               | House number                   |
| `cislo_orientacni`, `cislo_orientacni_znak` | …   | Orientation number + suffix    |
| `psc`                   | `integer`               | ZIP code                       |
| `plati_od`              | `date`                  | Valid from                     |
| `geometry_jtsk`         | `geometry(Point, 5514)` | Native S-JTSK / Křovák         |
| `geometry`              | `geometry(Point, 4326)` | WGS84                          |
| `search_label`          | `text`                  | Lowercased formatted address per vyhláška 359/2011 Sb., used by `/v1/search` |

Indexes: GIST on both geometry columns, btree on `obec_nazev`, `ulice_nazev`,
`psc`, GIN trigram (`gin_trgm_ops`) on `search_label`.

### `hr_addresses`

INSPIRE-flavoured schema as delivered by the DGU WFS. Key columns include
`ulica` (street), `kucni_broj` (house number), `naselje` (settlement),
`postanski_broj` (postcode), plus:

- `geometry_htrs96` — `geometry(Point, 3765)` (native HTRS96 / TM)
- `geometry`        — `geometry(Point, 4326)` (WGS84, generated column)
- `search_label`    — `text` (lowercased "ulica kucni_broj, postanski_broj naselje", generated column)

Indexes: GIST on both geometries, btree on the four attribute columns above,
GIN trigram on `search_label`.

### Required PostgreSQL extensions

The importer ensures both extensions on every run via
`CREATE EXTENSION IF NOT EXISTS`:

- **`postgis`** — geometry types and spatial functions
- **`pg_trgm`** — trigram fuzzy matching used by `/v1/search`

Both are pre-installed in the `postgis/postgis:18-3.6` image; the importer
only needs to enable them in the database.

## REST API

The bundled `addresses_api` service exposes a versioned REST API over the
two tables. Generic by design — no consumer-specific assumptions baked in.

Once the stack is up, browse the auto-generated docs at:

- `http://localhost:8000/docs` — Swagger UI
- `http://localhost:8000/openapi.json` — OpenAPI 3.1 spec (suitable for
  generating typed clients in PHP, TypeScript, …)

### Endpoints

| Method | Path                                       | Purpose                                                |
|--------|--------------------------------------------|--------------------------------------------------------|
| POST   | `/v1/lookup`                               | Structured lookup. CZ runs a 5-variant fallback ladder.|
| POST   | `/v1/lookup/batch`                         | Bulk version of `/lookup`.                             |
| GET    | `/v1/addresses/{source}/{registry_id}`     | Look up a single address by `kod_adm` or `ogc_fid`.    |
| POST   | `/v1/addresses/batch`                      | Bulk by-id lookup; mix CZ + HR ids in one request. Returns matches + the items that didn't resolve. |
| GET    | `/v1/reverse?country=&lat=&lon=&radius_m=` | Nearest addresses to a coordinate.                     |
| GET    | `/v1/search?country=&q=&limit=`            | Fuzzy autocomplete via `pg_trgm` on `search_label`. Tolerates typos and out-of-order tokens; ranks by similarity. |
| GET    | `/v1/meta`                                 | Row counts and last-refresh timestamps.                |
| GET    | `/v1/health`                               | Liveness + DB ping.                                    |

### Response shape

Every match has a normalised envelope:

```jsonc
{
  "registry_ref": "11855321",          // kod_adm (CZ) or ogc_fid (HR), as string
  "source": "cz",                      // "cz" or "hr"
  "street": "Karlova",
  "house_number": "248/19",
  "city": "Aš",
  "postal_code": "35201",
  "geometry": {                        // GeoJSON Point in WGS84
    "type": "Point",
    "coordinates": [10.4513, 50.9894]  // [lon, lat]
  },
  "distance_m": null,                  // metres; only set by /reverse
  "score": null,                       // 0–1; only set by /search (pg_trgm word similarity)
  "raw": null                          // populated only when ?include=raw
}
```

`/search` returns matches sorted by `score` descending. Clients can apply a
threshold (e.g. ignore matches with `score < 0.5`) if they want to suppress
weak hits.

### Optional payload extensions (`?include=…`)

By default each match returns a normalised envelope (registry_ref, source,
street, house_number, city, postal_code, geometry). Pass a comma-separated
`?include=` parameter to request additional fields:

- `?include=raw` — adds a `raw` dict containing the native source columns
  (`kod_adm`, `obec_nazev`, `momc_nazev`, …, `kucni_broj`, `naselje`, …)
  so consumers that need granular fields don't lose them.

This is forward-compatible: future additions (e.g. `geometry_native`) plug
in without breaking the URL contract.

### Authentication

Requests are gated by an optional `X-API-Key` header:

- If `API_KEYS` is empty (default), all requests pass — useful for dev.
- If `API_KEYS=watcher-crm:abc,watcher-nms:def`, the header must match one
  of the listed values verbatim.

### Examples

```bash
# Structured lookup, CZ — replicates the 5-variant fallback ladder server-side
curl -X POST http://localhost:8000/v1/lookup \
     -H 'Content-Type: application/json' \
     -d '{
           "country": "cz",
           "street": "Karlova",
           "number": "12/3",
           "city": "Praha",
           "postal_code": "11000"
         }'

# Reverse geocoding near Prague Castle
curl 'http://localhost:8000/v1/reverse?country=cz&lat=50.090&lon=14.401&radius_m=200&limit=5'

# Fuzzy autocomplete (HR) — finds "Stjepana Ivičevića 7, Makarska" even with
# misspelt city; ranked by similarity score
curl 'http://localhost:8000/v1/search?country=hr&q=Stjepana%20Ivi%C4%8Devi%C4%87a%207%20Makarska&limit=5'

# Same in CZ — typing "Buřany 33" finds the address by part-of-municipality
curl 'http://localhost:8000/v1/search?country=cz&q=Bu%C5%99any%2033&limit=5'
```

### HTTPS / TLS

The bundled `caddy` service provides automatic HTTPS in front of the API.
Behaviour is controlled by `SERVER_NAME`:

| `SERVER_NAME`          | Behaviour                                                     |
|------------------------|---------------------------------------------------------------|
| `localhost` (default)  | Caddy issues a cert from its **internal CA**. Browsers warn unless you install Caddy's root cert; good for dev. |
| Real public hostname   | Caddy auto-fetches a **Let's Encrypt** cert via ACME and renews it transparently. Requires ports 80 + 443 reachable from the public internet, with DNS pointing here. |

`SERVER_NAME` also accepts multiple space-separated hostnames (e.g.
`"example.com www.example.com"`) — Caddy issues a single SAN cert
covering all of them.

Cert state is persisted in the `caddy_data` named volume so renewals
survive container rebuilds.

By default the `addresses_api` container does **not** publish port 8000
to the host — all external traffic must go through Caddy on 80/443. This
ensures HTTPS, API-key auth, and any future rate-limiting can't be
bypassed by hitting the API directly. To expose the API on plain HTTP for
debugging, uncomment the `ports:` block in
[compose.production.yaml](compose.production.yaml). For ad-hoc poking
without re-exposing the port:

```bash
docker compose -f compose.production.yaml exec addresses_api \
    curl -s http://localhost:8000/v1/health
```

### Existing-database setup

The read-only API role (default name `addresses_api`, configurable via
`API_DB_USER`) is created automatically on the **first** PostGIS startup
by `db/init/01-api-role.sh`. If you already have a populated `postgis_data`
volume from before this API service existed, run the SQL manually as the
DB superuser (`POSTGRES_USER`), substituting your actual values:

```sql
CREATE ROLE "<API_DB_USER>" LOGIN PASSWORD '<API_DB_PASSWORD>';
GRANT USAGE ON SCHEMA public TO "<API_DB_USER>";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "<API_DB_USER>";
ALTER DEFAULT PRIVILEGES FOR ROLE "<POSTGRES_USER>" IN SCHEMA public
    GRANT SELECT ON TABLES TO "<API_DB_USER>";
```

PostgreSQL extensions (`postgis`, `pg_trgm`) and the `search_label` columns
are ensured/created by the importer on every run, so simply re-running the
importer after upgrading is enough to populate them on an existing DB.

## Operations

### Logs

```bash
docker compose -f compose.production.yaml logs -f addresses_importer
```

The scheduler prints when it next plans to fire and emits per-step progress
during a run. Failures are logged but do not crash the daemon — the next
scheduled run will retry.

### Manual run (bypass the schedule)

```bash
docker compose -f compose.production.yaml run --rm addresses_importer \
    python3 -m importer.import_cz_csv
```

### Connecting with an external client (psql, QGIS, DBeaver)

The DB port is not exposed to the host by default. To enable, uncomment the
`ports:` block in `compose.production.yaml` and `docker compose up -d` again.

### Updating

```bash
git pull
docker compose -f compose.production.yaml build
docker compose -f compose.production.yaml up -d
```

The PostGIS data volume (`postgis_data`) survives rebuilds.

## Development / bare-metal

If you want to run the importers outside Docker:

```bash
cd importer
pip install -r requirements.txt
export PG_CONN_ADDRESSES="host=localhost user=addresses dbname=addresses password=postgis"

# from the repo root:
python3 -m importer.import_cz_csv
python3 -m importer.import_hr_wfs
```

Requires `gdal-bin` on the host (provides `ogr2ogr`).

## Repo layout

```
.
├── compose.production.yaml          # Production stack (postgis + importer + api)
├── .env.example                     # Configuration template
├── importer/
│   ├── Dockerfile                   # Importer image (Ubuntu + GDAL + Python)
│   ├── db.py                        # Shared connection helpers
│   ├── scheduler.py                 # Monthly cron-like daemon
│   ├── import_cz_csv.py             # RUIAN CSV importer (parallel COPY + atomic swap)
│   ├── import_hr_wfs.py             # DGU WFS importer (ogr2ogr + atomic swap)
│   ├── requirements.txt
│   └── archive/                     # Earlier import implementations, kept for reference
├── api/
│   ├── Dockerfile                   # Lightweight API image (python:slim)
│   ├── main.py                      # FastAPI app + lifespan
│   ├── routes.py                    # All v1 endpoints
│   ├── queries.py                   # SQL templates + CZ fallback ladder
│   ├── models.py                    # Pydantic request/response models
│   ├── db.py                        # Async psycopg pool
│   ├── settings.py                  # Env-driven config
│   └── requirements.txt
├── db/
│   └── init/
│       └── 01-api-role.sh           # Creates the read-only API DB role
├── caddy/
│   └── Caddyfile                    # Reverse proxy + automatic HTTPS config
└── LICENSE.md
```

## Data sources & licensing

- **CZ RUIAN** — published by ČÚZK under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  Attribution: "Český úřad zeměměřický a katastrální".
- **HR DGU** — INSPIRE address dataset published by Državna geodetska uprava.
  See the [DGU geoportal](https://geoportal.dgu.hr/) for current terms.

You are responsible for complying with the upstream data licenses when
redistributing the imported data.

## License

This project is licensed under the **GNU Affero General Public License
v3.0** — see [LICENSE.md](LICENSE.md). The AGPL's network-use clause
(section 13) applies: if you run a modified version of this software as a
network service, you must make the modified source code available to your
users.

Copyright (C) 2026 Mapiiik
