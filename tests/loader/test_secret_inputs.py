"""Load-time rules for ``secret: true`` inputs.

A secret input may reach a ``shell:``/``python:`` step only through the environment
(``RAYSPEC_INPUT_<NAME>``) or that step's ``env:`` mapping; every other template/expression that
names it is a validation error. Resume re-obtains secrets through ``resolve_resume_secrets``.
"""

from __future__ import annotations

import pytest

from rayspec.errors import InputError
from rayspec.loader import load_workflow, validate_workflow
from rayspec.loader.inputs import (
    SECRET_PLACEHOLDER,
    redact_inputs,
    resolve_inputs,
    resolve_resume_secrets,
    secret_input_names,
    split_secret_inputs,
)
from rayspec.templating import TemplateEngine

from .conftest import Tree
from .fakes import capabilities_for

HEAD = """rayspec: 1
name: wf
inputs:
  token: { type: string, secret: true }
  issue: { type: integer, default: 1 }
agents:
  r: { provider: stub }
"""

RULE = "secret inputs can only reach shell/python steps via RAYSPEC_INPUT_TOKEN"


def _validate(tree: Tree, body: str, *, head: str = HEAD):
    tree.workflow("wf", head + body)
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    return validate_workflow(
        rw,
        capabilities_for=capabilities_for,
        template_checker=TemplateEngine(),
        provider_ids=["claude", "codex", "stub"],
    )


def test_env_mapping_of_shell_and_python_steps_is_allowed(tree: Tree):
    rep = _validate(
        tree,
        """
steps:
  - id: a
    shell: echo "$TOKEN" "$RAYSPEC_INPUT_TOKEN"
    env: { TOKEN: "{{ inputs.token }}" }
  - id: b
    python: print("x")
    env: { TOKEN: "{{ inputs.token }}", N: "{{ inputs.issue }}" }
""",
    )
    assert rep.errors == [], rep.errors


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ('  - {id: p, agent: r, prompt: "t {{ inputs.token }}"}', "steps.p.prompt"),
        ('  - {id: p, agent: r, prompt: "t", env: {T: "{{ inputs.token }}"}}', "steps.p.env.T"),
        ('  - {id: s, shell: "echo {{ inputs.token }}"}', "steps.s.shell"),
        ('  - {id: s, python: "print({{ inputs.token }})"}', "steps.s.python"),
        ('  - {id: s, shell: echo, cwd: "{{ inputs.token }}"}', "steps.s.cwd"),
        ("  - {id: s, shell: echo, when: inputs.token is defined}", "steps.s.when"),
        (
            "  - id: l\n    loop: {max_iterations: 2, until: inputs.token == 'x', "
            "steps: [{id: i, shell: echo}]}",
            "steps.l.loop.until",
        ),
        ("  - {id: e, each: inputs.token, steps: [{id: i, shell: echo}]}", "steps.e.each"),
        ('  - {id: g, approve: "ok {{ inputs.token }}?"}', "steps.g.approve.message"),
        ('  - {id: x, stop: {reason: "bye {{ inputs.token }}"}}', "steps.x.stop.reason"),
    ],
)
def test_every_other_reference_is_a_load_time_error(tree: Tree, body: str, where: str):
    rep = _validate(tree, "steps:\n" + body + "\n")
    hits = [e for e in rep.errors if e.startswith(where + ":") and RULE in e]
    assert hits, rep.errors
    assert "inputs.token" in hits[0] and "secret: true" in hits[0]


def test_outputs_and_include_with_cannot_name_a_secret(tree: Tree):
    tree.workflow(
        "block",
        "rayspec: 1\nname: block\ninputs:\n  t: {type: string}\n"
        "steps:\n  - {id: i, shell: echo}\noutputs:\n  o: x\n",
    )
    rep = _validate(
        tree,
        """
steps:
  - id: inc
    include: block
    with: { t: "{{ inputs.token }}" }
outputs:
  leaked: "{{ inputs.token }}"
""",
    )
    joined = "\n".join(rep.errors)
    assert "steps.inc.with.t: " in joined and RULE in joined
    assert "outputs.leaked: " in joined


def test_agent_instructions_cannot_name_a_secret(tree: Tree):
    rep = _validate(
        tree,
        """
steps:
  - {id: p, agent: r, prompt: hi}
""",
        head=HEAD.replace(
            "r: { provider: stub }", 'r: { provider: stub, instructions: "{{ inputs.token }}" }'
        ),
    )
    assert any(RULE in e and "instructions" in e for e in rep.errors), rep.errors


def test_included_workflow_cannot_declare_a_secret(tree: Tree):
    tree.workflow(
        "block",
        "rayspec: 1\nname: block\ninputs:\n  t: {type: string, secret: true}\n"
        "steps:\n  - {id: i, shell: echo}\n",
    )
    rep = _validate(
        tree,
        """
steps:
  - id: inc
    include: block
    with: { t: literal }
""",
    )
    assert any(
        e.startswith("steps.inc.with:") and "secret" in e and "root workflow" in e
        for e in rep.errors
    ), rep.errors


# -- resolution helpers ---------------------------------------------------------------------------


def _wf(tree: Tree):
    tree.workflow("wf", HEAD + "steps:\n  - {id: a, shell: echo}\n")
    return load_workflow("wf", project_root=tree.root, home=tree.home).workflow


def test_split_and_redact(tree: Tree):
    wf = _wf(tree)
    assert secret_input_names(wf) == ("token",)
    values = resolve_inputs(wf, cli_pairs=["token=ghp_x"], env={})
    public, secrets = split_secret_inputs(values, ("token",))
    assert public == {"issue": 1} and secrets == {"token": "ghp_x"}
    assert redact_inputs(values, ("token",)) == {"token": SECRET_PLACEHOLDER, "issue": 1}
    assert SECRET_PLACEHOLDER == "<secret>"
    # an optional secret that was not given stays absent (undefined), not a placeholder
    assert redact_inputs(resolve_inputs(wf, env={}), ("token",)) == {"issue": 1}


