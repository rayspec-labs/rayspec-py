"""The policy DOCUMENT against everything that reads it — asked once, in one place.

``Policy`` is a strict model, so a key it does not declare is not ignored: it is a load-time error
that stops every command. That makes "which keys exist" a question with two answers that have to
agree — the model's fields, and the keys the shipped code and the shipped pages actually use — and
they did not. ``budget:`` and ``max_consecutive_failures:`` are documented on
``docs/runs-and-resume.md`` and read by :mod:`rayspec.limits`, and following that page hard-failed
every command with ``unknown field budget for policy`` while ``limits_policy()`` never saw them.

Two parts of one release, each correct on its own. So the same completeness question the trigger
and the allow-list are held to is asked of the document: every policy block a page shows has to
parse, and every key the limits layer reads has to be a field.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from rayspec.policy.model import BudgetPolicy, Policy

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A fenced YAML block whose first line says it is a policy document. Both spellings the pages
#: use are accepted; a page that invents a third one is not covered, which is why the count is
#: asserted below rather than left to chance.
_BLOCK = re.compile(
    r"```ya?ml\n(#\s*(?:\.rayspec/)?policy(?:\.yaml| file)\s*\n.*?)```", re.DOTALL | re.IGNORECASE
)


def policy_blocks() -> list[tuple[str, str]]:
    """``(page, block)`` for every policy document any shipped page shows."""
    out: list[tuple[str, str]] = []
    for page in sorted((REPO_ROOT / "docs").glob("*.md")):
        text = page.read_text(encoding="utf-8")
        out.extend((page.name, block) for block in _BLOCK.findall(text))
    return out


def test_the_pages_do_show_policy_documents() -> None:
    """A scan that silently matches nothing is a completeness test that proves nothing."""
    pages = {page for page, _ in policy_blocks()}
    assert {"policy.md", "runs-and-resume.md"} <= pages, sorted(pages)


@pytest.mark.parametrize(
    ("page", "block"),
    policy_blocks(),
    ids=[f"{page}-{i}" for i, (page, _) in enumerate(policy_blocks())],
)
def test_every_policy_document_a_page_shows_actually_parses(page: str, block: str) -> None:
    """Following a page must not be a load-time error naming a key the page told you to write."""
    Policy.parse(yaml.safe_load(block) or {}, source=page)


def _keys_read_by(module: Path) -> set[str]:
    """Every ``_get(x, "<literal>")`` key one module reads off a policy object."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_get" or len(node.args) != 2:
            continue
        key = node.args[1]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out.add(key.value)
    return out


def test_every_key_the_limits_layer_reads_is_a_field_of_the_document() -> None:
    """The consumer is read off its source, so a key it starts reading has to be declared here.

    ``rayspec.limits`` reaches the policy through ``load_policy`` and narrows it by NAME. A name
    the document does not carry is silently ``None`` — an operational ceiling that is written
    down, parsed and then not applied, which is the one failure mode a guardrail may not have.
    """
    fields = set(Policy.model_fields) | set(BudgetPolicy.model_fields)
    read = _keys_read_by(REPO_ROOT / "src" / "rayspec" / "limits" / "policy.py")
    assert read, "the scan found no keys; keep it in step with how limits reads the policy"
    assert sorted(read - fields) == [], (
        f"declare these on Policy/BudgetPolicy: {sorted(read - fields)}"
    )


DOCUMENTED = """budget:
  per_run: 2.00
  per_day: 20.00
  per_month: 200.00
max_consecutive_failures: 3
max_concurrent_runs:
  claude: 2
  codex: 1
"""


def test_the_documented_operational_limits_reach_the_layer_that_enforces_them(tmp_path) -> None:
    """The combined check: the page's own YAML, loaded, arriving as the ceilings it describes."""
    from rayspec.limits import limits_policy

    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec").mkdir(parents=True)
    home.mkdir()
    (root / ".rayspec" / "policy.yaml").write_text(DOCUMENTED, encoding="utf-8")

    policy = limits_policy(root, home=home, environ={})
    assert policy.active
    assert policy.budget.per_run == 2.0
    assert policy.budget.per_day == 20.0
    assert policy.budget.per_month == 200.0
    assert policy.budget.max_consecutive_failures == 3
    assert dict(policy.max_concurrent_runs) == {"claude": 2, "codex": 1}
    assert policy.warnings == ()


def test_the_operational_ceilings_layer_most_restrictive_wins(tmp_path) -> None:
    """A user layer cannot raise what the project layer set — the property the page promises."""
    from rayspec.limits import limits_policy

    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec").mkdir(parents=True)
    home.mkdir()
    (root / ".rayspec" / "policy.yaml").write_text(
        "budget:\n  per_day: 5.0\nmax_concurrent_runs:\n  claude: 1\n", encoding="utf-8"
    )
    (home / "policy.yaml").write_text(
        "budget:\n  per_day: 50.0\n  per_run: 1.0\nmax_concurrent_runs: 8\n"
        "max_consecutive_failures: 2\n",
        encoding="utf-8",
    )
    policy = limits_policy(root, home=home, environ={})
    assert policy.budget.per_day == 5.0  # the project layer, not the wider user one
    assert policy.budget.per_run == 1.0  # the only layer with an opinion
    assert policy.budget.max_consecutive_failures == 2
    assert dict(policy.max_concurrent_runs) == {"claude": 1, "*": 8}


def test_an_operational_ceiling_is_a_control_like_every_other_policy_key(tmp_path) -> None:
    """A spending envelope governs the run, so it closes the escape hatch beside it."""
    from rayspec.policy import load_policy
    from rayspec.policy.controls import policy_controls

    root = tmp_path / "proj"
    home = tmp_path / "home"
    (root / ".rayspec").mkdir(parents=True)
    home.mkdir()
    (root / ".rayspec" / "policy.yaml").write_text(DOCUMENTED, encoding="utf-8")
    controls = policy_controls(load_policy(root, home=home, environ={}))
    keys = {control.key for control in controls}
    assert "budget.per_day" in keys
    assert "max_consecutive_failures" in keys
    assert "max_concurrent_runs" in keys
    for control in controls:
        assert control.tags, f"{control.key}: a control covers at least one kind of restriction"
        assert control.sources, f"{control.key}: a control names the file that imposes it"
