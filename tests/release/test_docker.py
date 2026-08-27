# SPDX-License-Identifier: Apache-2.0
"""PRD-08 — the Docker image: the ``Dockerfile`` and the release workflow's ``docker`` job.

Boundary: static analysis only — the Dockerfile is read as text, ``release.yml`` as YAML. No
image is built and no container is run here; that is ``tests/release/test_docker_image.py`` (the
``docker``-marked suite, gated on a pre-built image tag the dedicated CI job supplies — see that
file's header for why).

Neither ``Dockerfile`` nor a ``docker`` job in ``release.yml`` exists yet, so every test below
fails against the current tree: a missing file, a missing job, or a missing instruction — never a
collection error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README = REPO_ROOT / "README.md"

#: The exact gate `publish` and `announce` already use in `release.yml` — a `docker` job that
#: pushes has to be held to the same tag-only, push-event-only bar.
PUBLISH_GATE = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"

#: What marks a job in `release.yml` as the one that builds/pushes the image, independent of
#: whatever name it is eventually given.
DOCKER_JOB_MARKERS = ("buildx", "build-push-action", "login-action", "setup-qemu-action")


def dockerfile_text() -> str:
    assert DOCKERFILE.is_file(), "no Dockerfile at the repository root"
    return DOCKERFILE.read_text(encoding="utf-8")


def dockerfile_lines() -> list[str]:
    return dockerfile_text().splitlines()


def release_workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def docker_job_name_and_spec() -> tuple[str, dict[str, Any]]:
    jobs = release_workflow().get("jobs", {})
    candidates = {
        name: spec
        for name, spec in jobs.items()
        if "docker" in name.lower()
        or "image" in name.lower()
        or any(
            marker in str(step.get("uses", ""))
            for step in spec.get("steps", [])
            for marker in DOCKER_JOB_MARKERS
        )
    }
    assert candidates, "release.yml has no job that builds/pushes the Docker image"
    assert len(candidates) == 1, f"expected exactly one docker job, found {sorted(candidates)}"
    return next(iter(candidates.items()))


def docker_job() -> dict[str, Any]:
    return docker_job_name_and_spec()[1]


def steps_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return list(spec.get("steps", []))


# --------------------------------------------------------------------------- R1 — contents


def test_dockerfile_exists_at_the_repo_root() -> None:
    assert DOCKERFILE.is_file()


def test_dockerfile_bases_on_a_slim_python_311_or_newer() -> None:
    from_lines = [line for line in dockerfile_lines() if line.strip().upper().startswith("FROM")]
    assert from_lines, "no FROM instruction"
    match = re.search(r"python:(\d+)\.(\d+)[.\w-]*-slim", from_lines[0])
    assert match, f"FROM line is not a slim Python base: {from_lines[0]!r}"
    major, minor = int(match.group(1)), int(match.group(2))
    assert (major, minor) >= (3, 11), f"base image is older than 3.11: {from_lines[0]!r}"


def test_dockerfile_installs_uv() -> None:
    text = dockerfile_text()
    installs_uv = (
        "COPY --from=ghcr.io/astral-sh/uv" in text
        or "pip install uv" in text
        or ("astral.sh/uv/install.sh" in text)
    )
    assert installs_uv, "no step installs uv"


def test_dockerfile_installs_rayspec_at_build_time() -> None:
    """Not left for the container to fetch on first run — the image build installs it."""
    text = dockerfile_text()
    assert re.search(r"\brayspec\b", text), "rayspec is never mentioned"
    installs = "pip install" in text or "uv pip install" in text or "uv sync" in text
    assert installs, "nothing installs rayspec during the build"


def test_dockerfile_installs_both_agent_sdks_so_their_binaries_are_bundled_at_build_time() -> None:
    """Bundled at build time, never fetched at run time — R1's whole point.

    There is no separate binary-download step to look for: `claude-agent-sdk` and `openai-codex`
    carry their CLIs as data inside their wheels, so landing both packages in site-packages
    during the image build IS the bundling step.
    """
    text = dockerfile_text()
    assert "claude-agent-sdk" in text, "claude-agent-sdk is not installed at build time"
    assert "openai-codex" in text, "openai-codex is not installed at build time"


def test_dockerfile_installs_git_gh_and_jq() -> None:
    lines = dockerfile_lines()
    install_lines = " ".join(line for line in lines if "install" in line.lower())
    assert install_lines, "no install step of any kind"
    for tool in ("git", "gh", "jq"):
        assert re.search(rf"(?<![\w.-]){tool}(?![\w.-])", install_lines), (
            f"no install step mentions {tool}"
        )


# --------------------------------------------------------------------------- R2 — entrypoint


def test_dockerfile_entrypoint_is_bare_rayspec() -> None:
    entrypoints = [
        line.strip() for line in dockerfile_lines() if line.strip().upper().startswith("ENTRYPOINT")
    ]
    assert entrypoints, "no ENTRYPOINT instruction"
    assert entrypoints[-1] == 'ENTRYPOINT ["rayspec"]', entrypoints[-1]


# --------------------------------------------------------------------------- R5 — non-root user


def test_dockerfile_runs_as_a_non_root_user_with_an_explicit_fixed_uid() -> None:
    lines = dockerfile_lines()
    user_lines = [line.strip() for line in lines if line.strip().upper().startswith("USER")]
    assert user_lines, "no USER instruction"
    value = user_lines[-1].split(None, 1)[1].strip()
    assert value not in ("root", "0"), f"final USER is {value!r}"

    creation_lines = [line for line in lines if re.search(r"\b(useradd|adduser)\b", line)]
    assert creation_lines, "no useradd/adduser step creates the runtime user"
    uid_match = re.search(r"(?:-u|--uid)\s+(\d+)", " ".join(creation_lines))
    assert uid_match, "the user is created without an explicit numeric UID"


def test_the_fixed_uid_is_documented_for_volume_mount_permissions() -> None:
    dockerfile = dockerfile_text()
    creation_line = next(
        (line for line in dockerfile.splitlines() if re.search(r"\b(useradd|adduser)\b", line)),
        None,
    )
    assert creation_line, "no useradd/adduser line to read the UID from"
    uid_match = re.search(r"(?:-u|--uid)\s+(\d+)", creation_line)
    assert uid_match, "no explicit UID on the useradd/adduser line"
    uid = uid_match.group(1)

    comment_lines = [line for line in dockerfile.splitlines() if line.strip().startswith("#")]
    documented_in_dockerfile = any(uid in line for line in comment_lines)
    documented_in_readme = README.is_file() and uid in README.read_text(encoding="utf-8")
    assert documented_in_dockerfile or documented_in_readme, (
        f"UID {uid} is not documented in a Dockerfile comment or in README.md, so nobody can "
        "match it when setting volume-mount permissions"
    )


# --------------------------------------------------------------------------- R6 — no credentials


def test_dockerfile_bakes_in_no_credential_shaped_env_or_arg() -> None:
    credential_name = re.compile(r"\b[A-Z][A-Z0-9_]*_(API_KEY|TOKEN|SECRET)\b")
    for line in dockerfile_lines():
        stripped = line.strip()
        if not re.match(r"^(ENV|ARG)\s", stripped, re.IGNORECASE):
            continue
        assert not credential_name.search(stripped), f"credential-shaped value baked in: {stripped}"


def test_dockerfile_does_not_copy_a_credentials_or_env_file() -> None:
    forbidden = re.compile(r"\.env\b|credentials|secrets\.", re.IGNORECASE)
    for line in dockerfile_lines():
        stripped = line.strip()
        if not re.match(r"^(COPY|ADD)\s", stripped, re.IGNORECASE):
            continue
        assert not forbidden.search(stripped), f"copies a credential-shaped file: {stripped}"


# --------------------------------------------------------------------------- R3 — tags


def test_docker_job_needs_the_build_job() -> None:
    _name, spec = docker_job_name_and_spec()
    needs = spec.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert "build" in needs_list, f"docker job does not depend on build: needs={needs!r}"


def test_docker_job_derives_its_tag_from_the_build_jobs_version_output() -> None:
    spec = docker_job()
    text = str(spec)
    assert "needs.build.outputs.version" in text, (
        "the tag is not derived from build's version output"
    )
    assert "pyproject.toml" not in text, "the docker job re-parses pyproject.toml for the version"


def test_docker_job_only_tags_latest_for_a_final_release() -> None:
    spec = docker_job()
    text = str(spec)
    assert re.search(r"[:/]latest\b", text) or "latest" in text, "no `latest` tag is ever pushed"
    assert "needs.build.outputs.prerelease" in text, "the `latest` tag is not gated on prerelease"


def test_docker_job_never_uses_a_dev_or_edge_tag() -> None:
    text = str(docker_job())
    for forbidden in (":dev", "-dev", ":edge", "-edge"):
        assert forbidden not in text, f"docker job references a {forbidden!r} tag"


# --------------------------------------------------------------------------- R4 — multi-arch


def test_docker_job_sets_up_qemu_and_buildx() -> None:
    uses = [str(step.get("uses", "")) for step in steps_of(docker_job())]
    assert any("setup-qemu-action" in u for u in uses), "no QEMU setup step"
    assert any("setup-buildx-action" in u for u in uses), "no buildx setup step"


def test_docker_job_builds_for_both_amd64_and_arm64() -> None:
    build_steps = [
        s for s in steps_of(docker_job()) if "build-push-action" in str(s.get("uses", ""))
    ]
    assert build_steps, "no docker/build-push-action step"
    platforms = {
        p.strip()
        for s in build_steps
        for p in str((s.get("with") or {}).get("platforms", "")).split(",")
    }
    assert {"linux/amd64", "linux/arm64"} <= platforms, platforms


# --------------------------------------------------------------------------- R7 — published from CI


def test_docker_job_builds_from_the_wheel_artifact_the_build_job_produced() -> None:
    """Parallel with `publish`, not a PyPI round-trip: it downloads the artifact `build` already
    uploaded under the name `dist` (see `release.yml`'s `upload-artifact` step), the same way
    `publish` and `announce` do."""
    download_steps = [
        s for s in steps_of(docker_job()) if "download-artifact" in str(s.get("uses", ""))
    ]
    assert download_steps, "the docker job never downloads the dist artifact"
    names = {(s.get("with") or {}).get("name") for s in download_steps}
    assert "dist" in names, f"expected to download the 'dist' artifact, found {names}"


def test_docker_job_publishes_to_ghcr_with_no_stored_registry_credential() -> None:
    _name, spec = docker_job_name_and_spec()
    text = str(spec)
    assert "ghcr.io/rayspec-labs/rayspec" in text, "the image is not published to GHCR"
    assert "docker.io" not in text and "hub.docker.com" not in text, "a second registry is used"
    assert (spec.get("permissions") or {}).get("packages") == "write", (
        f"job does not grant itself packages: write, permissions={spec.get('permissions')!r}"
    )
    for forbidden in ("DOCKERHUB_TOKEN", "DOCKER_PASSWORD", "DOCKER_USERNAME"):
        assert forbidden not in text, f"a stored registry credential is referenced: {forbidden}"


def test_docker_job_pushes_under_the_same_gate_publish_and_announce_use() -> None:
    workflow = release_workflow()
    for name in ("publish", "announce"):
        assert PUBLISH_GATE in str(workflow["jobs"][name].get("if", "")), (
            f"{name} no longer uses the shared publish gate — the fixture assumption broke"
        )

    spec = docker_job()
    push_related = [
        s
        for s in steps_of(spec)
        if "login-action" in str(s.get("uses", ""))
        or str((s.get("with") or {}).get("push", "")).strip() not in ("", "false")
    ]
    assert push_related, "nothing in the docker job logs in to or pushes the registry"
    gated = " ".join(
        str(s.get("if", "")) + str((s.get("with") or {}).get("push", "")) for s in push_related
    )
    assert PUBLISH_GATE in gated, (
        f"the docker job's push is not gated the way publish/announce are: {gated!r}"
    )


def test_docker_job_still_builds_on_a_rehearsal_dispatch() -> None:
    """`workflow_dispatch` validates the build; only the irreversible push is skipped — the same
    rehearsal-vs-publish split `build`/`publish` already draw for PyPI."""
    _name, spec = docker_job_name_and_spec()
    job_if = str(spec.get("if", ""))
    assert PUBLISH_GATE not in job_if, (
        "the whole docker job is gated on a pushed tag, so a workflow_dispatch rehearsal "
        "never even builds the image to validate it"
    )


# ------------------------------------------------------- acceptance criteria run in CI (the gap)


def test_ci_builds_the_image_and_runs_the_docker_acceptance_suite() -> None:
    """The `docker`-marked suite is R1-R6 as executable acceptance tests, but it skips everywhere
    for want of a built image (`RAYSPEC_DOCKER_IMAGE`). One CI job must build the image with
    `load: true` and run `pytest -m docker` against it — otherwise the criteria are never verified
    and the image is green by skipping. This is the pull-request-time counterpart to the release
    job's own multi-arch build (which pushes but cannot load a multi-arch image to smoke-test it).
    """
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8")).get("jobs", {})
    for _name, spec in jobs.items():
        builds_loaded = any(
            "build-push-action" in str(step.get("uses", ""))
            and str((step.get("with") or {}).get("load")).lower() == "true"
            for step in spec.get("steps", [])
        )
        job_text = yaml.safe_dump(spec)
        runs_suite = "pytest -m docker" in job_text and "RAYSPEC_DOCKER_IMAGE" in job_text
        if builds_loaded and runs_suite:
            return
    raise AssertionError(
        "no CI job builds the image with `load: true` and runs `pytest -m docker` with "
        "RAYSPEC_DOCKER_IMAGE set — the docker acceptance suite skips on every runner"
    )
