#!/usr/bin/env bash
# Shared post-deploy assertions for tower-finder-service's smoke tests.
# Sourced by deploy/smoke-test.sh (curl, over a public URL) and
# deploy/smoke-local.sh (docker compose exec, no curl in the image), kept
# here rather than duplicated into each so the two cannot drift apart from
# one another. Not meant to run standalone: with no `fetch` in scope there
# is no transport to check anything over.
#
# `frequencies` and /api/elevation are asserted against the response body,
# not the status code: FastAPI answers 200 whether or not it honoured a
# query parameter, so status alone cannot tell "honoured" from "silently
# dropped". See retina-server's deploy/tower-contract.sh, which asserts
# this same service's production instance the same way, from outside.

# Phoenix, AZ: inland desert terrain, well inside open-meteo's DEM coverage
# (a direct query returns a plain 335.0, never null), so it doubles as the
# /api/elevation probe below without risking a legitimate no-data answer at
# this specific point. No broadcast tower transmits at 1234.5 MHz, so the
# echo can only be this probe's own value, never a tower's. Same
# coordinates and frequency as retina-server's deploy/tower-contract.sh,
# which runs the same check against this service's production instance
# from outside.
SMOKE_PROBE_QUERY="lat=33.45&lon=-112.07"
SMOKE_FREQ_QUERY="${SMOKE_PROBE_QUERY}&frequencies=1234.5"
# Exact shape, not just the value: the match below is fixed-string, so key
# name and JSON rendering both count. Pinned in two other places this copy
# cannot see: backend/tests/test_towers_routes.py::test_contract_echo_shape
# in this repo, and TOWER_CONTRACT_ECHO in retina-server's
# deploy/tower-contract.sh. Change the shape, change all three.
SMOKE_FREQ_ECHO='"user_frequencies_mhz":[1234.5]'
SMOKE_ELEVATION_QUERY="${SMOKE_PROBE_QUERY}"
SMOKE_ELEVATION_KEY='"elevation_m"'

# Retry budget for _smoke_fetch_with_retry, below. Overridable via env so a
# harness can shorten the sleep without editing this file.
SMOKE_RETRY_ATTEMPTS="${SMOKE_RETRY_ATTEMPTS:-2}"
SMOKE_RETRY_SLEEP="${SMOKE_RETRY_SLEEP:-5}"

# _smoke_fetch_with_retry <target>
# Retries the caller's fetch() on a transport failure or a 502: up to
# SMOKE_RETRY_ATTEMPTS attempts, sleeping SMOKE_RETRY_SLEEP between them.
# fetch always returns 0 and reports failure through its printed
# "<code>|<body>", never its exit status, so a failed probe can never trip
# the caller's `set -e`. "000" and smoke-local.sh's "DOWN" both mean no
# HTTP response at all and are treated identically.
#
# 502 is retryable because routes/towers.py answers it only when the FCC or
# Maprad fetch itself raises -- for the frequencies check, a real
# dependency failure. /api/elevation is not as clean: it collapses
# "open-meteo failed outright" and "open-meteo answered 200 with no data
# for this point" into the same 502, so a 502 there can persist with no
# dependency outage involved. _smoke_assert_contains, below, reports which
# case a failure was.
#
# Sets SMOKE_LAST_CODE, SMOKE_LAST_BODY and SMOKE_ATTEMPT_CODES
# (comma-separated, one entry per attempt made, so a failure mode that
# changed between attempts is visible rather than just the last one).
# Returns 0 on the first 200 seen, 1 once attempts are exhausted or a
# non-retryable code is seen.
#
# Passes fetch a second argument, non-empty while a retry is still
# possible; smoke-test.sh's fetch ignores it, smoke-local.sh's uses it to
# skip capturing a diagnostic for an attempt this loop is about to retry
# anyway.
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
# Shared body for both checks below: fetches with retry, then greps the
# body for $expected. Never retries a 200 with the wrong body, since that
# is a definite, repeatable answer from the app, not a transient one.
# Prints why it failed; returns non-zero.
_smoke_assert_contains() {
    local label="$1" target="$2" expected="$3"
    if ! _smoke_fetch_with_retry "$target"; then
        case "$SMOKE_LAST_CODE" in
            000 | DOWN)
                echo "${label}: unreachable [${SMOKE_ATTEMPT_CODES}]: ${target}"
                ;;
            502)
                # An attempt sequence ending in 502 is, by construction,
                # only ever 000/DOWN/502 throughout (anything else ends the
                # retry loop early) -- so no 000/DOWN entry means every
                # attempt got 502: the same answer every time, not a blip.
                case ",${SMOKE_ATTEMPT_CODES}," in
                    *,000,* | *,DOWN,*)
                        echo "${label}: upstream still failing [${SMOKE_ATTEMPT_CODES}]: ${target}"
                        echo "    Failure mode changed between attempts; looks like a genuine third-party blip (FCC/open-meteo)."
                        ;;
                    *)
                        echo "${label}: upstream returned 502 on every attempt [${SMOKE_ATTEMPT_CODES}]: ${target}"
                        echo "    Same result every time, not a blip: check the probe point or the route itself, not just the dependency."
                        ;;
                esac
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
# column, same one-line reason on failure. Expects integer PASS and FAIL
# counters already in scope, exactly like check_status.
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
