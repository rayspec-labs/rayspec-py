# SPDX-License-Identifier: Apache-2.0
"""PRD-08 — the built Docker image, run as a real container.

Boundary: ``docker run`` against an ALREADY-BUILT image; nothing here builds one. A slim base
plus two agent-SDK wheels is a multi-hundred-MB build with real registry/network reads at build
time — the opposite of the hermetic, fast suite this file has to stay part of. The dedicated CI
job with a Docker daemon (PRD-08 R7) builds and pushes the image first, then runs this suite with
``RAYSPEC_DOCKER_IMAGE=<tag>`` — the same shape as how the ``live`` marker's ``RAYSPEC_LIVE`` opts
a real-provider suite in (see ``tests/conftest.py``), just gated on an image tag instead of a
credential flag.

Every test here carries the ``docker`` marker and skips cleanly — no error — when ``docker`` is
not on PATH, when ``RAYSPEC_DOCKER_IMAGE`` is unset, or when the daemon turns out to be
unreachable once a command is actually tried. A default ``-m 'not live'`` run, on a machine with
neither a built image nor a daemon, stays green.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Set by the dedicated docker CI job to the tag it just built/pushed; unset everywhere else.
IMAGE_ENV = "RAYSPEC_DOCKER_IMAGE"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI on PATH"),
    pytest.mark.skipif(
        not os.environ.get(IMAGE_ENV),
        reason=f"needs {IMAGE_ENV} set to a built image tag by the dedicated docker CI job",
    ),
]


def image() -> str:
    return os.environ[IMAGE_ENV]


def _skip_if_daemon_unreachable(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0 and re.search(
        r"cannot connect to the docker daemon|daemon is not running|daemon.*not.*respond",
        result.stderr,
        re.IGNORECASE,
    ):
        pytest.skip("no docker daemon reachable on this runner")


def run(
    *args: str, network: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "run", "--rm"]
    if network is not None:
        cmd += ["--network", network]
    cmd += list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        pytest.skip("docker is not installed on this runner")
    _skip_if_daemon_unreachable(result)
    return result


def pyproject_version() -> str:
    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return meta["project"]["version"]


# --------------------------------------------------------------------------- R2 / acceptance


def test_the_image_version_matches_the_published_package() -> None:
    result = run(image(), "--version")
    assert result.returncode == 0, result.stdout + result.stderr
    assert pyproject_version() in result.stdout, result.stdout


def test_the_image_lists_workflows_with_the_bare_entrypoint() -> None:
    result = run(image(), "workflows")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bundled" in result.stdout, result.stdout


# --------------------------------------------------------------------------- R1 — bundled binaries


def test_bundled_agent_clis_need_no_network_at_run_time() -> None:
    """Both agent CLIs are pre-warmed at build time — `doctor` finds them with the network cut."""
    result = run(image(), "doctor", "--json", network="none")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    checks = {c["id"]: c for c in report["checks"]}
    for check_id in ("claude.cli", "codex.cli"):
        assert check_id in checks, checks
        assert checks[check_id]["status"] == "ok", checks[check_id]


# --------------------------------------------------------------------------- R5 — non-root user


def test_the_container_runs_as_a_non_root_user_with_a_writable_home() -> None:
    result = run(
        "--entrypoint", "sh", image(), "-c", "id -u && touch ~/.rayspec-probe && echo wrote-ok"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines and lines[0] != "0", f"container ran as root: {result.stdout!r}"
    assert "wrote-ok" in result.stdout, result.stdout


# --------------------------------------------------------------------------- R6 — no credentials


def test_no_credential_material_is_baked_into_the_image() -> None:
    result = run("--entrypoint", "env", image())
    assert result.returncode == 0, result.stdout + result.stderr
    credential_shaped = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)=.+$", re.MULTILINE)
    leaked = credential_shaped.findall(result.stdout)
    assert not leaked, f"credential-shaped variable(s) baked into the image: {leaked}"


# --------------------------------------------------------------------------- Acceptance — offline dry run


def test_a_dry_run_needs_neither_network_nor_credentials(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "smoke.yaml").write_text(
        "rayspec: 1\n"
        "name: smoke\n"
        "agents:\n"
        "  reviewer: { provider: stub, model: small }\n"
        "steps:\n"
        "  - id: review\n"
        "    agent: reviewer\n"
        "    prompt: hello\n",
        encoding="utf-8",
    )
    result = run(
        "-v",
        f"{project}:/work",
        "-w",
        "/work",
        image(),
        "run",
        "smoke",
        "--dry-run",
        "--no-interactive",
        network="none",
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- R4 — multi-arch


def test_the_pushed_image_is_multi_arch() -> None:
    result = subprocess.run(
        ["docker", "manifest", "inspect", image()], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        pytest.skip(
            f"no manifest to inspect for {image()!r} on this runner: {result.stderr.strip()}"
        )
    manifest = json.loads(result.stdout)
    platforms = {
        f"{entry['platform']['os']}/{entry['platform']['architecture']}"
        for entry in manifest.get("manifests", [])
    }
    assert {"linux/amd64", "linux/arm64"} <= platforms, platforms
