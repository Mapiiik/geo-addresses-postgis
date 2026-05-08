# geo-addresses-postgis

PostGIS-based address database for the Czech Republic and Croatia, kept up to
date by automated monthly imports from the official open-data sources:

- **CZ** — RUIAN address dump from [ČÚZK](https://vdp.cuzk.cz/) (CSV, monthly)
- **HR** — INSPIRE Address WFS from [DGU geoportal](https://geoportal.dgu.hr/)

The database is intended as a backend for downstream applications (e.g. CRM
systems, geocoding, address validation). A REST API on top is planned but not
yet part of this repo.

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
- **Self-contained Docker stack** — `compose.production.yaml` spins up
  PostGIS + the importer with one command.

## Architecture

```
┌────────────────────────┐        ┌──────────────────────────┐
│  postgis  (PostGIS 18) │◄──────►│  addresses_importer       │
│                         │        │  • importer.scheduler    │
│  cz_addresses           │        │    (monthly cron loop)   │
│  hr_addresses           │        │  • import_cz_csv         │
│  postgis_data (volume)  │        │  • import_hr_wfs         │
└────────────────────────┘        └──────────────────────────┘
                                          │
                                          ├── HTTP → vdp.cuzk.cz (CZ)
                                          └── WFS  → geoportal.dgu.hr (HR)
```

Both services live on the default Compose network; the importer reaches the DB
by service name (`host=postgis`).

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

The CZ import takes ~5–10 minutes (downloads ~120 MB of CSVs, ~3M rows). The
HR import takes longer because the WFS is paged and slower (~1.7M rows, may
take 30–60 minutes depending on DGU response times).

## Configuration

All configuration is via environment variables, set in `.env` (see
`.env.example` for the template).

| Variable            | Default       | Used by         | Purpose                                                       |
|---------------------|---------------|-----------------|---------------------------------------------------------------|
| `POSTGRES_USER`     | `addresses`   | postgis service | DB superuser created on first start                           |
| `POSTGRES_PASSWORD` | `postgis`     | postgis service | DB password — **change before deploying anywhere reachable**  |
| `POSTGRES_DB`       | `addresses`   | postgis service | Database name                                                 |
| `PG_CONN_ADDRESSES` | (constructed) | importer        | libpq connection string; built from `POSTGRES_*` by Compose   |
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

Indexes: GIST on both geometry columns, btree on `obec_nazev`, `ulice_nazev`,
`psc`.

### `hr_addresses`

INSPIRE-flavoured schema as delivered by the DGU WFS. Key columns include
`ulica` (street), `kucni_broj` (house number), `naselje` (settlement),
`postanski_broj` (postcode), plus:

- `geometry_htrs96` — `geometry(Point, 3765)` (native HTRS96 / TM)
- `geometry`        — `geometry(Point, 4326)` (WGS84, generated column)

Indexes: GIST on both geometries, btree on the four attribute columns above.

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
├── Dockerfile                       # Importer image (Ubuntu + GDAL + Python)
├── compose.production.yaml          # Production stack (postgis + importer)
├── .env.example                     # Configuration template
├── importer/
│   ├── db.py                        # Shared connection helpers
│   ├── scheduler.py                 # Monthly cron-like daemon
│   ├── import_cz_csv.py             # RUIAN CSV importer (parallel COPY + atomic swap)
│   ├── import_hr_wfs.py             # DGU WFS importer (ogr2ogr + atomic swap)
│   ├── requirements.txt
│   └── archive/                     # Earlier import implementations, kept for reference
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
