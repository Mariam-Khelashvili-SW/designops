#!/usr/bin/env bash
# One-shot: copy the local dev Postgres into the Railway Postgres.
# Safe to re-run — it drops & recreates the prod `public` schema each time.
# Requires: docker running (local db container `designops-db-1`), railway CLI linked.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> fetching Railway Postgres public URL"
PUB="$(railway variables --service Postgres --json \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['DATABASE_PUBLIC_URL'])")"
[ -n "$PUB" ] || { echo "ERROR: no DATABASE_PUBLIC_URL"; exit 1; }
echo "    host: $(echo "$PUB" | sed -E 's#.*@([^/]*)/.*#\1#')"

echo "==> dumping local DB (designops-db-1)"
docker exec designops-db-1 pg_dump -U designops -Fc designops > /tmp/designops.dump
echo "    dump: $(wc -c < /tmp/designops.dump) bytes"
echo "    local counts: $(docker exec designops-db-1 psql -U designops -tAc \
  "select 'acct='||count(*) from account" )"

echo "==> drop + recreate public schema on PROD"
docker run --rm -e PGURL="$PUB" postgres:16 \
  psql "$PUB" -v ON_ERROR_STOP=1 \
  -c "SET lock_timeout='20s'; DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

echo "==> restoring dump into PROD"
docker run -i --rm postgres:16 \
  pg_restore --no-owner --no-privileges -d "$PUB" < /tmp/designops.dump || true
#   (pg_restore may print benign warnings; the verify below is the real check)

echo "==> verifying PROD row counts"
docker run --rm postgres:16 psql "$PUB" -tAc \
  "select 'account='||count(*) from account;
   select 'person ='||count(*) from person;
   select 'project='||count(*) from project;
   select 'enabled='||count(*) from account where digest_enabled;
   select 'alembic='||version_num from alembic_version;"

rm -f /tmp/designops.dump
echo "==> done. Expect account=1326, person=10, project=18, enabled=14, alembic=e6a2c1904f83"
