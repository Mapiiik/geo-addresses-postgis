#!/usr/bin/env bash
# Creates the read-only DB role used by the API service.
# This script runs ONLY on the first initialisation of the postgis volume
# (i.e. when /var/lib/postgresql is empty). For an already-populated DB,
# run the equivalent SQL manually — see README.md.
set -euo pipefail

if [ -z "${API_DB_PASSWORD:-}" ]; then
  echo "WARN: API_DB_PASSWORD is not set; skipping addresses_api role creation."
  exit 0
fi

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname  "$POSTGRES_DB" <<EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'addresses_api') THEN
        CREATE ROLE addresses_api LOGIN PASSWORD '${API_DB_PASSWORD}';
    END IF;
END
\$\$;

GRANT USAGE ON SCHEMA public TO addresses_api;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO addresses_api;

-- Future tables (importer's _new working tables become the live tables after
-- the atomic swap) automatically grant SELECT to addresses_api too.
ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public
    GRANT SELECT ON TABLES TO addresses_api;
EOSQL
