#!/usr/bin/env bash
# Droplet-side smoke test for tower-finder-service.
# Staging and test verify on the droplet, not over their public names, and
# the container publishes no port, so this checks the container directly
# via `docker compose exec` rather than a URL. The image is python:3.12-slim
# and has no curl, so the checks run as python3/urllib inside the container.
set -euo pipefail

SERVICE="tower-finder-service"
PASS=0
FAIL=0

# SMOKE_FREQ_QUERY / SMOKE_FREQ_ECHO / SMOKE_ELEVATION_QUERY /
# SMOKE_ELEVATION_KEY / check_contains / assert_*: shared with smoke-test.sh.
# shellcheck source=deploy/smoke-common.sh
source "$(CDPATH= cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-common.sh"

# fetch runs inside $( ), so as a subshell it cannot hand a failure reason
# back through a variable; it appends one to this file instead, and everything
# collected is printed once after the results tally rather than interleaved
# with the aligned check list.
REASONS_FILE=$(mktemp)
trap 'rm -f "$REASONS_FILE"' EXIT

# Runs a python3 fetch of $1 inside the container and prints "<code>|<body>".
# A single helper for every endpoint keeps the request shape (timeout,
# HTTPError handling) in one place instead of duplicated per check.
#
# $2, set by the frequencies/elevation retry loop in smoke-common.sh:
# non-empty while it still has another attempt left after this one. Skips
# the diagnostic capture below on such a call: there is no point paying
# for a second `docker compose exec` to explain a failure that a later
# attempt may still turn into a pass.
fetch() {
  local path="$1" more_attempts="${2:-}"
  # The path arrives via argv, not interpolated into the source: it now comes
  # from smoke-common.sh, and a quote or newline in a future SMOKE_*_QUERY
  # would otherwise be a SyntaxError that 2>/dev/null hides as a bare "DOWN".
  local script="
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen('http://localhost:8000' + sys.argv[1], timeout=10) as resp:
        print(f'{resp.status}|{resp.read().decode()}')
except urllib.error.HTTPError as e:
    print(f'{e.code}|{e.read().decode()}')
except Exception:
    print('000|')
"
  local output
  if output=$(docker compose exec -T "$SERVICE" python3 -c "$script" "$path" 2>/dev/null); then
    echo "$output"
    return
  fi
  if [ -n "$more_attempts" ]; then
    echo "DOWN|"
    return
  fi
  # docker compose exec itself failed here (no running container, no such
  # service, a compose/daemon error), not the app declining to answer inside
  # a healthy container, which already reports itself as 000 above. Re-run
  # once with stderr captured, purely to surface why, and append it to
  # REASONS_FILE rather than writing it here: every caller strips anything
  # after the first "|" from fetch's output, so a reason returned on stdout
  # would never be read, and writing it to stderr now would land inside the
  # caller's still-open, not-yet-newlined status line.
  local reason
  reason=$(docker compose exec -T "$SERVICE" python3 -c "$script" "$path" 2>&1 >/dev/null)
  echo "fetch ${path}: docker compose exec failed: ${reason}" >>"$REASONS_FILE"
  echo "DOWN|"
}

# /api/health is fetched once here; both the status check and the
# environment check below read from this same response.
printf "  %-40s " "GET /api/health"
# Retried like every other check, but inline rather than via
# smoke_check_status because the environment check below reuses this body.
HEALTH_RESPONSE=$(
  _smoke_fetch_with_retry "/api/health" >/dev/null 2>&1
  printf '%s|%s' "$SMOKE_LAST_CODE" "$SMOKE_LAST_BODY"
)
HEALTH_CODE="${HEALTH_RESPONSE%%|*}"
HEALTH_BODY="${HEALTH_RESPONSE#*|}"
if [ "$HEALTH_CODE" = "200" ]; then
  echo "OK ($HEALTH_CODE)"; PASS=$((PASS + 1))
else
  echo "FAIL ($HEALTH_CODE != 200)"; FAIL=$((FAIL + 1))
fi

smoke_check_status "GET /api/config" "/api/config" "200"
smoke_check_status "GET /api/towers (Greenville SC)" "/api/towers?lat=34.85&lon=-82.40" "200"
smoke_check_contains "GET /api/towers (frequencies honoured)" smoke_assert_frequencies_honoured \
  "/api/towers?${SMOKE_FREQ_QUERY}"
smoke_check_contains "GET /api/elevation" smoke_assert_elevation_contract \
  "/api/elevation?${SMOKE_PROBE_QUERY}"

if [ -n "${EXPECT_ENV:-}" ]; then
  printf "  %-40s " "environment is ${EXPECT_ENV}"
  # No python3 on the droplet host, only inside the container, so this reads
  # the body already fetched above with sed rather than a second request.
  actual=$(printf '%s' "$HEALTH_BODY" | sed -n 's/.*"environment"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if [ "$actual" = "$EXPECT_ENV" ]; then
    echo "OK"; PASS=$((PASS + 1))
  else
    echo "FAIL (${actual:-?} != ${EXPECT_ENV})"; FAIL=$((FAIL + 1))
  fi
fi

smoke_result=0
smoke_print_results || smoke_result=$?

if [ -s "$REASONS_FILE" ]; then
  echo ""
  echo "Diagnostics:"
  cat "$REASONS_FILE"
fi

exit "$smoke_result"
