"""TemplateEngine: three environments, finalizers, expressions, compile helpers, references."""

import json
import shutil
import subprocess
import sys

import pytest

from rayspec.schema import StepStatus
from rayspec.templating import (
    RayspecUndefined,
    Ref,
    RenderedScript,
    Scope,
    StepView,
    TemplateCompileError,
    TemplateEngine,
    TemplateRenderError,
    build_context,
)


@pytest.fixture
def engine() -> TemplateEngine:
    return TemplateEngine()


def _view(id: str, **kw) -> StepView:
    kw.setdefault("kind", "shell")
    kw.setdefault("status", StepStatus.SUCCEEDED)
    return StepView(id=id, **kw)


@pytest.fixture
def ctx():
    scope = Scope(
        None,
        {
            "build": _view("build", output="build log\nDONE", exit_code=0, stderr=""),
            "plan": _view(
                "plan", kind="prompt", output={"score": 7, "tags": ["a", "b"], "url": None}
            ),
            "skipped": _view("skipped", status=StepStatus.SKIPPED, skip_reason="when_false"),
            "loop": _view(
                "loop",
                kind="loop",
                output={"review": "LGTM"},
                body_ids=frozenset({"review"}),
                iterations=2,
                converged=True,
            ),
        },
    )
    return build_context(
        scope,
        inputs={"issue": 12, "name": "o'neil", "tags": ["x"], "flag": False, "big": "z" * 70000},
        run={"id": "r1", "workdir": "/w", "artifacts_dir": "/a", "state_dir": "/s"},
        project={"root": "/p", "name": "p", "slug": "local/p"},
        env={"HOME": "/home/u"},
    )


