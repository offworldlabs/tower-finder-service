#!/usr/bin/env bash
# Post-deploy smoke test for tower-finder-service.
# Hits the public URL (through the Cloudflare tunnel) to validate the full path.
set -euo pipefail

BASE_URL="${BASE_URL:-https://towers.retina.fm}"
PASS=0
FAIL=0

# SMOKE_FREQ_QUERY / SMOKE_FREQ_ECHO / SMOKE_ELEVATION_QUERY /
# SMOKE_ELEVATION_KEY / check_contains / assert_*: shared with smoke-local.sh.
# shellcheck source=deploy/smoke-common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-common.sh"

# fetch <url>
# One GET attempt; prints "<code>|<body>". Used by the frequencies/elevation
# checks in smoke-common.sh, which retry through this rather than
# duplicating the request. Code "000" means curl could not complete the
# request at all (DNS/connect/timeout) -- there is no response to read a
# status from.
fetch() {
  local url="$1" resp code body
  resp=$(curl -s --connect-timeout 10 --max-time 60 -w '\n%{http_code}' "$url" 2>/dev/null) || {
    echo "000|"
    return
  }
  code=$(printf '%s' "$resp" | tail -n1)
  body=$(printf '%s' "$resp" | sed '$d')
  printf '%s|%s\n' "$code" "$body"
}

check_status() {
  local name="$1" url="$2" expected="$3"
  printf "  %-40s " "$name"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 60 "$url" 2>/dev/null) \
    || code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 10 --max-time 60 "$url" 2>/dev/null) \
    || { echo "FAIL (connection)"; FAIL=$((FAIL + 1)); return; }
  if [ "$code" = "$expected" ]; then
    echo "OK ($code)"; PASS=$((PASS + 1))
  else
    echo "FAIL ($code != $expected)"; FAIL=$((FAIL + 1))
  fi
}

check_environment() {
  local expected="$1"
  printf "  %-40s " "environment is ${expected}"
  local actual
  actual=$(curl -s --connect-timeout 10 --max-time 60 "${BASE_URL}/api/health" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('environment','?'))" 2>/dev/null) \
    || { echo "FAIL (unreadable)"; FAIL=$((FAIL + 1)); return; }
  if [ "$actual" = "$expected" ]; then
    echo "OK"; PASS=$((PASS + 1))
  else
    echo "FAIL (${actual} != ${expected})"; FAIL=$((FAIL + 1))
  fi
}

echo "── tower-finder-service smoke tests (${BASE_URL}) ──"
check_status "GET /api/health" "${BASE_URL}/api/health" "200"
check_status "GET /api/config" "${BASE_URL}/api/config" "200"
check_status "GET /api/towers (Greenville SC)" "${BASE_URL}/api/towers?lat=34.85&lon=-82.40" "200"
check_contains "GET /api/towers (frequencies honoured)" assert_frequencies_honoured "${BASE_URL}/api/towers?${SMOKE_FREQ_QUERY}"
check_contains "GET /api/elevation" assert_elevation_contract "${BASE_URL}/api/elevation?${SMOKE_ELEVATION_QUERY}"

if [ -n "${EXPECT_ENV:-}" ]; then
  check_environment "$EXPECT_ENV"
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
