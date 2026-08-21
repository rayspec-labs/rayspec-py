# SPDX-License-Identifier: Apache-2.0
"""``validate_workflow``: graph semantics, lints, template/reference checks, capability checks.

Boundary: pure checks over a :class:`ResolvedWorkflow`. Provider capabilities arrive through a
``capabilities_for(provider_id)`` callable (so this module never imports a provider) and
template compilation through the optional :class:`TemplateChecker` protocol (implemented by the
templating scope). Everything is reported, nothing is raised.
"""

from __future__ import annotations

import dataclasses
import json
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from rayspec.errors import RayspecError, UnsupportedFeatureError
from rayspec.loader.loader import GraphView, IncludedBody, ResolvedAgent, ResolvedWorkflow
from rayspec.loader.secrets import (
    check_secret_reference,
    include_secret_input_message,
    secret_reference_message,
    secret_whole_inputs_message,
)
from rayspec.policy import (
    EffectivePolicy,
    PolicyReport,
    apply_policy,
)
from rayspec.providers.base import TOOL_GROUPS, ProviderCapabilities
from rayspec.schema import (
    RESERVED_ROOTS,
    ApproveStep,
    EachStep,
    IncludeStep,
    InputSpec,
    LoopStep,
    PromptStep,
    PythonStep,
    ShellStep,
    StepModel,
    StopStep,
)
from rayspec.schema.base import suggest
from rayspec.schema.common import OnUnsupported

#: Context roots a template may reference (``<as>`` names of enclosing ``each:`` steps are added).
CONTEXT_ROOTS: frozenset[str] = frozenset(
    {"inputs", "steps", "run", "project", "env", "iteration", "each"}
)


class TemplateChecker(Protocol):
    """Load-time template services (implemented by :mod:`rayspec.templating`).

    * ``compile_template(text, where=)`` / ``compile_expr(text, where=)`` raise (any exception;
      :class:`RayspecError` preferred) when ``text`` does not compile;
    * ``references(text)`` returns the free references of a template *or* expression as ``Ref``
      objects (``root``/``name``/``attr_path``) or as tuples
      whose first two items are ``(root, name)`` — e.g. ``("steps", "fetch")`` for
      ``steps.fetch.output``; an optional third item is the remaining attribute path
      (``("output", "verdict")``) and is used to check ``steps.<include>.output.<key>``.
    """

    def compile_template(self, text: str, *, where: str) -> object: ...

    def compile_expr(self, text: str, *, where: str) -> object: ...

    def references(
        self, text: str
    ) -> Iterable[Any]: ...  # Ref objects or (root, name[, attrs]) tuples


CapabilitiesFor = Callable[[str], ProviderCapabilities | None]


