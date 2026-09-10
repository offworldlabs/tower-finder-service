#!/usr/bin/env bash
# Shared post-deploy checks for tower-finder-service, sourced by
# deploy/smoke-test.sh (curl, public URL) and deploy/smoke-local.sh (docker
# compose exec, no curl in the image) so the two cannot drift apart. Not
# runnable standalone: without a `fetch` in scope there is no transport.
#
# Callers must define, before sourcing: a `fetch` (below), and integer PASS and
# FAIL counters.
#
# `frequencies` is asserted on the body because the status proves nothing:
# FastAPI answers 200 whether or not it honoured the parameter. Neither check
# says anything about tower data, since the echo is rendered from the query
# whatever the FCC returned, so an FCC outage passes both as a 200 with an
# empty tower list. Deliberate: a third-party blip must not read as a bad
# deploy. See retina-server's deploy/tower-contract.sh, which asserts the same
# contract against production from outside.

# Phoenix, AZ: inland desert, well inside open-meteo's DEM coverage, so it
# doubles as the elevation probe without risking a legitimate no-data answer.
# No broadcast tower transmits at 1234.5 MHz, so the echo can only be ours.
# Same point and frequency as retina-server's deploy/tower-contract.sh.
SMOKE_PROBE_QUERY="lat=33.45&lon=-112.07"
SMOKE_FREQ_QUERY="${SMOKE_PROBE_QUERY}&frequencies=1234.5"
# Exact serialized shape: the match is fixed-string, so key name and JSON
# rendering both count. Pinned identically by test_contract_echo_shape here
# and TOWER_CONTRACT_ECHO in retina-server. Change the shape, change all three.
SMOKE_FREQ_ECHO='"user_frequencies_mhz":[1234.5]'
SMOKE_ELEVATION_KEY='"elevation_m"'
# The route's own answer when open-meteo cannot be reached. Distinct from the
# 404 it gives for a point with no data, which would be a real fault here: the
# probe point has DEM coverage.
SMOKE_ELEVATION_UNAVAILABLE='Elevation service unavailable'

SMOKE_RETRY_ATTEMPTS="${SMOKE_RETRY_ATTEMPTS:-2}"
SMOKE_RETRY_SLEEP="${SMOKE_RETRY_SLEEP:-5}"
# Both are read inside a command substitution whose stdout is captured but
# whose stderr is not, so a bad value would print a shell or sleep error into
# the middle of an unfinished status line. Attempts is compared with [ -lt ],
# so it must be a shell integer: digits alone are not enough, the value must
# also fit. Sleep only reaches sleep(1), which takes a fraction but not ".".
case "$SMOKE_RETRY_ATTEMPTS" in
    *[!0-9]* | "") SMOKE_RETRY_ATTEMPTS=2 ;;
esac
[ "$SMOKE_RETRY_ATTEMPTS" -ge 1 ] 2>/dev/null || SMOKE_RETRY_ATTEMPTS=2
case "$SMOKE_RETRY_SLEEP" in
    *[!0-9.]* | "" | "." | *.*.*) SMOKE_RETRY_SLEEP=5 ;;
esac

# _smoke_is_transient <code>
# Whether a code means "no definite answer from the app yet", which decides
# both whether to retry and how a failure reads. One list, two
# consumers: widening it must not leave the severity rule behind.
#
# 000 (curl could not connect) and smoke-local.sh's DOWN are no answer at all.
# The rest are the edge in front of the app: nginx 502/503/504 while the
# container starts, Cloudflare 520-527 and 530 (1033, tunnel reconnecting),
# 429 from edge.conf.template's limit_req. 500 is deliberately absent (the app
# raises it for a missing MAPRAD_API_KEY, which waiting does not fix), as is
# every 4xx, which is the app answering definitely about its own contract.
_smoke_is_transient() {
    case "$1" in
        000 | DOWN | 502 | 503 | 504 | 429 | 52[0-7] | 530) return 0 ;;
        *) return 1 ;;
    esac
}

# _smoke_fetch_with_retry <target>
# Up to SMOKE_RETRY_ATTEMPTS attempts, sleeping SMOKE_RETRY_SLEEP between,
# while _smoke_is_transient says the answer is not yet definite. Returns 0 on
# the first 200, else 1.
#
# fetch reports failure through its printed "<code>|<body>", never its exit
# status, so a failed probe cannot trip the caller's `set -e`. Its second
# argument is non-empty while a retry remains; smoke-local.sh uses it to skip
# an expensive diagnostic on an attempt about to be retried.
#
# Sets SMOKE_LAST_CODE, SMOKE_LAST_BODY, SMOKE_ATTEMPT_CODES and
# SMOKE_MODE_CHANGED. These do not reach the caller's shell: every entry point
# runs inside a command substitution, so read them only from the functions
# below.
_smoke_fetch_with_retry() {
    local target="$1" attempt=1 resp code body more_attempts first_code=""
    SMOKE_ATTEMPT_CODES=""
    SMOKE_MODE_CHANGED=""
    while :; do
        more_attempts=""
        [ "$attempt" -lt "$SMOKE_RETRY_ATTEMPTS" ] && more_attempts=1
        resp=$(fetch "$target" "$more_attempts")
        code="${resp%%|*}"
        body="${resp#*|}"
        # No output at all is no answer, not a blank status.
        [ -n "$code" ] || code="DOWN"
        SMOKE_ATTEMPT_CODES="${SMOKE_ATTEMPT_CODES:+${SMOKE_ATTEMPT_CODES},}${code}"
        [ -n "$first_code" ] || first_code="$code"
        [ "$code" = "$first_code" ] || SMOKE_MODE_CHANGED=1
        SMOKE_LAST_CODE="$code"
        SMOKE_LAST_BODY="$body"
        [ "$code" = "200" ] && return 0
        if _smoke_is_transient "$code" && [ "$attempt" -lt "$SMOKE_RETRY_ATTEMPTS" ]; then
            sleep "$SMOKE_RETRY_SLEEP"
            attempt=$((attempt + 1))
            continue
        fi
        return 1
    done
}

