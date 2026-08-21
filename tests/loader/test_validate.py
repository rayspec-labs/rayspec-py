"""validate_workflow: graph checks, lints, reference checks, capability mapping."""

from __future__ import annotations

from rayspec.loader import load_workflow, validate_workflow
from rayspec.loader.validate import topological_order

from .conftest import Tree
from .fakes import FakeChecker, capabilities_for

HEAD = "rayspec: 1\nname: wf\n"


def _validate(tree: Tree, text: str, *, checker: bool = False, **kw):
    tree.workflow("wf", text)
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    return validate_workflow(
        rw,
        capabilities_for=capabilities_for,
        template_checker=FakeChecker() if checker else None,
        provider_ids=["claude", "codex", "stub"],
        **kw,
    )


def test_needs_must_be_siblings(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: a
    shell: echo
  - id: b
    needs: [a, nope, b]
    shell: echo
  - id: l
    loop:
      max_iterations: 1
      steps:
        - id: inner
          needs: [a]
          shell: echo
  - id: c
    needs: [inner]
    shell: echo
""",
    )
    joined = "\n".join(rep.errors)
    assert "steps.b.needs: unknown step 'nope'" in joined
    assert "cannot depend on itself" in joined
    assert "steps.l/inner.needs: 'a' is not a sibling" in joined
    assert "steps.c.needs: 'inner' is not a sibling of 'c' (it lives inside 'l')" in joined
    assert "wf.yaml:" in joined


def test_cycle_detected(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: a
    needs: [c]
    shell: echo
  - id: b
    needs: [a]
    shell: echo
  - id: c
    needs: [b]
    shell: echo
""",
    )
    assert any("dependency cycle" in e for e in rep.errors)


def test_join_without_needs_warns_and_empty_bodies_error(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: a
    join: any
    shell: echo
  - id: l
    loop:
      max_iterations: 1
      steps: []
  - id: e
    each: inputs.xs
    steps: []
""",
    )
    assert any("join: any has no effect" in w for w in rep.warnings)
    assert any("loop body must contain at least one step" in e for e in rep.errors)
    assert any("each body must contain at least one step" in e for e in rep.errors)


def test_expression_lints(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: a
    when: "{{ inputs.x }}"
    shell: echo ${{ inputs.x }}
  - id: l
    loop:
      max_iterations: 1
      until: "{{ true }}"
      steps:
        - id: i
          shell: echo
  - id: e
    each: "{{ inputs.xs }}"
    steps:
      - id: j
        shell: echo
""",
    )
    joined = "\n".join(rep.errors)
    assert "steps.a.when: expression fields take a bare Jinja expression" in joined
    assert "steps.a.shell: '${{' is not rayspec syntax" in joined
    assert "steps.l.loop.until: expression fields" in joined
    assert "steps.e.each: expression fields" in joined


def test_session_rules(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
agents:
  c: {provider: claude}
  x: {provider: codex}
steps:
  - id: a
    prompt: hi
    agent: c
  - id: b
    prompt: hi
    agent: c
    session: a
  - id: bad_not_ancestor
    prompt: hi
    agent: c
    session: a
    needs: []
  - id: other_provider
    needs: [a]
    prompt: hi
    agent: x
    session: a
  - id: self_outside
    prompt: hi
    session: self_outside
  - id: not_prompt
    shell: echo
  - id: sess_shell
    needs: [not_prompt]
    prompt: hi
    session: not_prompt
  - id: l
    needs: [a]
    loop:
      max_iterations: 2
      steps:
        - id: impl
          prompt: hi
          agent: c
          session: impl
        - id: cont
          needs: [impl]
          prompt: hi
          agent: c
          session: a
""",
    )
    errors = "\n".join(rep.errors)
    assert "steps.b.session: 'a' must be an ancestor of 'b'" in errors
    assert "steps.other_provider.session: session target 'a' runs on provider 'claude'" in errors
    assert "steps.self_outside.session: session: <self> only makes sense inside a loop" in errors
    assert "steps.sess_shell.session: 'not_prompt' is not a prompt step" in errors
    assert "steps.l/impl" not in errors
    assert "steps.l/cont" not in errors


def test_include_with_validation(tree: Tree):
    tree.workflow(
        "blk",
        """
rayspec: 1
name: blk
inputs:
  target: {type: string, required: true}
  n: {type: integer, default: 1}
  flag: {type: boolean}
  tags: {type: array}
steps:
  - id: a
    shell: echo
outputs:
  out: "{{ steps.a.output }}"
""",
    )
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: good
    include: blk
    with: {target: x, n: "3", flag: "yes", tags: '["a"]'}
  - id: bad
    include: blk
    with: {targt: x, n: abc, flag: maybe, tags: "nope"}
  - id: templated
    include: blk
    with: {target: "{{ steps.good.output.out }}", n: "{{ inputs.k }}"}
    needs: [good]
""",
    )
    errors = "\n".join(rep.errors)
    assert "steps.good" not in errors
    assert "has no input 'targt'; did you mean 'target'?" in errors
    assert "missing required input(s) for included workflow 'blk': target" in errors
    assert "steps.bad.with.n: expected an integer, got 'abc'" in errors
    assert "steps.bad.with.flag: expected a boolean" in errors
    assert "steps.bad.with.tags: expected array (JSON)" in errors
    assert "steps.templated" not in errors


