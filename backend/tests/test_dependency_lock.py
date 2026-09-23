"""uv.lock is the only record of what gets installed, so every install honours it.

`uv sync --locked` refuses a lock that no longer matches pyproject.toml. Without
the flag it relocks instead, and CI would pass on a tree the lock does not
record.
"""

import re
import shlex
from pathlib import Path

import yaml

from tests._helpers import strip_shell_comments

REPO_ROOT = Path(__file__).resolve().parents[2]

UV_SYNC_RE = re.compile(r"\buv sync\b([^\n&;|]*)")


def _uv_syncs(text):
    """The arguments of every `uv sync` in a shell text."""
    commands = strip_shell_comments(text).replace("\\\n", " ")
    return [shlex.split(match.group(1)) for match in UV_SYNC_RE.finditer(commands)]


def test_ci_tests_the_locked_tree():
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    syncs = _uv_syncs("\n".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"]))
    assert syncs, "the test job does not install with uv sync"
    for args in syncs:
        assert "--locked" in args, f"the test job syncs without --locked: {args}"


def test_the_image_runs_the_locked_tree_without_dev_tools():
    syncs = _uv_syncs((REPO_ROOT / "Dockerfile").read_text())
    assert syncs, "the Dockerfile does not install with uv sync"
    for args in syncs:
        assert "--locked" in args, f"the image syncs without --locked: {args}"
        assert "--no-dev" in args, f"the image ships the dev group: {args}"