@dataclass(slots=True)
class ValidationReport:
    """Outcome of :func:`validate_workflow`; ``unsupported`` holds the capability hits."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: list[UnsupportedFeatureError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, where: str, message: str, *, location: str | None = None) -> None:
        self.errors.append(_fmt(where, message, location))

    def warn(self, where: str, message: str, *, location: str | None = None) -> None:
        self.warnings.append(_fmt(where, message, location))


def _fmt(where: str, message: str, location: str | None) -> str:
    text = f"{where}: {message}"
    return f"{text} (at {location})" if location else text


# --------------------------------------------------------------------------------------------------
# Graph helpers
# --------------------------------------------------------------------------------------------------


def _ancestors(steps: Sequence[StepModel]) -> dict[str, set[str]]:
    """Transitive ``needs`` closure per step id (within one sibling list)."""
    by_id = {s.id: s for s in steps}
    memo: dict[str, set[str]] = {}

    def walk(sid: str, stack: set[str]) -> set[str]:
        if sid in memo:
            return memo[sid]
        out: set[str] = set()
        step = by_id.get(sid)
        if step is not None:
            for dep in step.needs:
                if dep in stack or dep not in by_id:
                    continue  # cycles/unknowns are reported separately
                out.add(dep)
                out |= walk(dep, stack | {dep})
        memo[sid] = out
        return out

    return {s.id: walk(s.id, {s.id}) for s in steps}


def topological_order(steps: Sequence[StepModel]) -> list[StepModel]:
    """Stable Kahn ordering of one sibling list (document order among ready steps).

    Steps that are part of a cycle (or depend on unknown ids) are appended last in document order.
    """
    by_id = {s.id: s for s in steps}
    indeg = {s.id: sum(1 for d in s.needs if d in by_id) for s in steps}
    children: dict[str, list[str]] = {s.id: [] for s in steps}
    for s in steps:
        for d in s.needs:
            if d in by_id:
                children[d].append(s.id)
    ready = deque(s.id for s in steps if indeg[s.id] == 0)
    out: list[StepModel] = []
    seen: set[str] = set()
    while ready:
        sid = ready.popleft()
        if sid in seen:
            continue
        seen.add(sid)
        out.append(by_id[sid])
        for child in children[sid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    out.extend(s for s in steps if s.id not in seen)
    return out


def _find_cycles(steps: Sequence[StepModel]) -> list[list[str]]:
    by_id = {s.id: s for s in steps}
    color: dict[str, int] = {}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def visit(sid: str) -> None:
        color[sid] = 1
        stack.append(sid)
        for dep in by_id[sid].needs:
            if dep not in by_id:
                continue
            if color.get(dep, 0) == 0:
                visit(dep)
            elif color[dep] == 1:
                cycles.append([*stack[stack.index(dep) :], dep])
        stack.pop()
        color[sid] = 2

    for s in steps:
        if color.get(s.id, 0) == 0:
            visit(s.id)
    return cycles


@dataclass(slots=True)
class _Scope:
    """Lexical scope of one graph: what ``steps.X`` / ``inputs.Y`` / ``iteration`` may name."""

    graph: GraphView
    parent: _Scope | None
    inputs: Mapping[str, InputSpec]
    ancestors: dict[str, set[str]]
    include: IncludedBody | None  # set when this graph is an include body (or inherited)
    each_names: tuple[str, ...]
    in_loop: bool

    def visible_from_parent(self) -> set[str]:
        """Steps visible inside this body because they are ancestors of the enclosing composite.

        An include body is a closed lexical scope (the engine starts a fresh scope chain for it):
        nothing of the including workflow is visible, only ``inputs`` bound by ``with:``.
        """
        if self.parent is None or self.graph.parent is None or self.graph.kind == "include":
            return set()
        return self.parent.allowed_for(self.graph.parent.id)

    def allowed_for(self, step_id: str) -> set[str]:
        """Step ids a step of this graph may reference (its ancestors + the outer allowed set)."""
        return set(self.ancestors.get(step_id, set())) | self.visible_from_parent()

    def after_body(self) -> set[str]:
        """Step ids visible when the whole body has run (``until``): all siblings + outer."""
        return {s.id for s in self.graph.steps} | self.visible_from_parent()

    def sibling_ids(self) -> set[str]:
        return {s.id for s in self.graph.steps}


# --------------------------------------------------------------------------------------------------
# Capability mapping
# --------------------------------------------------------------------------------------------------


def _sorted(values: Iterable[Any]) -> list[str]:
    return sorted(str(v) for v in values)


@dataclass(frozen=True, slots=True)
class _CapCheck:
    capability: str
    ok: Callable[[ProviderCapabilities], bool]
    value: Callable[[ProviderCapabilities], object]


def _cap_flag(name: str) -> _CapCheck:
    return _CapCheck(name, lambda c: bool(getattr(c, name)), lambda c: getattr(c, name))


def _cap_member(name: str, member: str) -> _CapCheck:
    return _CapCheck(
        name,
        lambda c: member in {str(v) for v in getattr(c, name)},
        lambda c: _sorted(getattr(c, name)),
    )


_STRUCTURED = _CapCheck(
    "structured_output", lambda c: c.structured_output != "none", lambda c: c.structured_output
)


# --------------------------------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------------------------------


class _Validator:
    def __init__(
        self,
        resolved: ResolvedWorkflow,
        *,
        capabilities_for: CapabilitiesFor | None,
        template_checker: TemplateChecker | None,
        warn_unsupported: bool,
        provider_ids: Sequence[str],
        policy: EffectivePolicy | None = None,
    ):
        self.rw = resolved
        self.caps_for = capabilities_for
        self.checker = template_checker
        self.warn_unsupported = warn_unsupported
        self.provider_ids = list(provider_ids)
        self.policy = policy
        self.report = ValidationReport()
        self._caps_cache: dict[str, ProviderCapabilities | None] = {}
        self._agent_checked: set[str] = set()
        self._tools_checked: set[str] = set()
        self._step_kinds: dict[str, str] = {}  # id → description for did-you-mean hints
        self._id_homes: dict[str, str] = {}  # id → path of the composite it lives in ("" root)

    # -- entry --------------------------------------------------------------------------------

    def run(self) -> ValidationReport:
        for graph in self.rw.graphs():
            for step in graph.steps:
                self._id_homes.setdefault(step.id, graph.prefix[:-1])
        root = self.rw.graphs()[0]
        scope = _Scope(
            graph=root,
            parent=None,
            inputs=self.rw.workflow.inputs,
            ancestors=_ancestors(root.steps),
            include=None,
            each_names=(),
            in_loop=False,
        )
        self._check_graph(scope)
        self._check_outputs(self.rw.workflow.outputs, scope, where_prefix="outputs")
        self._check_policy()
        return self.report

    # -- policy (the ONE policy call site in the loader) ---------------------------------------

    def _check_policy(self) -> None:
        """Apply the ``policy.yaml`` layers (see :mod:`rayspec.policy` and ``docs/policy.md``).

        This is the only place the *loader* consults policy, and it reports rather than decides:
        the work — discovering the layers, checking them and folding the denials into the agents
        — is :func:`rayspec.policy.apply_policy`, which anything about to run a resolved workflow
        calls whether or not it validates first. A violation is reported here, at load time, with
        the workflow's own ``file:line`` *and* the policy layer that imposed the restriction, so
        a run the policy forbids never starts and never spends money finding out. Discovery is
        local: ``$RAYSPEC_POLICY``, the project file, the user file, nothing else.
        """
        self._report_policy(
            apply_policy(self.rw, capabilities_for=self.caps_for, policy=self.policy)
        )

    def _report_policy(self, outcome: PolicyReport) -> None:
        """Turn one policy outcome into validation errors and warnings."""
        for problem in outcome.errors:
            self.report.error(problem.where, problem.message, location=problem.location)
        for problem in outcome.warnings:
            self.report.warn(problem.where, problem.message, location=problem.location)

    # -- graphs -------------------------------------------------------------------------------

    def _child_scope(self, scope: _Scope, step: StepModel, graph: GraphView) -> _Scope:
        inputs = scope.inputs
        include = scope.include
        each_names = scope.each_names
        in_loop = scope.in_loop
        if isinstance(step, IncludeStep):
            # an include body is a closed scope: its own inputs, no outer steps/iteration/each
            body = self.rw.includes[graph.prefix[:-1]]
            inputs = body.inputs
            include = body
            each_names = ()
            in_loop = False
        elif isinstance(step, EachStep):
            each_names = (*each_names, step.as_)
        elif isinstance(step, LoopStep):
            in_loop = True
        return _Scope(
            graph=graph,
            parent=scope,
            inputs=inputs,
            ancestors=_ancestors(graph.steps),
            include=include,
            each_names=each_names,
            in_loop=in_loop,
        )

    def _check_graph(self, scope: _Scope) -> None:
        graph = scope.graph
        ids = scope.sibling_ids()
        for step in graph.steps:
            self._check_needs(step, graph, ids)
        for cycle in _find_cycles(graph.steps):
            self.report.error(
                f"steps.{graph.prefix}{cycle[0]}.needs",
                "dependency cycle: " + " -> ".join(cycle),
                location=self.rw.location_of(f"{graph.prefix}{cycle[0]}", "needs"),
            )
        bodies = {g.prefix: g for g in self.rw.graphs()}
        for step in graph.steps:
            path = graph.path_of(step)
            self._check_step(step, path, scope)
            body = bodies.get(f"{path}/")
            if body is not None:
                child = self._child_scope(scope, step, body)
                self._check_graph(child)
                if isinstance(step, IncludeStep):
                    self._check_outputs(
                        self.rw.includes[path].outputs, child, where_prefix=f"steps.{path}.outputs"
                    )

    def _check_needs(self, step: StepModel, graph: GraphView, ids: set[str]) -> None:
        path = graph.path_of(step)
        where = f"steps.{path}.needs"
        loc = self.rw.location_of(path, "needs")
        for dep in step.needs:
            if dep == step.id:
                self.report.error(where, f"step {step.id!r} cannot depend on itself", location=loc)
            elif dep not in ids:
                home = self._id_homes.get(dep)
                if home is not None:
                    msg = (
                        f"{dep!r} is not a sibling of {step.id!r} (it lives "
                        f"{'at the top level' if home == '' else f'inside {home!r}'}); "
                        "needs may only name steps of the same steps list"
                    )
                else:
                    hint = suggest(dep, ids - {step.id})
                    msg = f"unknown step {dep!r}"
                    if hint:
                        msg += f"; did you mean {hint!r}?"
                self.report.error(where, msg, location=loc)
        if step.join != "all" and not step.needs:
            self.report.warn(
                f"steps.{path}.join",
                f"join: {step.join} has no effect without needs",
                location=self.rw.location_of(path, "join"),
            )

    # -- steps --------------------------------------------------------------------------------

    def _check_step(self, step: StepModel, path: str, scope: _Scope) -> None:
        allowed = scope.allowed_for(step.id)
        if step.when is not None:
            self._check_expr(
                step.when, f"steps.{path}.when", path, "when", scope=scope, allowed=allowed
            )
        if isinstance(step, PromptStep):
            self._check_prompt(step, path, scope, allowed)
        elif isinstance(step, ShellStep):
            self._check_script(step.shell, "shell", path, scope, allowed)
            self._check_leaf_common(step, path, scope, allowed)
        elif isinstance(step, PythonStep):
            self._check_script(step.python, "python", path, scope, allowed)
            self._check_leaf_common(step, path, scope, allowed)
        elif isinstance(step, LoopStep):
            if not step.loop.steps:
                self.report.error(
                    f"steps.{path}.loop.steps",
                    "loop body must contain at least one step",
                    location=self.rw.location_of(path, "loop"),
                )
            if step.loop.until is not None:
                body = _Scope(
                    graph=GraphView("loop", f"{path}/", tuple(step.loop.steps), path, step),
                    parent=scope,
                    inputs=scope.inputs,
                    ancestors=_ancestors(step.loop.steps),
                    include=scope.include,
                    each_names=scope.each_names,
                    in_loop=True,
                )
                self._check_expr(
                    step.loop.until,
                    f"steps.{path}.loop.until",
                    path,
                    ("loop", "until"),
                    scope=body,
                    allowed=body.after_body(),
                )
        elif isinstance(step, EachStep):
            if not step.steps:
                self.report.error(
                    f"steps.{path}.steps",
                    "each body must contain at least one step",
                    location=self.rw.location_of(path, "steps"),
                )
            self._check_expr(
                step.each, f"steps.{path}.each", path, "each", scope=scope, allowed=allowed
            )
        elif isinstance(step, ApproveStep):
            self._check_no_timeout(step, path)
            self._check_template(
                step.approve.message, f"steps.{path}.approve.message", path, scope, allowed
            )
            if step.approve.auto_if is not None:
                # an expression field, checked exactly like `when:` — including the secret
                # placement rule: `eval_bool` names the offending value in its error, so a
                # secret reference here would be a secret in a persisted step error
                self._check_expr(
                    step.approve.auto_if,
                    f"steps.{path}.approve.auto_if",
                    path,
                    ("approve", "auto_if"),
                    scope=scope,
                    allowed=allowed,
                )
        elif isinstance(step, StopStep):
            self._check_no_timeout(step, path)
            if step.stop.reason is not None:
                self._check_template(
                    step.stop.reason, f"steps.{path}.stop.reason", path, scope, allowed
                )
        elif isinstance(step, IncludeStep):
            self._check_include(step, path, scope, allowed)

    def _check_no_timeout(self, step: ApproveStep | StopStep, path: str) -> None:
        """``timeout:`` is a leaf/composite knob; a gate or ``stop:`` would ignore it silently."""
        if step.timeout is not None:
            self.report.error(
                f"steps.{path}.timeout",
                f"timeout is not supported on {type(step).kind} steps (it would be ignored)",
                location=self.rw.location_of(path, "timeout"),
            )

    def _check_leaf_common(
        self, step: ShellStep | PythonStep, path: str, scope: _Scope, allowed: set[str]
    ) -> None:
        for key, value in step.env.items():
            # the ONE place a template may name a secret input — the value lands in the
            # step's process environment only (never persisted)
            self._check_template(
                value, f"steps.{path}.env.{key}", path, scope, allowed, secret_ok=True
            )
        if step.cwd is not None:
            self._check_template(step.cwd, f"steps.{path}.cwd", path, scope, allowed)

    def _check_script(
        self, body: str, kind: str, path: str, scope: _Scope, allowed: set[str]
    ) -> None:
        where = f"steps.{path}.{kind}"
        if kind == "shell" and "${{" in body:
            self.report.error(
                where,
                "'${{' is not rayspec syntax; write '{{ expr }}' (it renders as a shell "
                "variable reference) or '${VAR}' for plain shell variables",
                location=self.rw.location_of(path, kind),
            )
        self._check_template(body, where, path, scope, allowed, kind=kind)

    def _check_prompt(self, step: PromptStep, path: str, scope: _Scope, allowed: set[str]) -> None:
        text = self.rw.prompt_text(path)
        if text is not None:
            field_name = "prompt" if step.prompt is not None else "prompt_file"
            self._check_template(text, f"steps.{path}.{field_name}", path, scope, allowed)
        for key, value in step.env.items():
            self._check_template(value, f"steps.{path}.env.{key}", path, scope, allowed)
        agent = self.rw.agents.get(self.rw.step_agents.get(path, ""))
        if agent is None:
            self.report.error(f"steps.{path}.agent", "no agent resolved for this prompt step")
            return
        if agent.instructions is not None:
            self._check_template(
                agent.instructions,
                f"{agent.yaml_path}.instructions (used by steps.{path})",
                path,
                scope,
                allowed,
            )
        if step.session is not None:
            self._check_session(step, path, scope, allowed, agent)
        self._check_capabilities(step, path, agent)

    def _check_session(
        self, step: PromptStep, path: str, scope: _Scope, allowed: set[str], agent: ResolvedAgent
    ) -> None:
        where = f"steps.{path}.session"
        loc = self.rw.location_of(path, "session")
        target = step.session
        if target == step.id:
            if not scope.in_loop:
                self.report.error(
                    where,
                    "session: <self> only makes sense inside a loop body (it continues the "
                    "previous iteration's session)",
                    location=loc,
                )
            return
        if target not in allowed:
            if target in scope.sibling_ids():
                msg = f"{target!r} must be an ancestor of {step.id!r} (add it to needs:)"
            else:
                msg = f"{target!r} is not an ancestor of {step.id!r} (or not visible from here)"
            self.report.error(where, msg, location=loc)
            return
        target_path = self._path_of_visible(target, scope)
        target_step = self.rw.step(target_path) if target_path is not None else None
        if not isinstance(target_step, PromptStep):
            self.report.error(where, f"{target!r} is not a prompt step", location=loc)
            return
        other = self.rw.agents.get(self.rw.step_agents.get(target_path or "", ""))
        if other is not None and other.provider != agent.provider:
            self.report.error(
                where,
                f"session target {target!r} runs on provider {other.provider!r} but this step "
                f"runs on {agent.provider!r}; sessions cannot cross providers",
                location=loc,
            )

    def _path_of_visible(self, step_id: str, scope: _Scope) -> str | None:
        cur: _Scope | None = scope
        while cur is not None:
            if step_id in cur.sibling_ids():
                return f"{cur.graph.prefix}{step_id}"
            cur = cur.parent
        return None

    def _check_include(
        self, step: IncludeStep, path: str, scope: _Scope, allowed: set[str]
    ) -> None:
        body = self.rw.includes.get(path)
        if body is None:
            return
        where = f"steps.{path}.with"
        loc = self.rw.location_of(path, "with")
        secret_bodies = [name for name, spec in body.inputs.items() if spec.secret]
        if secret_bodies:
            # the wording lives with the other placement rules (loader/secrets.py)
            self.report.error(
                where,
                include_secret_input_message(body.workflow_name, secret_bodies),
                location=loc,
            )
        for key, value in step.with_.items():
            if key not in body.inputs:
                hint = suggest(key, set(body.inputs))
                msg = f"included workflow {body.workflow_name!r} has no input {key!r}"
                if hint:
                    msg += f"; did you mean {hint!r}?"
                self.report.error(where, msg, location=loc)
                continue
            problem = _coercion_problem(value, body.inputs[key])
            if problem:
                self.report.error(f"{where}.{key}", problem, location=loc)
            self._check_deep(value, f"{where}.{key}", path, scope, allowed)
        missing = [
            name for name, spec in body.inputs.items() if spec.required and name not in step.with_
        ]
        if missing:
            self.report.error(
                where,
                f"missing required input(s) for included workflow {body.workflow_name!r}: "
                + ", ".join(missing),
                location=loc,
            )

    def _check_outputs(self, outputs: Mapping[str, Any], scope: _Scope, *, where_prefix: str):
        allowed = scope.after_body()
        for key, value in outputs.items():  # keys are schema-checked Names already
            self._check_deep(value, f"{where_prefix}.{key}", None, scope, allowed)

    def _check_deep(
        self, value: Any, where: str, path: str | None, scope: _Scope, allowed: set[str]
    ) -> None:
        if isinstance(value, str):
            self._check_template(value, where, path, scope, allowed)
        elif isinstance(value, dict):
            for k, v in value.items():
                self._check_deep(v, f"{where}.{k}", path, scope, allowed)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self._check_deep(v, f"{where}[{i}]", path, scope, allowed)

    # -- templates & expressions --------------------------------------------------------------

    def _check_expr(
        self,
        text: str,
        where: str,
        path: str,
        field_name: str | tuple[str, ...],
        *,
        scope: _Scope,
        allowed: set[str],
    ) -> None:
        fields = (field_name,) if isinstance(field_name, str) else field_name
        loc = self.rw.location_of(path, *fields)
        if "{{" in text or "{%" in text:
            self.report.error(
                where,
                "expression fields take a bare Jinja expression (no '{{ }}' / '{% %}')",
                location=loc,
            )
            return
        if not text.strip():
            self.report.error(where, "expression must not be empty", location=loc)
            return
        if self.checker is None:
            return
        try:
            self.checker.compile_expr(text, where=where)
        except Exception as exc:  # the checker decides the error type; report everything
            self.report.error(where, str(exc), location=loc)
            return
        # kind="expr": a bare expression parsed as a text template has no references at all
        self._check_refs(text, where, scope, allowed, loc, kind="expr")

    def _template_location(self, where: str, path: str | None) -> str | None:
        """Best-effort ``file:line`` for ``steps.<path>.<field...>`` (falls back to prefixes)."""
        if path is None:
            return None
        prefix = f"steps.{path}."
        if not where.startswith(prefix):
            return None
        fields = where[len(prefix) :].split(" ", 1)[0].split(".")
        for n in range(len(fields), 0, -1):
            loc = self.rw.location_of(path, *fields[:n])
            if loc is not None:
                return loc
        return None

    def _check_template(
        self,
        text: str,
        where: str,
        path: str | None,
        scope: _Scope,
        allowed: set[str],
        *,
        kind: str = "text",
        secret_ok: bool = False,
    ) -> None:
        if self.checker is None:
            return
        loc = self._template_location(where, path)
        try:
            _call_with_kind(self.checker.compile_template, text, where=where, kind=kind)
        except Exception as exc:
            self.report.error(where, str(exc), location=loc)
            return
        self._check_refs(text, where, scope, allowed, loc, kind=kind, secret_ok=secret_ok)

    def _check_refs(
        self,
        text: str,
        where: str,
        scope: _Scope,
        allowed: set[str],
        loc: str | None,
        *,
        kind: str = "text",
        secret_ok: bool = False,
    ) -> None:
        assert self.checker is not None
        try:
            refs = list(_call_with_kind(self.checker.references, text, kind=kind))
        except Exception as exc:
            self.report.error(where, str(exc), location=loc)
            return
        for raw_ref in refs:
            normalized = _normalize_ref(raw_ref)
            # ===== secret placement: the ONE call site into rayspec.loader.secrets;
            # that module owns every rule about where a `secret: true` input may appear =====
            verdict = check_secret_reference(
                _bare_root(raw_ref), normalized, scope.inputs, secret_ok=secret_ok
            )
            if verdict.message is not None:
                self.report.error(where, verdict.message, location=loc)
            if verdict.stop:
                continue
            # ===== end secret placement =====
            if normalized is None:
                continue
            root, name, attrs = normalized
            if root == "steps":
                self._check_step_ref(name, attrs, where, scope=scope, allowed=allowed, loc=loc)
            elif root == "inputs":
                if name not in scope.inputs:
                    hint = suggest(name, set(scope.inputs))
                    msg = f"inputs.{name} is not declared under inputs:"
                    if hint:
                        msg += f"; did you mean {hint!r}?"
                    self.report.error(where, msg, location=loc)
            elif root == "iteration":
                self._check_iteration_ref(name, attrs, where, scope, loc)
            elif root == "each":
                if not scope.each_names:
                    self.report.error(
                        where, "'each' is only available inside an each: body", location=loc
                    )
            elif root in scope.each_names or root in CONTEXT_ROOTS or root in RESERVED_ROOTS:
                continue
            else:
                roots = ", ".join(sorted(CONTEXT_ROOTS | set(scope.each_names)))
                self.report.error(
                    where,
                    f"unknown name {root!r}; context roots are: {roots}",
                    location=loc,
                )

    def _check_step_ref(
        self,
        name: str,
        attrs: tuple[Any, ...],
        where: str,
        *,
        scope: _Scope,
        allowed: set[str],
        loc: str | None,
    ) -> None:
        if name in allowed:
            target_path = self._path_of_visible(name, scope)
            body = self.rw.includes.get(target_path or "")
            if body is not None and len(attrs) >= 2 and attrs[0] == "output":
                key = str(attrs[1])
                if key not in body.outputs:
                    self.report.error(
                        where,
                        f"include {name!r} has no output {key!r} "
                        f"(outputs: {', '.join(sorted(body.outputs)) or 'none'})",
                        location=loc,
                    )
            return
        home = self._id_homes.get(name)
        cur: _Scope | None = scope
        enclosing = []
        while cur is not None:
            if cur.graph.parent is not None:
                enclosing.append(cur.graph.parent.id)
            cur = cur.parent
        if name in enclosing:
            msg = (
                f"steps.{name} is the enclosing composite step; its output is not available "
                "until the body has finished"
            )
        elif name in scope.sibling_ids():
            msg = f"steps.{name} is not an ancestor of this step (add {name!r} to needs:)"
        elif home:
            kind = type(self.rw.step(home)).kind
            msg = (
                f"steps.{name} is inside {kind} {home.rsplit('/', 1)[-1]!r}; use "
                f"steps.{home.rsplit('/', 1)[-1]}.output.{name}"
            )
        elif home == "":
            msg = f"steps.{name} is not an ancestor of this step"
        else:
            hint = suggest(name, set(self._id_homes))
            msg = f"unknown step {name!r}"
            if hint:
                msg += f"; did you mean {hint!r}?"
        self.report.error(where, msg, location=loc)

    def _check_iteration_ref(
        self, name: str, attrs: tuple[Any, ...], where: str, scope: _Scope, loc: str | None
    ) -> None:
        if not scope.in_loop:
            self.report.error(
                where, "'iteration' is only available inside a loop body", location=loc
            )
            return
        target: str | None = None
        if name == "prev" and attrs:
            target = str(attrs[0])
        elif name.startswith("prev."):
            target = name.split(".")[1]
        if target is None:
            return
        loop_scope: _Scope | None = scope
        while loop_scope is not None and not isinstance(loop_scope.graph.parent, LoopStep):
            loop_scope = loop_scope.parent
        siblings = loop_scope.sibling_ids() if loop_scope is not None else set()
        if target not in siblings:
            self.report.error(
                where,
                f"iteration.prev.{target}: {target!r} is not a step of the enclosing loop body "
                f"(body steps: {', '.join(sorted(siblings))})",
                location=loc,
            )

    # -- capabilities -------------------------------------------------------------------------

    def _caps(self, provider: str) -> ProviderCapabilities | None:
        if self.caps_for is None:
            return None
        if provider not in self._caps_cache:
            caps = self.caps_for(provider)
            self._caps_cache[provider] = caps
            if caps is None:
                self.report.warn(
                    f"provider {provider!r}",
                    "not registered; capability checks skipped for its agents",
                )
        return self._caps_cache[provider]

    def _unsupported(
        self,
        *,
        path: str,
        value: object,
        provider: str,
        check: _CapCheck,
        caps: ProviderCapabilities,
        location: str | None,
        field: str | None = None,
    ) -> None:
        alternatives = []
        for pid in self.provider_ids:
            if pid == provider:
                continue
            other = self._caps_cache.get(pid)
            if other is None and self.caps_for is not None:
                other = self.caps_for(pid)
                self._caps_cache[pid] = other
            if other is not None and check.ok(other):
                alternatives.append(pid)
        err = UnsupportedFeatureError(
            path=path,
            value=value,
            provider=provider,
            capability=check.capability,
            capability_value=check.value(caps),
            alternatives=alternatives,
            location=location,
            field=field,
        )
        self.report.unsupported.append(err)
        if self.warn_unsupported:
            self.report.warnings.append(str(err))
        else:
            self.report.errors.append(str(err))

    def _check_capabilities(self, step: PromptStep, path: str, agent: ResolvedAgent) -> None:
        caps = self._caps(agent.provider)
        self._check_tools_syntax(agent)
        if caps is None:
            return
        if agent.key not in self._agent_checked:
            self._agent_checked.add(agent.key)
            self._check_agent_capabilities(agent, caps)
        # the agent may have been rewritten (effort alias) — re-fetch
        agent = self.rw.agents[agent.key]
        if step.output_schema is not None and not _STRUCTURED.ok(caps):
            self._unsupported(
                path=f"steps.{path}.output_schema",
                value="{...}",
                provider=agent.provider,
                check=_STRUCTURED,
                caps=caps,
                location=self.rw.location_of(path, "output_schema"),
            )
        if step.session is not None and not caps.session_resume:
            self._unsupported(
                path=f"steps.{path}.session",
                value=step.session,
                provider=agent.provider,
                check=_cap_flag("session_resume"),
                caps=caps,
                location=self.rw.location_of(path, "session"),
            )
        if step.env and not caps.env_injection:
            self._unsupported(
                path=f"steps.{path}.env",
                value="{...}",
                provider=agent.provider,
                check=_cap_flag("env_injection"),
                caps=caps,
                location=self.rw.location_of(path, "env"),
            )

    def _check_tools_syntax(self, agent: ResolvedAgent) -> None:
        if agent.key in self._tools_checked:
            return
        self._tools_checked.add(agent.key)
        tools = agent.tools
        if agent.access == "read-only":
            bad = sorted({t for t in tools.allow if t in {"edit", "shell"}})
            if bad:
                self.report.error(
                    agent.field_path("tools.allow"),
                    f"access: read-only cannot allow {', '.join(bad)}",
                    location=agent.location("tools") or agent.location("access"),
                )
        for list_name in ("allow", "deny"):
            for entry in getattr(tools, list_name):
                if entry in TOOL_GROUPS or entry.startswith("mcp:"):
                    continue
                if ":" in entry:
                    continue  # provider-prefixed raw name; capability-checked below
                self.report.error(
                    agent.field_path(f"tools.{list_name}"),
                    f"unknown tool {entry!r}; use a group ({', '.join(sorted(TOOL_GROUPS))}), "
                    "mcp:<server>[/<tool>] or a provider-prefixed name like claude:WebFetch",
                    location=agent.location("tools"),
                )

    def _check_agent_capabilities(self, agent: ResolvedAgent, caps: ProviderCapabilities) -> None:
        provider = agent.provider

        def unsupported(
            field_name: str, value: object, check: _CapCheck, *, field: str | None = None
        ) -> None:
            self._unsupported(
                path=agent.field_path(field_name),
                value=value,
                provider=provider,
                check=check,
                caps=caps,
                location=agent.location(field_name.split(".", 1)[0]),
                field=field,
            )

        if agent.max_turns is not None and not caps.max_turns:
            unsupported("max_turns", agent.max_turns, _cap_flag("max_turns"))
        if agent.budget_usd is not None and not caps.budget_usd:
            unsupported("budget_usd", agent.budget_usd, _cap_flag("budget_usd"))
        if agent.thinking is not None and not caps.thinking:
            unsupported("thinking", agent.thinking, _cap_flag("thinking"))
        if agent.mcp and not caps.mcp_servers:
            unsupported("mcp", "{" + ", ".join(sorted(agent.mcp)) + "}", _cap_flag("mcp_servers"))
        if agent.access not in {str(a) for a in caps.access_levels}:
            unsupported("access", agent.access, _cap_member("access_levels", agent.access))
        if agent.instructions_mode not in {str(m) for m in caps.instructions_modes}:
            unsupported(
                "instructions_mode",
                agent.instructions_mode,
                _cap_member("instructions_modes", agent.instructions_mode),
            )
        if agent.effort is not None and agent.effort not in caps.effort_levels:
            alias = caps.effort_aliases.get(agent.effort)
            if alias is not None:
                self.report.warn(
                    agent.field_path("effort"),
                    f"provider {provider!r} has no effort level {agent.effort!r}; using "
                    f"{alias!r} instead",
                    location=agent.location("effort"),
                )
                self.rw.agents[agent.key] = dataclasses.replace(agent, effort=alias)
            else:
                unsupported("effort", agent.effort, _cap_member("effort_levels", agent.effort))
        for list_name in ("allow", "deny"):
            for entry in getattr(agent.tools, list_name):
                group = "mcp" if entry.startswith("mcp:") else entry
                if group in TOOL_GROUPS:
                    if group not in caps.tool_groups:
                        unsupported(
                            f"tools.{list_name}",
                            entry,
                            _cap_member("tool_groups", group),
                            field=f"{group} tools",
                        )
                    continue
                prefix, _, _name = entry.partition(":")
                if prefix == provider:
                    if not caps.raw_tool_names:
                        unsupported(
                            f"tools.{list_name}",
                            entry,
                            _cap_flag("raw_tool_names"),
                            field="raw tool names",
                        )
                elif prefix in self.provider_ids:
                    self.report.warn(
                        agent.field_path(f"tools.{list_name}"),
                        f"{entry!r} targets provider {prefix!r}; ignored for {provider!r}",
                        location=agent.location("tools"),
                    )


# --------------------------------------------------------------------------------------------------
# with: coercion check (shared with inputs)
# --------------------------------------------------------------------------------------------------


def _coercion_problem(value: Any, spec: InputSpec) -> str | None:
    """Return a message when a literal ``with:`` value cannot become ``spec.type``."""
    if isinstance(value, str) and ("{{" in value or "{%" in value):
        return None  # a template; checked at run time
    t = spec.type
    if t == "string":
        return (
            None
            if isinstance(value, str | int | float) and not isinstance(value, bool)
            else (f"expected a string, got {type(value).__name__}")
        )
    if t == "boolean":
        if isinstance(value, bool):
            return None
        if isinstance(value, str) and value.strip().lower() in _BOOL_WORDS:
            return None
        return f"expected a boolean, got {value!r}"
    if t == "integer":
        if isinstance(value, bool):
            return f"expected an integer, got {value!r}"
        if isinstance(value, int):
            return None
        if isinstance(value, str):
            try:
                int(value.strip())
                return None
            except ValueError:
                pass
        return f"expected an integer, got {value!r}"
    if t == "number":
        if isinstance(value, bool):
            return f"expected a number, got {value!r}"
        if isinstance(value, int | float):
            return None
        if isinstance(value, str):
            try:
                float(value.strip())
                return None
            except ValueError:
                pass
        return f"expected a number, got {value!r}"
    if t in {"array", "object"}:
        want = list if t == "array" else dict
        if isinstance(value, want):
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return f"expected {t} (JSON), got {value!r}"
            return None if isinstance(parsed, want) else f"expected {t} (JSON), got {value!r}"
        return f"expected {t}, got {type(value).__name__}"
    return None


_BOOL_WORDS = frozenset({"true", "false", "yes", "no", "1", "0"})


def validate_workflow(
    resolved: ResolvedWorkflow,
    *,
    capabilities_for: CapabilitiesFor | None = None,
    template_checker: TemplateChecker | None = None,
    on_unsupported: OnUnsupported = "error",
    provider_ids: Iterable[str] = (),
    policy: EffectivePolicy | None = None,
) -> ValidationReport:
    """Validate a loaded workflow; never raises.

    ``capabilities_for(provider_id)`` supplies :class:`ProviderCapabilities` (``None`` = unknown
    provider → warning, checks skipped); ``None`` skips capability checks altogether.
    ``template_checker`` enables compile + reference checks. Capability mismatches are errors
    unless ``on_unsupported == "warn"`` or the workflow sets ``defaults.on_unsupported: warn``.
    ``provider_ids`` are used to name alternatives in the fix hint.

    ``policy`` are the ``policy.yaml`` layers to enforce; the default (``None``) discovers them
    from the workflow's own project root, ``$RAYSPEC_HOME`` and ``$RAYSPEC_POLICY``. Pass an
    empty :class:`~rayspec.policy.EffectivePolicy` to validate without any policy at all.
    """
    warn = on_unsupported == "warn" or resolved.workflow.defaults.on_unsupported == "warn"
    validator = _Validator(
        resolved,
        capabilities_for=capabilities_for,
        template_checker=template_checker,
        warn_unsupported=warn,
        provider_ids=list(provider_ids),
        policy=policy,
    )
    try:
        return validator.run()
    except RayspecError as exc:  # defensive: a loader inconsistency must surface as an error
        validator.report.errors.append(str(exc))
        return validator.report


__all__ = [
    "CONTEXT_ROOTS",
    "CapabilitiesFor",
    "TemplateChecker",
    "ValidationReport",
    "secret_reference_message",
    "secret_whole_inputs_message",
    "topological_order",
    "validate_workflow",
]


def _call_with_kind(func: Any, text: str, **kwargs: Any) -> Any:
    """Call a checker method, passing ``kind=`` only when the checker accepts it.

    The TemplateChecker protocol is kind-agnostic; the real templating engine compiles shell/python
    bodies with their own environments (``{{# #}}`` comments), so pass the kind when supported.
    """
    kind = kwargs.pop("kind", "text")
    if kind != "text":
        try:
            return func(text, kind=kind, **kwargs)
        except TypeError as exc:
            if "kind" not in str(exc):
                raise
    return func(text, **kwargs)


def _bare_root(ref: Any) -> str | None:
    """The root of a reference whose first segment is dynamic or missing (``{{ inputs }}``,
    ``inputs[expr]``, ``inputs.get(...)``) — ``None`` for a plain ``root.name`` reference."""
    if hasattr(ref, "root") and hasattr(ref, "name"):
        return str(ref.root) if ref.name is None else None
    if isinstance(ref, list | tuple) and len(ref) >= 2 and ref[1] is None:
        return str(ref[0])
    return None


def _normalize_ref(ref: Any) -> tuple[str, str, tuple[str, ...]] | None:
    """Accept ``Ref(root, name, attr_path)`` objects or ``(root, name[, attrs])`` tuples."""
    if hasattr(ref, "root") and hasattr(ref, "name"):
        name = ref.name
        if name is None:
            return None  # dynamic first segment (steps[inputs.x]) or bare root: nothing to check
        attrs = tuple(str(a) for a in getattr(ref, "attr_path", ()) or ())
        return str(ref.root), str(name), attrs
    if isinstance(ref, list | tuple):
        if len(ref) < 2 or ref[1] is None:
            return None
        attrs = (
            tuple(str(a) for a in ref[2])
            if len(ref) > 2 and isinstance(ref[2], list | tuple)
            else ()
        )
        return str(ref[0]), str(ref[1]), attrs
    return None
