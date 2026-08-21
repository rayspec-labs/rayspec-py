"""Scope chain, StepView resolution (hint-bearing), build_context, export_env, context file."""

import json

import pytest

from rayspec.providers.base import Usage
from rayspec.schema import StepStatus
from rayspec.templating import (
    RayspecUndefined,
    Scope,
    StepView,
    build_context,
    export_env,
    write_context_file,
)


def _view(id: str, **kw) -> StepView:
    kw.setdefault("kind", "shell")
    kw.setdefault("status", StepStatus.SUCCEEDED)
    return StepView(id=id, **kw)


class TestScope:
    def test_innermost_first_resolution(self):
        outer = Scope(None, {"a": _view("a", output="outer-a"), "b": _view("b", output="outer-b")})
        inner = Scope(outer, {"a": _view("a", output="inner-a")})
        assert inner.visible_steps()["a"].output == "inner-a"
        assert inner.visible_steps()["b"].output == "outer-b"
        assert inner.lookup_step("zzz") is None
        assert list(inner.visible_steps()) == ["a", "b"]
        assert outer.visible_steps()["a"].output == "outer-a"

    def test_vars_chain(self):
        outer = Scope(None, {}, {"iteration": {"n": 1}, "x": 1})
        inner = Scope(outer, {}, {"x": 2})
        assert inner.lookup_var("x") == 2
        assert inner.lookup_var("iteration") == {"n": 1}
        assert inner.merged_vars() == {"iteration": {"n": 1}, "x": 2}
        assert inner.child({"c": _view("c")}).lookup_step("c") is not None

    def test_body_step_hint(self):
        build = _view("build", kind="loop", output={"review": "ok"}, body_ids=frozenset({"review"}))
        scope = Scope(None, {"build": build})
        hint = scope.missing_step_hint("review")
        assert "inside loop 'build'" in hint
        assert "steps.build.output.review" in hint
        assert "unknown step" in scope.missing_step_hint("nope")


class TestStepView:
    def test_plain_attributes(self):
        v = _view("t", output="hello", exit_code=0, stderr="", duration_s=1.5, usage=Usage(input=3))
        assert v.resolve("output") == "hello"
        assert v.resolve("status") == "succeeded"
        assert type(v.resolve("status")) is str
        assert v.resolve("ok") is True
        assert v.resolve("exit_code") == 0
        assert v.resolve("usage") == {
            "input": 3,
            "cached_input": 0,
            "cache_write": 0,
            "output": 0,
            "reasoning": 0,
        }
        assert v.resolve("duration_s") == 1.5

    def test_text_output_carries_path_for_hints(self):
        v = _view("t", output="hello")
        out = v.resolve("output")
        assert out == "hello"
        assert out._rayspec_path == "steps.t.output"  # type: ignore[attr-defined]
        assert isinstance(out, str)

    def test_skipped_step_output_is_undefined_with_guard_hint(self):
        v = _view("t", status=StepStatus.SKIPPED, skip_reason="when_false")
        out = v.resolve("output")
        assert isinstance(out, RayspecUndefined)
        hint = out.rayspec_hint or ""
        assert "steps.t.status == 'succeeded'" in hint
        assert "skipped (when_false)" in hint

    def test_failed_step_output_hint(self):
        v = _view("t", status=StepStatus.FAILED)
        out = v.resolve("output")
        assert isinstance(out, RayspecUndefined)
        assert "failed" in (out.rayspec_hint or "")
        assert v.resolve("ok") is False

    def test_failed_but_tolerated_with_stdout_keeps_output(self):
        v = _view("t", status=StepStatus.FAILED, tolerated=True, output="partial", exit_code=3)
        assert v.resolve("output") == "partial"
        assert v.resolve("ok") is False

    def test_none_attribute_is_undefined_with_hint(self):
        v = _view("p", kind="prompt", output="x")
        ec = v.resolve("exit_code")
        assert isinstance(ec, RayspecUndefined)
        assert "prompt" in (ec.rayspec_hint or "")

    def test_unknown_attribute_suggests_output(self):
        v = _view("p", kind="prompt", output={"score": 1})
        u = v.resolve("score")
        assert isinstance(u, RayspecUndefined)
        assert "steps.p.output.score" in (u.rayspec_hint or "")

    def test_to_json(self):
        v = _view("t", output={"a": 1}, usage=Usage(input=1))
        data = v.to_json()
        assert data["id"] == "t"
        assert data["status"] == "succeeded"
        assert data["output"] == {"a": 1}
        assert data["usage"]["input"] == 1
        json.dumps(data)


