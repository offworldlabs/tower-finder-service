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
    "smoke-prod": "https://towers.retina.fm",
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

# Production reads its environment label off that same edge. Staging and test
# read theirs inside deploy/smoke-local.sh instead, so only production needs it
# here; it is the one environment with no droplet-local smoke of its own.
EDGE_ENV_EXPECTED = {
    "deploy-prod": "prod",
}

# The probe that reads the label: a second request to this job's own 8443 edge
# whose body, unlike the status probe's, is kept and parsed for "environment".
EDGE_ENV_PROBE_RE = re.compile(
    r'edge_env="\$\(curl.*?--resolve\s+"(?P<host>[\w.-]+):8443:127\.0\.0\.1"'
    r'.*?"https://(?P<url_host>[\w.-]+):8443/api/health".*?environment.*?\)"',
    re.DOTALL,
)

# The assertion on it. An unparseable body leaves `edge_env` empty, which is
# not the expected label either, so the same comparison covers both.
EDGE_ENV_GUARD_RE = re.compile(
    r'if\s+\[\s+"\$edge_env"\s+!=\s+"(?P<env>[\w-]+)"\s+\];\s*then(?P<body>.*?)\bfi\b',
    re.DOTALL,
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
def test_smoke_jobs_target_their_own_public_url(job, workflow):
    """Each smoke job must address its own environment's public name."""
    assert job in workflow["jobs"], f"{job}: smoke job is missing"
    env = _smoke_env(workflow["jobs"][job])
    assert env["BASE_URL"] == SMOKE_EXPECTED[job]


@pytest.mark.parametrize("job", sorted(SMOKE_EXPECTED))
def test_the_public_smoke_asserts_no_environment(job, workflow):
    """`towers.retina.fm` is retina-server's vhost, and it forwards only the
    tower paths on to this service. `/api/health` there is answered by its own
    backend, which reports no environment at all, so an environment assertion
    over the public name can only ever fail. It belongs on this service's own
    8443 edge, which is where EDGE_ENV_EXPECTED below puts it."""
    assert "EXPECT_ENV" not in _smoke_env(workflow["jobs"][job])
    script = _strip_comments((REPO_ROOT / "deploy" / "smoke-test.sh").read_text())
    assert "EXPECT_ENV" not in script, "deploy/smoke-test.sh still reads EXPECT_ENV"


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


@pytest.mark.parametrize("job", sorted(EDGE_ENV_EXPECTED))
def test_production_reads_its_environment_from_its_own_edge(job, workflow):
    """Which of three near-identical stacks answered is the one thing the
    public smoke cannot establish, so the deploy job establishes it, against
    the listener the public name will address once the origin port flips."""
    script = _ssh_script(workflow["jobs"][job])
    probe = EDGE_ENV_PROBE_RE.search(script)
    assert probe, f"{job}: does not read the environment from its own 8443 edge"
    assert probe.group("host") == EDGE_PROBE_EXPECTED[job]
    assert probe.group("url_host") == EDGE_PROBE_EXPECTED[job]


@pytest.mark.parametrize("job", sorted(EDGE_ENV_EXPECTED))
def test_a_wrong_environment_on_the_edge_fails_the_deploy(job, workflow):
    """Reading the label proves nothing on its own: the deploy has to stop when
    it is not this environment's, and it has to read it before it judges it."""
    script = _ssh_script(workflow["jobs"][job])
    probe = EDGE_ENV_PROBE_RE.search(script)
    assert probe, f"{job}: does not read the environment from its own 8443 edge"
    guard = EDGE_ENV_GUARD_RE.search(script)
    assert guard, f"{job}: nothing compares the edge's environment to an expected one"
    assert guard.group("env") == EDGE_ENV_EXPECTED[job]
    assert probe.end() <= guard.start(), f"{job}: judges the environment before reading it"
    assert re.search(r"exit\s+[1-9]\d*", guard.group("body")), f"{job}: a wrong environment does not fail the deploy"


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


# The edge's config travels in its image (deploy/nginx/Dockerfile) rather than a
# bind mount, so a template change alters the image and the `up` below recreates
# the container. Compose never recreates one for a mounted file's contents, and
# nginx renders its config once, at start.
COMPOSE = REPO_ROOT / "docker-compose.yml"

PREFLIGHT_RE = re.compile(r"docker compose\b[^\n]*\brun\b[^\n]*\bedge\b[^\n]*\bnginx -t\b")
RENDER_RE = re.compile(r"\bsed\b[^\n]*\bedge\.conf\.template\b")
READ_RUNNING_RE = re.compile(r"\bcat /etc/nginx/conf\.d/default\.conf\b")


def _guard(script, opener):
    """(top-level body, nested body) of the `if` whose line contains `opener`.

    Split by indentation because an `exit` that sits inside a nested condition
    does not make the outer guard fatal, and a body-wide search cannot tell the
    two apart.
    """
    lines = [line for line in script.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if opener in line and line.lstrip().startswith("if "):
            outer = len(line) - len(line.lstrip())
            top, nested = [], []
            for nxt in lines[i + 1 :]:
                depth = len(nxt) - len(nxt.lstrip())
                if depth <= outer:
                    break
                (top if depth == outer + 2 else nested).append(nxt.strip())
            return top, nested
    return None


def _redirect(script, pattern, job, missing):
    """The path a matched command writes to."""
    match = pattern.search(script)
    assert match, f"{job}: {missing}"
    line = script[script.rfind("\n", 0, match.start()) + 1 : script.find("\n", match.start())]
    target = re.search(r">\s*([^\s;]+)", line)
    assert target, f"{job}: {missing}: {line.strip()}"
    return target.group(1)


def _position(script, pattern, job, missing):
    match = pattern.search(script)
    assert match, f"{job}: {missing}"
    return match.start()


@pytest.mark.parametrize("job", sorted(EDGE_PROBE_EXPECTED))
def test_a_rejected_template_stops_before_the_running_edge_is_replaced(job, workflow):
    """`nginx -t` reads the certificates, so it runs on the droplet rather than
    in the image build. Ordering is the whole point: after the `up` it would
    report a fault the deploy had already shipped."""
    script = _ssh_script(workflow["jobs"][job])
    check = _position(script, PREFLIGHT_RE, job, "nothing syntax-checks the config before the deploy")
    up = _position(script, DEPLOY_UP_RE, job, "expected 'docker compose ... up -d --build'")
    assert check < up, f"{job}: the config is checked only after the container it replaces is gone"


@pytest.mark.parametrize("job", sorted(EDGE_PROBE_EXPECTED))
def test_the_running_config_is_compared_against_the_template(job, workflow):
    """A header is present from the day it is added, so it cannot show which
    config is loaded. The rendered file can."""
    script = _ssh_script(workflow["jobs"][job])
    up = _position(script, DEPLOY_UP_RE, job, "expected 'docker compose ... up -d --build'")
    render = _position(script, RENDER_RE, job, "nothing renders the template to compare against")
    read = _position(script, READ_RUNNING_RE, job, "nothing reads the config the edge is running")
    assert EDGE_PROBE_EXPECTED[job] in RENDER_RE.search(script).group(), (
        f"{job}: renders another environment's hostname into the expected config"
    )
    assert up < read, f"{job}: reads the running config before the deploy replaces it"
    guard = _guard(script, "diff")
    assert guard, f"{job}: the rendered and running configs are never compared"
    top, _ = guard
    assert any(re.fullmatch(r"exit [1-9]\d*", line) for line in top), (
        f"{job}: a config that does not match the template does not fail the deploy"
    )
    assert render < read, f"{job}: compares against a template rendered after the read"
    # The two files the steps above wrote, not whatever the diff happens to name:
    # a comparison of two other paths passes every other assertion here.
    expected = _redirect(script, RENDER_RE, job, "the rendered template goes nowhere")
    running = _redirect(script, READ_RUNNING_RE, job, "the running config goes nowhere")
    assert expected != running, f"{job}: both halves of the comparison are written to {expected}"
    line = next(text for text in script.splitlines() if "diff" in text and text.lstrip().startswith("if "))
    assert expected in line and running in line, (
        f"{job}: the comparison does not name the files this deploy wrote: {line.strip()}"
    )


@pytest.mark.parametrize("job", sorted(EDGE_PROBE_EXPECTED))
def test_the_document_is_asserted_not_just_the_api(job, workflow):
    """`/api/health` stays 200 when a frontend build leaves no index.html, and
    every header carries `always`, so a 404 document satisfies a header check."""
    script = _ssh_script(workflow["jobs"][job])
    guard = _guard(script, "edge_doc")
    assert guard, f"{job}: nothing asserts the status of the document"
    top, _ = guard
    assert any(re.fullmatch(r"exit [1-9]\d*", line) for line in top), (
        f"{job}: a non-200 document does not fail the deploy"
    )
    request = re.search(
        rf'--resolve "{re.escape(EDGE_PROBE_EXPECTED[job])}:8443:127\.0\.0\.1"'
        rf' "https://{re.escape(EDGE_PROBE_EXPECTED[job])}:8443/"',
        script,
    )
    assert request, f"{job}: the document request does not address this job's own edge"


def test_the_edge_config_travels_in_the_image_not_a_bind_mount():
    """A bind-mounted template is invisible to compose's recreate logic, which
    is what left both droplets serving a config the deploy had replaced."""
    compose = yaml.safe_load(COMPOSE.read_text())
    edge = compose["services"]["edge"]
    assert "build" in edge, "the edge must be built, or a template change never reaches nginx"
    assert "image" not in edge, "a stock image cannot carry this repo's template"
    mounts = [v for v in edge.get("volumes", []) if "edge.conf.template" in v]
    assert not mounts, f"the template is still bind-mounted, so compose cannot see it change: {mounts}"