class TestTextEnv:
    def test_plain_str_and_numbers(self, engine, ctx):
        assert (
            engine.render_text("issue {{ inputs.issue }} by {{ inputs.name }}", ctx)
            == "issue 12 by o'neil"
        )
        assert engine.render_text("{{ 1.5 }} {{ 2 }}", ctx) == "1.5 2"

    def test_bool_renders_lowercase(self, engine, ctx):
        assert engine.render_text("flag={{ inputs.flag }}", ctx) == "flag=false"
        assert engine.render_text("x={{ steps.build.exit_code == 0 }}", ctx) == "x=true"

    def test_composites_render_as_json(self, engine, ctx):
        out = engine.render_text("tags: {{ inputs.tags }}\n{{ steps.plan.output }}", ctx)
        assert out.startswith('tags: [\n  "x"\n]\n')
        assert json.loads(out.split("\n", 3)[3]) == {"score": 7, "tags": ["a", "b"], "url": None}

    def test_none_is_an_error_naming_default(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match=r"value is null; use \| default\(\.\.\.\)"):
            engine.render_text("url: {{ steps.plan.output.url }}", ctx)

    def test_undefined_chain_with_default(self, engine, ctx):
        assert (
            engine.render_text("{{ steps.plan.output.missing.deep | default('n/a') }}", ctx)
            == "n/a"
        )
        assert engine.render_text("{{ inputs.nope is defined }}", ctx) is False
        assert engine.render_text("defined={{ inputs.nope is defined }}", ctx) == "defined=false"

    def test_undefined_use_raises_with_hint(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="declare it under inputs:") as exc:
            engine.render_text("{{ inputs.nope }}", ctx)
        assert "'inputs' has no attribute 'nope'" in str(exc.value)
        with pytest.raises(
            TemplateRenderError,
            match="has no attribute 'missing' \\(available: score, tags, url\\)",
        ):
            engine.render_text("{{ steps.plan.output.missing }}", ctx)

    def test_text_output_field_hint(self, engine, ctx):
        with pytest.raises(
            TemplateRenderError, match=r"no output_schema \(try \| fromjson\)"
        ) as exc:
            engine.render_text("{{ steps.build.output.score }}", ctx)
        assert "'steps.build.output'" in str(exc.value)

    def test_text_output_keeps_str_methods(self, engine, ctx):
        assert engine.render_text("{{ steps.build.output.upper() }}", ctx) == "BUILD LOG\nDONE"
        assert engine.render_text("{{ steps.build.output | has_signal('DONE') }}", ctx) is True

    def test_skipped_step_hint(self, engine, ctx):
        with pytest.raises(
            TemplateRenderError, match=r"steps\.skipped\.status == 'succeeded'"
        ) as exc:
            engine.render_text("{{ steps.skipped.output }}", ctx)
        assert "skipped (when_false)" in str(exc.value)

    def test_body_step_from_outside_hint(self, engine, ctx):
        with pytest.raises(
            TemplateRenderError, match=r"inside loop 'loop'; use steps\.loop\.output\.review"
        ):
            engine.render_text("{{ steps.review.output }}", ctx)
        assert engine.render_text("{{ steps.loop.output.review }}", ctx) == "LGTM"

    def test_unknown_step_lists_available(self, engine, ctx):
        with pytest.raises(
            TemplateRenderError,
            match="'steps' has no attribute 'nope' \\(available: build, plan, skipped, loop\\)",
        ):
            engine.render_text("{{ steps.nope.output }}", ctx)

    def test_step_attributes(self, engine, ctx):
        assert (
            engine.render_text(
                "{{ steps.build.status }}/{{ steps.build.ok }}/{{ steps.loop.iterations }}/{{ steps.loop.converged }}",
                ctx,
            )
            == "succeeded/true/2/true"
        )
        with pytest.raises(TemplateRenderError, match="not set for this prompt step"):
            engine.render_text("{{ steps.plan.exit_code }}", ctx)
        assert engine.render_text("{{ steps.plan.exit_code | default('-') }}", ctx) == "-"
        with pytest.raises(TemplateRenderError, match=r"did you mean steps\.plan\.output\.score\?"):
            engine.render_text("{{ steps.plan.score }}", ctx)

    def test_dict_style_step_access(self, engine, ctx):
        assert engine.render_text('exit={{ steps["build"]["exit_code"] }}', ctx) == "exit=0"
        assert engine.render_text("{{ steps['plan'].output['score'] }}", ctx) == 7

    def test_mapping_attribute_is_item_first_then_safe_method(self, engine, ctx):
        # inputs.items is an item lookup, not dict.items
        c = dict(ctx)
        c["data"] = {"items": [1], "x": 2}
        assert engine.render_text("{{ data.items }}", c) == [1]
        assert engine.render_text("items: {{ data.items }}", c) == "items: [\n  1\n]"
        assert (
            engine.render_text(
                "{% for k, v in data.items() %}{{ k }}={{ v }};{% endfor %}", {"data": {"a": 1}}
            )
            == "a=1;"
        )
        with pytest.raises(TemplateRenderError, match="has no attribute 'nope'"):
            engine.render_text("{{ data.nope }}", c)

    def test_sandbox_blocks_private_attributes(self, engine, ctx):
        with pytest.raises(TemplateRenderError):
            engine.render_text("{{ inputs.__class__ }}", ctx)
        with pytest.raises(TemplateRenderError):
            engine.render_text("{{ inputs.tags.__class__.__mro__ }}", ctx)

    def test_trim_and_keep_trailing_newline(self, engine, ctx):
        tpl = "{% if true %}\n  a\n  {% endif %}\nb\n"
        assert engine.render_text(tpl, ctx) == "  a\nb\n"

    def test_single_expression_keeps_type(self, engine, ctx):
        assert engine.render_text("{{ inputs.issue }}", ctx) == 12
        assert engine.render_text("{{ steps.plan.output }}", ctx) == {
            "score": 7,
            "tags": ["a", "b"],
            "url": None,
        }
        assert type(engine.render_text("{{ steps.plan.output }}", ctx)) is dict
        assert engine.render_text("{{ inputs.tags }}", ctx) == ["x"]
        assert engine.render_text("{{ inputs.flag }}", ctx) is False
        assert engine.render_text("{{ steps.plan.output.url }}", ctx) is None
        assert engine.render_text("{{ steps.build.output }}", ctx) == "build log\nDONE"
        assert type(engine.render_text("{{ steps.build.output }}", ctx)) is str
        # not "exactly one expression" → str
        assert engine.render_text("{{ inputs.issue }} ", ctx) == "12 "
        assert engine.render_text("{{ inputs.issue }}{{ inputs.issue }}", ctx) == "1212"
        assert engine.render_text("12", ctx) == "12"
        with pytest.raises(TemplateRenderError, match="has no attribute 'nope'"):
            engine.render_text("{{ inputs.nope }}", ctx)

    def test_filters_available(self, engine, ctx):
        assert engine.render_text("{{ '{\"a\": 1}' | fromjson }}", ctx) == {"a": 1}
        assert engine.render_text("{{ 'score: 42' | regex_search('(\\\\d+)', 1) }}", ctx) == "42"
        assert (
            engine.render_text("{{ 'nothing' | regex_search('\\\\d+') | default('none') }}", ctx)
            == "none"
        )
        with pytest.raises(TemplateRenderError, match=r"use output\.field =="):
            engine.render_text("{{ steps.plan.output | has_signal('DONE') }}", ctx)
        assert engine.render_text("{{ 'ok\\nDONE' is has_signal('DONE') }}", ctx) is True
        assert engine.render_text("{{ [3, 1] | sort | join(',') }}", ctx) == "1,3"

    def test_render_value_deep(self, engine, ctx):
        value = {
            "n": "{{ inputs.issue }}",
            "s": "issue-{{ inputs.issue }}",
            "list": ["{{ inputs.tags }}", 3, True, None],
            "nested": {"flag": "{{ inputs.flag }}"},
        }
        assert engine.render_value(value, ctx) == {
            "n": 12,
            "s": "issue-12",
            "list": [["x"], 3, True, None],
            "nested": {"flag": False},
        }

    def test_render_error_reports_runtime_errors(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="fromjson: invalid JSON"):
            engine.render_text("{{ 'nope' | fromjson }}", ctx)
        with pytest.raises(TemplateRenderError):
            engine.render_text("{{ 1 / 0 }}", ctx)


