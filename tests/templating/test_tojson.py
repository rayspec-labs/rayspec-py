# SPDX-License-Identifier: Apache-2.0
"""``| tojson`` over the rayspec context roots.

``{{ inputs | tojson }}`` is the documented way to hand a whole step's inputs to a script — the
one whole-``inputs`` construct the ``secret: true`` rules permit in a ``shell:``/``python:``
``env:`` mapping (docs/schema.md § Secret inputs). The context roots are not plain
dicts (``Namespace``, ``StepsNamespace``, ``StepView``), so the stock serialiser refused them
with a raw ``TypeError: Object of type Namespace is not JSON serializable`` — at run time, after
``rayspec validate`` had passed.

The rule pinned here: ``tojson`` serialises a context value the same way ``RAYSPEC_CONTEXT``
does (:func:`rayspec.templating.scope.to_jsonable`), and an undefined still fails loudly.
"""

from __future__ import annotations

import ast
import json

import pytest

from rayspec.schema import StepStatus
from rayspec.templating import (
    Scope,
    StepView,
    TemplateEngine,
    TemplateRenderError,
    build_context,
)


@pytest.fixture
def engine() -> TemplateEngine:
    return TemplateEngine()


@pytest.fixture
def ctx():
    scope = Scope(
        None,
        {
            "build": StepView(
                id="build",
                kind="shell",
                status=StepStatus.SUCCEEDED,
                output="build log",
                ok=True,
                exit_code=0,
            ),
            "plan": StepView(
                id="plan",
                kind="prompt",
                status=StepStatus.SUCCEEDED,
                output={"score": 7, "tags": ["a", "b"]},
                ok=True,
            ),
        },
    )
    return build_context(
        scope,
        inputs={"issue": 12, "name": "o'neil", "tags": ["x"], "flag": False},
        run={"id": "r1", "workdir": "/w", "artifacts_dir": "/a", "state_dir": "/s"},
        project={"root": "/p", "name": "p", "slug": "local/p"},
        env={"HOME": "/home/u"},
    )


def test_inputs_tojson_renders(engine: TemplateEngine, ctx) -> None:
    """The canonical spelling: a whole ``inputs`` root as one JSON document."""
    rendered = engine.render_str("{{ inputs | tojson }}", ctx)
    assert json.loads(rendered) == {
        "issue": 12,
        "name": "o'neil",
        "tags": ["x"],
        "flag": False,
    }


def test_steps_tojson_renders(engine: TemplateEngine, ctx) -> None:
    """``steps`` is a view over the scope chain, not a dict — it serialises like the context file."""
    parsed = json.loads(engine.render_str("{{ steps | tojson }}", ctx))
    assert set(parsed) == {"build", "plan"}
    assert parsed["build"]["output"] == "build log"
    assert parsed["build"]["status"] == "succeeded"
    assert parsed["plan"]["output"] == {"score": 7, "tags": ["a", "b"]}


@pytest.mark.parametrize("root", ["run", "project", "env"])
def test_every_context_root_tojson_renders(engine: TemplateEngine, ctx, root: str) -> None:
    """No root may be the one that blows up at run time."""
    assert isinstance(json.loads(engine.render_str(f"{{{{ {root} | tojson }}}}", ctx)), dict)


def test_a_single_step_view_tojson_renders(engine: TemplateEngine, ctx) -> None:
    parsed = json.loads(engine.render_str("{{ steps.plan | tojson }}", ctx))
    assert parsed["id"] == "plan" and parsed["kind"] == "prompt"


def test_tojson_in_a_shell_body_becomes_an_env_slot(engine: TemplateEngine, ctx) -> None:
    """The shell environment substitutes the JSON through ``${RAYSPEC_V1}``, never inline."""
    rendered = engine.render_shell('printf "%s" "{{ inputs | tojson }}"', ctx)
    assert "${RAYSPEC_V1}" in rendered.script
    assert json.loads(rendered.env["RAYSPEC_V1"])["issue"] == 12


def test_tojson_in_a_python_body_is_a_literal(engine: TemplateEngine, ctx) -> None:
    """The python environment substitutes the JSON as a Python string literal."""
    rendered = engine.render_python("data = {{ inputs | tojson }}", ctx)
    literal = ast.literal_eval(rendered.script.split("=", 1)[1].strip())
    assert json.loads(literal)["issue"] == 12


def test_an_undefined_still_fails_loudly(engine: TemplateEngine, ctx) -> None:
    """``tojson`` must not turn a typo into ``null``: the strictness of every other path holds."""
    with pytest.raises(TemplateRenderError) as exc:
        engine.render_str("{{ inputs.nope | tojson }}", ctx)
    assert "nope" in str(exc.value)


def test_a_plain_value_is_unaffected(engine: TemplateEngine, ctx) -> None:
    """Jinja's own behaviour for ordinary data is untouched (sorted keys, no indent)."""
    assert engine.render_str("{{ {'b': 1, 'a': 2} | tojson }}", ctx) == '{"a": 2, "b": 1}'
    assert engine.render_str("{{ [1, 'x'] | tojson }}", ctx) == '[1, "x"]'