class TestBuildContext:
    def _ctx(self, **kw):
        scope = Scope(None, {"a": _view("a", output="A")})
        return build_context(
            scope,
            inputs={"issue": 12, "tags": ["x", "y"], "flag": True},
            run={
                "id": "20260820-090000-abcd",
                "workdir": "/w",
                "artifacts_dir": "/w/art",
                "state_dir": "/s",
            },
            project={"root": "/p", "name": "p", "slug": "local/p-1234"},
            env={"HOME": "/home/u"},
            **kw,
        )

    def test_roots_present(self):
        ctx = self._ctx()
        assert set(ctx) >= {"inputs", "steps", "run", "project", "env"}
        assert ctx["steps"]["a"].output == "A"
        assert "a" in ctx["steps"]
        assert list(ctx["steps"]) == ["a"]
        assert ctx["inputs"]["issue"] == 12
        assert "iteration" not in ctx

    def test_iteration_prev_on_first_iteration_is_undefined(self):
        ctx = self._ctx(iteration={"n": 1, "max": 3, "first": True, "prev": None})
        assert ctx["iteration"]["n"] == 1
        with pytest.raises(KeyError):
            ctx["iteration"]["prev"]
        hint = ctx["iteration"]._rayspec_missing_hint("prev")
        assert "first iteration" in hint

    def test_each_and_item(self):
        ctx = self._ctx(each={"index": 0, "total": 2}, item_var="issue", item={"n": 1})
        assert ctx["each"]["index"] == 0
        assert ctx["issue"] == {"n": 1}
        ctx2 = self._ctx(each={"index": 0, "total": 2}, item="x")
        assert ctx2["item"] == "x"

    def test_scope_vars_are_merged(self):
        scope = Scope(None, {}, {"iteration": {"n": 2, "max": 5, "first": False, "prev": {}}})
        ctx = build_context(scope, inputs={}, run={}, project={})
        assert ctx["iteration"]["n"] == 2
        assert ctx["env"] == {}

    def test_export_env(self):
        env = export_env(self._ctx())
        assert env == {
            "RAYSPEC_INPUT_ISSUE": "12",
            "RAYSPEC_INPUT_TAGS": '["x", "y"]',
            "RAYSPEC_INPUT_FLAG": "true",
            "RAYSPEC_RUN_ID": "20260820-090000-abcd",
            "RAYSPEC_WORKDIR": "/w",
            "RAYSPEC_ARTIFACTS_DIR": "/w/art",
            "RAYSPEC_STATE_DIR": "/s",
        }

    def test_write_context_file(self, tmp_path):
        ctx = self._ctx(iteration={"n": 1, "max": 2, "first": True, "prev": None})
        path = write_context_file(ctx, tmp_path / "ctx" / "context.json")
        data = json.loads(path.read_text())
        assert data["inputs"]["issue"] == 12
        assert data["steps"]["a"]["output"] == "A"
        assert data["steps"]["a"]["status"] == "succeeded"
        assert data["run"]["id"] == "20260820-090000-abcd"
        assert data["iteration"] == {"n": 1, "max": 2, "first": True}  # prev dropped on iteration 1
        assert "env" not in data  # the process environment is never persisted (secrets)


class TestReviewFixes:
    def test_step_view_model(self):
        v = _view("p", kind="prompt", output="x", model="gpt-5.4")
        assert v.resolve("model") == "gpt-5.4"
        assert v.to_json()["model"] == "gpt-5.4"
        u = _view("q", kind="prompt", output="x").resolve("model")
        assert isinstance(u, RayspecUndefined)
        assert "steps.q.model is not set" in (u.rayspec_hint or "")

    def test_scope_variables_keyword(self):
        outer = Scope(None, {}, variables={"x": 1})
        inner = outer.child({}, variables={"y": 2})
        assert inner.merged_vars() == {"x": 1, "y": 2}
        assert inner.variables == {"y": 2}

    def test_write_context_file_is_atomic(self, tmp_path, monkeypatch):
        scope = Scope(None, {"a": _view("a", output="A")})
        ctx = build_context(scope, inputs={"issue": 1}, run={}, project={})
        target = tmp_path / "context.json"
        write_context_file(ctx, target)
        before = target.read_text()
        assert [p.name for p in tmp_path.iterdir()] == ["context.json"]

        from rayspec.templating import scope as scope_module

        real_open_private = scope_module.open_private

        class CrashingFile:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

            def write(self, data):
                self._fh.write(data[:5])  # a partial write ...
                raise OSError("disk full")  # ... then the crash

        def crash_mid_write(path, mode="w", **k):
            return CrashingFile(real_open_private(path, mode, **k))

        monkeypatch.setattr(scope_module, "open_private", crash_mid_write)
        with pytest.raises(OSError):
            write_context_file(ctx, target)
        assert target.read_text() == before  # the previous file is intact
        assert [p.name for p in tmp_path.iterdir()] == ["context.json"]  # no tmp left behind