class TestExpressions:
    def test_eval_expr(self, engine, ctx):
        assert engine.eval_expr("inputs.issue + 1", ctx) == 13
        assert engine.eval_expr("steps.plan.output.tags", ctx) == ["a", "b"]
        assert engine.eval_expr("steps.plan.output", ctx) == {
            "score": 7,
            "tags": ["a", "b"],
            "url": None,
        }
        assert type(engine.eval_expr("steps.plan.output", ctx)) is dict
        assert engine.eval_expr("steps.build.output", ctx) == "build log\nDONE"
        assert type(engine.eval_expr("steps.build.output", ctx)) is str
        assert engine.eval_expr("inputs.nope | default(none)", ctx) is None

    def test_eval_expr_undefined_result_raises(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="has no attribute 'nope'"):
            engine.eval_expr("inputs.nope", ctx)
        with pytest.raises(TemplateRenderError, match=r"steps\.skipped\.status == 'succeeded'"):
            engine.eval_expr("steps.skipped.output", ctx)

    def test_eval_bool(self, engine, ctx):
        assert engine.eval_bool("steps.build.exit_code == 0", ctx) is True
        assert engine.eval_bool("inputs.nope is defined", ctx) is False
        assert engine.eval_bool("steps.build.output | has_signal('DONE')", ctx) is True
        assert engine.eval_bool("steps.skipped.status == 'succeeded'", ctx) is False
        assert (
            engine.eval_bool(
                "steps.skipped.status != 'succeeded' and steps.plan.output.score > 5", ctx
            )
            is True
        )

    @pytest.mark.parametrize(
        ("expr", "got"),
        [
            ("inputs.issue", "int 12"),
            ("steps.build.output", "str 'build log\\nDONE'"),
            ("inputs.tags", "list ['x']"),
            ("none", "NoneType None"),
            ("steps.plan.output.url", "NoneType None"),
        ],
    )
    def test_eval_bool_strict(self, engine, ctx, expr, got):
        with pytest.raises(TemplateRenderError) as exc:
            engine.eval_bool(expr, ctx)
        assert f"must evaluate to true/false, got {got}" in str(exc.value)
        assert "compare explicitly or test emptiness" in str(exc.value)

    def test_eval_bool_truthiness_of_undefined_raises(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="declare it under inputs"):
            engine.eval_bool("inputs.nope", ctx)
        with pytest.raises(TemplateRenderError, match="declare it under inputs"):
            engine.eval_bool("inputs.nope and true", ctx)


class TestCompile:
    def test_compile_template_error(self, engine):
        with pytest.raises(TemplateCompileError) as exc:
            engine.compile_template("line1\n{{ inputs.x }\n", where="steps[0] (id: a).prompt")
        err = exc.value
        assert err.where == "steps[0] (id: a).prompt"
        assert err.lineno == 2
        assert str(err).startswith("steps[0] (id: a).prompt: ")
        assert "line 2" in str(err)

    def test_compile_template_ok_each_kind(self, engine):
        for kind in ("text", "shell", "python"):
            engine.compile_template("{{ inputs.x }}", where="w", kind=kind)

    def test_compile_expr(self, engine):
        engine.compile_expr("steps.a.ok and inputs.x > 1", where="w")
        with pytest.raises(TemplateCompileError) as exc:
            engine.compile_expr("steps.a.ok and", where="steps[1] (id: b).when")
        assert exc.value.where == "steps[1] (id: b).when"
        with pytest.raises(TemplateCompileError, match="chunk after expression"):
            engine.compile_expr("a b", where="w")

    def test_compile_errors_in_shell_kind_use_shell_delimiters(self, engine):
        # `{#` is not a comment in code bodies, so this compiles fine
        engine.compile_template("echo ${#VAR} {{ inputs.x }}", where="w", kind="shell")
        with pytest.raises(TemplateCompileError):
            engine.compile_template("echo ${#VAR} {{ inputs.x }}", where="w", kind="text")