def test_reference_checks_with_checker(tree: Tree):
    tree.workflow(
        "blk",
        """
rayspec: 1
name: blk
inputs:
  target: {type: string, required: true}
steps:
  - id: a
    shell: echo {{ inputs.target }} {{ inputs.missing }}
outputs:
  out: "{{ steps.a.output }}"
""",
    )
    rep = _validate(
        tree,
        HEAD
        + """
inputs:
  issue: {type: integer}
steps:
  - id: fetch
    shell: echo {{ inputs.issue }} {{ inputs.isue }}
  - id: assess
    needs: [fetch]
    prompt: "{{ steps.fetch.output }} {{ steps.later.output }} {{ steps.impl.output }} {{ nope.x }}"
  - id: later
    shell: echo {{ steps.assess.output }} {{ iteration.n }} {{ each.index }}
  - id: build
    needs: [assess]
    loop:
      max_iterations: 3
      until: steps.review.output == 'ok' and steps.fetch.output
      steps:
        - id: impl
          prompt: "{{ steps.fetch.output }} {{ iteration.prev.review.output }} {{ iteration.prev.nope.output }} {{ steps.build.output }}"
        - id: review
          needs: [impl]
          prompt: "{{ steps.impl.output }} {{ steps.assess.output }} {{ steps.later.output }}"
  - id: inc
    needs: [build]
    include: blk
    with: {target: "{{ steps.build.output.review }}"}
  - id: after
    needs: [inc]
    shell: echo {{ steps.inc.output.out }} {{ steps.inc.output.nope }}
  - id: fan
    needs: [fetch]
    each: steps.fetch.output
    as: it
    steps:
      - id: p
        shell: echo {{ it }} {{ each.index }} {{ item }}
  - id: bad_tpl
    prompt: "{{ BAD"
  - id: bad_when
    when: BAD expr
    shell: echo
outputs:
  final: "{{ steps.after.output }} {{ steps.p.output }}"
""",
        checker=True,
    )
    errors = "\n".join(rep.errors)
    assert (
        "steps.fetch.shell: inputs.isue is not declared under inputs:; did you mean 'issue'?"
        in errors
    )
    assert (
        "steps.assess.prompt: steps.later is not an ancestor of this step (add 'later' to needs:)"
        in errors
    )
    assert (
        "steps.assess.prompt: steps.impl is inside loop 'build'; use steps.build.output.impl"
        in errors
    )
    assert "steps.assess.prompt: unknown name 'nope'" in errors
    assert "steps.later.shell: 'iteration' is only available inside a loop body" in errors
    assert "steps.later.shell: 'each' is only available inside an each: body" in errors
    assert "steps.later.shell: steps.assess is not an ancestor" in errors
    assert (
        "steps.build/impl.prompt: iteration.prev.nope: 'nope' is not a step of the enclosing loop body"
        in errors
    )
    assert "steps.build/impl.prompt: steps.build is the enclosing composite step" in errors
    assert "steps.build/review.prompt: steps.later is not an ancestor" in errors
    assert "steps.build/review.prompt: steps.impl" not in errors
    assert "steps.build/review.prompt: steps.assess" not in errors
    assert "steps.build.loop.until" not in errors  # review + fetch are visible after the body
    assert "steps.inc/a.shell: inputs.missing is not declared" in errors
    assert "steps.inc/a.shell: inputs.target" not in errors
    assert "steps.after.shell: include 'inc' has no output 'nope' (outputs: out)" in errors
    assert "steps.fan/p.shell: unknown name 'it'" not in errors
    assert "steps.bad_tpl.prompt: unbalanced braces in template" in errors
    assert "steps.bad_when.when: unexpected token 'BAD'" in errors
    assert "outputs.final: steps.p is inside each 'fan'; use steps.fan.output.p" in errors
    assert "outputs.final: steps.after" not in errors


