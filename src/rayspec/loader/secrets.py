# SPDX-License-Identifier: Apache-2.0
"""Where a ``secret: true`` input may appear — the load-time placement rules.

Module boundary: pure rules over already-parsed references. Nothing here reads files, resolves
values or knows about runs; :mod:`rayspec.loader.validate` calls
:func:`check_secret_reference` at exactly ONE place in its reference walk and turns the verdict
into a report entry, and :func:`include_secret_input_message` is the include rule's wording.

The rules, unchanged in substance since 1.0 and deliberately narrow:

* a secret input reaches a step **only** as the process environment of a ``shell:``/``python:``
  step — ``RAYSPEC_INPUT_<NAME>`` automatically, or through that step's ``env:`` mapping;
* every other template position (a prompt body, an expression, ``cwd:``, an ``outputs:`` entry,
  an included workflow's ``with:`` binding) is a load-time error;
* using ``inputs`` *as a whole* (``inputs | tojson``, ``inputs.get(…)``, ``inputs[expr]``) while
  any input is secret is the same error — the whole mapping would carry the secret along.

**Why ``env:`` on a ``prompt:`` step stays refused.** The rule was re-examined with
a live run against both adapters, feeding a probe value through a prompt step's ``env:`` and
grepping for it afterwards. Claude was clean. The Codex CLI, however, writes a snapshot of the
child's environment to ``~/.codex/shell_snapshots/<id>.<ts>.sh`` — mode ``0644``, containing a
literal ``export PROBE_TOKEN=<value>`` line — which outlives the run, sits outside
``$RAYSPEC_HOME`` and is therefore beyond the reach of :mod:`rayspec.redact`. One adapter
persisting the value in a world-readable file is enough: the rule is NOT relaxed. See
``tests/integration/test_secret_placement_live.py`` for the reproduction.

**Why an include ``with:`` binding stays refused.** It is also unnecessary: every secret the run
was given is exported into *every* scope's ``shell:``/``python:`` steps, include bodies
included (``RunContext.secret_env`` ignores the scope on purpose), and every ``config.secrets``
entry is exported under its own name. An included body already has the secret; binding it
through ``with:`` would only add a persisted copy in the body's inputs and ``context.json``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rayspec.schema import InputSpec, PythonStep, ShellStep


@dataclass(frozen=True, slots=True)
class SecretVerdict:
    """The outcome of one reference check: an error ``message`` (or ``None``) and whether the
    caller should stop looking at this reference (``stop``)."""

    message: str | None = None
    stop: bool = False


#: The one template position a secret input may be named in.
SECRET_OK_WHERE = "a shell/python step's env: mapping"


def secret_reference_message(name: str) -> str:
    """The rule text for a template/expression that names a secret input outside a shell/python
    ``env:`` mapping."""
    return (
        f"inputs.{name} is declared secret: true — secret inputs can only reach shell/python "
        f"steps via RAYSPEC_INPUT_{name.upper()} (or a shell/python env: mapping); they are "
        "never rendered into prompts, expressions, outputs or any other template"
    )


def secret_whole_inputs_message(names: Sequence[str]) -> str:
    """The rule text for a template/expression that uses ``inputs`` as a whole (``inputs |
    tojson``, ``inputs.get(...)``, ``inputs.items()``, ``inputs[expr]``) while ``names`` are
    declared ``secret: true``."""
    listed = ", ".join(f"inputs.{n}" for n in names)
    env_vars = ", ".join(f"RAYSPEC_INPUT_{n.upper()}" for n in names)
    return (
        f"inputs is used as a whole while {listed} "
        f"{'is' if len(names) == 1 else 'are'} declared secret: true — secret inputs can only "
        f"reach shell/python steps via {env_vars} (or a shell/python env: mapping); name the "
        "other inputs individually (inputs.<name>) instead"
    )


def include_secret_input_message(workflow_name: str, names: Sequence[str]) -> str:
    """The rule text for an included workflow that declares secret inputs of its own."""
    env_vars = ", ".join(f"RAYSPEC_INPUT_{n.upper()}" for n in names)
    return (
        f"included workflow {workflow_name!r} declares secret input(s) "
        f"{', '.join(names)}; secret inputs are only supported on the root "
        "workflow (a with: binding would be persisted) — the body does not need one: every "
        f"secret of the run already reaches its shell/python steps as {env_vars}"
    )


def check_secret_reference(
    bare_root: str | None,
    normalized: tuple[str, str, tuple[Any, ...]] | None,
    inputs: Mapping[str, InputSpec],
    *,
    secret_ok: bool,
) -> SecretVerdict:
    """Judge one template reference against the placement rules.

    ``bare_root`` is the root of a reference whose first segment is dynamic or missing
    (``{{ inputs }}``, ``inputs[expr]``); ``normalized`` is ``(root, name, attrs)`` for a plain
    ``root.name`` reference. ``secret_ok`` is true only at the one position a secret may be
    named. A ``stop`` verdict means the reference needs no further checking (a bare ``inputs``
    has no name to resolve; a refused secret reference is already reported).
    """
    if bare_root == "inputs" and not secret_ok:
        secrets = [name for name, spec in inputs.items() if spec.secret]
        return SecretVerdict(secret_whole_inputs_message(secrets) if secrets else None, stop=True)
    if normalized is None or secret_ok:
        return SecretVerdict()
    root, name, _attrs = normalized
    spec = inputs.get(name)
    if root == "inputs" and spec is not None and spec.secret:
        return SecretVerdict(secret_reference_message(name), stop=True)
    return SecretVerdict()


def config_secrets_in_use(steps: Iterable[Any], names: Iterable[str]) -> tuple[str, ...]:
    """The ``config.secrets`` names a run of ``steps`` could actually read.

    A ``secrets:`` entry reaches a step as the environment variable ``<NAME>`` and only ever a
    ``shell:``/``python:`` step, so "in use" means: the name appears — as a whole word — in such
    a step's body, in one of its ``env:`` values or keys, or in its ``cwd:``. That is what lets
    the CLI resolve the table LAZILY: a stale entry in ``~/.rayspec/config.yaml`` must not make
    every workflow on the machine unrunnable, and a ``cmd:`` helper (``op read``, a keychain
    prompt) must not run on a ``rayspec run`` that has nothing to do with it.

    Deliberately textual and deliberately generous: it errs towards "in use" (a mention in a
    comment counts) because resolving one secret too many only costs time, while missing one
    would hand the step an empty variable. A body that reads the whole environment
    dynamically (``env | grep``) is the documented exception — name the entry in the script, or
    keep it in the shell that launches rayspec.
    """
    wanted = tuple(dict.fromkeys(names))
    if not wanted:
        return ()
    haystack: list[str] = []
    for step in steps:
        if not isinstance(step, ShellStep | PythonStep):
            continue  # config secrets are handed to shell:/python: steps and nothing else
        haystack.append(step.shell if isinstance(step, ShellStep) else step.python)
        haystack.extend(step.env)
        haystack.extend(str(value) for value in step.env.values())
        if step.cwd:
            haystack.append(step.cwd)
    text = "\n".join(haystack)
    return tuple(
        name
        for name in wanted
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
    )


__all__ = [
    "SECRET_OK_WHERE",
    "SecretVerdict",
    "check_secret_reference",
    "config_secrets_in_use",
    "include_secret_input_message",
    "secret_reference_message",
    "secret_whole_inputs_message",
]
