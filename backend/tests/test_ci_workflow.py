"""The deploy workflow's environment wiring.

Three near-identical droplets and three secret pairs mean a mis-set secret
deploys the wrong box. These assertions pin the guards that make that loud.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "deploy-prod": ("DEPLOY_HOST", "DEPLOY_SSH_KEY", "retina-prod", "env.prod.example"),
    "deploy-staging": ("STAGING_HOST", "STAGING_SSH_KEY", "retina-staging", "env.staging.example"),
    "deploy-test": ("TEST_HOST", "TEST_SSH_KEY", "retina-test", "env.test.example"),
}

SMOKE_EXPECTED = {
    "smoke-prod": ("https://towers.retina.fm", "prod"),
}

# Staging and test verify inside the deploy script, against the container,
# once the health poll above it has succeeded — the droplet-local path cannot
# be blurred by Cloudflare caching, and proves this deploy rather than
# whatever their public names still route to.
LOCAL_SMOKE_EXPECTED = {
    "deploy-staging": "staging",
    "deploy-test": "test",
}

LOCAL_SMOKE_RE = re.compile(r"EXPECT_ENV=(?P<env>\S+)\s+bash\s+deploy/smoke-local\.sh")

# Every deploy job must also prove this service's own ingress answers on 8443.
# Nothing routes there until a Cloudflare Origin Rule rewrites the origin port,
# so the probe has to address the droplet directly, under the hostname the edge
# serves — which is also the hostname `server_name` is rendered from.
EDGE_PROBE_EXPECTED = {
    "deploy-prod": "towers.retina.fm",
    "deploy-staging": "staging-towers.retina.fm",
    "deploy-test": "test-towers.retina.fm",
}

# `--resolve` and not DNS: two of the three hostnames have no record at all, and
# production's resolves to Cloudflare, which cannot reach a dark port.
EDGE_PROBE_RE = re.compile(
    r'--resolve\s+"(?P<host>[\w.-]+):8443:127\.0\.0\.1"[^\n]*\n\s*"https://(?P<url_host>[\w.-]+):8443/api/health"'
)

# The line that brings the stack up, allowing for flags between `compose`
# and `up` (e.g. `docker compose --profile x up -d --build`).
DEPLOY_UP_RE = re.compile(r"docker compose\b[^\n]*\bup\s+-d\s+--build")

# `exit` with no argument exits with the status of the preceding command,
# which on the success path is a passing `echo`, so a bare `exit` is as
# fatal to reachability as an explicit `exit 0`. `exit 1`, used by the
# health-failure branch just above the smoke line, must not match.
EXIT_SUCCESS_RE = re.compile(r"\bexit\b(?!\s+[1-9])")

# Staging and production deploy on a merge and nothing else. A pull request
# must never reach either box.
MAIN_PUSH_ONLY = "github.ref == 'refs/heads/main' && github.event_name == 'push'"

# Matches `test "$(hostname)" = "<host>" || { ...; exit N; }` so the guard's
# structure, not just the hostname string, can be checked: it must actually
# exit non-zero, and it must run before any command that touches the box.
HOSTNAME_GUARD_RE = re.compile(
    r'test\s+"\$\(hostname\)"\s*=\s*"(?P<host>[\w.-]+)"\s*\|\|\s*\{(?P<body>.*?)\}',
    re.DOTALL,
)

MUTATING_COMMAND_RE = re.compile(r'cd\s+"\$APP_DIR"|git reset --hard|docker compose up')


def _strip_comments(text):
    """Return `text` with shell comments removed.

    Every guard below searches for a command, so prose must not be searchable:
    a comment mentioning `exit` cannot fail a run, and commenting a guarded
    command out cannot pass one. A `#` opens a comment only at the start of a
    word and outside quotes, so `echo "a # b"; exit` keeps both its hash and
    the command after it. Quote state is tracked per line, which is all these
    scripts need.
    """
    stripped = []
    for line in text.splitlines():
        quote = ""
        cut = len(line)
        for i, char in enumerate(line):
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        stripped.append(line[:cut].rstrip())
    return "\n".join(stripped)


def _ssh_script(job):
    """The deploy script, comments stripped: every caller wants commands."""
    for step in job["steps"]:
        if step.get("uses", "").startswith("appleboy/ssh-action"):
            return _strip_comments(step["with"]["script"])
    raise AssertionError("no ssh-action deploy step in job")


def _smoke_env(job):
    for step in job["steps"]:
        env = step.get("env") or {}
        if "BASE_URL" in env:
            return env
    raise AssertionError("no smoke-test step carrying a BASE_URL")


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_one_deploy_job_per_environment(workflow):
    assert set(EXPECTED) <= set(workflow["jobs"])


def test_every_deploy_job_gates_on_the_tests(workflow):
    for job in EXPECTED:
        assert "test" in workflow["jobs"][job]["needs"]


@pytest.mark.parametrize("job", sorted(EXPECTED))
def test_each_job_uses_its_own_secrets_hostname_and_env_file(job, workflow):
    host_secret, key_secret, hostname, env_example = EXPECTED[job]
    rendered = yaml.safe_dump(workflow["jobs"][job])
    assert host_secret in rendered
    assert key_secret in rendered
    assert env_example in rendered

    # The guard: refuse to deploy if the box is not the one this job names,
    # and stop the job outright (non-zero exit) before anything mutates it.
    script = _ssh_script(workflow["jobs"][job])
    guard = HOSTNAME_GUARD_RE.search(script)
    assert guard, f"{job}: no hostname guard in the deploy script"
    assert guard.group("host") == hostname
    assert re.search(r"exit\s+[1-9]\d*", guard.group("body")), f"{job}: guard does not exit non-zero"
    mutating = MUTATING_COMMAND_RE.search(script)
    assert mutating, f"{job}: no deploy commands found for the guard to precede"
    assert guard.end() <= mutating.start(), f"{job}: hostname guard does not precede the deploy commands"


def test_test_environment_is_dispatch_only(workflow):
    """retina-test is for rehearsing, not for every merge to main."""
    assert "workflow_dispatch" in yaml.safe_dump(workflow["jobs"]["deploy-test"]["if"])


def test_production_waits_for_staging(workflow):
    assert "deploy-staging" in workflow["jobs"]["deploy-prod"]["needs"]


@pytest.mark.parametrize("job", sorted(SMOKE_EXPECTED))
def test_smoke_jobs_target_their_own_environment(job, workflow):
    """Each smoke job must address its own URL and assert the answer came from
    the environment it meant to reach."""
    base_url, expect_env = SMOKE_EXPECTED[job]
    assert job in workflow["jobs"], f"{job}: smoke job is missing"
    env = _smoke_env(workflow["jobs"][job])
    assert env["BASE_URL"] == base_url
    assert env["EXPECT_ENV"] == expect_env


@pytest.mark.parametrize("job", sorted(LOCAL_SMOKE_EXPECTED))
def test_staging_and_test_run_the_local_smoke_with_their_own_environment(job, workflow):
    """Staging and test verify against the container they just deployed, not
    a public URL, so the local smoke script must run with EXPECT_ENV pinned to
    the job's own environment rather than left unset or copied from another."""
    expected_env = LOCAL_SMOKE_EXPECTED[job]
    script = _ssh_script(workflow["jobs"][job])
    match = LOCAL_SMOKE_RE.search(script)
    assert match, f"{job}: does not run deploy/smoke-local.sh"
    assert match.group("env") == expected_env


