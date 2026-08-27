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
    return path


_STEP_LINE_RE = re.compile(r"^(?P<symbol>[✓✗○↻●■])\s+(?P<path>\S+)\s+(?P<status>\S+)")
_FENCE_RE = re.compile(r"```(?P<lang>[a-z]*)\n(?P<body>.*?)```", re.DOTALL)
_AT_LINE_RE = re.compile(r"at \.rayspec/workflows/unsupported_demo\.yaml:(\d+)")


def _invoke(args: list[str], home: Path, **env: str | None) -> Any:
    return check_examples._invoke(args, home=home, env_overrides=env or None)


def _fenced_blocks(readme: Path) -> list[str]:
    return [m.group("body") for m in _FENCE_RE.finditer(readme.read_text(encoding="utf-8"))]


def _step_lines(text: str) -> list[tuple[str, str, str]]:
    """``(symbol, path, status)`` of every console step line in ``text``, sorted; durations dropped.

    Sorted because an ``each:`` fans its items out concurrently and they finish in whatever order
    the event loop wakes them, so the console prints them in a different order from one run to the
    next — a README cannot pin that, and comparing it made this test fail under load while passing
    on its own. What the block still promises is every step, once, with the status it ended in.
    """
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        m = _STEP_LINE_RE.match(raw.strip())
        if m and m.group("symbol") not in "●■":
            out.append((m.group("symbol"), m.group("path"), m.group("status")))
    return sorted(out, key=lambda line: line[1])


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


# --------------------------------------------------------------------------------------------
# review_sweep: what `artifacts:` puts in front of the reader, and when
# --------------------------------------------------------------------------------------------


REVIEW_SWEEP = EXAMPLES_DIR / "review_sweep"


def _run_id(inv: Any) -> str:
    summary = check_examples._summary_from_json(inv.stdout)
    assert summary is not None, inv.output
    return str(summary["run_id"])


def _shown(run_id: str, root: Path, home: Path) -> Any:
    inv = _invoke(["show", run_id, "--json", "--root", str(root)], home)
    assert inv.exit_code == 0, inv.output
    return json.loads(inv.stdout)


def test_review_sweep_dry_run_records_no_artifacts(home: Path) -> None:
    """The README's credential-free walkthrough. A dry run executes no shell step, so no report
    is written and there is no artifact to check, copy or record — `rayspec show` has nothing to
    list, and a README sentence promising otherwise sends the reader looking for a table."""
    inv = _invoke(
        ["run", "review_sweep", "--dry-run", "--stubs", str(REVIEW_SWEEP / "stubs.yaml"),
         "--root", str(REVIEW_SWEEP), "--json"],
        home,
    )  # fmt: skip
    assert inv.exit_code == 1, inv.output  # one angle is scripted to fail
    shown = _shown(_run_id(inv), REVIEW_SWEEP, home)
    assert shown["artifacts"] == []
    assert all(step["artifacts"] == [] for step in shown["steps"]), shown["steps"]
    assert not (REVIEW_SWEEP / "reports").exists()  # nor did it write into the checkout


@pytest.fixture
def review_sweep_project(tmp_path: Path) -> Path:
    """``examples/review_sweep`` as a project of its own, with the reviewer switched to the stub
    provider: the shell steps, the artifact check and the copy into the run directory are the
    real path, only the three angles' answers are scripted instead of bought."""
    root = tmp_path / "review_sweep"
    shutil.copytree(REVIEW_SWEEP, root)
    workflow = root / ".rayspec" / "workflows" / "review_sweep.yaml"
    text = workflow.read_text(encoding="utf-8").replace("provider: claude", "provider: stub")
    workflow.write_text(text, encoding="utf-8")
    return root


def test_review_sweep_keeps_the_reports_of_a_run_that_really_ran(
    review_sweep_project: Path, home: Path
) -> None:
    """The other half of the same sentence: this is where `rayspec show` does list them, with
    their size and sha256, because a real run wrote three files and the store copied them."""
    inv = _invoke(
        ["run", "review_sweep", "-i", "target=stubs.yaml",
         "--stubs", str(review_sweep_project / "stubs_clean.yaml"),
         "--root", str(review_sweep_project), "--json"],
        home,
    )  # fmt: skip
    assert inv.exit_code == 0, inv.output
    shown = _shown(_run_id(inv), review_sweep_project, home)
    # The three angles run together (`max_parallel: 3`, and the workflow says so in a comment),
    # and `show` lists artifacts in record order -- so which report is recorded first is the
    # scheduler's business, not a promise. Sorting is what the claim actually is: all three were
    # written, hashed and kept. Asserting the order instead turned `test (3.13)` red on `main`
    # while the other three interpreters passed.
    assert sorted(a["path"] for a in shown["artifacts"]) == [
        "reports/api.md",
        "reports/docs.md",
        "reports/tests.md",
    ]
    for artifact in shown["artifacts"]:
        assert artifact["size"] > 0 and len(artifact["sha256"]) == 64
        assert (Path(shown["run_dir"]) / artifact["ref"]).is_file()


