"""The workspace layer creates its directories private (``0700``) and the registry
``config.yaml`` ``0600`` regardless of the umask — only NEW paths; pre-existing user directories
keep their modes and the git checkout inside a worktree keeps git's modes."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from rayspec.workspace.project import project_dir, project_from_root
from rayspec.workspace.registry import add_project, user_config_path
from rayspec.workspace.repos import ensure_bare_source, source_dir
from rayspec.workspace.worktrees import create_worktree, recreate_worktree, worktrees_dir

from .gitfixtures import git, make_repo

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


@pytest.fixture(params=[0o022, 0o002], ids=lambda u: f"umask{u:03o}")
def umask(request: pytest.FixtureRequest) -> Iterator[int]:
    old = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_private_chain(home: Path, leaf: Path) -> None:
    """Every directory the call created between (and including) ``home``'s children and ``leaf``
    is ``0700``."""
    current = leaf
    while current != home:
        assert current.is_dir(), current
        assert _mode(current) == 0o700, (current, oct(_mode(current)))
        current = current.parent


def test_create_worktree_makes_private_dirs(repo: Path, umask: int, tmp_path: Path) -> None:
    home = tmp_path / "fresh-home"  # does not exist yet: the worktree is the first writer
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="fix", run_id="20260820-101500-ab3k")
    wt_dir = worktrees_dir(home, project.slug)
    assert wt.path.parent == wt_dir
    assert _mode(home) == 0o700
    _assert_private_chain(home, wt_dir)
    # the checkout itself is git's: a normal directory under the umask, files as committed
    assert _mode(wt.path) == (0o777 & ~umask)
    assert (wt.path / "file0.txt").is_file()


def test_recreate_worktree_makes_private_parent(repo: Path, home: Path, umask: int) -> None:
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="fix", run_id="20260820-101500-ab3k")
    shutil.rmtree(wt.path)
    shutil.rmtree(wt.path.parent)  # the worktrees/ dir is gone too
    again = recreate_worktree(project, path=wt.path, branch=wt.branch)
    assert again.path == wt.path and again.path.is_dir()
    assert _mode(wt.path.parent) == 0o700


def test_bare_source_parent_is_private(tmp_path: Path, umask: int) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=1)
    git("update-server-info", cwd=upstream)
    url = f"file://{upstream}"
    home = tmp_path / "fresh-home"
    dest = ensure_bare_source(url, home=home)
    slug = str(dest.parent.relative_to(home / "projects"))
    assert dest == source_dir(home, slug) and slug.startswith("local/upstream-")
    _assert_private_chain(home, dest.parent)
    assert _mode(home) == 0o700
    # the bare clone itself is git's (``git init --bare``): not ours to tighten
    assert dest.is_dir() and (dest / "HEAD").is_file()


def test_registry_config_is_private(tmp_path: Path, umask: int) -> None:
    home = tmp_path / "fresh-home"
    add_project(home, "demo", "git@github.com:o/r.git", base=None)
    path = user_config_path(home)
    assert _mode(home) == 0o700
    assert _mode(path) == 0o600, oct(_mode(path))
    assert not path.with_name(path.name + ".tmp").exists()
    assert yaml.safe_load(path.read_text())["projects"][0]["name"] == "demo"
    # a rewrite keeps the file private and the content atomic
    add_project(home, "other", "git@github.com:o/s.git", base="main")
    assert _mode(path) == 0o600
    names = [p["name"] for p in yaml.safe_load(path.read_text())["projects"]]
    assert names == ["demo", "other"]


def test_registry_config_stays_private_despite_stale_tmp(tmp_path: Path, umask: int) -> None:
    """Review of a stale, world-readable ``config.yaml.tmp`` (a crash of an older version,
    or anything else that put a file there) must not be reused as the temp file — the rewritten
    ``config.yaml`` is always created fresh ``0600``."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = user_config_path(home)
    stale = path.with_name(path.name + ".tmp")
    stale.write_text("projects: []\n")
    os.chmod(stale, 0o644)
    add_project(home, "demo", "git@github.com:o/r.git", base=None)
    assert _mode(path) == 0o600, oct(_mode(path))
    assert yaml.safe_load(path.read_text())["projects"][0]["name"] == "demo"
    # no temp file of ours is left behind (the stale one is not ours to touch)
    leftovers = [p for p in home.iterdir() if p.name.startswith("config.yaml.") and p != stale]
    assert leftovers == []


def test_pre_existing_user_dirs_are_left_alone(repo: Path, tmp_path: Path, umask: int) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    os.chmod(home, 0o755)
    (home / "projects").mkdir(mode=0o755)
    os.chmod(home / "projects", 0o755)
    project = project_from_root(repo)
    create_worktree(project, home=home, workflow_name="fix", run_id="20260820-101500-ab3k")
    assert _mode(home) == 0o755  # a user's directory: never re-chmodded
    assert _mode(home / "projects") == 0o755
    assert _mode(project_dir(home, project.slug)) == 0o700  # created by this call
    assert _mode(worktrees_dir(home, project.slug)) == 0o700
    # config.yaml is rayspec's own file (the registry is its only writer): a rewrite — tmp file
    # + os.replace, like run.json — leaves it 0600 even when an older version created it 0644
    cfg = user_config_path(home)
    cfg.write_text("projects: []\n")
    os.chmod(cfg, 0o644)
    add_project(home, "demo", "git@github.com:o/r.git", base=None)
    assert _mode(cfg) == 0o600
