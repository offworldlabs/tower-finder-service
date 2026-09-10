#!/usr/bin/env bash
# Post-deploy smoke test for tower-finder-service.
# Hits the public URL (through the Cloudflare tunnel) to validate the full path.
# Functional checks only. retina-server's vhost for the public name forwards
# just the tower paths here, so /api/health on 443 is answered by its own
# backend; which environment replied is asserted on this service's 8443 edge,
# in the deploy job, and cannot be read over the public name until the flip.
set -euo pipefail

BASE_URL="${BASE_URL:-https://towers.retina.fm}"
PASS=0
FAIL=0

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

echo "── tower-finder-service smoke tests (${BASE_URL}) ──"
check_status "GET /api/health" "${BASE_URL}/api/health" "200"
check_status "GET /api/config" "${BASE_URL}/api/config" "200"
check_status "GET /api/towers (Greenville SC)" "${BASE_URL}/api/towers?lat=34.85&lon=-82.40" "200"

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[ "$FAIL" -eq 0 ]
