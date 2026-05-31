#!/bin/bash
set -euo pipefail

QUERY="${1:-What is required in a 510(k) submission?}"

echo "Query: $QUERY"
echo "─────────────────────────────────────────────────────────"

curl -s -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key-change-me" \
  -d "{\"query\": \"$QUERY\"}" > /tmp/smoke-response.json

echo
echo "─── Retrieved passages ───"
jq -r '.passages[] | "  [\(.guidance_id):\(.chunk_index)] score=\(.score) — \(.guidance_title[0:60])"' /tmp/smoke-response.json

echo
echo "─── Answer ───"
jq -r '.answer' /tmp/smoke-response.json

echo
echo "─── Citations ───"
jq -r '.citations[] | "  [\(.guidance_id):\(.chunk_index)] chars \(.char_start)-\(.char_end)\n    \"\(.quoted_text[0:120])...\""' /tmp/smoke-response.json