def test_capability_mapping_exact_format(tree: Tree):
    rep = _validate(
        tree,
        """rayspec: 1
name: wf
agents:
  implementer:
    provider: codex
    max_turns: 60
    budget_usd: 1.5
    thinking: true
    tools: {deny: [edit], allow: [codex:Shell, claude:WebFetch, bogus]}
    effort: max
    access: read-only
steps:
  - id: a
    prompt: x
    agent: implementer
    output_schema: {type: object}
    session: a
    env: {FOO: bar}
  - id: b
    prompt: x
    agent: {provider: codex, access: read-only, tools: {allow: [edit]}}
""",
    )
    first = next(e for e in rep.errors if e.startswith("unsupported: agents.implementer.max_turns"))
    assert first == (
        "unsupported: agents.implementer.max_turns = 60\n"
        "  provider 'codex' does not support `max_turns` (capability max_turns=False)\n"
        "  fix: remove it, use a provider that supports it (claude, stub), or set "
        "defaults.on_unsupported: warn / --allow-unsupported\n"
        "  at .rayspec/workflows/wf.yaml:6"
    )
    joined = "\n".join(rep.errors)
    assert "unsupported: agents.implementer.budget_usd = 1.5" in joined
    assert "unsupported: agents.implementer.thinking = True" in joined
    assert (
        "unsupported: agents.implementer.tools.deny = edit\n  provider 'codex' does not support `edit tools` (capability tool_groups=['web'])"
        in joined
    )
    assert (
        "unsupported: agents.implementer.tools.allow = codex:Shell\n  provider 'codex' does not support `raw tool names` (capability raw_tool_names=False)"
        in joined
    )
    assert "agents.implementer.tools.allow: unknown tool 'bogus'" in joined
    assert any(
        "'claude:WebFetch' targets provider 'claude'; ignored for 'codex'" in w
        for w in rep.warnings
    )
    assert any(
        "agents.implementer.effort: provider 'codex' has no effort level 'max'; using 'xhigh'" in w
        for w in rep.warnings
    )
    assert "unsupported: agents.implementer.effort" not in joined
    assert "unsupported: agents.implementer.access" not in joined
    assert "steps.b.agent.tools.allow: access: read-only cannot allow edit" in joined
    assert "steps.a.output_schema" not in joined  # codex: structured enforced
    assert "steps.a.session: session: <self> only makes sense inside a loop" in joined
    assert {u.capability for u in rep.unsupported} == {
        "max_turns",
        "budget_usd",
        "thinking",
        "tool_groups",
        "raw_tool_names",
    }
    assert len(rep.unsupported) == 6  # + steps.b.agent.tools.allow = edit


def test_unsupported_becomes_warning(tree: Tree):
    text = (
        HEAD
        + """
agents:
  i: {provider: codex, max_turns: 3}
steps:
  - id: a
    prompt: x
    agent: i
"""
    )
    rep = _validate(tree, text, on_unsupported="warn")
    assert rep.errors == []
    assert len(rep.unsupported) == 1
    assert rep.warnings and rep.warnings[0].startswith("unsupported: agents.i.max_turns = 3")
    rep2 = _validate(
        tree, text.replace("name: wf\n", "name: wf\ndefaults: {on_unsupported: warn}\n")
    )
    assert rep2.errors == []
    assert len(rep2.unsupported) == 1