def _sections(readme: Path) -> dict[str, str]:
    """The README's `## ` sections by heading."""
    out: dict[str, str] = {}
    heading = ""
    for line in readme.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            out[heading] = ""
        elif heading:
            out[heading] += line + "\n"
    return out


def test_review_sweep_readme_puts_the_artifacts_claim_where_it_holds() -> None:
    """`rayspec show` lists artifacts for a run that produced files, and only there."""
    sections = _sections(REVIEW_SWEEP / "README.md")
    dry, real = sections["Try it without credentials"], sections["Run it for real"]
    assert "under `artifacts:`" in real and "sha256" in real
    assert "sha256" not in dry
    assert "no `artifacts:` section" in dry  # the dry-run half states the absence, not a table


# --------------------------------------------------------------------------------------------
# an example is scaffolded into a project of its own: what it names, it must ship
# --------------------------------------------------------------------------------------------


def _example_names() -> list[str]:
    from rayspec.cli.commands.init import example_names

    return sorted(example_names())


#: Any link into this repository's `docs/` from a file that lands in somebody else's project.
_REPO_DOC_URL_RE = re.compile(r"https://github\.com/rayspec-labs/rayspec-py/\S*?docs/[^\s)\]]*")

_TREE_ENTRY_RE = re.compile(r"^(?:[├└]──|\|--)\s*(?P<name>\S+)")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


def _tree_blocks(readme: Path) -> list[tuple[str, list[str]]]:
    """``(root line, entry names)`` for every ASCII tree diagram in ``readme``."""
    found: list[tuple[str, list[str]]] = []
    for body in _fenced_blocks(readme):
        lines = [line for line in body.splitlines() if line.strip()]
        if len(lines) < 2 or not lines[0].rstrip().endswith("/"):
            continue
        entries = [m.group("name") for line in lines[1:] if (m := _TREE_ENTRY_RE.match(line))]
        if entries:
            found.append((lines[0].strip(), entries))
    return found


def _expand(entry: str) -> list[str]:
    """``workflows/{a,b}.yaml`` -> the two paths it stands for."""
    match = _BRACE_RE.search(entry)
    if match is None:
        return [entry]
    return [
        expanded
        for option in match.group(1).split(",")
        for expanded in _expand(entry[: match.start()] + option.strip() + entry[match.end() :])
    ]


@pytest.mark.parametrize("name", _example_names())
def test_every_file_a_readme_tree_lists_is_actually_scaffolded(name: str) -> None:
    """`rayspec init --from <name>` writes a project; its README describes THAT project. A tree
    diagram naming a file the scaffold does not write sends a wheel user after nothing."""
    from rayspec.cli.commands.init import example_files

    shipped = {rel for rel, _ in example_files(name)}
    readme = EXAMPLES_DIR / name / "README.md"
    for root_line, entries in _tree_blocks(readme):
        prefix = root_line.removeprefix(f"examples/{name}/")
        for entry in entries:
            for path in _expand(entry):
                assert f"{prefix}{path}" in shipped, (
                    f"{readme}: the tree lists {prefix}{path}, which `init --from {name}` "
                    f"does not write (it ships {sorted(shipped)})"
                )


def test_the_readme_trees_are_actually_being_checked() -> None:
    """Not every example draws one; the ones that do must not all have been parsed away."""
    with_trees = [n for n in _example_names() if _tree_blocks(EXAMPLES_DIR / n / "README.md")]
    assert len(with_trees) >= 4, with_trees


@pytest.mark.parametrize("name", _example_names())
def test_no_scaffolded_file_points_outside_the_project_it_scaffolds(name: str) -> None:
    """A scaffolded file may cite a doc only by URL — `docs/cli.md` explains why for hints, and a
    file that lands in somebody's project is in exactly the same position: there is no checkout
    above it to resolve `../../docs/schema.md` or `examples/README.md` against."""
    from rayspec.cli._docs import DOCS_BASE
    from rayspec.cli.commands.init import example_files

    for rel, node in example_files(name):
        if not rel.endswith((".md", ".yaml", ".yml")):
            continue
        text = node.read_text(encoding="utf-8")
        assert "](../" not in text, f"{name}/{rel} links outside the scaffolded project"
        assert "examples/README.md" not in text, (
            f"{name}/{rel} cites examples/README.md, which `init --from` never writes"
        )
        assert "scripts/" not in text, (
            f"{name}/{rel} names something under scripts/, which only exists in a checkout of "
            f"the repository — a scaffolded project has `rayspec test` and nothing else"
        )
        for url in _REPO_DOC_URL_RE.findall(text):
            assert url.startswith(DOCS_BASE), (
                f"{name}/{rel} cites {url}, which does not start with `_docs.DOCS_BASE` "
                f"({DOCS_BASE}) — that constant is where the location of the docs is decided"
            )
