"""The workflow files under ``.github/workflows`` — read as YAML and as text, never executed.

Boundary: no runner, no network. These pin the parts of the release pipeline that cannot be
tried out before the repository is public, and that are unsafe to discover on release day:
nothing carries a long-lived credential, every third-party action is pinned to a commit, no
``run:`` block interpolates a value into a shell, and the reusable dry-run workflow makes no
assumption about which repository calls it. Two of the embedded scripts (the tag/version guard
and the pull-request comment) are lifted out of the YAML and run here against real input.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS_DIR / "release.yml"
RECIPE = WORKFLOWS_DIR / "rayspec-dry-run.yml"
DOCS = WORKFLOWS_DIR / "docs.yml"
CI = WORKFLOWS_DIR / "ci.yml"

#: ``owner/repo@<40 hex>`` — a tag or a branch is a moving target, a commit is not.
PINNED = r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$"


def load(path: Path) -> dict[Any, Any]:
    """One workflow as plain data (``on:`` survives YAML 1.1's boolean, see ``_on``)."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The trigger block. YAML 1.1 reads a bare ``on:`` key as the boolean ``True``."""
    triggers: Any = workflow["on"] if "on" in workflow else workflow[True]
    assert isinstance(triggers, dict), "the triggers of a workflow are a mapping"
    return triggers


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))