def test_tools_syntax_errors_reported_once_per_agent_without_capabilities(tree: Tree):
    text = (
        HEAD
        + """
agents:
  i: {provider: codex, access: read-only, tools: {allow: [bogus, edit]}}
steps:
  - id: a
    prompt: x
    agent: i
  - id: b
    prompt: x
    agent: i
  - id: c
    prompt: x
    agent: i
"""
    )
    tree.workflow("wf", text)
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    for caps_for in (None, lambda _provider: None):
        rep = validate_workflow(rw, capabilities_for=caps_for)
        unknown = [e for e in rep.errors if "unknown tool 'bogus'" in e]
        read_only = [e for e in rep.errors if "read-only cannot allow edit" in e]
        assert len(unknown) == 1, rep.errors
        assert len(read_only) == 1, rep.errors


def test_unknown_provider_skips_capabilities_with_warning(tree: Tree):
    tree.workflow(
        "wf",
        HEAD
        + "agents:\n  o: {provider: other, max_turns: 3}\nsteps:\n  - id: a\n    prompt: x\n    agent: o\n",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home, known_providers=["other"])
    rep = validate_workflow(rw, capabilities_for=capabilities_for)
    assert rep.errors == []
    assert any("provider 'other': not registered" in w for w in rep.warnings)
    rep = validate_workflow(rw, capabilities_for=None)
    assert rep.errors == [] and rep.warnings == []


def test_topological_order(tree: Tree):
    tree.workflow(
        "wf",
        HEAD
        + """
steps:
  - id: d
    needs: [b, c]
    shell: echo
  - id: c
    needs: [a]
    shell: echo
  - id: b
    needs: [a]
    shell: echo
  - id: a
    shell: echo
""",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    assert [s.id for s in topological_order(rw.workflow.steps)] == ["a", "c", "b", "d"]


def test_clean_workflow_has_no_errors(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
inputs:
  issue: {type: integer, required: true}
agents:
  triage: {provider: claude, model: small, access: read-only}
steps:
  - id: fetch
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE"
  - id: assess
    needs: [fetch]
    agent: triage
    prompt: "{{ steps.fetch.output }}"
    output_schema: {type: object}
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    stop: {status: cancelled, reason: "no — {{ steps.assess.output.reason }}"}
  - id: build
    needs: [assess]
    loop:
      max_iterations: 3
      until: steps.review.output == 'ok'
      steps:
        - id: implement
          session: implement
          prompt: "{{ steps.fetch.output }} {{ iteration.prev.review.output }}"
        - id: review
          needs: [implement]
          agent: triage
          prompt: "{{ steps.implement.output }}"
  - id: confirm
    needs: [build]
    approve: "Open a PR for {{ inputs.issue }}?"
outputs:
  verdict: "{{ steps.assess.output.verdict }}"
""",
        checker=True,
    )
    assert rep.errors == []
    assert rep.unsupported == []


def test_stop_inside_body_is_allowed(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: l
    loop:
      max_iterations: 2
      steps:
        - id: check
          shell: echo
        - id: halt
          needs: [check]
          when: steps.check.ok
          stop: {status: succeeded, reason: done}
""",
        checker=True,
    )
    assert rep.errors == []


def test_include_body_is_a_closed_scope(tree: Tree):
    """The engine starts a fresh scope chain for an include body; validate must agree."""
    tree.workflow(
        "blk",
        """
rayspec: 1
name: blk
steps:
  - id: inner
    shell: "echo {{ steps.a.output }}"
""",
    )
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: a
    shell: echo a
  - id: use
    needs: [a]
    include: blk
""",
        checker=True,
    )
    assert any("steps.use/inner.shell" in e and "steps.a" in e for e in rep.errors), rep.errors


def test_timeout_on_approve_or_stop_is_an_error(tree: Tree):
    rep = _validate(
        tree,
        HEAD
        + """
steps:
  - id: gate
    approve: ok?
    timeout: 1s
  - id: halt
    needs: [gate]
    stop: {status: cancelled}
    timeout: 2s
""",
    )
    assert any(e.startswith("steps.gate.timeout") for e in rep.errors), rep.errors
    assert any(e.startswith("steps.halt.timeout") for e in rep.errors), rep.errors
