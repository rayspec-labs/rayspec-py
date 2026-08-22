"""Workflow / agent discovery: project overrides user by name."""

from pathlib import Path

from rayspec.loader import discover_agents, discover_workflows, find_project_root

WF = "rayspec: 1\nname: {name}\ndescription: {desc}\nsteps:\n  - id: a\n    shell: echo\n"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "agents").mkdir(parents=True)
    (home / "workflows").mkdir(parents=True)
    (home / "agents").mkdir(parents=True)
    return root, home


def test_discover_workflows_project_overrides_user(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/review.yaml").write_text(
        WF.format(name="review", desc="proj review")
    )
    (root / ".rayspec/workflows/build.yaml").write_text(WF.format(name="build", desc="build it"))
    (home / "workflows/review.yaml").write_text(WF.format(name="review", desc="user review"))
    (home / "workflows/deploy.yml").write_text(WF.format(name="deploy", desc="ship"))
    refs = discover_workflows(root, home=home)
    by_name = {r.name: r for r in refs}
    assert [r.name for r in refs] == ["build", "deploy", "review"]
    assert by_name["review"].scope == "project"
    assert by_name["review"].description == "proj review"
    assert by_name["review"].path == root / ".rayspec/workflows/review.yaml"
    assert by_name["deploy"].scope == "user"
    assert by_name["build"].description == "build it"


def test_discover_workflows_unparseable_file_is_listed_with_error(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/bad.yaml").write_text("a: [oops\n")
    refs = discover_workflows(root, home=home)
    assert len(refs) == 1
    assert refs[0].name == "bad"
    assert refs[0].description == ""
    assert refs[0].error is not None


def test_discover_workflows_missing_dirs(tmp_path: Path):
    assert discover_workflows(tmp_path / "nope", home=tmp_path / "nohome") == []


def test_discover_agents_project_overrides_user(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/agents/reviewer.yaml").write_text("provider: claude\nmodel: small\n")
    (home / "agents/reviewer.yaml").write_text("provider: codex\n")
    (home / "agents/writer.yaml").write_text("provider: codex\nmodel: large\n")
    refs = discover_agents(root, home=home)
    names = [(r.name, r.scope) for r in refs]
    assert names == [("reviewer", "project"), ("writer", "user")]
    assert refs[0].path == root / ".rayspec/agents/reviewer.yaml"


def test_find_project_root_is_the_only_one() -> None:
    """One public project-root discovery, not two that answer differently.

    A second ``find_project_root`` used to live in ``rayspec.workspace``: it returned the git
    top level, so in a repository whose project sits at ``packages/foo/.rayspec`` the two
    disagreed about what "the project" is. This scan keeps the duplicate from coming back.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "rayspec"
    defining = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "def find_project_root(" in path.read_text(encoding="utf-8")
    )
    assert defining == ["loader/discovery.py"]


def test_find_project_root_walks_up(tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".rayspec").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == root
    other = tmp_path / "plain"
    other.mkdir()
    assert find_project_root(other) == other
