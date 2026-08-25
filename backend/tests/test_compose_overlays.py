"""The deploy overlays must render, and must differ only where intended.

Two of the values asserted here are contracts with things outside this repo:
retina-server's nginx resolves the `tower-finder-service` network alias over
retina-edge, and CI's health poll addresses the Compose service by that same
name. Renaming either breaks a caller that cannot see this file.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = ("prod", "staging", "test")

# Paths permitted to differ between overlays. Anything else diverging is drift.
ALLOWED_DIVERGENCE = (
    "services.tower-finder-service.container_name",
    "services.tower-finder-service.environment.TOWER_FINDER_ENV",
)


BACKEND_ENV_SENTINEL_KEY = "TOWER_FINDER_TEST_SENTINEL"
BACKEND_ENV_SENTINEL_VALUE = "from-backend-env-not-root"


@pytest.fixture(scope="module", autouse=True)
def backend_env_file():
    """`docker compose config` refuses to render when an env_file is missing,
    and a sentinel value is needed to prove which .env Compose actually read.

    A real backend/.env is moved aside for the duration and restored
    afterwards, including on test failure, so a developer's secrets are
    never overwritten or lost.
    """
    path = REPO_ROOT / "backend" / ".env"
    backup = path.with_name(".env.test-compose-overlays-backup")
    had_real_file = path.exists()
    if had_real_file:
        path.rename(backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{BACKEND_ENV_SENTINEL_KEY}={BACKEND_ENV_SENTINEL_VALUE}\n")
    try:
        yield
    finally:
        path.unlink()
        if had_real_file:
            backup.rename(path)


def render(environment: str) -> dict:
    args = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        f"docker-compose.{environment}.yml",
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROJECT_NAME": "tower-finder-service"},
    )
    assert result.returncode == 0, f"{environment} did not render:\n{result.stderr}"
    return json.loads(result.stdout)


def flatten(value, prefix=""):
    if isinstance(value, dict):
        out = {}
        for key, sub in value.items():
            out.update(flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(value, list):
        out = {}
        for index, sub in enumerate(value):
            out.update(flatten(sub, f"{prefix}[{index}]"))
        return out
    return {prefix: value}


def test_every_environment_renders():
    for environment in ENVIRONMENTS:
        assert render(environment)["services"]


def test_container_names_are_distinct_and_named_for_their_environment():
    names = {
        environment: render(environment)["services"]["tower-finder-service"]["container_name"]
        for environment in ENVIRONMENTS
    }
    assert names == {
        "prod": "tower-finder-prod",
        "staging": "tower-finder-staging",
        "test": "tower-finder-test",
    }


def test_each_overlay_labels_its_environment():
    for environment in ENVIRONMENTS:
        service = render(environment)["services"]["tower-finder-service"]
        assert service["environment"]["TOWER_FINDER_ENV"] == environment


def test_network_alias_is_stable():
    """retina-server's nginx proxies to this exact name over retina-edge."""
    for environment in ENVIRONMENTS:
        service = render(environment)["services"]["tower-finder-service"]
        assert service["networks"]["retina-edge"]["aliases"] == ["tower-finder-service"]


def test_compose_service_name_is_stable():
    """CI's health poll runs `docker compose exec -T tower-finder-service`."""
    for environment in ENVIRONMENTS:
        assert "tower-finder-service" in render(environment)["services"]


def test_secrets_come_from_backend_env_not_the_disposable_root_env():
    """The sentinel is written only to backend/.env by the fixture above.

    Its presence in the rendered environment is what proves Compose read
    backend/.env: unlike env_file, which some Compose versions fold into
    `environment` and others report separately, the resolved value here has
    one shape everywhere.
    """
    for environment in ENVIRONMENTS:
        service = render(environment)["services"]["tower-finder-service"]
        assert service["environment"][BACKEND_ENV_SENTINEL_KEY] == BACKEND_ENV_SENTINEL_VALUE


def test_overlays_differ_only_where_allowed():
    reference = flatten(render("prod"))
    for environment in ("staging", "test"):
        candidate = flatten(render(environment))
        for key in sorted(set(reference) | set(candidate)):
            if key in ALLOWED_DIVERGENCE:
                continue
            assert reference.get(key) == candidate.get(key), (
                f"{environment} diverges from prod at {key}: {reference.get(key)!r} != {candidate.get(key)!r}"
            )


def test_env_examples_pin_the_project_name():
    """The runtime volume is tower-finder-service_runtime-data. A different
    project name silently creates an empty one and orphans the live config."""
    for environment in ENVIRONMENTS:
        text = (REPO_ROOT / "deploy" / f"env.{environment}.example").read_text()
        assert "COMPOSE_PROJECT_NAME=tower-finder-service" in text
        assert f"docker-compose.yml:docker-compose.{environment}.yml" in text
