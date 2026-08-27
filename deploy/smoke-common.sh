#!/usr/bin/env bash
# Shared post-deploy assertions for tower-finder-service's smoke tests.
#
# A status code cannot tell a query parameter that was honoured from one
# FastAPI silently dropped -- both answer 200 -- so `frequencies` is checked
# by asserting the response body, not the code. /api/elevation gets the same
# treatment: a shape check for the key callers actually read, since a route
# this new has no other coverage after deploy. See retina-server's
# deploy/tower-contract.sh, which already asserts this same service's
# production instance the same way, from the outside.
#
# Sourced by both deploy/smoke-test.sh (curl, over a public URL) and
# deploy/smoke-local.sh (docker compose exec -- the image has no curl)
# rather than duplicated into each: the assertions are identical and only
# the transport differs, and a fork of this logic has already drifted once
# within a single commit.
#
# The transport is the seam. Each caller defines its own `fetch <target>`,
# which makes one request and prints "<code>|<body>", always returning 0
# itself -- failure is reported through the printed code, never through
# fetch's exit status, so one failed probe can never trip the caller's
# `set -e`. <target> means whatever that script's own fetch expects: a full
# URL for curl, a bare path for docker exec. Code "000" is the shared
# sentinel for "no HTTP response at all" (DNS/connect/timeout/exec
# failure), whichever transport produced it; smoke-local.sh's fetch also
# uses "DOWN" for the same thing when `docker compose exec` itself fails,
# and is treated identically below.
#
# check_contains, below, expects the sourcing script to already have
# integer PASS and FAIL counters in scope, matching its own check_status.
#
# Not meant to run standalone: with no `fetch` in scope there is no
# transport to check anything over.

# Phoenix, AZ. No broadcast tower transmits at 1234.5 MHz, so the echo below
# can only be this probe's own value, never a tower's. Same coordinates and
# frequency as retina-server's tower-contract.sh, which asserts this
# service's production instance from outside its own pipeline; this is the
# same check, run from inside this one. Reused for /api/elevation too, so
# there is a single location to reason about rather than one per endpoint.
SMOKE_PROBE_QUERY="lat=33.45&lon=-112.07"
SMOKE_FREQ_QUERY="${SMOKE_PROBE_QUERY}&frequencies=1234.5"
# Exact shape, not just the value: key name and JSON rendering both count,
# since the match below is fixed-string. Pinned on the other side too, by
# backend/tests/test_towers_routes.py::test_contract_echo_shape.
SMOKE_FREQ_ECHO='"user_frequencies_mhz":[1234.5]'
SMOKE_ELEVATION_QUERY="${SMOKE_PROBE_QUERY}"
SMOKE_ELEVATION_KEY='"elevation_m"'

# One retry, one sleep. Both routes below fan out to a third party (FCC,
# open-meteo) and a blip there must not read as this deploy's fault -- but
# only for the failure kinds a blip actually produces, see
# _smoke_fetch_with_retry, so a genuine application fault still fails on
# the first attempt rather than being retried until it happens to clear.
# Overridable like BASE_URL/SERVICE above, so a test harness can shorten the
# sleep without touching this file.
SMOKE_RETRY_ATTEMPTS="${SMOKE_RETRY_ATTEMPTS:-2}"
SMOKE_RETRY_SLEEP="${SMOKE_RETRY_SLEEP:-5}"

