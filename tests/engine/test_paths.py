from __future__ import annotations

import pytest

from rayspec.engine.paths import StepPath


def test_parse_and_str_roundtrip():
    p = StepPath.parse("build[2]/fix_all[0]/patch")
    assert str(p) == "build[2]/fix_all[0]/patch"
    assert p.segments == (("build", 2), ("fix_all", 0), ("patch", None))
    assert p.leaf_id == "patch" and p.depth == 3


def test_root_and_child_navigation():
    root = StepPath.root()
    assert str(root) == "" and root.is_root and root.depth == 0
    build = root.child("build")
    assert str(build) == "build" and build.parent == root
    it = build.indexed(2)
    assert str(it) == "build[2]" and it.index == 2 and it.leaf_id == "build"
    impl = it.child("implement")
    assert str(impl) == "build[2]/implement"
    assert impl.parent == it and it.parent == root


def test_child_rejects_bad_ids():
    with pytest.raises(ValueError):
        StepPath.root().child("Bad-Id")
    with pytest.raises(ValueError):
        StepPath.root().child("steps")


def test_parse_rejects_malformed():
    for bad in ["build[", "build[x]", "/a", "a//b", "a[1][2]", "A"]:
        with pytest.raises(ValueError):
            StepPath.parse(bad)


def test_fs_path_is_filesystem_safe_and_matches_str():
    p = StepPath.parse("review/lint[3]")
    assert p.fs_path() == "review/lint[3]"


def test_glob_matching_for_stubs():
    p = StepPath.parse("build[2]/implement")
    assert p.matches("build[*]/implement")
    assert p.matches("build/*/implement") is False
    assert p.matches("*/implement")
    assert not p.matches("implement")


def test_paths_are_hashable_and_comparable():
    a = StepPath.parse("a/b")
    b = StepPath.parse("a/b")
    assert a == b and hash(a) == hash(b)
    assert StepPath.parse("a") != StepPath.parse("a[1]")
    assert sorted([StepPath.parse("b"), StepPath.parse("a")])[0] == StepPath.parse("a")
