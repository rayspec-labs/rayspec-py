# SPDX-License-Identifier: Apache-2.0
"""``engine.context_rebuild`` — the lexical context of a stored step, rebuilt after the fact.

The helper `rayspec explain`, `rayspec eval` and `rayspec plan --render` share: it must place a
step in exactly the scope chain the engine gave it (loop `iteration`, each `item`, an include
body's own inputs and lexical isolation) without running anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rayspec.engine import context_rebuild
from rayspec.errors import RayspecError
from rayspec.providers.stub import StubScript
from rayspec.store.model import RunRecord
from rayspec.templating import RayspecUndefined, TemplateEngine

from .conftest import Harness

pytestmark = pytest.mark.anyio

WF = """
rayspec: 1
name: t
inputs:
  topic: {type: string, default: bugs}
steps:
  - id: fetch
    shell: "printf hello"
  - id: build
    needs: [fetch]
    loop:
      max_iterations: 2
      until: "iteration.n == 2"
      steps:
        - id: implement
          shell: "printf i{{ iteration.n }}"
        - id: check
          needs: [implement]
          shell: "printf c{{ iteration.n }}"
  - id: fan
    needs: [fetch]
    each: "['a', 'b']"
    as: letter
    steps:
      - id: patch
        shell: "printf p-{{ letter }}"
  - id: never
    needs: [fetch]
    when: "false"
    shell: "printf nope"
"""

INCLUDED = """
rayspec: 1
name: inner
inputs:
  who: {type: string}
steps:
  - id: greet
    shell: "printf hi-{{ inputs.who }}"
outputs:
  greeting: "{{ steps.greet.output }}"
"""

WF_INCLUDE = """
rayspec: 1
name: outer
steps:
  - id: seed
    shell: "printf world"
  - id: sub
    needs: [seed]
    include: inner
    with: {who: "{{ steps.seed.output }}"}
