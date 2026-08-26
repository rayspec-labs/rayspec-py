"""The bundled ``create_issue`` workflow against a fake ``gh``.

Declarative ``exec_shell: true`` cases from ``create_issue/checks.yaml``, driven through
``rayspec test --exec-shell`` with the stub provider and a ``gh`` first on ``PATH`` that answers
``issue list`` from ``$GH_ISSUES`` and records ``issue create`` — the two things the dry run in
``tests/workflows/checks.yaml`` can only simulate: the search results the judge is shown, and the
exact command (title, body, whitelisted labels) that files the issue.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
SUITE = HERE / "create_issue"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

FAKE_GH = """#!/bin/sh
# Records every invocation (and the body of `issue create`) in $GH_LOG; answers `issue list`
# with $GH_ISSUES (default: nothing found); fails the subcommand named by $GH_FAIL the way a
# real `gh` would (non-zero exit, a line on stderr).
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ -n "${GH_FAIL:-}" ] && [ "$2" = "$GH_FAIL" ]; then
  echo "gh: simulated $GH_FAIL failure" >&2
  exit 1
fi
case "$1 $2" in
  "issue list") printf '%s' "${GH_ISSUES:-[]}" ;;
  "issue create")
    cat >> "$GH_LOG"
    printf '\\n--- end of body\\n' >> "$GH_LOG"
    echo "https://github.com/example/repo/issues/99" ;;
esac
exit 0
"""

CREATE_LINE = (
    "gh issue create --title Crash when the input list is empty --body-file - --label bug\n"
)


@pytest.fixture
def root(tmp_path: Path, home: Path) -> Path:
    """A plain project directory (no git, no .rayspec/): the workflow runs in place."""
    path = tmp_path / "proj"
    path.mkdir()
    return path


def install_fake_gh(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gh`` that logs instead of talking to GitHub; returns the log file."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(FAKE_GH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = root / "gh.log"
    # a case's `env:` is a literal string and cannot name this directory: the driver sets both
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    for name in ("GH_FAIL", "GH_ISSUES"):
        monkeypatch.delenv(name, raising=False)
    return log


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    shutil.copytree(SUITE / "policy", root / "policy")
    log = install_fake_gh(root, monkeypatch)
    monkeypatch.chdir(root)  # a case's RAYSPEC_POLICY is relative to the cwd
    # `rayspec test` reads the operator policy once per suite root, at start-up — before a case's
    # own `env:` is applied — so the policy a case names has to be in force before the command runs.
    (case,) = [c for c in CASES if c.id == case_id]
    if policy := case.env.get("RAYSPEC_POLICY"):
        monkeypatch.setenv("RAYSPEC_POLICY", policy)
    res = runner.invoke(app, ["test", "--root", str(root), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_fake_gh_case_passes(
    root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_case(root, case_id, monkeypatch)


def test_the_issue_is_filed_with_the_draft_and_the_whitelisted_label_only(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both search terms are looked up (open and closed issues, as JSON), then one `gh issue
    create` with the title, the body on stdin and `--label bug` — `wontfix` never reaches gh."""
    log = _run_case(root, "filed", monkeypatch)
    searches, create = log.split("gh issue create", 1)
    assert searches == (
        "gh issue list --search empty input --state all --limit 20 --json number,title,state,url\n"
        "gh issue list --search IndexError total --state all --limit 20 --json number,title,state,url\n"
    )
    assert ("gh issue create" + create).startswith(CREATE_LINE)
    body = ("gh issue create" + create)[len(CREATE_LINE) :]
    assert body == (
        "Calling total([]) raises IndexError in app.py; expected 0.\n\n"
        "Repro: python -c 'import app; app.total([])'\n--- end of body\n"
    )
    assert "wontfix" not in log


def test_a_duplicate_files_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _run_case(root, "duplicate", monkeypatch)
    assert "gh issue list --search empty input" in log
    assert "issue create" not in log


def test_an_empty_whitelist_files_without_labels(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _run_case(root, "unlabelled", monkeypatch)
    assert "gh issue create --title Crash when the input list is empty --body-file -\n" in log
    assert "--label" not in log


def test_a_held_chore_class_pauses_before_anything_is_filed(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _run_case(root, "held", monkeypatch)
    assert log.count("gh issue list") == 2  # the search ran; the gate then paused the run
    assert "issue create" not in log


def test_a_failed_search_files_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _run_case(root, "search_fails", monkeypatch)
    assert (
        log
        == "gh issue list --search empty input --state all --limit 20 --json number,title,state,url\n"
    )