@pytest.mark.parametrize("job", sorted(LOCAL_SMOKE_EXPECTED))
def test_local_smoke_is_reachable(job, workflow):
    """Restoring `exit 0` inside the health-poll loop, as production still
    has, would make deploy/smoke-local.sh dead code while the assertion
    above still passes, since that only checks the line's content, not
    whether the script ever reaches it."""
    script = _ssh_script(workflow["jobs"][job])
    up_match = DEPLOY_UP_RE.search(script)
    assert up_match, f"{job}: expected 'docker compose ... up -d --build' in the deploy script"
    match = LOCAL_SMOKE_RE.search(script)
    assert match, f"{job}: does not run deploy/smoke-local.sh"
    between = script[up_match.start() : match.start()]
    assert not EXIT_SUCCESS_RE.search(between), f"{job}: an exit before the smoke line makes it unreachable"


@pytest.mark.parametrize("job", ["deploy-staging", "deploy-prod"])
def test_main_line_deploys_only_on_a_push_to_main(job, workflow):
    assert workflow["jobs"][job]["if"] == MAIN_PUSH_ONLY


@pytest.mark.parametrize("job", sorted(EDGE_PROBE_EXPECTED))
def test_each_deploy_job_probes_its_own_edge(job, workflow):
    """The direct-origin probe is the only verification the edge gets until the
    flip, and a job probing another environment's hostname would pass against a
    server_name this droplet never renders."""
    script = _ssh_script(workflow["jobs"][job])
    match = EDGE_PROBE_RE.search(script)
    assert match, f"{job}: no direct-origin probe of https://<host>:8443/api/health"
    assert match.group("host") == EDGE_PROBE_EXPECTED[job]
    assert match.group("url_host") == EDGE_PROBE_EXPECTED[job]


@pytest.mark.parametrize("job", sorted(EDGE_PROBE_EXPECTED))
def test_the_edge_probe_is_reachable_and_fatal(job, workflow):
    """Production's health poll used to `exit 0` on success, which would leave
    the probe below it as dead code; and a probe whose failure does not exit
    non-zero verifies nothing."""
    script = _ssh_script(workflow["jobs"][job])
    up_match = DEPLOY_UP_RE.search(script)
    assert up_match, f"{job}: expected 'docker compose ... up -d --build' in the deploy script"
    probe = EDGE_PROBE_RE.search(script)
    between = script[up_match.start() : probe.start()]
    assert not EXIT_SUCCESS_RE.search(between), f"{job}: an exit before the edge probe makes it unreachable"
    after = script[probe.end() :]
    assert re.search(r"exit\s+[1-9]\d*", after), f"{job}: a failing edge probe does not fail the deploy"
