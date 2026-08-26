"""Workflow / agent discovery: project overrides user by name."""

import importlib
import pkgutil
from pathlib import Path

import rayspec
import rayspec.workspace
from rayspec.loader import discover_agents, discover_workflows, find_project_root
from rayspec.loader.bundled import bundled_dir

WF = "rayspec: 1\nname: {name}\ndescription: {desc}\nsteps:\n  - id: a\n    shell: echo\n"
#: What the package ships, in listing order — every discovery ends with these.
BUNDLED = [
    "architect",
    "create_issue",
    "fix_issue",
    "pr_review",
    "prd_to_pr",
    "refactor_safely",
    "release_check",
    "resolve_conflicts",
    "review_block",
    "review_panel",
    "validate_pr",
]


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
    assert [r.name for r in refs] == sorted(["build", "deploy", "review", *BUNDLED])
    assert by_name["review"].scope == "project"
    assert by_name["review"].overrides is None
    assert by_name["fix_issue"].scope == "bundled"
    assert by_name["review"].description == "proj review"
    assert by_name["review"].path == root / ".rayspec/workflows/review.yaml"
    assert by_name["deploy"].scope == "user"
    assert by_name["build"].description == "build it"


def test_discover_workflows_unparseable_file_is_listed_with_error(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/bad.yaml").write_text("a: [oops\n")
    own = [r for r in discover_workflows(root, home=home) if r.scope != "bundled"]
    assert len(own) == 1
    assert own[0].name == "bad"
    assert own[0].description == ""
    assert own[0].error is not None


def test_discover_workflows_missing_dirs_still_lists_the_bundled_library(tmp_path: Path):
    """No project, no home: a fresh install still has something to run."""
    refs = discover_workflows(tmp_path / "nope", home=tmp_path / "nohome")
    assert [r.name for r in refs] == BUNDLED
    assert all(r.scope == "bundled" and r.overrides is None for r in refs)
    assert all(r.path.parent == bundled_dir() and r.path.name == f"{r.name}.yaml" for r in refs)


def test_a_project_or_user_file_shadows_the_bundled_one_and_records_it(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/pr_review.yaml").write_text(
        WF.format(name="pr_review", desc="mine")
    )
    (home / "workflows/fix_issue.yaml").write_text(WF.format(name="fix_issue", desc="theirs"))
    by_name = {r.name: r for r in discover_workflows(root, home=home)}
    assert by_name["pr_review"].scope == "project"
    assert by_name["pr_review"].path == root / ".rayspec/workflows/pr_review.yaml"
    assert by_name["pr_review"].overrides == bundled_dir() / "pr_review.yaml"
    assert by_name["fix_issue"].scope == "user"
    assert by_name["fix_issue"].overrides == bundled_dir() / "fix_issue.yaml"
    assert by_name["review_block"].scope == "bundled"
    assert by_name["review_block"].overrides is None
    assert sum(r.name == "pr_review" for r in by_name.values()) == 1


def test_a_ref_exposes_its_raw_inputs(tmp_path: Path):
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/typed.yaml").write_text(
        "rayspec: 1\nname: typed\ninputs:\n  issue: {type: integer, required: true}\n"
        "  junk: 3\nsteps:\n  - id: a\n    shell: echo\n"
    )
    (root / ".rayspec/workflows/plain.yaml").write_text(WF.format(name="plain", desc="d"))
    (root / ".rayspec/workflows/bad.yaml").write_text("a: [oops\n")
    by_name = {r.name: r for r in discover_workflows(root, home=home)}
    assert by_name["typed"].inputs == {"issue": {"type": "integer", "required": True}, "junk": 3}
    assert by_name["plain"].inputs == {}
    assert by_name["bad"].inputs == {} and by_name["bad"].error is not None


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
    disagreed about what "the project" is. Two checks, because the source scan below only sees
    a second ``def``: an alias (``find_project_root = _toplevel_root``) restores exactly the
    same ambiguity without one, so the *exported names* are checked as well.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "rayspec"
    defining = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "def find_project_root(" in path.read_text(encoding="utf-8")
    )
    assert defining == ["loader/discovery.py"]

    exported = {}
    for info in pkgutil.walk_packages(rayspec.__path__, "rayspec."):
        exposed = getattr(importlib.import_module(info.name), "find_project_root", None)
        if exposed is not None:
            exported[info.name] = exposed
    assert "rayspec.loader" in exported, "the scan imported nothing — it has stopped working"
    assert not hasattr(rayspec.workspace, "find_project_root"), (
        "rayspec.workspace answers a different question: discover_project(cwd).root"
    )
    assert len({id(f) for f in exported.values()}) == 1, (
        f"one function under two names: {[(m, getattr(f, '__module__', None)) for m, f in exported.items()]}"
    )


def test_find_project_root_walks_up(tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".rayspec").mkdir(parents=True)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == root
    other = tmp_path / "plain"
    other.mkdir()
    assert find_project_root(other) == other


def _count_reads(monkeypatch, suffix: str = ".yaml") -> dict[str, int]:
    """Count `Path.read_text` per file name — whichever code path does the reading."""
    reads: dict[str, int] = {}
    original = Path.read_text

    def counting(self: Path, *args, **kwargs):
        if self.name.endswith(suffix):
            reads[self.name] = reads.get(self.name, 0) + 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return reads


def test_discovery_opens_no_workflow_file(tmp_path: Path, monkeypatch) -> None:
    """Discovery answers "which workflows are there", which is a directory listing.

    It used to parse every file to read one `description:` — work that every caller paid and
    almost none used, and that a command resolving N names paid N times over.
    """
    root, home = _tree(tmp_path)
    for i in range(5):
        (root / f".rayspec/workflows/w{i}.yaml").write_text(WF.format(name=f"w{i}", desc=f"d{i}"))
    reads = _count_reads(monkeypatch)
    refs = discover_workflows(root, home=home)
    assert [r.name for r in refs if r.scope != "bundled"] == [f"w{i}" for i in range(5)]
    assert reads == {}, reads


def test_a_ref_reads_its_file_once_when_asked_for_the_description(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: the fields are still there, still right, and cost one read each ref."""
    root, home = _tree(tmp_path)
    (root / ".rayspec/workflows/w.yaml").write_text(WF.format(name="w", desc="the description"))
    (root / ".rayspec/workflows/bad.yaml").write_text("a: [oops\n")
    reads = _count_reads(monkeypatch)
    by_name = {r.name: r for r in discover_workflows(root, home=home)}
    good, bad = by_name["w"], by_name["bad"]
    assert good.description == "the description" and good.error is None
    assert bad.description == "" and bad.error is not None
    assert good.description == "the description"  # asked twice, read once
    assert good.inputs == {}  # the third field of the same read
    assert reads == {"w.yaml": 1, "bad.yaml": 1}, reads
