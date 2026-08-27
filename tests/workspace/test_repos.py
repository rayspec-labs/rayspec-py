"""--repo sources: local path, registered project, git URL (bare clone + fetch)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.config import Config, ProjectSpec
from rayspec.workspace import git as g
from rayspec.workspace.errors import WorkspaceError
from rayspec.workspace.project import project_dir
from rayspec.workspace.repos import (
    RepoSource,
    default_base,
    ensure_bare_source,
    is_git_url,
    remote_tracking_ref,
    resolve_source,
    source_slug,
    source_slug_for,
)

from .gitfixtures import git, make_repo


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("git@github.com:o/r.git", True),
        ("ssh://git@github.com/o/r", True),
        ("https://github.com/o/r.git", True),
        ("git://host/o/r", True),
        ("file:///tmp/x.git", True),
        ("/tmp/some/dir", False),
        ("./rel", False),
        ("myapp", False),
        ("C:\\repo", False),
    ],
)
def test_is_git_url(text: str, expected: bool) -> None:
    assert is_git_url(text) is expected


def test_source_slug() -> None:
    assert source_slug("git@github.com:Acme/Widget.git") == "github.com/Acme/Widget"
    local = source_slug("file:///tmp/up/stream.git")
    assert local.startswith("local/stream-") and len(local) == len("local/stream-") + 8
    assert source_slug("file:///tmp/up/stream.git") == local  # stable


def test_resolve_local_path(repo: Path, home: Path) -> None:
    src = resolve_source(str(repo), None, home=home)
    assert isinstance(src, RepoSource)
    assert src.kind == "path"
    assert src.root == repo.resolve()
    assert src.slug == "github.com/Acme/Widget"
    assert src.base is None
    assert src.project_name is None
    # relative paths resolve against cwd
    rel = resolve_source(repo.name, None, home=home, cwd=repo.parent)
    assert rel.root == repo.resolve()


def test_resolve_registered_project_path(repo: Path, home: Path) -> None:
    config = Config(projects=[ProjectSpec(name="widget", source=str(repo), base="main")])
    src = resolve_source("widget", config, home=home)
    assert src.kind == "path"
    assert src.root == repo.resolve()
    assert src.base == "main"
    assert src.project_name == "widget"


def test_resolve_registered_name_wins_over_same_named_dir(
    tmp_path: Path, repo: Path, home: Path
) -> None:
    (tmp_path / "widget").mkdir()
    config = Config(projects=[ProjectSpec(name="widget", source=str(repo))])
    src = resolve_source("widget", config, home=home, cwd=tmp_path)
    assert src.root == repo.resolve()
    # an explicit path form always means the path
    src2 = resolve_source("./widget", config, home=home, cwd=tmp_path)
    assert src2.root == (tmp_path / "widget").resolve()


def test_resolve_unknown(home: Path, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="nope"):
        resolve_source("nope", Config(), home=home, cwd=tmp_path)
    with pytest.raises(WorkspaceError, match="not a directory"):
        resolve_source(str(tmp_path / "missing"), None, home=home)
    # a non-git directory is a valid path source (prepare_workspace runs it in place)
    plain = tmp_path / "plain"
    plain.mkdir()
    src = resolve_source(str(plain), None, home=home)
    assert src.kind == "path" and src.slug.startswith("local/plain-")


def test_resolve_url_bare_clone_and_fetch(tmp_path: Path, home: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=1)
    url = upstream.resolve().as_uri()
    src = resolve_source(url, None, home=home)
    assert src.kind == "url"
    assert src.url == url
    assert src.root == project_dir(home, src.slug) / "source.git"
    assert src.root.is_dir()
    assert g.run_git(["rev-parse", "--is-bare-repository"], src.root).stdout == "true"
    assert g.remote_default_branch(src.root) == "origin/main"
    assert default_base(src) == "origin/main"
    assert g.rev_parse(src.root, "origin/main") == g.rev_parse(upstream, "main")

    # a later use fetches (no re-clone) and sees the new upstream commit
    (upstream / "new.txt").write_text("n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    src2 = resolve_source(url, None, home=home)
    assert src2.root == src.root
    assert g.rev_parse(src.root, "origin/main") == g.rev_parse(upstream, "main")


def test_ensure_bare_source_prunes_and_keeps_rayspec_branches(tmp_path: Path, home: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=1)
    git("branch", "topic", cwd=upstream)
    url = upstream.resolve().as_uri()
    bare = ensure_bare_source(url, home=home)
    assert g.ref_exists(bare, "origin/topic")
    # a local rayspec/* branch in the bare repo survives fetch --prune
    g.run_git(["branch", "rayspec/wf-abcd", "origin/main"], bare)
    git("branch", "-D", "topic", cwd=upstream)
    ensure_bare_source(url, home=home)
    assert not g.ref_exists(bare, "origin/topic")
    assert g.branch_exists(bare, "rayspec/wf-abcd")


def test_resolve_registered_project_url(tmp_path: Path, home: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=1)
    git("branch", "develop", cwd=upstream)
    url = upstream.resolve().as_uri()
    config = Config(projects=[ProjectSpec(name="up", source=url, base="develop")])
    src = resolve_source("up", config, home=home)
    assert src.kind == "url" and src.project_name == "up"
    assert src.base == "develop"
    assert default_base(src) == "origin/develop"  # registered base → remote tracking ref
    assert g.ref_exists(src.root, "origin/develop")


def test_resolve_url_clone_failure(tmp_path: Path, home: Path) -> None:
    with pytest.raises(WorkspaceError, match="clone"):
        resolve_source((tmp_path / "missing").resolve().as_uri(), None, home=home)


def test_bare_source_has_no_stale_local_heads(tmp_path: Path, home: Path) -> None:
    """The bare source only tracks ``origin/*``; a bare branch name maps to the remote tip."""
    upstream = make_repo(tmp_path / "upstream", commits=1)
    url = upstream.resolve().as_uri()
    bare = ensure_bare_source(url, home=home)
    assert not g.branch_exists(bare, "main")  # no refs/heads/main snapshot from the clone
    assert g.ref_exists(bare, "origin/main")
    # upstream advances → the next use sees the fresh tip through origin/main
    (upstream / "new.txt").write_text("n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    ensure_bare_source(url, home=home)
    tip = g.rev_parse(upstream, "main")
    assert g.rev_parse(bare, "origin/main") == tip
    assert remote_tracking_ref(bare, "main") == "origin/main"
    assert remote_tracking_ref(bare, "origin/main") == "origin/main"
    assert remote_tracking_ref(bare, tip) == tip
    src = resolve_source(url, None, home=home)
    assert default_base(src) == "origin/main"
    assert g.rev_parse(src.root, default_base(src) or "") == tip


def test_registered_base_on_url_source_follows_upstream(tmp_path: Path, home: Path) -> None:
    """Regression: ``base: main`` on a URL project must resolve to origin/main every run."""
    from rayspec.workspace import prepare_workspace

    upstream = make_repo(tmp_path / "upstream", commits=1)
    url = upstream.resolve().as_uri()
    config = Config(projects=[ProjectSpec(name="up", source=url, base="main")])
    ws1 = prepare_workspace(
        tmp_path,
        home=home,
        workflow_name="wf",
        run_id="20260820-101500-one1",
        repo_arg="up",
        config=config,
    )
    assert ws1.base_sha == g.rev_parse(upstream, "main")
    (upstream / "new.txt").write_text("n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    ws2 = prepare_workspace(
        tmp_path,
        home=home,
        workflow_name="wf",
        run_id="20260820-101500-two2",
        repo_arg="up",
        config=config,
    )
    assert ws2.base_sha == g.rev_parse(upstream, "main")
    assert ws2.base_branch == "origin/main"
    # an explicit --base with a bare branch name maps the same way
    ws3 = prepare_workspace(
        tmp_path,
        home=home,
        workflow_name="wf",
        run_id="20260820-101500-thr3",
        repo_arg=url,
        base="main",
    )
    assert ws3.base_sha == g.rev_parse(upstream, "main")
    assert ws3.base_branch == "origin/main"


def test_stale_clone_heads_are_ignored(tmp_path: Path, home: Path) -> None:
    """A source.git created by an older ``git clone --bare`` (stale refs/heads/main) still
    resolves bare branch names to origin/*."""
    upstream = make_repo(tmp_path / "upstream", commits=1)
    url = upstream.resolve().as_uri()
    bare = ensure_bare_source(url, home=home)
    g.run_git(["branch", "main", "origin/main"], bare)  # simulate the old clone layout
    (upstream / "new.txt").write_text("n")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "new", cwd=upstream)
    src = resolve_source(url, None, home=home)
    assert g.rev_parse(bare, remote_tracking_ref(bare, "main")) == g.rev_parse(upstream, "main")
    assert default_base(src) == "origin/main"


def test_source_slug_for_matches_resolve_without_cloning(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """The launcher's slug for a --repo source equals what resolve_source/prepare_workspace use,
    and computing it materialises nothing (no source.git clone for a URL)."""
    # a path source
    assert source_slug_for(str(repo), None) == resolve_source(str(repo), None, home=home).slug
    # a registered project name (path)
    config = Config(projects=[ProjectSpec(name="widget", source=str(repo), base="main")])
    assert source_slug_for("widget", config) == resolve_source("widget", config, home=home).slug
    # a URL source: the slug is derived, nothing is cloned
    url = "git@github.com:Acme/Widget.git"
    assert source_slug_for(url, None) == source_slug(url)
    assert not (home / "projects" / source_slug(url) / "source.git").exists()
    # a relative bare name resolves against cwd
    assert source_slug_for(repo.name, None, cwd=repo.parent) == source_slug_for(str(repo), None)


def test_source_slug_for_rejects_an_unresolvable_argument(home: Path) -> None:
    with pytest.raises(WorkspaceError):
        source_slug_for("nope-not-a-thing", None)
