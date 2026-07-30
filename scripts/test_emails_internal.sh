#!/usr/bin/env bash
# §10.3 gate — does a roster designer's daily actually land in Fairwind
# `emails_internal`, with its per-project sections intact?
#
# Run this BEFORE build step 1. It is the only thing that decides whether the
# export path can carry the digest (source_mode=fairwind) or it flips to gmail.
#
# Test subject: Predrag Gavrilovikj (§11.2) — zero Fairwind search hits, so a pass
# on him is a real pass, not a false positive from an easy case.
#
# Reads FW_CLIENT_ID / FW_CLIENT_SECRET from env ONLY. Rotate the leaked secret
# first (§9.6). Requires: curl, jq.
set -euo pipefail

BASE="${FW_BASE_URL:-https://fairwind.scandiweb.com}"
: "${FW_CLIENT_ID:?set FW_CLIENT_ID in env}"
: "${FW_CLIENT_SECRET:?set FW_CLIENT_SECRET in env}"
ACC="${1:?usage: $0 <design_account_id>   # e.g. Northerner (Predrag) or Felco/SGD}"
FROM="${2:-2026-07-13}"
TO="${3:-2026-07-18}"
SUBJECT="${SUBJECT:-Predrag}"   # display name / surname to grep for

echo "→ token…"
TOKEN=$(curl -s -X POST "$BASE/api/auth/oauth2/token" \
  -d grant_type=client_credentials -d client_id="$FW_CLIENT_ID" \
  -d client_secret="$FW_CLIENT_SECRET" -d resource="$BASE/api/v1" -d scope=api \
  | jq -r .access_token)
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || { echo "✗ no token — check creds / rotation"; exit 1; }

echo "→ create export for account $ACC ($FROM..$TO)…"
EXPORT=$(curl -s -X POST "$BASE/api/v1/exports" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACC\",\"date_from\":\"$FROM\",\"date_to\":\"$TO\",
       \"data_types\":[\"emails_internal\",\"emails_external\",\"jira\",\"transcripts\"],
       \"include_files\":false}" | jq -r .id)
echo "  export id: $EXPORT"

echo "→ poll…"
for i in $(seq 1 60); do
  STATUS=$(curl -s "$BASE/api/v1/exports/$EXPORT" -H "Authorization: Bearer $TOKEN" | jq -r .status)
  echo "  [$i] status=$STATUS"
  [ "$STATUS" = "ready" ] && break
  [ "$STATUS" = "failed" ] && { echo "✗ export failed"; exit 1; }
  sleep 5
done

echo "→ counts:"
curl -s "$BASE/api/v1/exports/$EXPORT" -H "Authorization: Bearer $TOKEN" | jq '.status, .counts'

echo "→ manifest internal files:"
curl -s "$BASE/api/v1/exports/$EXPORT.json" -H "Authorization: Bearer $TOKEN" \
  | jq -r '.files[]' | grep -i internal || echo "  (no internal files in manifest)"

echo
echo "PASS CRITERION (§10.3): a daily from '$SUBJECT' for 17 Jul appears under"
echo "json/threads/internal/ WITH its per-project sections intact."
echo "If it does NOT → source_mode flips to gmail, permanently."
