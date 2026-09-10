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

# SMOKE_* constants and the smoke_* check helpers, shared with smoke-local.sh.
# PASS/FAIL above must exist before this runs.
# shellcheck source=deploy/smoke-common.sh
source "$(CDPATH= cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-common.sh"

# fetch <url>
# One GET attempt; prints "<code>|<body>". Used by the frequencies/elevation
# checks in smoke-common.sh, which retry through this rather than
# duplicating the request. Code "000" means curl could not complete the
# request at all (DNS/connect/timeout): there is no response to read a
# status from. The second argument (a retry remains) is unused here.
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

echo "── tower-finder-service smoke tests (${BASE_URL}) ──"
smoke_check_status "GET /api/health" "${BASE_URL}/api/health" "200"
smoke_check_status "GET /api/config" "${BASE_URL}/api/config" "200"
smoke_check_status "GET /api/towers (Greenville SC)" "${BASE_URL}/api/towers?lat=34.85&lon=-82.40" "200"
smoke_check_contains "GET /api/towers (frequencies honoured)" smoke_assert_frequencies_honoured \
  "${BASE_URL}/api/towers?${SMOKE_FREQ_QUERY}"
smoke_check_contains "GET /api/elevation" smoke_assert_elevation_contract \
  "${BASE_URL}/api/elevation?${SMOKE_PROBE_QUERY}"

smoke_print_results