class TestReferences:
    def test_template_refs(self, engine):
        refs = engine.references(
            "{{ steps.a.output.b }} {{ inputs.x }} {% if steps['c'].ok %}{{ iteration.prev.y.output }}{% endif %}"
        )
        assert refs == frozenset(
            {
                Ref("steps", "a", ("output", "b")),
                Ref("inputs", "x", ()),
                Ref("steps", "c", ("ok",)),
                Ref("iteration", "prev", ("y", "output")),
            }
        )

    def test_expr_refs(self, engine):
        refs = engine.references(
            "steps.a.ok and inputs.n > 1 and each.index == 0 and env.HOME and run.id", kind="expr"
        )
        assert Ref("steps", "a", ("ok",)) in refs
        assert Ref("inputs", "n", ()) in refs
        assert Ref("each", "index", ()) in refs
        assert Ref("env", "HOME", ()) in refs
        assert Ref("run", "id", ()) in refs

    def test_refs_in_filters_and_subscripts(self, engine):
        refs = engine.references("{{ steps[inputs.name].output | default(steps.z.output) }}")
        assert Ref("inputs", "name", ()) in refs
        assert Ref("steps", "z", ("output",)) in refs
        assert Ref("steps", None, ()) in refs  # dynamic key: name unknown

    def test_refs_ignore_other_roots_and_shell_delims(self, engine):
        refs = engine.references("echo ${#X} {{ item.name }} {{ steps.a.output }}", kind="shell")
        assert refs == frozenset({Ref("steps", "a", ("output",))})
        assert engine.references("{{ project.root }}") == frozenset({Ref("project", "root", ())})


