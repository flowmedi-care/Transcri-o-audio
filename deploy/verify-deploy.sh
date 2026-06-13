#!/usr/bin/env bash
# Run on the VPS after DNS and .env are configured.
# Usage: bash deploy/verify-deploy.sh https://transcribe.SEUDOMINIO.com YOUR_API_KEY

set -euo pipefail

BASE_URL="${1:-}"
API_KEY="${2:-}"

if [[ -z "$BASE_URL" || -z "$API_KEY" ]]; then
  echo "Usage: bash deploy/verify-deploy.sh <base_url> <api_key>"
  echo "Example: bash deploy/verify-deploy.sh https://transcribe.flowmedi.care abc123..."
  exit 1
fi

echo "==> Health check (no auth)"
curl -fsS "${BASE_URL}/health" | python3 -m json.tool

echo "==> Auth check (expect 404 for missing job — means auth passed)"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${API_KEY}" \
  "${BASE_URL}/v1/jobs/00000000-0000-0000-0000-000000000000")

if [[ "$HTTP_CODE" != "404" ]]; then
  echo "Unexpected status: ${HTTP_CODE} (expected 404 for unknown job)"
  exit 1
fi

echo "==> Service status"
systemctl is-active transcribe-api

echo "Deploy verification passed."
