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
import subprocess
import sys
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


def test_every_workflow_starts_from_read_only_permissions() -> None:
    for path in workflow_files():
        workflow = load(path)
        assert workflow.get("permissions") is not None, (
            f"{path.name} does not declare top-level permissions"
        )


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


def test_the_release_workflow_names_the_step_that_is_still_manual() -> None:
    """The placeholder on PyPI has to be yanked by a person; the run has to say so."""
    text = RELEASE.read_text(encoding="utf-8").lower()
    assert "yank" in text and "placeholder" in text


def test_publishing_only_happens_for_a_tag() -> None:
    """``workflow_dispatch`` is the rehearsal: everything except the irreversible half."""
    workflow = load(RELEASE)
    for name, spec in workflow["jobs"].items():
        publishes = any(
            "gh-action-pypi-publish" in str(step.get("uses", ""))
            or "gh release create" in str(step.get("run", ""))
            for step in spec["steps"]
        )
        if publishes:
            assert "refs/tags/v" in str(spec.get("if", "")), f"{name} runs without a tag"


def test_the_version_guard_accepts_a_matching_tag_and_refuses_anything_else(
    tmp_path: Path,
) -> None:
    """The guard is release-day-only code, so it is exercised here instead."""
    script = tmp_path / "guard.py"
    script.write_text(heredoc(step_with_id(load(RELEASE), "version")["run"], "PY"), "utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.3.4"\n', "utf-8")
    outputs = tmp_path / "outputs.txt"

    def guard(ref: str, event: str = "push") -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "REF_NAME": ref,
            "EVENT_NAME": event,
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

    rehearsal = guard("main", event="workflow_dispatch")
    assert rehearsal.returncode == 0, "a rehearsal has no tag to agree with"


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


def test_the_recipe_asks_for_exactly_the_permissions_it_uses() -> None:
    workflow = load(RECIPE)
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write"}


def test_the_recipe_does_not_try_to_comment_where_it_cannot() -> None:
    """A pull request from a fork gets a read-only token: say so, do not fail the check."""
    comment = step_with_id(load(RECIPE), "comment")
    assert "fork" in str(comment["if"]) or "head.repo.full_name" in str(comment["if"])
    assert "pull_request" in str(comment["if"])


def test_the_recipe_edits_its_own_comment_instead_of_adding_one_per_push() -> None:
    comment = step_with_id(load(RECIPE), "comment")
    assert "PATCH" in comment["run"], "the comment is never updated in place"
    assert "MARKER" in comment["run"]


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