"""


async def _run(
    harness: Harness, name: str, text: str, inputs: dict[str, Any] | None = None
) -> tuple[RunRecord, Any]:
    harness.workflow(name, text)
    resolved = harness.load(name)
    result = await harness.run(resolved, inputs or {})
    assert result.status.value == "succeeded", result.reason
    return harness.store.load(result.run_id), resolved


def _rebuilder(harness: Harness, run: RunRecord, resolved: Any) -> context_rebuild.ContextRebuilder:
    return context_rebuild.from_run(run, resolved, store=harness.store)


async def test_root_scope_sees_every_recorded_root_step(harness: Harness) -> None:
    run, resolved = await _run(harness, "t", WF, {"topic": "bugs"})
    engine = TemplateEngine()
    root = _rebuilder(harness, run, resolved).at()
    assert root.step is None and root.def_path == ""
    assert engine.eval_expr("steps.fetch.output", root.context) == "hello"
    assert engine.eval_expr("inputs.topic", root.context) == "bugs"
    assert engine.eval_expr("run.workflow", root.context) == "t"
    # a body step is not addressable from the root scope — with the engine's own hint
    with pytest.raises(RayspecError) as exc:
        engine.eval_expr("steps.implement.output", root.context)
    assert "inside loop 'build'" in str(exc.value)


async def test_loop_body_scope_carries_iteration_and_prev(harness: Harness) -> None:
    run, resolved = await _run(harness, "t", WF)
    engine = TemplateEngine()
    rebuilder = _rebuilder(harness, run, resolved)
    second = rebuilder.at("build[2]/implement")
    assert second.def_path == "build/implement"
    assert str(second.record_path) == "build[2]/implement"
    assert second.record is not None and second.record.status.value == "succeeded"
    assert engine.eval_expr("iteration.n", second.context) == 2
    assert engine.eval_expr("iteration.max", second.context) == 2
    assert engine.eval_expr("iteration.first", second.context) is False
    assert engine.eval_expr("iteration.prev.implement.output", second.context) == "i1"
    assert engine.eval_expr("steps.fetch.output", second.context) == "hello"
    first = rebuilder.at("build[1]/implement")
    assert engine.eval_expr("iteration.first", first.context) is True
    assert isinstance(engine.eval_expr("iteration.prev | default(none)", first.context), type(None))


async def test_each_body_scope_binds_the_item_and_index(harness: Harness) -> None:
    run, resolved = await _run(harness, "t", WF)
    engine = TemplateEngine()
    second = _rebuilder(harness, run, resolved).at("fan[1]/patch")
    assert engine.eval_expr("letter", second.context) == "b"
    assert engine.eval_expr("each.index", second.context) == 1
    assert engine.eval_expr("each.total", second.context) == 2
    assert engine.eval_expr("steps.patch.output | default('')", second.context) == "p-b"


async def test_include_body_is_a_closed_lexical_scope_with_its_own_inputs(
    harness: Harness,
) -> None:
    harness.write("workflows/inner.yaml", INCLUDED)
    run, resolved = await _run(harness, "outer", WF_INCLUDE)
    engine = TemplateEngine()
    inner = _rebuilder(harness, run, resolved).at("sub/greet")
    assert engine.eval_expr("inputs.who", inner.context) == "world"
    with pytest.raises(RayspecError):
        engine.eval_expr("steps.seed.output", inner.context)


async def test_skipped_step_keeps_its_record_and_hint(harness: Harness) -> None:
    run, resolved = await _run(harness, "t", WF)
    engine = TemplateEngine()
    root = _rebuilder(harness, run, resolved).at("never")
    assert root.record is not None and root.record.skip_reason == "when_false"
    value = engine.eval_expr("steps.never.ok | default('undefined')", root.context)
    assert value == "undefined"
    assert isinstance(root.context["steps"]["never"].resolve("output"), RayspecUndefined)


async def test_unknown_step_path_is_a_friendly_error(harness: Harness) -> None:
    run, resolved = await _run(harness, "t", WF)
    rebuilder = _rebuilder(harness, run, resolved)
    with pytest.raises(context_rebuild.ContextRebuildError) as exc:
        rebuilder.at("nope")
    assert "nope" in str(exc.value)
    with pytest.raises(context_rebuild.ContextRebuildError):
        rebuilder.at("fetch/inner")


def test_plan_source_stubs_upstream_values(harness: Harness) -> None:
    harness.workflow("t", WF)
    resolved = harness.load("t")
    engine = TemplateEngine()
    script = StubScript.from_dict({"steps": {"fetch": {"text": "stubbed"}}})
    rebuilder = context_rebuild.from_plan(
        resolved, inputs={"topic": "bugs"}, project_root=harness.root, script=script
    )
    root = rebuilder.at()
    assert engine.eval_expr("steps.fetch.output", root.context) == "stubbed"
    # a step without a stub entry gets a visible placeholder, never an undefined
    body = rebuilder.at("build/implement")
    assert engine.eval_expr("iteration.n", body.context) == 1
    assert engine.eval_expr("steps.check.output", body.context) == "<build[1]/check output>"
    item = rebuilder.at("fan/patch")
    assert engine.eval_expr("letter", item.context) == "a"


def test_stub_precedence_matches_the_runner(harness: Harness) -> None:
    """A glob declared before an exact key must not win — `StubScript.resolve` decides.

    The preview and the run have to agree on the value the downstream step sees; deriving the
    precedence a second time here is how they silently drift apart.
    """
    harness.workflow("t", WF)
    resolved = harness.load("t")
    engine = TemplateEngine()
    script = StubScript.from_dict(
        {"steps": {"*": {"text": "FROM_GLOB"}, "fetch": {"text": "FROM_EXACT"}}}
    )
    entry = script.resolve("fetch", "")
    assert entry is not None and entry.outcome_for(1).text == "FROM_EXACT"  # what the run writes
    rebuilder = context_rebuild.from_plan(
        resolved, inputs={"topic": "bugs"}, project_root=harness.root, script=script
    )
    assert engine.eval_expr("steps.fetch.output", rebuilder.at().context) == "FROM_EXACT"


def test_a_match_entry_never_feeds_the_preview(harness: Harness) -> None:
    """`match:` keys on the prompt, which does not exist before the upstream values are known."""
    harness.workflow("t", WF)
    resolved = harness.load("t")
    engine = TemplateEngine()
    script = StubScript.from_dict({"match": [{"prompt_regex": ".*", "text": "FROM_MATCH"}]})
    rebuilder = context_rebuild.from_plan(
        resolved, inputs={"topic": "bugs"}, project_root=harness.root, script=script
    )
    assert engine.eval_expr("steps.fetch.output", rebuilder.at().context) == "<fetch output>"


def test_render_body_shows_the_script_when_a_value_is_too_large_to_inline() -> None:
    """A >64 KiB upstream value is exactly when a user reaches for `explain`.

    The preview must still show the script; the oversized slot degrades to a visible placeholder
    naming a file the reader can open, never to an internal `pass spill_dir=` hint.
    """
    engine = TemplateEngine()
    ctx: dict[str, Any] = {"inputs": {"big": "x" * 70_000}}
    shell = context_rebuild.render_body(engine, "wc -c '{{ inputs.big }}'", ctx, kind="shell")
    assert shell.error is None
    assert shell.text is not None and shell.text.startswith("wc -c ")
    assert "spill_dir" not in shell.text
    assert "70000 bytes" in shell.text and "output file" in shell.text
    python = context_rebuild.render_body(engine, "v = {{ inputs.big }}", ctx, kind="python")
    assert python.error is None and python.text is not None
    assert "too large to inline" in python.text


def test_render_body_leaves_no_spill_files_behind() -> None:
    engine = TemplateEngine()
    ctx: dict[str, Any] = {"inputs": {"big": "x" * 70_000}}
    rendered = context_rebuild.render_body(engine, "echo '{{ inputs.big }}'", ctx, kind="shell")
    assert rendered.error is None and rendered.text is not None
    leftovers = [word for word in rendered.text.split("'") if word.startswith("/")]
    assert not [Path(w) for w in leftovers if Path(w).exists()]


async def test_a_definition_path_resolves_to_the_record_path_it_previews(
    harness: Harness,
) -> None:
    """`build/implement` reads as iteration 1 — `record_path` must say so, and find the record."""
    run, resolved = await _run(harness, "t", WF, {"topic": "bugs"})
    rebuilder = _rebuilder(harness, run, resolved)
    loop_body = rebuilder.at("build/implement")
    assert str(loop_body.record_path) == "build[1]/implement"
    assert loop_body.def_path == "build/implement"
    assert loop_body.record is not None and loop_body.record.path == "build[1]/implement"
    each_body = rebuilder.at("fan/patch")
    assert str(each_body.record_path) == "fan[0]/patch"
    assert each_body.record is not None
    # an explicit index still wins
    assert str(rebuilder.at("build[2]/implement").record_path) == "build[2]/implement"


async def test_an_index_on_a_step_that_has_no_iterations_is_refused(harness: Harness) -> None:
    """`a[999]` on a plain shell step used to be silently discarded and answered about `a`."""
    run, resolved = await _run(harness, "t", WF, {"topic": "bugs"})
    rebuilder = _rebuilder(harness, run, resolved)
    with pytest.raises(context_rebuild.ContextRebuildError) as exc:
        rebuilder.at("fetch[999]")
    assert "fetch" in str(exc.value) and "shell" in str(exc.value)
    with pytest.raises(context_rebuild.ContextRebuildError):
        rebuilder.at("build[1]/implement[2]")
