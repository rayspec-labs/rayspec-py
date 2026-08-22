"""``rayspec.resources.walk_files``: the one recursive listing of a packaged data tree.

The walk is shared by the skill, the ``rayspec init`` scaffolds and the example corpus, so its
contract is checked directly rather than through whichever of the three happens to exercise it.
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable

import pytest

from rayspec.resources import walk_files


def _skill_tree() -> Traversable:
    return resources.files("rayspec.skill") / "rayspec"


def test_predicates_see_paths_relative_to_the_walk_root() -> None:
    seen: list[tuple[str, str]] = []

    def keep(rel: str, name: str) -> bool:
        seen.append((rel, name))
        return True

    found = walk_files(_skill_tree(), keep_file=keep)
    assert ("SKILL.md", "SKILL.md") in seen
    assert any(rel.startswith("references/") and rel.endswith(name) for rel, name in seen)
    paths = [rel for rel, _ in found]
    assert paths == sorted(paths)


def test_a_skipped_directory_is_not_descended_into() -> None:
    found = walk_files(_skill_tree(), keep_dir=lambda rel, name: False)
    assert [rel for rel, _ in found] == ["SKILL.md"]


def test_the_walk_has_no_public_recursion_knob() -> None:
    """``rel`` is documented as relative to the walk's root, so nothing may prepend to it.

    A caller that could pass the recursion's own prefix would make that documentation false for
    every predicate it hands in.
    """
    with pytest.raises(TypeError):
        walk_files(_skill_tree(), prefix="somewhere/")  # type: ignore[call-arg]