class TestShellEnv:
    def test_env_refs(self, engine, ctx, tmp_path):
        r = engine.render_shell(
            'echo "{{ inputs.name }}" {{ inputs.issue }}\ngh issue view {{ inputs.issue }}',
            ctx,
            spill_dir=tmp_path,
        )
        assert isinstance(r, RenderedScript)
        assert r.script == 'echo "${RAYSPEC_V1}" ${RAYSPEC_V2}\ngh issue view ${RAYSPEC_V3}'
        assert r.env == {"RAYSPEC_V1": "o'neil", "RAYSPEC_V2": "12", "RAYSPEC_V3": "12"}
        assert r.spills == []

    def test_value_kinds(self, engine, ctx, tmp_path):
        r = engine.render_shell(
            "{{ inputs.flag }} {{ inputs.tags }} {{ steps.plan.output.score }}",
            ctx,
            spill_dir=tmp_path,
        )
        assert r.env["RAYSPEC_V1"] == "false"
        assert json.loads(r.env["RAYSPEC_V2"]) == ["x"]
        assert r.env["RAYSPEC_V3"] == "7"
        with pytest.raises(TemplateRenderError, match="value is null"):
            engine.render_shell("{{ steps.plan.output.url }}", ctx, spill_dir=tmp_path)

    def test_comment_delimiters_let_hash_through(self, engine, ctx, tmp_path):
        r = engine.render_shell(
            'x="{{ inputs.name }}"; echo ${#x} {{# a comment #}}end', ctx, spill_dir=tmp_path
        )
        assert r.script == 'x="${RAYSPEC_V1}"; echo ${#x} end'

    def test_spill_over_64k(self, engine, ctx, tmp_path):
        """The body keeps the plain ``${RAYSPEC_V<n>}`` reference; a preamble line above it
        reads the file back, sentinel and all, so bash's quoting rules and the value's trailing
        bytes are the same either side of the threshold."""
        r = engine.render_shell("cat <<<{{ inputs.big }}", ctx, spill_dir=tmp_path / "tmp")
        assert len(r.spills) == 1
        path = r.spills[0]
        assert path.parent == tmp_path / "tmp"
        assert r.script == (
            f"unset RAYSPEC_V1; RAYSPEC_V1=$(cat '{path}' && printf x) || exit $?; "
            "RAYSPEC_V1=${RAYSPEC_V1%x}\n"
            "cat <<<${RAYSPEC_V1}"
        )
        assert path.read_text() == "z" * 70000
        assert r.env == {}

    def test_a_spill_path_with_a_quote_is_escaped_in_the_preamble(self, engine, ctx, tmp_path):
        """``spill_dir`` is a run directory, and a project the user names reaches into its path.

        A quote there must close nothing: it is escaped the shell's way, ``'"\'"'``.
        """
        r = engine.render_shell("cat {{ inputs.big }}", ctx, spill_dir=tmp_path / "it's here")
        path = r.spills[0]
        assert "'" in str(path)
        quoted = "'" + str(path).replace("'", "'\"'\"'") + "'"
        assert f"$(cat {quoted} && printf x)" in r.script

    def test_spill_requires_dir(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="spill"):
            engine.render_shell("{{ inputs.big }}", ctx)

    def test_raw_block_for_go_templates(self, engine, ctx, tmp_path):
        r = engine.render_shell(
            "docker ps --format '{% raw %}{{.ID}}{% endraw %}' {{ inputs.issue }}",
            ctx,
            spill_dir=tmp_path,
        )
        assert r.script == "docker ps --format '{{.ID}}' ${RAYSPEC_V1}"

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
    def test_bash_inertness_and_word_splitting(self, engine, tmp_path):
        ctx = {"inputs": {"evil": "a'b\"c\n$(rm -rf /) `x` $HOME", "words": "one two"}}
        r = engine.render_shell(
            'printf "%s" "{{ inputs.evil }}"; printf "|%s" {{ inputs.words }}; printf "|%s" "{{ inputs.words }}"',
            ctx,
            spill_dir=tmp_path,
        )
        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", r.script],
            env={**r.env, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout == "a'b\"c\n$(rm -rf /) `x` $HOME|one|two|one two"


class TestPythonEnv:
    def test_repr_literals(self, engine, ctx, tmp_path):
        r = engine.render_python(
            "issue = {{ inputs.issue }}\nname = {{ inputs.name }}\nflag = {{ inputs.flag }}\ntags = {{ inputs.tags }}\nplan = {{ steps.plan.output }}\nnothing = {{ steps.plan.output.url }}\nx = {{ steps.build.output }}",
            ctx,
            spill_dir=tmp_path,
        )
        ns: dict = {}
        exec(r.script, ns)
        assert ns["issue"] == 12
        assert ns["name"] == "o'neil"
        assert ns["flag"] is False
        assert ns["tags"] == ["x"]
        assert ns["plan"] == {"score": 7, "tags": ["a", "b"], "url": None}
        assert ns["nothing"] is None
        assert ns["x"] == "build log\nDONE"
        assert r.env == {}
        assert r.spills == []

    def test_injection_is_inert(self, engine, tmp_path):
        evil = "'); import os; os.system('echo pwned'); ('"
        r = engine.render_python(
            "v = {{ inputs.evil }}\nprint(v)", {"inputs": {"evil": evil}}, spill_dir=tmp_path
        )
        proc = subprocess.run(
            [sys.executable, "-c", r.script], capture_output=True, text=True, check=True
        )
        assert proc.stdout.strip() == evil

    def test_non_json_value_is_error(self, engine, tmp_path):
        with pytest.raises(TemplateRenderError, match="JSON"):
            engine.render_python("x = {{ v }}", {"v": object()}, spill_dir=tmp_path)
        with pytest.raises(TemplateRenderError, match="JSON"):
            engine.render_python("x = {{ v }}", {"v": float("nan")}, spill_dir=tmp_path)

    def test_comment_delimiters_and_hash(self, engine, tmp_path):
        r = engine.render_python(
            "x = {{ v }}  {{# note #}}# {# not a comment\n", {"v": 1}, spill_dir=tmp_path
        )
        assert r.script == "x = 1  # {# not a comment\n"

    def test_spill_over_64k(self, engine, ctx, tmp_path):
        r = engine.render_python("x = {{ inputs.big }}\nprint(len(x))", ctx, spill_dir=tmp_path)
        assert len(r.spills) == 1
        proc = subprocess.run(
            [sys.executable, "-c", r.script], capture_output=True, text=True, check=True
        )
        assert proc.stdout.strip() == "70000"


class TestEndToEnd:
    def test_loop_body_scope(self, engine, tmp_path):
        outer = Scope(
            None,
            {
                "plan": _view("plan", kind="prompt", output={"goal": "fix #12", "files": ["a.py"]}),
                "tests_before": _view(
                    "tests_before",
                    output="3 failed",
                    exit_code=1,
                    ok=False,
                    status=StepStatus.FAILED,
                    tolerated=True,
                ),
            },
        )
        body = outer.child(
            {
                "implement": _view("implement", kind="prompt", output="changed a.py\nDONE"),
                "tests": _view("tests", output="ok", exit_code=0),
            },
            {
                "iteration": {
                    "n": 2,
                    "max": 5,
                    "first": False,
                    "prev": {
                        "tests": _view(
                            "tests",
                            output="1 failed",
                            exit_code=1,
                            ok=False,
                            status=StepStatus.FAILED,
                            tolerated=True,
                        )
                    },
                }
            },
        )
        ctx = build_context(
            body,
            inputs={"issue": 12},
            run={"id": "r1", "workdir": str(tmp_path), "artifacts_dir": "/a", "state_dir": "/s"},
            project={"root": "/p", "name": "p", "slug": "local/p"},
            env={},
        )
        prompt = engine.render_text(
            "{% set plan = steps.plan.output %}"
            "Goal: {{ plan.goal }}\n"
            "Files: {{ plan.files | join(', ') }}\n"
            "Iteration {{ iteration.n }}/{{ iteration.max }}\n"
            "{% if not iteration.first %}Previous test run: {{ iteration.prev.tests.output }}\n{% endif %}"
            "Baseline: {{ steps.tests_before.output }} (exit {{ steps.tests_before.exit_code }})\n"
            "Last implement said DONE: {{ steps.implement.output | has_signal('DONE') }}\n"
            "{{ iteration.prev.review.output | default('no previous review') }}\n",
            ctx,
        )
        assert prompt == (
            "Goal: fix #12\n"
            "Files: a.py\n"
            "Iteration 2/5\n"
            "Previous test run: 1 failed\n"
            "Baseline: 3 failed (exit 1)\n"
            "Last implement said DONE: true\n"
            "no previous review\n"
        )
        script = engine.render_shell(
            "pytest -q {{ steps.plan.output.files | join(' ') }} >{{ run.workdir }}/out.txt",
            ctx,
            spill_dir=tmp_path,
        )
        assert script.script == "pytest -q ${RAYSPEC_V1} >${RAYSPEC_V2}/out.txt"
        assert script.env == {"RAYSPEC_V1": "a.py", "RAYSPEC_V2": str(tmp_path)}
        assert (
            engine.eval_bool(
                "steps.tests.exit_code == 0 and steps.implement.output | has_signal('DONE')", ctx
            )
            is True
        )
        assert engine.eval_bool("iteration.prev.tests.ok", ctx) is False
        # first iteration: prev chainable
        first_ctx = build_context(
            outer.child({}, {"iteration": {"n": 1, "max": 5, "first": True, "prev": None}}),
            inputs={},
            run={},
            project={},
        )
        assert (
            engine.render_text("{{ iteration.prev.tests.output | default('') }}", first_ctx) == ""
        )
        with pytest.raises(TemplateRenderError, match="first iteration"):
            engine.eval_bool("iteration.prev.tests.ok", first_ctx)
        # body steps are not visible from the outer scope
        outer_ctx = build_context(outer, inputs={}, run={}, project={})
        with pytest.raises(TemplateRenderError, match="unknown step 'tests'"):
            engine.eval_bool("steps.tests.ok", outer_ctx)


def test_prompt_in_text_env_with_undefined_no_silent_empty(engine):
    assert isinstance(RayspecUndefined(name="x"), RayspecUndefined)
    with pytest.raises(TemplateRenderError):
        engine.render_text("{{ nothing }}", {})


def test_null_and_undefined_are_render_time_not_load_time(engine):
    # constant folding must not turn a null/undefined into a load-time failure
    engine.compile_template("{{ none }} {{ nothing }}", where="w")
    with pytest.raises(TemplateRenderError, match="value is null"):
        engine.render_text("x {{ none }}", {})


def test_compiled_templates_are_cached(engine):
    a = engine.compile_template("{{ inputs.x }}", where="w")
    b = engine.compile_template("{{ inputs.x }}", where="w")
    assert a is b
    assert engine.render_text("{{ inputs.x }}", {"inputs": {"x": 1}}) == 1
    assert engine.render_text("{{ inputs.x }}", {"inputs": {"x": 2}}) == 2


class TestCodeBodyConstructsRejected:
    """Review blocker: macros / set-blocks / filter-blocks / call-blocks re-finalize already
    finalized fragments in the shell and python environments (double ``${RAYSPEC_V<n>}``
    indirection, mangled placeholders) — they are rejected at compile time with a fix."""

    @pytest.mark.parametrize(
        ("body", "construct"),
        [
            ("{% macro m(v) %}{{ v }}{% endmacro %}echo {{ m(inputs.n) }}", "macro"),
            ("{% set msg %}hi {{ inputs.n }}{% endset %}echo {{ msg }}", "set block"),
            ("{% filter lower %}echo {{ inputs.n }}{% endfilter %}", "filter block"),
            ("{% call inputs.f() %}{{ inputs.n }}{% endcall %}", "call block"),
        ],
    )
    @pytest.mark.parametrize("kind", ["shell", "python"])
    def test_rejected_at_compile_time(self, engine, body, construct, kind):
        with pytest.raises(TemplateCompileError) as exc:
            engine.compile_template(body, where="steps[0] (id: a)." + kind, kind=kind)
        msg = str(exc.value)
        assert exc.value.where == "steps[0] (id: a)." + kind
        assert construct in msg
        assert "{% set x = expr %}" in msg or "inline filter" in msg
        assert exc.value.lineno == 1

    def test_rejected_at_render_time_too(self, engine, tmp_path):
        ctx = {"inputs": {"n": "3"}}
        with pytest.raises(TemplateRenderError, match="macro"):
            engine.render_shell(
                "{% macro m(v) %}{{ v }}{% endmacro %}echo {{ m(inputs.n) }}",
                ctx,
                spill_dir=tmp_path,
            )
        with pytest.raises(TemplateRenderError, match="set block"):
            engine.render_python(
                "{% set msg %}hi {{ inputs.n }}{% endset %}x = {{ msg }}", ctx, spill_dir=tmp_path
            )

    def test_text_environment_still_allows_them(self, engine):
        ctx = {"inputs": {"n": "Abc"}}
        assert (
            engine.render_text(
                "{% macro m(v) %}<{{ v }}>{% endmacro %}{{ m(inputs.n) }} "
                "{% set msg %}hi {{ inputs.n }}{% endset %}{{ msg }} "
                "{% filter lower %}{{ inputs.n }}{% endfilter %}",
                ctx,
            )
            == "<Abc> hi Abc abc"
        )

    def test_plain_set_and_inline_filters_still_work_in_code_bodies(self, engine, tmp_path):
        r = engine.render_shell(
            "{% set msg = 'hi ' ~ inputs.n %}echo {{ msg }} {{ inputs.n | lower }}",
            {"inputs": {"n": "ABC"}},
            spill_dir=tmp_path,
        )
        assert r.script == "echo ${RAYSPEC_V1} ${RAYSPEC_V2}"
        assert r.env == {"RAYSPEC_V1": "hi ABC", "RAYSPEC_V2": "abc"}


class TestReviewShouldFix:
    def test_references_drop_method_segments(self, engine):
        assert engine.references("{{ steps.a.output.strip().x }}") == frozenset(
            {Ref("steps", "a", ("output",))}
        )
        assert engine.references(
            "{% for k, v in steps.inc.output.items() %}{{ k }}{% endfor %}"
        ) == frozenset({Ref("steps", "inc", ("output",))})
        # arguments of the call are still walked
        refs = engine.references("{{ steps.a.output.get(inputs.k, steps.b.output.d) }}")
        assert refs == frozenset(
            {
                Ref("steps", "a", ("output",)),
                Ref("inputs", "k", ()),
                Ref("steps", "b", ("output", "d")),
            }
        )
        assert engine.references("{{ inputs.get('x') }}") == frozenset({Ref("inputs", None, ())})
        assert engine.references("{{ steps.a.output | trim }}") == frozenset(
            {Ref("steps", "a", ("output",))}
        )

    def test_callables_do_not_leak_reprs_into_text(self, engine, ctx):
        with pytest.raises(TemplateRenderError, match="did you mean to call it"):
            engine.render_text("x {{ steps.build.output.upper }}", ctx)
        with pytest.raises(TemplateRenderError, match="did you mean to call it"):
            engine.render_shell("echo {{ steps.build.output.upper }}", ctx)
        # mapping methods other than items/keys/values/get are not exposed at all
        with pytest.raises(TemplateRenderError, match="has no attribute 'copy'"):
            engine.render_text("{{ inputs.copy() }}", ctx)
        with pytest.raises(TemplateRenderError, match="method 'keys'; did you mean to call it"):
            engine.render_text("x {{ steps.plan.output.keys }}", ctx)
        with pytest.raises(TemplateRenderError, match="did you mean to call it"):
            engine.render_str("{{ inputs.get }}", ctx)
        assert engine.render_text("{{ steps.plan.output.keys() | list }}", ctx) == [
            "score",
            "tags",
            "url",
        ]

    def test_unknown_types_are_an_error_not_a_repr(self, engine):
        from rayspec.templating import stringify_text

        with pytest.raises(TemplateRenderError, match="cannot render object"):
            stringify_text(object())
        with pytest.raises(TemplateRenderError, match="did you mean to call it"):
            stringify_text(len)
        with pytest.raises(TemplateRenderError, match="cannot render object"):
            engine.render_text("x {{ v }}", {"v": object()})

    def test_spills_do_not_collide_across_renders(self, engine, ctx, tmp_path):
        r1 = engine.render_shell("cat {{ inputs.big }}", ctx, spill_dir=tmp_path)
        r2 = engine.render_shell("cat {{ inputs.big }}", ctx, spill_dir=tmp_path)
        r3 = TemplateEngine().render_python("x = {{ inputs.big }}", ctx, spill_dir=tmp_path)
        paths = {r1.spills[0], r2.spills[0], r3.spills[0]}
        assert len(paths) == 3
        for p in paths:
            assert p.parent == tmp_path
            assert p.name.startswith("v1")
            assert p.exists()
        assert r1.spills[0].read_text() == "z" * 70000
        assert json.loads(r3.spills[0].read_text()) == "z" * 70000

    def test_relative_spill_dir_is_made_absolute(self, engine, ctx, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        r = engine.render_shell("cat {{ inputs.big }}", ctx, spill_dir=Path("rel"))
        assert r.spills[0].is_absolute()
        assert str(tmp_path / "rel") in r.script
        assert "'rel/" not in r.script
        r2 = engine.render_python("x = {{ inputs.big }}", ctx, spill_dir="rel")
        assert r2.spills[0].is_absolute()
        assert str(tmp_path / "rel") in r2.script

    def test_step_model_attribute(self, engine, ctx):
        from rayspec.templating import STEP_ATTRIBUTES

        assert "model" in STEP_ATTRIBUTES
        scope = Scope(
            None,
            {
                "p": _view("p", kind="prompt", output="x", model="claude-sonnet"),
                "q": _view("q", kind="prompt", output="y"),
            },
        )
        c = build_context(scope, inputs={}, run={}, project={})
        assert engine.render_text("{{ steps.p.model }}", c) == "claude-sonnet"
        assert engine.render_text("{{ steps.q.model | default('-') }}", c) == "-"
        with pytest.raises(TemplateRenderError, match=r"steps\.q\.model is not set"):
            engine.render_text("{{ steps.q.model }}", c)
        assert engine.render_text("{{ steps.p.model }}", c) == "claude-sonnet"
        assert "model" in engine.render_str("{{ steps.p }}", c)

    def test_render_str_for_text_fields(self, engine, ctx):
        assert engine.render_str("{{ inputs.issue }}", ctx) == "12"
        assert engine.render_str("issue {{ inputs.issue }}", ctx) == "issue 12"
        assert engine.render_str("{{ inputs.flag }}", ctx) == "false"
        assert engine.render_str("{{ steps.plan.output }}", ctx) == json.dumps(
            {"score": 7, "tags": ["a", "b"], "url": None}, indent=2
        )
        with pytest.raises(TemplateRenderError, match="value is null"):
            engine.render_str("{{ steps.plan.output.url }}", ctx)
        with pytest.raises(TemplateRenderError, match="has no attribute 'nope'"):
            engine.render_str("{{ inputs.nope }}", ctx)


class TestReviewNits:
    def test_python_spill_closes_the_file(self, engine, ctx, tmp_path):
        r = engine.render_python("x = {{ inputs.big }}\nprint(len(x))", ctx, spill_dir=tmp_path)
        proc = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-X", "dev", "-c", r.script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "70000"
        assert "ResourceWarning" not in proc.stderr

    def test_collector_is_not_reachable_from_templates(self, engine, tmp_path):
        r = engine.render_shell(
            "echo {{ __rayspec_collector__ is defined }}", {"inputs": {}}, spill_dir=tmp_path
        )
        assert r.env == {"RAYSPEC_V1": "false"}
        with pytest.raises(TemplateRenderError):
            engine.render_shell("echo {{ __rayspec_collector__.env }}", {}, spill_dir=tmp_path)

    def test_python_literal_rejects_non_str_dict_keys(self, engine, tmp_path):
        with pytest.raises(TemplateRenderError, match="non-string key"):
            engine.render_python("x = {{ v }}", {"v": {1: "a"}}, spill_dir=tmp_path)
        with pytest.raises(TemplateRenderError, match="non-string key"):
            engine.render_python("x = {{ v }}", {"v": {"a": [{2: 1}]}}, spill_dir=tmp_path)
        r = engine.render_python("x = {{ v }}", {"v": {"1": "a"}}, spill_dir=tmp_path)
        assert r.script == "x = {'1': 'a'}"
