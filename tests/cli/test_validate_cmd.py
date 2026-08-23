"""`rayspec validate` rendering of load failures: markup-safe text, one entry per error."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

IDS = """\
rayspec: 1
name: ids
steps:
  - id: "../../../escape"
    shell: echo hi
  - id: "a/b"
    shell: echo hi
"""

UNKNOWN = """\
rayspec: 1
name: unknown
agents:
  r: { provider: claude, model: small, acess: read-only }
steps:
  - id: files
    shell: echo hi
    timeout_secs: 10
  - id: review
    needs: [files]
    agent: r
    prompt: "hi {{ steps.files.output }}"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    return root


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_identifier_regex_survives_rich_markup(project: Path) -> None:
    _write(project, "ids", IDS)
    res = runner.invoke(app, ["validate", "ids", "--root", str(project)])
    assert res.exit_code == 2, res.output
    assert "must match ^[a-z][a-z0-9_]*$ (lowercase snake_case)" in res.output
    assert "^*$" not in res.output


def test_schema_errors_are_one_entry_each_text_and_json(project: Path) -> None:
    _write(project, "unknown", UNKNOWN)
    res = runner.invoke(app, ["validate", "unknown", "--root", str(project)])
    assert res.exit_code == 2, res.output
    lines = res.output.splitlines()
    bullets = [ln for ln in lines if ln.startswith("  - ")]
    assert len(bullets) == 2, res.output
    assert "unknown field 'acess'" in bullets[0] and "did you mean 'access'" in bullets[0]
    assert "unknown field 'timeout_secs'" in bullets[1]
    assert "1 with errors (2 error(s))" in res.output

    res = runner.invoke(app, ["validate", "unknown", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["name"] == "unknown"
    assert row["path"] == ".rayspec/workflows/unknown.yaml"
    assert row["ok"] is False
    assert len(row["errors"]) == 2
    assert all("\n" not in e for e in row["errors"])
    assert row["errors"][0].startswith(".rayspec/workflows/unknown.yaml:4: agents.r.acess:")
    assert [p["line"] for p in row["problems"]] == [4, 8]


def test_json_path_is_set_for_a_yaml_syntax_error(project: Path) -> None:
    _write(project, "broken", "a: [\n")
    res = runner.invoke(app, ["validate", "broken", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["path"] == ".rayspec/workflows/broken.yaml"
    assert row["ok"] is False and len(row["errors"]) == 1


def test_json_path_is_set_when_validating_by_file_path(project: Path) -> None:
    _write(project, "broken", "a: [\n")
    target = project / ".rayspec" / "workflows" / "broken.yaml"
    res = runner.invoke(app, ["validate", str(target), "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["path"] == ".rayspec/workflows/broken.yaml"


def test_error_entries_split_schema_errors_only() -> None:
    """Only schema errors are split (one per problem, file-prefixed); any other error — even a
    multi-line message such as the unsupported-feature block — is one entry."""
    from rayspec.cli.commands._loader_common import error_entries
    from rayspec.errors import LoaderError
    from rayspec.schema import SchemaError

    schema = SchemaError(["a: bad", "b: worse"], source="wf.yaml")
    assert error_entries(schema) == ["wf.yaml: a: bad", "wf.yaml: b: worse"]
    assert error_entries(SchemaError(["x"])) == ["x"]
    multi = LoaderError("unsupported: agents.i.max_turns = 3\n  provider 'codex' …\n  fix: …")
    assert error_entries(multi) == [str(multi)]


def test_json_path_prefers_the_discovered_workflow_over_a_cwd_file(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name is resolved by discovery (the loader's rule), even when a plain file of the
    same name sits in the cwd."""
    _write(project, "ids", IDS)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "ids").write_text("not a workflow\n", encoding="utf-8")
    monkeypatch.chdir(cwd)
    res = runner.invoke(app, ["validate", "ids", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["path"] == ".rayspec/workflows/ids.yaml"


def test_json_path_resolves_a_relative_path_against_the_project_root(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project, "broken", "a: [\n")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    res = runner.invoke(
        app, ["validate", ".rayspec/workflows/broken.yaml", "--json", "--root", str(project)]
    )
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["path"] == ".rayspec/workflows/broken.yaml"


def test_workflow_label_is_none_for_an_unknown_name_and_a_missing_path(project: Path) -> None:
    from rayspec.cli.commands._loader_common import make_context, workflow_label

    ctx = make_context(project)
    assert workflow_label("nope", ctx) is None
    assert workflow_label("nope.yaml", ctx) is None


WF = "rayspec: 1\nname: {name}\ndescription: workflow {name}\nisolation: none\nsteps:\n  - id: a\n    shell: echo hi\n"


def _validate_cost(
    root: Path, n: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, dict[str, int]]:
    """`rayspec validate` over ``n`` generated workflows → (project listings, reads per file)."""
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    for i in range(n):
        (workflows / f"w{i:02d}.yaml").write_text(WF.format(name=f"w{i:02d}"))

    reads: dict[str, int] = {}
    listings: list[str] = []
    read_text, iterdir = Path.read_text, Path.iterdir

    def counting_read(self: Path, *args, **kwargs):
        if self.suffix == ".yaml" and self.parent.name == "workflows":
            reads[self.name] = reads.get(self.name, 0) + 1
        return read_text(self, *args, **kwargs)

    def counting_iterdir(self: Path):
        if self.name == "workflows":
            listings.append(str(self))
        return iterdir(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", counting_read)
        patch.setattr(Path, "iterdir", counting_iterdir)
        res = runner.invoke(app, ["validate", "--root", str(root)])
    assert res.exit_code == 0, res.output
    assert f"{n} workflow(s) validated, no errors" in res.output
    return len(listings), reads


def test_validate_is_linear_in_the_number_of_workflows(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec validate` does a bounded amount of work PER workflow, not per pair of them.

    It used to be quadratic twice over: resolving each of the N names listed the project again,
    and each listing parsed all N files just to read their `description:`. N=200 took 32 s, and
    it rose with the square. What is pinned here is the two facts behind those seconds rather
    than a wall-clock number, which would only be flaky — and they are measured at two sizes,
    because "listed a few times" and "listed once per workflow" are the same number at one size:

    * the project is LISTED a constant number of times, whatever N is;
    * each workflow file is READ once — the load that validates it — whatever N is.

    Either half alone still leaves the other quadratic, so both are counted.
    """
    small_listings, small_reads = _validate_cost(tmp_path / "small", 4, monkeypatch)
    large_listings, large_reads = _validate_cost(tmp_path / "large", 16, monkeypatch)
    assert len(small_reads) == 4 and len(large_reads) == 16
    assert max(large_reads.values()) == max(small_reads.values()) == 1, large_reads
    assert large_listings == small_listings, (small_listings, large_listings)