def steps_of(workflow: dict[Any, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(job, step) for job, spec in workflow["jobs"].items() for step in spec.get("steps", [])]


def step_with_id(workflow: dict[Any, Any], step_id: str) -> dict[str, Any]:
    matches = [step for _job, step in steps_of(workflow) if step.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step with id {step_id!r}, found {len(matches)}"
    return matches[0]


def heredoc(script: str, marker: str) -> str:
    """The body of a ``<<'MARKER'`` heredoc inside a ``run:`` block, already dedented by YAML."""
    _before, sep, rest = script.partition(f"<<'{marker}'\n")
    assert sep, f"no <<'{marker}' heredoc in the step"
    body, sep, _after = rest.partition(f"\n{marker}\n")
    assert sep, f"unterminated {marker} heredoc"
    return body + "\n"


def test_the_pipeline_is_all_there() -> None:
    for path in (CI, RELEASE, RECIPE, DOCS):
        assert path.is_file(), f"missing workflow {path.name}"


def test_every_workflow_parses_and_has_jobs() -> None:
    for path in workflow_files():
        workflow = load(path)
        assert workflow.get("jobs"), f"{path.name} has no jobs"
        assert _on(workflow), f"{path.name} has no triggers"


def test_every_action_is_pinned_to_a_commit() -> None:
    """A tag can be moved onto another commit; a release pipeline may not depend on one."""
    problems = []
    for path in workflow_files():
        for _job, step in steps_of(load(path)):
            uses = step.get("uses")
            if uses is None or uses.startswith("./"):
                continue
            import re

            if not re.match(PINNED, uses):
                problems.append(f"{path.name}: {uses} is not pinned to a commit sha")
    assert not problems, "\n".join(problems)


def test_every_pinned_action_says_which_version_it_is() -> None:
    """A bare sha is unreadable and never gets updated — the version goes in a comment."""
    import re

    problems = []
    for path in workflow_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "uses:" in line and "@" in line and not line.strip().startswith("#"):
                if re.search(r"@[0-9a-f]{40}\s*#\s*v?\d", line):
                    continue
                if "./" in line:
                    continue
                problems.append(f"{path.name}: {line.strip()}")
    assert not problems, "\n".join(problems)


def test_no_workflow_carries_a_long_lived_credential() -> None:
    """Publishing is Trusted Publishing: an OIDC exchange, not a token in a secret."""
    forbidden = ("PYPI_API_TOKEN", "TWINE_PASSWORD", "TWINE_USERNAME", "password:", "api-token")
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} mentions {needle}"


def test_the_only_secret_any_workflow_reads_is_the_job_token() -> None:
    import re

    used = {
        name
        for path in workflow_files()
        for name in re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", path.read_text("utf-8"))
    }
    assert used <= {"GITHUB_TOKEN"}, f"workflows read secrets other than the job token: {used}"


def test_no_run_block_interpolates_a_value_into_the_shell() -> None:
    """``${{ … }}`` inside ``run:`` is string substitution before bash ever sees it.

    Everything a script needs arrives through ``env:``, where a value stays a value.
    """
    problems = [
        f"{path.name}: {job}: {step.get('name', step.get('id', '?'))}"
        for path in workflow_files()
        for job, step in steps_of(load(path))
        if "${{" in str(step.get("run", ""))
    ]
    assert not problems, "\n".join(problems)


def test_every_workflow_that_starts_itself_starts_from_read_only_permissions() -> None:
    """A job that needs more says so; the file it lives in never begins with it.

    A reusable workflow is the exception and is pinned separately below: it has no token of its
    own to narrow.
    """
    for path in workflow_files():
        workflow = load(path)
        if "workflow_call" in _on(workflow):
            continue
        declared = workflow.get("permissions")
        assert declared is not None, f"{path.name} does not declare top-level permissions"
        writable = [scope for scope, level in declared.items() if level not in {"read", "none"}]
        assert not writable, f"{path.name} starts with write access to {writable}"


def test_ci_lints_the_workflow_files() -> None:
    """A misspelled ``steps.<id>.outputs`` is valid YAML and passes every test in this file.

    These workflows cannot be tried out before they are needed, so the one tool that reads them
    the way the runner does has to run on every pull request rather than on someone's laptop.
    """
    linting = [
        name
        for name, spec in load(CI)["jobs"].items()
        if any("actionlint" in str(step.get("run", "")) for step in spec.get("steps", []))
    ]
    assert linting, "nothing lints the workflow files: a typo in an expression reaches the runner"


def test_the_workflow_linter_is_pinned_to_a_version_and_its_bytes_are_checked() -> None:
    """It is fetched rather than used as an action, so the pin is a version plus a checksum."""
    [step] = [s for _job, s in steps_of(load(CI)) if "actionlint" in str(s.get("run", ""))]
    env = step.get("env") or {}
    assert "ACTIONLINT_VERSION" in env, "the linter is not pinned to a version"
    assert "sha256sum -c" in step["run"], "the download is not verified against a checksum"


def test_ci_installs_from_the_lockfile_wherever_it_gates_a_change() -> None:
    """A ``uv.lock`` that has drifted from ``pyproject.toml`` fails the build, silently never.

    One job resolves afresh on purpose: the floors in ``pyproject.toml`` are what a user's
    ``pip install`` reads, and no locked cell can say what they pull. The exemption is a property
    rather than a name on a list, because a list is exactly how this would come loose: a job that
    does not install from the lock has to be kept off pull requests, so it can never be the thing
    that says a change is fine. A job that resolves afresh AND gates a pull request fails here
    whatever it is called.
    """
    problems = []
    for path in workflow_files():
        workflow = load(path)
        for job, spec in workflow["jobs"].items():
            for step in spec.get("steps", []):
                for line in str(step.get("run", "")).splitlines():
                    command = line.strip()
                    resolves = command.startswith("uv sync") or (
                        "uv run" in command and "--group" in command
                    )
                    if not resolves or "--locked" in command or "--frozen" in command:
                        continue
                    if "pull_request" not in str(spec.get("if", "")):
                        problems.append(
                            f"{path.name}: job {job!r} runs {command!r}, which resolves afresh, "
                            "and nothing keeps it off pull requests"
                        )
    assert not problems, "\n".join(problems)


#: What makes ``uv run`` use the environment as it stands instead of deriving one of its own.
KEEPS_THE_ENVIRONMENT = ("--no-sync", "--no-project", "--isolated")


def test_no_step_re_derives_the_environment_a_sync_step_already_built() -> None:
    """Once a job has run ``uv sync``, that environment is the one its later steps must get.

    A bare ``uv run`` builds the environment again from scratch: it re-reads ``.python-version``
    and syncs to the DEFAULT groups, and where the interpreter disagrees it deletes ``.venv`` and
    recreates it. A job that syncs one interpreter with every group and then runs a bare
    ``uv run`` is checking something other than what it asked for — which is how a four-
    interpreter matrix came to run 3.12 in every cell, each of them missing the docs group and so
    unable to type-check ``scripts/mkdocs_hooks.py``.

    Either the job says the environment is already built (``UV_NO_SYNC``), or the step says so
    itself. A job with no sync step is not covered: there ``uv run`` IS how the environment is
    built, which is what ``docs.yml`` does on purpose.
    """
    problems = []
    for path in workflow_files():
        workflow = load(path)
        for job, spec in workflow["jobs"].items():
            synced = False
            for step in spec.get("steps") or []:
                env: dict[str, Any] = {}
                for source in (workflow.get("env"), spec.get("env"), step.get("env")):
                    env.update(source or {})
                for line in str(step.get("run", "")).splitlines():
                    command = line.strip()
                    words = command.split()
                    if words[:2] == ["uv", "sync"]:
                        synced = True
                    elif words[:2] == ["uv", "run"] and synced:
                        if any(flag in command for flag in KEEPS_THE_ENVIRONMENT):
                            continue
                        if str(env.get("UV_NO_SYNC", "")).strip():
                            continue
                        problems.append(f"{path.name}: {job}: {command}")
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- release.yml


def test_the_release_workflow_only_fires_on_a_version_tag() -> None:
    triggers = _on(load(RELEASE))
    assert set(triggers) <= {"push", "workflow_dispatch"}, triggers
    tags = triggers["push"]["tags"]
    assert tags and all(tag.startswith("v") for tag in tags), tags
    assert "branches" not in triggers["push"], "a branch push must never publish"


def test_the_release_workflow_builds_a_sdist_and_a_wheel() -> None:
    runs = " ".join(str(step.get("run", "")) for _job, step in steps_of(load(RELEASE)))
    assert "uv build" in runs
    assert "--sdist" in runs and "--wheel" in runs


def test_the_release_workflow_refuses_a_tag_that_disagrees_with_the_version() -> None:
    guard = step_with_id(load(RELEASE), "version")
    assert "pyproject.toml" in guard["run"]


def test_the_release_workflow_publishes_through_trusted_publishing() -> None:
    workflow = load(RELEASE)
    jobs = {
        name: spec
        for name, spec in workflow["jobs"].items()
        if any("gh-action-pypi-publish" in str(step.get("uses", "")) for step in spec["steps"])
    }
    assert len(jobs) == 1, f"expected exactly one publishing job, found {list(jobs)}"
    [(name, spec)] = jobs.items()
    assert spec["permissions"]["id-token"] == "write", f"{name} cannot mint an OIDC token"
    assert spec.get("environment"), f"{name} publishes outside a protected environment"
    for step in spec["steps"]:
        if "gh-action-pypi-publish" in str(step.get("uses", "")):
            assert "password" not in (step.get("with") or {})


def test_the_release_workflow_signs_the_artefacts_with_sigstore() -> None:
    workflow = load(RELEASE)
    signing = [
        (job, spec)
        for job, spec in workflow["jobs"].items()
        if any("sigstore" in str(step.get("uses", "")) for step in spec["steps"])
    ]
    assert signing, "nothing signs the artefacts"
    for job, spec in signing:
        assert spec["permissions"]["id-token"] == "write", f"{job} cannot sign"


def test_the_release_workflow_produces_a_bill_of_materials() -> None:
    runs = " ".join(str(step.get("run", "")) for _job, step in steps_of(load(RELEASE)))
    assert "cyclonedx" in runs, "no SBOM is generated"


def test_the_release_notes_are_the_changelog_section() -> None:
    runs = " ".join(str(step.get("run", "")) for _job, step in steps_of(load(RELEASE)))
    assert "scripts/release_notes.py" in runs


def test_the_release_workflow_names_the_steps_that_are_still_manual() -> None:
    """A person still has to move `v1` and roll the changelog; the run summary has to say so.

    This used to pin the PyPI yank instead. That was correct until 1.0.0: the `rayspec` name was
    parked with a 0.0.1 placeholder that a person had to yank, and the run had to say so. The
    placeholder was yanked on release day and there is not another one, so pinning it would hold
    the workflow to a step that can never apply again. What stays manual every release is the
    tag move and the changelog roll-over, and those are what this asserts now.
    """
    # The summary is echoed from a shell step, so its backticks are backslash-escaped in the YAML.
    text = RELEASE.read_text(encoding="utf-8").lower().replace("\\`", "`")
    assert "move the `v1` tag" in text, "the summary must name the tag move"
    assert "`## [unreleased]`" in text, "the summary must name the changelog roll-over"


def publishing_jobs() -> list[tuple[str, dict[str, Any]]]:
    """The two jobs nobody can take back: the PyPI upload and the GitHub release."""
    return [
        (name, spec)
        for name, spec in load(RELEASE)["jobs"].items()
        if any(
            "gh-action-pypi-publish" in str(step.get("uses", ""))
            or "gh release create" in str(step.get("run", ""))
            for step in spec["steps"]
        )
    ]


def test_the_page_and_the_run_summary_count_the_same_manual_steps() -> None:
    """A release-day checklist that under-counts itself is how a step gets skipped.

    ``docs/ci.md`` is what a person reads before a release; the run's summary is what actually
    prints when one finishes. They may not disagree about how much is left to do by hand.
    """
    import re

    [summary] = [
        step
        for _job, step in steps_of(load(RELEASE))
        if "GITHUB_STEP_SUMMARY" in str(step.get("run", ""))
    ]
    numbered = re.findall(r'echo "(\d+)\.', str(summary["run"]))
    assert numbered, "the run's summary lists nothing that stays manual"

    page = (REPO_ROOT / "docs" / "ci.md").read_text(encoding="utf-8")
    counted = {
        1: "One thing stays manual",
        2: "Two things stay manual",
        3: "Three things stay manual",
    }
    assert counted[len(numbered)] in page, f"the page does not say {counted[len(numbered)]!r}"
    [paragraph] = [block for block in page.split("\n\n") if "stay manual" in block]
    assert "`v1`" in paragraph, "the tag every foreign repository follows is not in the checklist"


def test_publishing_only_happens_for_a_pushed_tag() -> None:
    """``workflow_dispatch`` is the rehearsal: everything except the irreversible half.

    A dispatch can name a tag (``gh workflow run release.yml --ref v0.9.0``), so a gate that
    only reads ``github.ref`` is a gate the person dispatching can step around.
    """
    jobs = publishing_jobs()
    assert jobs, "nothing in this workflow publishes"
    for name, spec in jobs:
        gate = str(spec.get("if", ""))
        assert "refs/tags/v" in gate, f"{name} runs without a tag"
        assert "event_name" in gate and "push" in gate, f"{name} publishes on a dispatch: {gate}"


def test_a_prerelease_is_published_as_one() -> None:
    """PyPI reads the version string; GitHub does not, so the flag has to be passed."""
    [(_name, spec)] = [
        (name, spec)
        for name, spec in load(RELEASE)["jobs"].items()
        if any("gh release create" in str(step.get("run", "")) for step in spec["steps"])
    ]
    [step] = [s for s in spec["steps"] if "gh release create" in str(s.get("run", ""))]
    assert "--prerelease" in step["run"], "a release candidate would become the latest release"
    assert "prerelease" in str(step.get("env", {})), "the flag is not derived from the version"


def test_the_version_guard_accepts_a_matching_tag_and_refuses_anything_else(
    tmp_path: Path,
) -> None:
    """The guard is release-day-only code, so it is exercised here instead."""
    script = tmp_path / "guard.py"
    script.write_text(heredoc(step_with_id(load(RELEASE), "version")["run"], "PY"), "utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.3.4"\n', "utf-8")
    outputs = tmp_path / "outputs.txt"

    def guard(ref: str, ref_type: str = "tag") -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "REF_NAME": ref,
            "REF_TYPE": ref_type,
            "GITHUB_OUTPUT": str(outputs),
        }
        return subprocess.run(
            [sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True
        )

    ok = guard("v2.3.4")
    assert ok.returncode == 0, ok.stderr
    assert "version=2.3.4" in outputs.read_text(encoding="utf-8")

    bad = guard("v2.3.5")
    assert bad.returncode != 0
    assert "2.3.4" in bad.stdout + bad.stderr and "2.3.5" in bad.stdout + bad.stderr

    rehearsal = guard("main", ref_type="branch")
    assert rehearsal.returncode == 0, "a rehearsal off a branch has no tag to agree with"

    # A dispatch can be pointed at a tag, and then the tag still has to describe the build.
    dispatched = guard("v2.3.5")
    assert dispatched.returncode != 0, "a rehearsal on a mismatched tag is not a rehearsal"


def test_the_version_guard_says_whether_the_version_is_a_prerelease(tmp_path: Path) -> None:
    """`gh release create` has no idea that ``1.0.0rc1`` is not the latest release."""
    script = tmp_path / "guard.py"
    script.write_text(heredoc(step_with_id(load(RELEASE), "version")["run"], "PY"), "utf-8")

    def outputs_for(version: str) -> str:
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nversion = "{version}"\n', "utf-8"
        )
        out = tmp_path / f"outputs-{version}.txt"
        env = {
            **os.environ,
            "REF_NAME": f"v{version}",
            "REF_TYPE": "tag",
            "GITHUB_OUTPUT": str(out),
        }
        done = subprocess.run(
            [sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return out.read_text(encoding="utf-8")

    assert "prerelease=false" in outputs_for("2.3.4")
    assert "prerelease=false" in outputs_for("2.3.4.post1")
    for candidate in ("2.3.4rc1", "2.3.4b2", "2.3.4a1", "2.4.0.dev3"):
        assert "prerelease=true" in outputs_for(candidate), candidate


# ------------------------------------------------------------------ rayspec-dry-run.yml


def test_the_recipe_is_a_reusable_workflow_with_the_inputs_a_stranger_needs() -> None:
    call = _on(load(RECIPE))["workflow_call"]
    assert call["inputs"]["workflow"]["required"] is True
    for name in ("project-dir", "stubs", "rayspec-version", "comment"):
        assert name in call["inputs"], f"the recipe has no {name} input"
    assert call["inputs"]["project-dir"]["default"] == "."


def test_the_recipe_assumes_nothing_about_the_repository_that_calls_it() -> None:
    """It is the marketplace-facing artefact: it must work in a tree that is not this one."""
    text = RECIPE.read_text(encoding="utf-8")
    for local in (".rayspec/workflows/", "scripts/", "examples/", "uv sync", "pyproject.toml"):
        assert local not in text, f"the recipe depends on this repository's {local}"
    assert "uv tool install" in text, "the recipe must install the published package"


def test_the_recipe_never_starts_a_real_agent() -> None:
    """A pull-request check may not spend tokens: every invocation is a dry run."""
    runs = [str(step.get("run", "")) for _job, step in steps_of(load(RECIPE))]
    invoking = [run for run in runs if "rayspec run" in run]
    assert invoking, "the recipe never runs anything"
    for run in invoking:
        assert "--dry-run" in run, run
        conditional = [line for line in run.splitlines() if "--dry-run" in line and "if " in line]
        assert not conditional, f"the dry run must not be conditional: {conditional}"
    assert any("--no-interactive" in run for run in runs), "a gate must never wait for a person"


def test_the_recipe_asks_its_caller_for_no_permission_of_its_own() -> None:
    """In a called workflow a ``permissions:`` block is a request, not a narrowing.

    A caller whose job token does not already carry what the called workflow asks for has the
    call rejected before any job starts — the opposite of what this file documents, which is to
    report into the job summary and say why the comment is missing. Declaring nothing leaves the
    token exactly what the caller granted (a called workflow can never hold more) and lets that
    fallback happen.
    """
    workflow = load(RECIPE)
    assert "workflow_call" in _on(workflow), "the recipe is not a reusable workflow"
    assert "permissions" not in workflow, "the recipe requests a permission its caller may not have"
    for name, spec in workflow["jobs"].items():
        assert "permissions" not in spec, f"{name} requests permissions of its own"


def test_the_recipe_says_what_its_caller_has_to_grant() -> None:
    """It is the marketplace-facing file: the caller cannot read the code to find out."""
    header = RECIPE.read_text(encoding="utf-8").split("name: rayspec dry run")[0]
    assert "pull-requests: write" in header


def test_the_recipe_does_not_try_to_comment_where_it_cannot() -> None:
    """A pull request from a fork gets a read-only token: say so, do not fail the check."""
    comment = step_with_id(load(RECIPE), "comment")
    assert "fork" in str(comment["if"]) or "head.repo.full_name" in str(comment["if"])
    assert "pull_request" in str(comment["if"])


def test_the_recipe_edits_its_own_comment_instead_of_adding_one_per_push() -> None:
    comment = step_with_id(load(RECIPE), "comment")
    assert "PATCH" in comment["run"], "the comment is never updated in place"
    assert "MARKER" in comment["run"]


def _post_comment(
    tmp_path: Path,
    comment_tag: str,
    *,
    gh_exit: int = 0,
    gh_said: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the comment step's script with ``gh`` replaced by a stub that records its arguments.

    ``gh_exit``/``gh_said`` are how the failure branch is reached: what ``gh`` writes to stderr is
    the only evidence the step has about why it could not comment.
    """
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    script = work / "comment.sh"
    script.write_text(step_with_id(load(RECIPE), "comment")["run"], encoding="utf-8")
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = work / "gh-calls.txt"
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        f"printf %s {shlex.quote(gh_said)} >&2\n"
        f"exit {gh_exit}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    body = work / "comment.md"
    body.write_text("hello\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REPO": "o/r",
        "NUMBER": "7",
        "BODY": str(body),
        "COMMENT_TAG": comment_tag,
        "MARKER": f"<!-- rayspec-dry-run: {comment_tag} -->",
    }
    done = subprocess.run(
        ["bash", "-e", str(script)], env=env, capture_output=True, text=True, cwd=work
    )
    return done, calls


def test_a_comment_tag_that_would_rewrite_the_jq_program_is_refused(tmp_path: Path) -> None:
    """It is the one value a stranger picks that is spliced into code rather than passed as data.

    A tag carrying a quote or a backslash would rewrite the filter that searches for the marker
    instead of being searched for, so the shape is checked before anything is called.
    """
    done, calls = _post_comment(tmp_path, '" ) | .id // empty] | .[0] | ("')
    assert done.returncode != 0, done.stdout
    assert "comment-tag" in done.stdout + done.stderr
    assert not calls.exists(), "the tag reached the API before it was checked"


def test_an_ordinary_comment_tag_posts(tmp_path: Path) -> None:
    done, calls = _post_comment(tmp_path, "dry-run.1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "issues/7/comments" in calls.read_text(encoding="utf-8")
    assert "::warning::" not in done.stdout, "a comment that posted still warned about something"


def test_only_one_comment_id_survives_pagination() -> None:
    """``--paginate`` applies ``--jq`` once per page, so the filter can emit an id per page.

    A pull request whose marker matches on two pages would then build a URL out of two ids and
    report the failure as a missing permission, which is the wrong diagnosis.
    """
    run = step_with_id(load(RECIPE), "comment")["run"]
    assert "--paginate" not in run or "tail -n1" in run


def test_the_warning_is_a_single_annotation_that_says_where_the_report_is() -> None:
    """GitHub reads one line as the annotation; a second line is an ordinary log line."""
    run = step_with_id(load(RECIPE), "comment")["run"]
    [line] = [item for item in run.splitlines() if "::warning::" in item]
    assert "job summary" in line, "the half that says where to look is not in the annotation"


@pytest.mark.parametrize(
    ("gh_said", "expected"),
    [
        pytest.param(
            "gh: Resource not accessible by integration (HTTP 403)",
            "pull-requests: write",
            id="403-denied",
        ),
        pytest.param(
            "gh: API rate limit exceeded for installation ID 1234. (HTTP 403)",
            "rate limit",
            id="403-rate-limited",
        ),
        pytest.param("gh: Not Found (HTTP 404)", "o/r#7", id="404"),
        pytest.param("dial tcp: lookup api.github.com: no such host", "no such host", id="network"),
        pytest.param("", "network problem", id="silent"),
    ],
)
def test_the_warning_names_what_actually_went_wrong(
    tmp_path: Path, gh_said: str, expected: str
) -> None:
    """One diagnosis for every failure sends the reader after a fix that is not the problem.

    A missing grant is a 403 and the caller can fix it; a 404 is a pull request this token cannot
    see, and a network blip is neither. Blaming ``pull-requests: write`` for all three is how a
    green repository spends an afternoon on its permissions block.

    And a 403 is not by itself a missing grant: a rate limit — primary or secondary — and an
    archived repository carry the same code, so gh's own words have to reach the warning rather
    than be replaced by the one diagnosis the caller has already ruled out.
    """
    done, _calls = _post_comment(tmp_path, "default", gh_exit=1, gh_said=gh_said)
    assert done.returncode == 0, done.stdout + done.stderr
    [line] = [item for item in done.stdout.splitlines() if item.startswith("::warning::")]
    assert expected in line, line
    assert "job summary" in line, line


def test_a_comment_that_could_not_be_posted_does_not_fail_the_step(tmp_path: Path) -> None:
    """The report is in the job summary either way; a missing comment is not a failed check."""
    done, _calls = _post_comment(tmp_path, "default", gh_exit=1, gh_said="whatever")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_recipe_always_writes_the_job_summary() -> None:
    """The comment is a convenience; the run's own summary is the record that always exists."""
    runs = " ".join(str(step.get("run", "")) for _job, step in steps_of(load(RECIPE)))
    assert "GITHUB_STEP_SUMMARY" in runs


#: A workflow that loads: two steps, the second reading the first through a template.
LOADS = """rayspec: 1
name: review
agents:
  reviewer: { provider: stub, model: small }
steps:
  - id: collect
    shell: echo hi
  - id: review
    needs: [collect]
    agent: reviewer
    prompt: 'review {{ steps.collect.output }}'
outputs:
  verdict: '{{ steps.review.output }}'
"""

#: The same workflow after the step a template reads was deleted — what the check exists to catch.
DOES_NOT_LOAD = """rayspec: 1
name: review
agents:
  reviewer: { provider: stub, model: small }
steps:
  - id: review
    agent: reviewer
    prompt: 'review {{ steps.gone.output }}'
"""


def _dry_run(
    tmp_path: Path, home: Path, source: str = LOADS, *, workflow: str = "review"
) -> tuple[Path, int]:
    """A real ``rayspec run --dry-run --json``, split into the two files the recipe writes.

    The recipe sends stdout to the event stream and stderr to the error file, so a test that
    fabricates either one proves nothing about what the CLI actually emits.
    """
    root = tmp_path / "project"
    (root / ".rayspec" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".rayspec" / "workflows" / "review.yaml").write_text(source, encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["run", workflow, "--root", str(root), "--dry-run", "--no-interactive", "--json"],
        env={"RAYSPEC_HOME": str(home)},
    )
    events = tmp_path / "events.jsonl"
    events.write_text(result.stdout, encoding="utf-8")
    (tmp_path / "errors.txt").write_text(result.stderr, encoding="utf-8")
    return events, result.exit_code


def _dry_run_events(tmp_path: Path, home: Path) -> tuple[Path, int]:
    """A real ``rayspec run --dry-run --json`` stream of a workflow that loads."""
    return _dry_run(tmp_path, home)


def _render(tmp_path: Path, events: Path, exit_code: int | str, **extra: str) -> str:
    script = tmp_path / "render.py"
    script.write_text(heredoc(step_with_id(load(RECIPE), "render")["run"], "PY"), "utf-8")
    body = tmp_path / "comment.md"
    errors = tmp_path / "errors.txt"
    if not errors.exists():
        errors.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "RAYSPEC_EVENTS": str(events),
        "RAYSPEC_ERRORS": str(tmp_path / "errors.txt"),
        "RAYSPEC_EXIT": str(exit_code),
        "RAYSPEC_WORKFLOW": "review",
        "RAYSPEC_MARKER": "<!-- rayspec-dry-run: default -->",
        "RAYSPEC_OUTPUT": str(body),
        "RAYSPEC_RUN_URL": "https://github.com/o/r/actions/runs/1",
        **extra,
    }
    done = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return body.read_text(encoding="utf-8")


def test_the_recipe_renders_a_real_dry_run_as_a_comment(tmp_path: Path, home: Path) -> None:
    events, exit_code = _dry_run_events(tmp_path, home)
    assert exit_code == 0, events.read_text(encoding="utf-8")
    comment = _render(tmp_path, events, exit_code)
    assert comment.startswith("<!-- rayspec-dry-run: default -->"), comment
    assert "review" in comment
    assert "succeeded" in comment
    assert "collect" in comment, "the step table is missing a step"
    assert "https://github.com/o/r/actions/runs/1" in comment
    assert len(comment) < 65536, "a GitHub comment cannot be longer than 65536 characters"


def test_the_comment_says_a_dry_run_called_no_agent(tmp_path: Path, home: Path) -> None:
    events, exit_code = _dry_run_events(tmp_path, home)
    comment = _render(tmp_path, events, exit_code)
    assert "--dry-run" in comment and "stub" in comment.lower()


def test_the_comment_reports_a_workflow_that_did_not_load(tmp_path: Path, home: Path) -> None:
    """The one failure the check exists to catch has to reach the person who caused it.

    ``--json`` reports a load failure as an object on **stdout** and leaves stderr empty, so a
    report that only ever quotes stderr renders a red check with no reason in it.
    """
    events, exit_code = _dry_run(tmp_path, home, DOES_NOT_LOAD)
    assert exit_code == 2, events.read_text(encoding="utf-8")
    comment = _render(tmp_path, events, exit_code)
    assert "unknown step 'gone'" in comment, comment
    assert "exit code `2`" in comment
    assert ":x:" in comment, "a workflow that never started is not an open question"


def test_the_comment_reports_a_run_that_never_started(tmp_path: Path, home: Path) -> None:
    """A name that matches no workflow: nothing on stdout, the message on stderr."""
    events, exit_code = _dry_run(tmp_path, home, workflow="nope")
    assert exit_code == 2
    assert not events.read_text(encoding="utf-8").strip(), "a usage error emits no events"
    comment = _render(tmp_path, events, exit_code)
    assert "unknown workflow" in comment
    assert "exit code `2`" in comment
    assert ":x:" in comment, "a workflow that never started is not an open question"


def test_the_comment_never_calls_a_step_that_did_not_run_a_success(tmp_path: Path) -> None:
    """With ``always()`` the report also renders when the run step never produced an exit code."""
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    comment = _render(tmp_path, events, "")
    assert "succeeded" not in comment, comment
    assert ":x:" in comment


def test_the_run_step_puts_the_event_stream_in_the_job_log_when_it_failed() -> None:
    """stdout is redirected into a file, so a failing run leaves an empty job log otherwise."""
    run = step_with_id(load(RECIPE), "run")["run"]
    _redirect, _, after = run.partition("code=$?")
    assert "rayspec-events.jsonl" in after, "the event stream is never shown in the log"
    assert "rayspec-errors.txt" in after


def test_the_report_is_written_even_when_the_run_step_itself_failed() -> None:
    """``working-directory`` is resolved before the script runs: a bad one skips every step."""
    recipe = load(RECIPE)
    assert "always()" in str(step_with_id(recipe, "render").get("if", ""))
    assert "always()" in str(step_with_id(recipe, "comment").get("if", ""))


def test_the_comment_survives_a_truncated_event_stream(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "run.started"}\n{"type": "step.fin', encoding="utf-8")
    comment = _render(tmp_path, events, 1)
    assert comment.strip(), "a half-written stream must still produce a comment"


def test_the_comment_shows_the_outputs_of_the_run(tmp_path: Path, home: Path) -> None:
    events, exit_code = _dry_run_events(tmp_path, home)
    comment = _render(tmp_path, events, exit_code)
    summary = json.loads(
        [line for line in events.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    )
    assert summary["run_id"] in comment


# ------------------------------------------- rayspec-dry-run.yml: the step that reports

#: Every exit code the ``status`` output and ``docs/ci.md`` promise a name for.
DOCUMENTED_OUTCOMES: list[tuple[int, str]] = [
    (0, "succeeded"),
    (1, "failed"),
    (2, "not started (usage error)"),
    (3, "paused"),
    (4, "cancelled"),
    (130, "interrupted"),
]


def _run_step(
    tmp_path: Path,
    exit_code: int,
    *,
    stdout: str = "",
    stderr: str = "",
    stubs: str = "",
    inputs_file: str = "",
    installed: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], Path]:
    """The *Dry-run the workflow* step, started the way the runner starts it.

    A ``run:`` block runs as ``bash -e {0}``: errexit is on before the script's own ``set`` line
    is read, and no ``set`` inside it turns that off. That is not a detail a test may paper over
    — it is what made every line of this step's error path unreachable — so the script is lifted
    out of the YAML and started exactly that way, with ``rayspec`` replaced by a stub that exits
    on command. ``PATH`` holds the stub and the system tools only, so nothing else answers.
    """
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    script = work / "run.sh"
    script.write_text(step_with_id(load(RECIPE), "run")["run"], encoding="utf-8")

    bin_dir = work / "bin"
    bin_dir.mkdir()
    if installed:
        stub = bin_dir / "rayspec"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {shlex.quote(str(work / "rayspec-argv.txt"))}\n'
            'if [ "$1" = "version" ]; then echo "rayspec 1.0.0"; exit 0; fi\n'
            f"printf %s {shlex.quote(stdout)}\n"
            f"printf %s {shlex.quote(stderr)} >&2\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    runner_temp = work / "runner-temp"
    runner_temp.mkdir()
    outputs = work / "github-output.txt"
    outputs.write_text("", encoding="utf-8")
    env = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "WORKFLOW": "review",
        "STUBS": stubs,
        "INPUTS_FILE": inputs_file,
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(outputs),
    }
    done = subprocess.run(
        ["bash", "-e", str(script)], env=env, capture_output=True, text=True, cwd=work
    )
    written: dict[str, str] = {}
    for line in outputs.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            written[key] = value
    return done, written, work


@pytest.mark.parametrize(
    ("code", "status"),
    DOCUMENTED_OUTCOMES,
    ids=[status for _code, status in DOCUMENTED_OUTCOMES],
)
def test_every_documented_outcome_sets_both_documented_outputs(
    tmp_path: Path, code: int, status: str
) -> None:
    """The two outputs are the contract a caller reads instead of the check's colour.

    They existed only for a run that succeeded: any other exit code ended the step at the
    ``rayspec run`` line, before the mapping below it or either ``$GITHUB_OUTPUT`` write.
    """
    done, outputs, _work = _run_step(tmp_path, code)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs.get("exit-code") == str(code), outputs
    assert outputs.get("status") == status, outputs


@pytest.mark.parametrize(
    ("code", "status"),
    DOCUMENTED_OUTCOMES,
    ids=[status for _code, status in DOCUMENTED_OUTCOMES],
)
def test_the_report_and_the_output_call_one_outcome_by_one_name(
    tmp_path: Path, code: int, status: str
) -> None:
    """Two tables turn an exit code into a name: the run step's ``case`` and the report's.

    Different audiences read them — a person reads the comment, a calling workflow reads
    ``status`` — and they drifted: an interrupted run was ``interrupted`` in the comment and
    ``exit 130`` in the output, which reads as two different things having happened.

    The event stream is empty here on purpose. A real run puts its own status in the summary
    event and the report prefers it; the table below is what answers when there is no stream to
    read, which is exactly the case nobody looks at.
    """
    _done, outputs, _work = _run_step(tmp_path, code)
    events = tmp_path / f"events-{code}.jsonl"
    events.write_text("", encoding="utf-8")
    comment = _render(tmp_path, events, code)
    [heading] = [line for line in comment.splitlines() if line.startswith("###")]
    assert heading.endswith(status), heading
    assert outputs.get("status") == status, outputs


def test_the_step_reports_through_an_errexit_shell_instead_of_dying_in_it(tmp_path: Path) -> None:
    """The regression, named: ``set -uo pipefail`` does not disable the ``-e`` in ``bash -e {0}``.

    Everything below the invocation — the exit-code-to-status mapping, both ``$GITHUB_OUTPUT``
    writes and the tail that puts a failed run in the log — was unreachable for every outcome
    except success, which is every outcome this check exists to report.
    """
    script = step_with_id(load(RECIPE), "run")["run"]
    _before, sep, after = script.partition("rayspec run ")
    assert sep, "the step no longer invokes rayspec"
    invocation, sep, _rest = after.partition("\ncase ")
    assert sep, "the exit code is no longer mapped to a status right after the run"
    assert "|| code=$?" in invocation, "a non-zero exit is trusted to fall through an errexit shell"

    done, outputs, _work = _run_step(tmp_path, 1)
    assert outputs, "the step recorded nothing: $GITHUB_OUTPUT is empty for a run that failed"
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_exit_code_nobody_documented_is_still_reported(tmp_path: Path) -> None:
    """An unknown code is a fact about the run, not a reason to report nothing about it."""
    done, outputs, _work = _run_step(tmp_path, 9)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == {"exit-code": "9", "status": "exit 9"}


def test_a_rayspec_that_never_installed_is_an_outcome_like_any_other(tmp_path: Path) -> None:
    """``rayspec version`` must not be the line that decides whether the step reports at all."""
    done, outputs, _work = _run_step(tmp_path, 0, installed=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == {"exit-code": "127", "status": "exit 127"}


def test_a_failed_dry_run_puts_both_streams_in_the_job_log(tmp_path: Path) -> None:
    """stdout is redirected into a file, so a red check leaves an empty log without the tails."""
    done, _outputs, _work = _run_step(
        tmp_path,
        1,
        stdout='{"type": "run.finished", "exit_code": 1}\n',
        stderr="boom: the reason it failed\n",
    )
    assert "boom: the reason it failed" in done.stdout
    assert "--- the end of the event stream" in done.stdout
    assert "run.finished" in done.stdout


def test_a_dry_run_that_succeeded_does_not_dump_its_event_stream_into_the_log(
    tmp_path: Path,
) -> None:
    done, _outputs, _work = _run_step(tmp_path, 0, stdout='{"type": "run.finished"}\n')
    assert "--- the end of the event stream" not in done.stdout


def test_the_step_leaves_the_two_files_the_report_reads(tmp_path: Path) -> None:
    _done, _outputs, work = _run_step(tmp_path, 1, stdout="events\n", stderr="errors\n")
    temp = work / "runner-temp"
    assert (temp / "rayspec-events.jsonl").read_text(encoding="utf-8") == "events\n"
    assert (temp / "rayspec-errors.txt").read_text(encoding="utf-8") == "errors\n"


def test_the_optional_inputs_reach_the_command_line_only_when_they_are_set(
    tmp_path: Path,
) -> None:
    _done, _outputs, work = _run_step(tmp_path, 0)
    _version, run = (work / "rayspec-argv.txt").read_text(encoding="utf-8").splitlines()
    assert run == "run review --dry-run --no-interactive --json"

    _done, _outputs, work = _run_step(tmp_path, 0, stubs="s.yaml", inputs_file="i.yaml")
    _version, run = (work / "rayspec-argv.txt").read_text(encoding="utf-8").splitlines()
    assert run.endswith("--stubs s.yaml --inputs-file i.yaml"), run


def test_only_the_last_step_decides_whether_the_check_fails(tmp_path: Path) -> None:
    """``fail-on-error: false`` can only hand the verdict over if nothing else took it first.

    A run step that reports a failure *by failing* fails the job before the input is ever read,
    which is exactly what the caller asked it not to do.
    """
    [gate] = [
        step for _job, step in steps_of(load(RECIPE)) if "fail-on-error" in str(step.get("if", ""))
    ]
    assert "steps.run.outputs.status != 'succeeded'" in str(gate["if"])
    assert "exit 1" in str(gate["run"])
    for code, _status in DOCUMENTED_OUTCOMES:
        done, _outputs, _work = _run_step(tmp_path, code)
        assert done.returncode == 0, f"exit {code} failed the step before fail-on-error was read"


def test_the_job_and_the_workflow_hand_the_run_steps_outputs_straight_through() -> None:
    """A misspelled ``steps.<id>.outputs`` is valid YAML and reaches the caller as an empty string."""
    recipe = load(RECIPE)
    job = recipe["jobs"]["dry-run"]
    assert job["outputs"] == {
        "status": "${{ steps.run.outputs.status }}",
        "exit-code": "${{ steps.run.outputs.exit-code }}",
    }
    declared = _on(recipe)["workflow_call"]["outputs"]
    assert declared["status"]["value"] == "${{ jobs.dry-run.outputs.status }}"
    assert declared["exit-code"]["value"] == "${{ jobs.dry-run.outputs.exit-code }}"


def test_the_status_output_is_described_by_every_name_it_can_carry() -> None:
    described = str(_on(load(RECIPE))["workflow_call"]["outputs"]["status"]["description"])
    for _code, status in DOCUMENTED_OUTCOMES:
        assert status in described, f"the status output never mentions {status!r}"


def test_a_run_that_did_not_load_reaches_the_report_with_its_exit_code(
    tmp_path: Path, home: Path
) -> None:
    """End to end: a real failing CLI run, through the step, into the report it feeds.

    With the step dying at ``rayspec run`` the exit code never reached the renderer, and the job
    summary of a red check ended in ``exit code `?``` with no status and no reason.
    """
    events, exit_code = _dry_run(tmp_path, home, DOES_NOT_LOAD)
    assert exit_code == 2, events.read_text(encoding="utf-8")

    done, outputs, work = _run_step(tmp_path, exit_code, stdout=events.read_text(encoding="utf-8"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == {"exit-code": "2", "status": "not started (usage error)"}

    comment = _render(tmp_path, work / "runner-temp" / "rayspec-events.jsonl", outputs["exit-code"])
    assert "exit code `2`" in comment, comment
    assert "unknown step 'gone'" in comment, comment


def test_a_document_that_parses_but_does_not_validate_reports_on_stdout(
    tmp_path: Path, home: Path
) -> None:
    """One of the two failure shapes: the reason is an `errors` object on stdout.

    Named for what it checks. A document that does not PARSE is the other shape and reports the
    other way — stdout empty, one `error:` line on stderr — which is why the renderer reads both
    and why the comment beside it says so. A justification that is half true next to the code it
    justifies is how the other half stops being checked.
    """
    events, exit_code = _dry_run(tmp_path, home, DOES_NOT_LOAD)
    assert exit_code == 2
    stdout = events.read_text(encoding="utf-8")
    stderr = (tmp_path / "errors.txt").read_text(encoding="utf-8")
    assert "unknown step 'gone'" in stdout
    assert "unknown step 'gone'" not in stderr
    assert stderr.strip(), "stderr is not empty: every run writes the policy note to it"

    render = heredoc(step_with_id(load(RECIPE), "render")["run"], "PY")
    assert "nothing on stderr" not in render, "the justification claims something that is false"


# --------------------------------------- rayspec-dry-run.yml: what it installs, and from where


def _install_step(
    tmp_path: Path, version: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The *Install rayspec* step with ``uv`` replaced by a stub that records its arguments."""
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    script = work / "install.sh"
    [step] = [s for _job, s in steps_of(load(RECIPE)) if s.get("name") == "Install rayspec"]
    script.write_text(str(step["run"]), encoding="utf-8")

    bin_dir = work / "bin"
    bin_dir.mkdir()
    calls = work / "uv-calls.txt"
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        'if [ "$1" = "tool" ] && [ "$2" = "dir" ]; then echo /opt/uv/bin; fi\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "VERSION": version,
        "GITHUB_PATH": str(work / "github-path.txt"),
    }
    done = subprocess.run(
        ["bash", "-e", str(script)], env=env, capture_output=True, text=True, cwd=work
    )
    recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, recorded


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        pytest.param("1.0.0", "tool install rayspec==1.0.0", id="bare"),
        pytest.param("1.1.0rc1", "tool install rayspec==1.1.0rc1", id="prerelease"),
        pytest.param("v1.0.0", "tool install rayspec==1.0.0", id="tag-shaped"),
        pytest.param(">=1.0.0,<2", "tool install rayspec>=1.0.0,<2", id="range"),
        pytest.param("==1.0.0", "tool install rayspec==1.0.0", id="operator"),
        # A repository variable with a stray space is the accident this guards; `rayspec 1.0.0`
        # is not a specifier and uv refuses it with a message that never mentions whitespace.
        pytest.param(" 1.0.0 ", "tool install rayspec==1.0.0", id="padded"),
        pytest.param(">= 1.0.0, < 2", "tool install rayspec>=1.0.0,<2", id="spaced-range"),
    ],
)
def test_the_version_input_becomes_the_specifier_it_reads_as(
    tmp_path: Path, version: str, expected: str
) -> None:
    done, calls = _install_step(tmp_path, version)
    assert done.returncode == 0, done.stdout + done.stderr
    assert expected in calls, calls


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="one-space"),
        pytest.param("\t", id="tab"),
        pytest.param("  ", id="two-spaces"),
    ],
)
def test_an_empty_version_is_refused_instead_of_resolving_to_whatever_is_newest(
    tmp_path: Path, version: str
) -> None:
    """The ``rayspec`` name was parked on PyPI with a 0.0.1 placeholder before the first release.

    *Latest* therefore had a wrong answer available, and an empty input is how a consumer
    following the documentation installed it — as a passing check that ran a stub.

    Whitespace is the same input wearing a different coat, and the likelier accident of the two:
    a repository variable with a stray space became ``rayspec `` — a name with no specifier,
    which resolves to the latest release — and only the empty string was ever refused.
    """
    done, calls = _install_step(tmp_path, version)
    assert done.returncode != 0, done.stdout
    assert "rayspec-version" in done.stdout + done.stderr
    assert not calls, "PyPI was reached before the input was looked at"


def test_the_default_version_is_the_line_the_v1_ref_follows() -> None:
    """A default meaning "whatever is newest" is not a default; this one names a line."""
    default = str(_on(load(RECIPE))["workflow_call"]["inputs"]["rayspec-version"]["default"])
    assert default == ">=1.0.0,<2", default


def test_the_default_version_installs_without_the_caller_passing_anything(tmp_path: Path) -> None:
    default = str(_on(load(RECIPE))["workflow_call"]["inputs"]["rayspec-version"]["default"])
    done, calls = _install_step(tmp_path, default)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "tool install rayspec>=1.0.0,<2" in calls, calls


def test_the_uv_every_workflow_installs_is_pinned_to_one_exact_version() -> None:
    """Pinning the action pins the action, not the tool it downloads.

    ``uv build`` in ``release.yml`` produces the files that are signed and uploaded, and the
    reusable workflow resolves and installs rayspec with the same tool — neither may be whatever
    was released this morning, and the two may not quietly drift apart either.
    """
    unpinned = []
    versions = set()
    for path in workflow_files():
        for job, step in steps_of(load(path)):
            if "astral-sh/setup-uv" not in str(step.get("uses", "")):
                continue
            pinned = str((step.get("with") or {}).get("version", "")).strip()
            if pinned:
                versions.add(pinned)
            else:
                unpinned.append(f"{path.name}: {job} installs whatever uv is newest")
    assert not unpinned, "\n".join(unpinned)
    assert versions, "no workflow sets up uv"
    assert len(versions) == 1, f"the workflows install different uv versions: {sorted(versions)}"
    [only] = versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", only), f"{only!r} is a range, not a pin"


# ------------------------------------------------ rayspec-dry-run.yml: and what the page says


def _ci_page() -> str:
    return (REPO_ROOT / "docs" / "ci.md").read_text(encoding="utf-8")


def _input_table() -> dict[str, str]:
    """The ``| input | default | meaning |`` table on the page, as ``{input: default}``."""
    rows = {}
    for line in _ci_page().splitlines():
        if line.startswith("| `") and line.count("|") == 4:
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            rows[cells[0].strip("`")] = cells[1]
    return rows


def test_the_page_lists_every_input_the_recipe_takes_with_the_default_it_has() -> None:
    """The table is what a stranger reads instead of the file; a default that drifts from it lies."""
    rows = _input_table()
    for name, spec in _on(load(RECIPE))["workflow_call"]["inputs"].items():
        assert name in rows, f"docs/ci.md does not list the `{name}` input"
        raw = spec.get("default", "")
        default = str(raw).lower() if isinstance(raw, bool) else str(raw)
        if default:
            assert f"`{default}`" in rows[name], (name, default, rows[name])


def test_the_page_names_every_status_the_check_can_report() -> None:
    """``status`` is documented as a promise; the page has to say what it can hold."""
    page = _ci_page()
    for _code, status in DOCUMENTED_OUTCOMES:
        assert status in page, f"docs/ci.md never mentions the `{status}` status"


def test_the_page_says_the_outputs_are_set_whatever_happened() -> None:
    """They were empty for every outcome except success while the page promised otherwise."""
    page = _ci_page()
    [paragraph] = [block for block in page.split("\n\n") if "**Outputs.**" in block]
    assert "`status`" in paragraph and "`exit-code`" in paragraph
    assert "fail-on-error" in paragraph, "the page does not say who decides when they are set"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """``git`` in this repository, never raising — the caller reads the return code."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )


def test_every_ref_the_page_tells_a_stranger_to_call_exists() -> None:
    """The page pinned ``@v1`` while no tag of that name had ever been pushed.

    A ``uses:`` ref that does not resolve is not a slow failure: GitHub rejects the call before any
    job of the caller's workflow starts, so the copy-paste example broke on the reader's first pull
    request while reading as the finished thing. Every ref the page names is resolved against this
    repository — a commit sha or a tag, both of which ``rev-parse`` answers without a network.
    """
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout — the refs the page names cannot be resolved here")
    refs = sorted(set(re.findall(r"uses:\s+rayspec-labs/rayspec-py/\S+@(\S+)", _ci_page())))
    assert refs, "docs/ci.md no longer shows how to call the reusable workflow"
    missing = [
        ref
        for ref in refs
        if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0
    ]
    shallow = _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    hint = " — this checkout is shallow, so it cannot see them either" if shallow else ""
    assert not missing, f"docs/ci.md calls refs this repository does not have: {missing}{hint}"


def test_every_ref_the_page_pins_carries_the_workflow_the_page_describes() -> None:
    """Resolving is not enough: a stale sha resolves perfectly and ships the wrong workflow.

    The sibling test above only asks whether the ref exists. It stayed green when the page was
    re-pinned to a commit predating the workflow entirely, so it could not see a pin left behind by
    a later change to the very file it points at — which is how the page came to promise statuses
    the pinned copy could not report. Compare the bytes instead: whatever ref the page hands a
    stranger must carry the workflow this repository currently ships.
    """
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout — the refs the page names cannot be resolved here")
    here = (REPO_ROOT / ".github" / "workflows" / "rayspec-dry-run.yml").read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r"uses:\s+rayspec-labs/rayspec-py/(\S+)@(\S+)", _ci_page())))
    assert refs, "docs/ci.md no longer shows how to call the reusable workflow"
    stale = []
    for path, ref in refs:
        shown = _git("show", f"{ref}:{path}")
        if shown.returncode != 0 or shown.stdout != here:
            stale.append(ref)
    assert not stale, (
        f"docs/ci.md pins {stale}, whose copy of the reusable workflow differs from the one this "
        "repository ships — a reader copying the snippet gets a check that behaves differently "
        "from the page describing it. Re-pin to a commit carrying the current file."
    )


def test_the_copy_paste_example_points_at_files_rayspec_init_writes(
    tmp_path: Path, home: Path
) -> None:
    """A stranger's first move is ``rayspec init``; the example has to work in what it wrote.

    An example naming a path the scaffold never creates is a copy-paste that fails on their first
    pull request, in the one file that is supposed to be the easy part.
    """
    root = tmp_path / "consumer"
    root.mkdir()  # --root names an existing directory: a mistyped path is never scaffolded into
    result = CliRunner().invoke(app, ["init", "--root", str(root), "--no-skill"])
    assert result.exit_code == 0, result.stdout

    header = RECIPE.read_text(encoding="utf-8").split("name: rayspec dry run")[0]
    text = header + _ci_page()

    # Anchored to the start of a line (a `#` comment counts): prose elsewhere in the file says
    # "workflow: dry-run a rayspec workflow", which is a sentence and not an example.
    stubs = set(re.findall(r"(?m)^[#\s]*stubs:[ ]+(\S+)", text))
    assert stubs, "no example names a stub file"
    for path in sorted(stubs):
        assert (root / path).is_file(), f"`rayspec init` never writes {path}"

    named = set(re.findall(r"(?m)^[#\s]*workflow:[ ]+(\S+)", text))
    assert named, "no example names a workflow"
    for name in sorted(named):
        found = root / ".rayspec" / "workflows" / f"{name}.yaml"
        assert found.is_file(), f"`rayspec init` never writes a workflow called {name}"


# ------------------------------------------------------------------------------ docs.yml


def test_the_docs_site_is_built_before_it_is_deployed() -> None:
    workflow = load(DOCS)
    build = " ".join(str(step.get("run", "")) for _job, step in steps_of(workflow) if "run" in step)
    assert "mkdocs build" in build
    deploy = [
        spec
        for spec in workflow["jobs"].values()
        if any("deploy-pages" in str(step.get("uses", "")) for step in spec["steps"])
    ]
    assert len(deploy) == 1
    assert deploy[0]["permissions"]["pages"] == "write"
    assert "main" in str(deploy[0].get("if", "")), "only main is published"


def test_a_pull_request_builds_the_docs_but_never_deploys_them() -> None:
    triggers = _on(load(DOCS))
    assert "pull_request" in triggers
    workflow = load(DOCS)
    for name, spec in workflow["jobs"].items():
        if any("deploy-pages" in str(step.get("uses", "")) for step in spec["steps"]):
            assert "github.ref" in str(spec["if"]), name


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in workflow_files()])
def test_no_workflow_uses_a_yaml_anchor(path: Path) -> None:
    """Every YAML parser here takes anchors; GitHub's workflow parser does not.

    A file that reads fine locally and is rejected on the runner is the worst kind of drift, so
    the duplication is written out instead.
    """
    import re

    text = path.read_text(encoding="utf-8")
    anchors = re.findall(r"(?m)^\s*[\w-]+:\s*[&*][\w-]+", text)
    assert not anchors, f"{path.name} uses YAML anchors: {anchors}"


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in workflow_files()])
def test_workflow_yaml_is_indented_consistently(path: Path) -> None:
    """A tab in a workflow file is a parse error on the runner, not in this test's loader."""
    assert "\t" not in path.read_text(encoding="utf-8"), f"{path.name} contains a tab"