def test_resume_secrets_from_cli_then_env(tree: Tree):
    wf = _wf(tree)
    recorded = {"token": SECRET_PLACEHOLDER, "issue": 1}
    assert resolve_resume_secrets(wf, recorded, cli_pairs=["token=a"], env={}) == {"token": "a"}
    assert resolve_resume_secrets(wf, recorded, cli_pairs=[], env={"RAYSPEC_INPUT_TOKEN": "b"}) == {
        "token": "b"
    }
    # --input wins over the environment
    assert resolve_resume_secrets(
        wf, recorded, cli_pairs=["token=a"], env={"RAYSPEC_INPUT_TOKEN": "b"}
    ) == {"token": "a"}


def test_resume_secrets_missing_and_non_secret_inputs(tree: Tree):
    wf = _wf(tree)
    recorded = {"token": SECRET_PLACEHOLDER, "issue": 1}
    with pytest.raises(InputError) as info:
        resolve_resume_secrets(wf, recorded, cli_pairs=[], env={})
    (msg,) = info.value.errors
    assert msg == (
        "missing secret input(s): token — pass --input token=… or set RAYSPEC_INPUT_TOKEN"
    )
    with pytest.raises(InputError, match="inputs are fixed per run") as info:
        resolve_resume_secrets(wf, recorded, cli_pairs=["issue=2", "token=a"], env={})
    assert "issue" in info.value.errors[0] and "secret" in info.value.errors[0]
    # an optional secret that was not given at launch is not required on resume …
    assert resolve_resume_secrets(wf, {"issue": 1}, cli_pairs=[], env={}) == {}
    # … but may be supplied now
    assert resolve_resume_secrets(wf, {"issue": 1}, cli_pairs=["token=z"], env={}) == {"token": "z"}


# -- whole-inputs references / value redaction ----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "{{ inputs.get('token') }}",
        "{% for k, v in inputs.items() %}{{ v }}{% endfor %}",
        "{{ inputs | tojson }}",
        "{{ inputs[name] }}",
    ],
)
def test_whole_inputs_references_are_refused_when_a_secret_is_declared(tree: Tree, body: str):
    rep = _validate(tree, "steps:\n  - id: ask\n    agent: r\n    prompt: |\n      " + body + "\n")
    hits = [e for e in rep.errors if e.startswith("steps.ask.prompt:") and RULE in e]
    assert hits, rep.errors
    assert "inputs.token" in hits[0] and "secret: true" in hits[0]
    # … the same in an expression
    rep = _validate(tree, "steps:\n  - {id: s, shell: echo, when: inputs | length > 1}\n")
    assert any(e.startswith("steps.s.when:") and RULE in e for e in rep.errors), rep.errors


def test_whole_inputs_references_are_fine_in_env_mappings_and_without_secrets(tree: Tree):
    rep = _validate(
        tree,
        'steps:\n  - {id: s, shell: echo, env: {ALL: "{{ inputs | tojson }}"}}\n',
    )
    assert rep.errors == [], rep.errors
    rep = _validate(
        tree,
        'steps:\n  - {id: p, agent: r, prompt: "{{ inputs | tojson }}"}\n',
        head=HEAD.replace("token: { type: string, secret: true }", "token: { type: string }"),
    )
    assert rep.errors == [], rep.errors


def test_an_invalid_secret_value_is_never_echoed(tree: Tree):
    from rayspec.loader.inputs import coerce_input
    from rayspec.schema import InputSpec

    with pytest.raises(InputError) as info:
        coerce_input("notanint_SECRETVAL", InputSpec(type="integer", secret=True), name="token")
    (msg,) = info.value.errors
    assert "SECRETVAL" not in msg and "<secret>" in msg and "integer" in msg
    # non-secret inputs keep echoing the offending value
    with pytest.raises(InputError, match="notanint"):
        coerce_input("notanint", InputSpec(type="integer"), name="issue")
    # schema validation (enum) and the env source redact as well
    tree.workflow(
        "wf",
        HEAD.replace(
            "token: { type: string, secret: true }",
            "token: { type: string, secret: true, enum: [a, b] }",
        )
        + "steps:\n  - {id: a, shell: echo}\n",
    )
    wf = load_workflow("wf", project_root=tree.root, home=tree.home).workflow
    for cli_pairs, env in (
        (["token=zzz_SECRETVAL"], {}),
        ([], {"RAYSPEC_INPUT_TOKEN": "zzz_SECRETVAL"}),
    ):
        with pytest.raises(InputError) as info:
            resolve_inputs(wf, cli_pairs=cli_pairs, env=env)
        joined = "\n".join(info.value.errors) + "\n".join(
            m for ms in info.value.problems.values() for m in ms
        )
        assert "SECRETVAL" not in joined and "<secret>" in joined, joined
    # and on resume
    tree.workflow(
        "wf",
        HEAD.replace(
            "token: { type: string, secret: true }", "token: { type: integer, secret: true }"
        )
        + "steps:\n  - {id: a, shell: echo}\n",
    )
    wf = load_workflow("wf", project_root=tree.root, home=tree.home).workflow
    with pytest.raises(InputError) as info:
        resolve_resume_secrets(
            wf, {"token": SECRET_PLACEHOLDER}, cli_pairs=["token=notanint_SECRETVAL"], env={}
        )
    assert "SECRETVAL" not in "\n".join(info.value.errors)