# _smoke_fetch_with_retry <target>
# Calls the caller's fetch(), retrying only "000"/"DOWN" (unreachable, no
# response at all) and "502". Both routes below turn a third-party failure
# into exactly 502 and nothing else does (routes/towers.py wraps the FCC
# call and the open-meteo call each in their own broad except, and neither
# raises any other status) -- so 502 here is provably the dependency, not
# our own code, and any other code is a real, definite answer from the app
# and is not retried. Sets SMOKE_LAST_CODE, SMOKE_LAST_BODY and
# SMOKE_ATTEMPT_CODES (comma-separated, one entry per attempt actually
# made, so a reader can see a failure mode that changed between attempts
# rather than just the last one). Returns 0 once a 200 is seen, 1 once
# retries are exhausted or a non-retryable code is seen.
#
# Passes fetch a second argument, non-empty while a retry is still possible.
# smoke-test.sh's fetch ignores it. smoke-local.sh's uses it to skip the
# stderr-capturing second `docker compose exec` it otherwise runs to explain
# a failure: paying for that, and recording it, on an attempt this loop is
# about to retry anyway would leave a stale "docker compose exec failed"
# diagnostic sitting next to a check that goes on to pass.
_smoke_fetch_with_retry() {
    local target="$1" attempt=1 resp code body more_attempts
    SMOKE_ATTEMPT_CODES=""
    while :; do
        more_attempts=""
        [ "$attempt" -lt "$SMOKE_RETRY_ATTEMPTS" ] && more_attempts=1
        resp=$(fetch "$target" "$more_attempts")
        code="${resp%%|*}"
        body="${resp#*|}"
        SMOKE_ATTEMPT_CODES="${SMOKE_ATTEMPT_CODES:+${SMOKE_ATTEMPT_CODES},}${code}"
        if [ "$code" = "200" ]; then
            SMOKE_LAST_CODE="$code"
            SMOKE_LAST_BODY="$body"
            return 0
        fi
        case "$code" in
            000 | DOWN | 502)
                if [ "$attempt" -lt "$SMOKE_RETRY_ATTEMPTS" ]; then
                    sleep "$SMOKE_RETRY_SLEEP"
                    attempt=$((attempt + 1))
                    continue
                fi
                ;;
        esac
        SMOKE_LAST_CODE="$code"
        SMOKE_LAST_BODY="$body"
        return 1
    done
}

# _smoke_assert_contains <label> <target> <expected>
# Shared body for both checks below. Prints why it failed in a form that
# tells "unreachable" apart from "answered wrong" on sight; returns
# non-zero. Never retries a 200 with the wrong body: that is a definite,
# repeatable answer from the app, not a transient one.
_smoke_assert_contains() {
    local label="$1" target="$2" expected="$3"
    if ! _smoke_fetch_with_retry "$target"; then
        case "$SMOKE_LAST_CODE" in
            000 | DOWN)
                echo "${label}: unreachable [${SMOKE_ATTEMPT_CODES}]: ${target}"
                ;;
            502)
                echo "${label}: upstream still failing [${SMOKE_ATTEMPT_CODES}]: ${target}"
                echo "    Third-party dependency (FCC/open-meteo), not this route; see deploy/smoke-common.sh."
                ;;
            *)
                echo "${label}: answered HTTP ${SMOKE_LAST_CODE} [${SMOKE_ATTEMPT_CODES}], not retried: ${target}"
                ;;
        esac
        return 1
    fi
    if ! printf '%s' "$SMOKE_LAST_BODY" | grep -qF "$expected"; then
        echo "${label}: answered 200 without ${expected}. First 300 bytes:"
        printf '    %s\n' "$(printf '%s' "$SMOKE_LAST_BODY" | head -c 300)"
        return 1
    fi
    return 0
}

# assert_frequencies_honoured <target>
# <target> must already carry SMOKE_FREQ_QUERY's query string.
assert_frequencies_honoured() {
    _smoke_assert_contains "frequencies" "$1" "$SMOKE_FREQ_ECHO"
}

# assert_elevation_contract <target>
# <target> must already carry SMOKE_ELEVATION_QUERY's query string.
assert_elevation_contract() {
    _smoke_assert_contains "elevation" "$1" "$SMOKE_ELEVATION_KEY"
}

# check_contains <name> <assert-fn> <target>
# check_status's counterpart for the assertions above: same aligned name
# column, same PASS/FAIL accounting, one line of reason on failure.
check_contains() {
    local name="$1" assert_fn="$2" target="$3" reason
    printf "  %-40s " "$name"
    if reason=$("$assert_fn" "$target"); then
        echo "OK"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        printf '%s\n' "$reason"
        FAIL=$((FAIL + 1))
    fi
}
