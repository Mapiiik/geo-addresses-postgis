#!/usr/bin/env bash
# Creates the read-only DB role used by the API service.
# This script runs ONLY on the first initialisation of the postgis volume
# (i.e. when /var/lib/postgresql is empty). For an already-populated DB,
# run the equivalent SQL manually — see README.md.
set -euo pipefail

API_DB_USER="${API_DB_USER:-addresses_api}"

if [ -z "${API_DB_PASSWORD:-}" ]; then
  echo "WARN: API_DB_PASSWORD is not set; skipping ${API_DB_USER} role creation."
  exit 0
fi

# Identifiers are quoted with "" in SQL to handle role names with mixed case
# or special characters; password is a string literal in single quotes.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname  "$POSTGRES_DB" <<EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${API_DB_USER}') THEN
        CREATE ROLE "${API_DB_USER}" LOGIN PASSWORD '${API_DB_PASSWORD}';
    END IF;
END
\$\$;

GRANT USAGE ON SCHEMA public TO "${API_DB_USER}";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "${API_DB_USER}";

-- Future tables (importer's _new working tables become the live tables after
-- the atomic swap) automatically grant SELECT to the API role too.
ALTER DEFAULT PRIVILEGES FOR ROLE "${POSTGRES_USER}" IN SCHEMA public
    GRANT SELECT ON TABLES TO "${API_DB_USER}";
EOSQL
