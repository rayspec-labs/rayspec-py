"""The example READMEs' "expected output" blocks and walkthroughs hold against the real CLI.

Boundary: docs-accuracy tests for ``examples/**/README.md``. Every check drives the Typer app
in process through ``scripts/check_examples.py``'s ``_invoke`` under a throwaway
``RAYSPEC_HOME``; no network, no real providers. Durations, token counts and trailing error
text of console lines are deliberately *not* compared — only the step symbol, path and status,
so the blocks stay honest without pinning timing noise.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .test_examples import EXAMPLES_DIR, check_examples

REPO_ROOT = EXAMPLES_DIR.parent


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ``RAYSPEC_HOME`` so the runs never touch the developer's store."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(path))
    for name in list(check_examples.os.environ):
        if name.startswith("RAYSPEC_INPUT_"):
            monkeypatch.delenv(name)
    return path


_STEP_LINE_RE = re.compile(r"^(?P<symbol>[✓✗○↻●■])\s+(?P<path>\S+)\s+(?P<status>\S+)")
_FENCE_RE = re.compile(r"```(?P<lang>[a-z]*)\n(?P<body>.*?)```", re.DOTALL)
_AT_LINE_RE = re.compile(r"at \.rayspec/workflows/unsupported_demo\.yaml:(\d+)")


def _invoke(args: list[str], home: Path, **env: str | None) -> Any:
    return check_examples._invoke(args, home=home, env_overrides=env or None)


def _fenced_blocks(readme: Path) -> list[str]:
    return [m.group("body") for m in _FENCE_RE.finditer(readme.read_text(encoding="utf-8"))]


def _step_lines(text: str) -> list[tuple[str, str, str]]:
    """``(symbol, path, status)`` of every console step line in ``text`` (durations dropped)."""
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        m = _STEP_LINE_RE.match(raw.strip())
        if m and m.group("symbol") not in "●■":
            out.append((m.group("symbol"), m.group("path"), m.group("status")))
    return out


def _expected_block(readme: Path, *, containing: str) -> str:
    """The first fenced block of ``readme`` that mentions ``containing`` (an expected-output block)."""
    for body in _fenced_blocks(readme):
        if containing in body:
            return body
    raise AssertionError(f"{readme}: no fenced block containing {containing!r}")


# --------------------------------------------------------------------------------------------------
# unsupported_demo: the `at …:NN` line numbers in the README match the real validate output
# --------------------------------------------------------------------------------------------------


def test_unsupported_demo_readme_line_numbers_match_validate(home: Path) -> None:
    root = EXAMPLES_DIR / "unsupported_demo"
    readme = root / "README.md"
    inv = _invoke(["validate", "unsupported_demo", "--root", str(root)], home)
    assert inv.exit_code == 2, inv.output
    real = _AT_LINE_RE.findall(inv.output)
    documented = _AT_LINE_RE.findall(readme.read_text(encoding="utf-8"))
    assert real and documented
    assert documented == real, f"README cites lines {documented}, validate prints {real}"


# --------------------------------------------------------------------------------------------------
# hello_review — the expected-output blocks list the real step lines (order, symbol, status)
# --------------------------------------------------------------------------------------------------


def test_triage_fanout_readme_expected_block_matches_dry_run(home: Path) -> None:
    root = EXAMPLES_DIR / "triage_fanout"
    expected = _step_lines(_expected_block(root / "README.md", containing="triage[0]/classify"))
    inv = _invoke(
        ["run", "triage_fanout", "--dry-run", "--stubs", str(root / "stubs.yaml"),
         "--root", str(root)],
        home,
    )  # fmt: skip
    assert inv.exit_code == 0, inv.output
    real = _step_lines(inv.output)
    assert expected == real, f"README block:\n{expected}\nreal run:\n{real}"


def test_hello_review_readme_expected_tail_matches_dry_run(home: Path) -> None:
    root = EXAMPLES_DIR / "hello_review"
    expected = _step_lines(_expected_block(root / "README.md", containing="review succeeded"))
    inv = _invoke(
        [
            "run", "hello_review", "-i", "target=src/", "--dry-run",
            "--stubs", str(root / "stubs.yaml"), "--root", str(root),
        ],
        home,
    )  # fmt: skip
    assert inv.exit_code == 0, inv.output
    assert expected == _step_lines(inv.output)


def test_hello_review_readme_documents_the_json_tail_correctly() -> None:
    """The last ``--json`` stdout line is the summary object; ``run.finished`` comes before it."""
    text = (EXAMPLES_DIR / "hello_review" / "README.md").read_text(encoding="utf-8")
    assert "tail -n 1   # last event" not in text
    assert "summary object" in text
    assert 'select(.type == "run.finished")' in text or "run.finished" in text


def test_hello_review_readme_calls_the_full_block_output_not_tail() -> None:
    """The block shows the whole run (from `▶ run … started`), so the lead-in must not say "tail"."""
    text = (EXAMPLES_DIR / "hello_review" / "README.md").read_text(encoding="utf-8")
    assert "Expected tail of the run" not in text
    assert "Expected output of the run:" in text


# --------------------------------------------------------------------------------------------------
# release_check: the dry-run block and the `--exec-shell` walkthrough hold in a fresh repo
# --------------------------------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def small_repo(tmp_path: Path) -> Path:
    """A copy of ``examples/release_check`` as a fresh one-commit repo with no remote and no tests."""
    root = tmp_path / "small"
    shutil.copytree(EXAMPLES_DIR / "release_check", root)
    _git("init", "-q", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_release_check_readme_expected_block_matches_dry_run(home: Path) -> None:
    root = EXAMPLES_DIR / "release_check"
    expected = _step_lines(_expected_block(root / "README.md", containing="last_tag succeeded"))
    inv = _invoke(
        ["run", "release_check", "-i", "tag=v0.4.0", "--dry-run",
         "--stubs", str(root / "stubs.yaml"), "--root", str(root)],
        home,
        SLACK_WEBHOOK=None,
    )  # fmt: skip
    assert inv.exit_code == 0, inv.output
    assert expected == _step_lines(inv.output)


@pytest.mark.skipif(shutil.which("jq") is None, reason="the `tests` step uses jq")
def test_release_check_exec_shell_walkthrough_holds_in_a_small_repo(
    small_repo: Path, home: Path
) -> None:
    """`meta` must emit valid JSON without HEAD~20 / a previous tag; `tests` must not assume a suite."""
    inv = _invoke(
        ["run", "release_check", "-i", "tag=v0.4.0", "--dry-run",
         "--stubs", str(small_repo / "stubs.yaml"), "--exec-shell", "--json",
         "--root", str(small_repo)],
        home,
        SLACK_WEBHOOK=None,
    )  # fmt: skip
    summary = check_examples._summary_from_json(inv.stdout)
    assert summary is not None, inv.output
    statuses = check_examples._step_statuses(inv.stdout)
    assert statuses.get("meta") == "succeeded", inv.output
    assert statuses.get("tests") == "succeeded", inv.output
    assert summary["status"] == "succeeded" and summary["exit_code"] == 0, inv.output
    # the shell really ran: `meta` counted the single commit of the fresh repo
    run_dir = Path(summary["run_dir"])
    meta = json.loads((run_dir / "steps" / "meta" / "output.json").read_text(encoding="utf-8"))
    assert meta == {"previous": "", "commits": 1}, meta


def test_release_check_readme_says_the_notes_token_count_is_path_dependent() -> None:
    """The stub counts the rendered prompt, which embeds `run.workdir`: `notes … NN tok` varies."""
    text = (EXAMPLES_DIR / "release_check" / "README.md").read_text(encoding="utf-8")
    assert "checkout path" in text
    assert re.search(r"token count[s]? of `notes`", text), "note the path-dependent token count"


def test_dogfood_release_check_notes_range_uses_a_single_root_commit() -> None:
    """`git rev-list --max-parents=0 HEAD` prints every root; the range must take exactly one."""
    twin = REPO_ROOT / ".rayspec" / "workflows" / "release_check.yaml"
    text = twin.read_text(encoding="utf-8")
    assert "--max-parents=0 HEAD" in text
    assert "--max-parents=0 HEAD | tail -1" in text
