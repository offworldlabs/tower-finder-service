#!/usr/bin/env bash
# Droplet-side smoke test for tower-finder-service.
# Staging and test have no public hostname, and the container publishes no
# port (it sits only on retina-edge), so this checks the container directly
# via `docker compose exec` rather than a URL. The image is python:3.12-slim
# and has no curl, so the checks run as python3/urllib inside the container.
set -euo pipefail

SERVICE="tower-finder-service"
PASS=0
FAIL=0

# fetch runs inside $( ), so as a subshell it cannot hand a failure reason
# back through a variable; it appends one to this file instead, and everything
# collected is printed once after the results tally rather than interleaved
# with the aligned check list.
REASONS_FILE=$(mktemp)
trap 'rm -f "$REASONS_FILE"' EXIT

# Runs a python3 fetch of $1 inside the container and prints "<code>|<body>".
# A single helper for every endpoint keeps the request shape (timeout,
# HTTPError handling) in one place instead of duplicated per check.
fetch() {
  local path="$1"
  local script="
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen('http://localhost:8000${path}', timeout=10) as resp:
        print(f'{resp.status}|{resp.read().decode()}')
except urllib.error.HTTPError as e:
    print(f'{e.code}|{e.read().decode()}')
except Exception:
    print('000|')
"
  local output
  if output=$(docker compose exec -T "$SERVICE" python3 -c "$script" 2>/dev/null); then
    echo "$output"
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
  reason=$(docker compose exec -T "$SERVICE" python3 -c "$script" 2>&1 >/dev/null)
  echo "fetch ${path}: docker compose exec failed: ${reason}" >>"$REASONS_FILE"
  echo "DOWN|"
}

check_status() {
  local name="$1" path="$2" expected="$3"
  printf "  %-40s " "$name"
  local code
  code=$(fetch "$path")
  code="${code%%|*}"
  if [ "$code" = "$expected" ]; then
    echo "OK ($code)"; PASS=$((PASS + 1))
  else
    echo "FAIL ($code != $expected)"; FAIL=$((FAIL + 1))
  fi
}

# /api/health is fetched once here; both the status check and the
# environment check below read from this same response.
printf "  %-40s " "GET /api/health"
HEALTH_RESPONSE=$(fetch "/api/health")
HEALTH_CODE="${HEALTH_RESPONSE%%|*}"
HEALTH_BODY="${HEALTH_RESPONSE#*|}"
if [ "$HEALTH_CODE" = "200" ]; then
  echo "OK ($HEALTH_CODE)"; PASS=$((PASS + 1))
else
  echo "FAIL ($HEALTH_CODE != 200)"; FAIL=$((FAIL + 1))
fi

check_status "GET /api/config" "/api/config" "200"
check_status "GET /api/towers (Greenville SC)" "/api/towers?lat=34.85&lon=-82.40" "200"

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

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [ -s "$REASONS_FILE" ]; then
  echo ""
  echo "Diagnostics:"
  cat "$REASONS_FILE"
fi

[ "$FAIL" -eq 0 ]