# _smoke_report_failure <label> <target> <hint>
# Diagnosis for a fetch that never produced a usable answer.
_smoke_report_failure() {
    local label="$1" target="$2" hint="$3"
    if ! _smoke_is_transient "$SMOKE_LAST_CODE"; then
        echo "${label}: answered HTTP ${SMOKE_LAST_CODE} [${SMOKE_ATTEMPT_CODES}], not retried: ${target}"
    elif [ -n "$SMOKE_MODE_CHANGED" ]; then
        echo "${label}: never answered, and the failure moved [${SMOKE_ATTEMPT_CODES}]: ${target}"
        echo "    The codes differ between attempts, so this is something in flight rather than one steady fault."
    else
        echo "${label}: ${SMOKE_LAST_CODE} on every attempt [${SMOKE_ATTEMPT_CODES}]: ${target}"
        echo "    ${hint}"
    fi
}

# _smoke_body_contains <label> <expected>
# Greps the body already fetched. Never retried: a 200 with the wrong body is
# a definite, repeatable answer from the app.
_smoke_body_contains() {
    local label="$1" expected="$2"
    if ! printf '%s' "$SMOKE_LAST_BODY" | grep -qF "$expected"; then
        echo "${label}: answered 200 without ${expected}. First 300 bytes:"
        printf '    %s\n' "$(printf '%s' "$SMOKE_LAST_BODY" | head -c 300)"
        return 1
    fi
    return 0
}

# smoke_assert_frequencies_honoured <target>
# Named with the smoke_ prefix because retina-server's deploy/tower-contract.sh
# defines its own assert_* helpers with a different argument and exit-code
# contract; a shell that sourced both would otherwise silently take one.
smoke_assert_frequencies_honoured() {
    local target="$1"
    if ! _smoke_fetch_with_retry "$target"; then
        _smoke_report_failure "frequencies" "$target" \
            "/api/towers catches and logs FCC and Maprad failures on this path, so it does not 502 on a third-party outage: suspect the edge reaching the service, or the container."
        return 1
    fi
    _smoke_body_contains "frequencies" "$SMOKE_FREQ_ECHO"
}

# smoke_assert_elevation_contract <target>
# Passes on an elevation, and also on the route reporting its own dependency
# unavailable: both prove the route is present and answering for itself, which
# is the only thing this check can soundly establish. open-meteo's uptime is
# not this deploy's to assert. A 404, a 500 or a missing route still fails.
smoke_assert_elevation_contract() {
    local target="$1"
    if ! _smoke_fetch_with_retry "$target"; then
        if [ "$SMOKE_LAST_CODE" = 503 ] &&
            printf '%s' "$SMOKE_LAST_BODY" | grep -qF "$SMOKE_ELEVATION_UNAVAILABLE"; then
            echo "elevation: route healthy, open-meteo unavailable [${SMOKE_ATTEMPT_CODES}]"
            return 0
        fi
        _smoke_report_failure "elevation" "$target" \
            "The route answers 503 when open-meteo is unreachable and 404 for a point with no data; this is neither, so suspect the deploy or the edge."
        return 1
    fi
    _smoke_body_contains "elevation" "$SMOKE_ELEVATION_KEY"
}

# smoke_check_status <name> <target> <expected>
# Status-only check, through the same retry as the body checks so a container
# still starting does not fail the run on whichever check happens to run first.
smoke_check_status() {
    local name="$1" target="$2" expected="$3" out
    printf "  %-40s " "$name"
    out=$(
        _smoke_fetch_with_retry "$target" >/dev/null 2>&1
        printf '%s|%s' "$SMOKE_LAST_CODE" "$SMOKE_ATTEMPT_CODES"
    )
    local code="${out%%|*}" attempts="${out#*|}"
    if [ "$code" = "$expected" ]; then
        echo "OK ($code)"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        printf '    %s\n' "${name}: answered ${code} [${attempts}], wanted ${expected}"
        FAIL=$((FAIL + 1))
    fi
}

# smoke_check_contains <name> <assert-fn> <target>
smoke_check_contains() {
    local name="$1" assert_fn="$2" target="$3" reason
    printf "  %-40s " "$name"
    if reason=$("$assert_fn" "$target"); then
        echo "OK"
        PASS=$((PASS + 1))
        [ -z "$reason" ] || printf '%s\n' "$reason" | sed 's/^/    /'
    else
        echo "FAIL"
        printf '%s\n' "$reason" | sed 's/^/    /'
        FAIL=$((FAIL + 1))
    fi
}

# smoke_print_results
# Final tally and the run's verdict. Non-zero if anything failed.
smoke_print_results() {
    echo ""
    echo "Results: ${PASS} passed, ${FAIL} failed"
    [ "$FAIL" -eq 0 ]
}
