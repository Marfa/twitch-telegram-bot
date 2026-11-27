#!/usr/bin/env bash
# Print the row count of every base table in schema public to <output-file>.
# The table list is read from information_schema.tables, so the counts follow the
# catalog that the dump holds.
#
# Usage: scripts/pg-row-counts.sh <output-file>
#
# Run it inside scripts/bot-maintenance.sh (through scripts/pg-upgrade-to-*.sh),
# where the bot is stopped.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitch-telegram-bot}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.vps.yml}"
out="${1:?usage: pg-row-counts.sh <output-file>}"

cd "$APP_DIR"

psql_db() {
  docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U bot -d bot -v ON_ERROR_STOP=1 -At "$@" </dev/null
}

table_counts_sql="$(psql_db -c "
SELECT string_agg(
         format('SELECT %L::text AS t, COUNT(*)::bigint AS c FROM %I.%I',
                table_name, table_schema, table_name),
         ' UNION ALL ' ORDER BY table_name)
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';")"

if [[ -z "$table_counts_sql" ]]; then
  echo "ERROR: pg-row-counts: no base tables in schema public" >&2
  exit 1
fi

psql_db -c "SELECT t || E'\t' || c::text FROM ($table_counts_sql) x ORDER BY t;" >"$out"
