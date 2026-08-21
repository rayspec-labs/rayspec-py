# SPDX-License-Identifier: Apache-2.0
"""``resolve_inputs``: CLI pairs / inputs file / env / defaults → one validated inputs dict.

Boundary: pure data resolution for a :class:`Workflow`'s ``inputs:`` declarations. Precedence is
``--input k=v`` > ``--inputs-file`` > ``RAYSPEC_INPUT_<NAME>`` > ``default``. Values from text
sources are coerced by the declared type; the result is validated against
:func:`inputs_to_json_schema`. Problems are collected and raised together as :class:`InputError`.

Secret inputs (``secret: true``) resolve like any other input; the helpers at the end split
them off (:func:`split_secret_inputs`), replace them with :data:`SECRET_PLACEHOLDER` for
everything that is persisted or printed (:func:`redact_inputs`) and re-obtain them on resume
(:func:`resolve_resume_secrets`: ``--input`` for secret names only, else the env var).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

from rayspec.errors import InputError, LoaderError
from rayspec.loader.yaml import load_yaml
from rayspec.schema import InputSpec, Workflow, inputs_to_json_schema
from rayspec.schema.base import suggest

ENV_PREFIX = "RAYSPEC_INPUT_"
#: What ``run.json``/``context.json``/``plan``/``show`` store or print in place of a secret value.
SECRET_PLACEHOLDER = "<secret>"
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0"})


def env_var_name(input_name: str) -> str:
    """``issue`` → ``RAYSPEC_INPUT_ISSUE``."""
    return ENV_PREFIX + input_name.upper()


def coerce_input(value: Any, spec: InputSpec, *, name: str) -> Any:
    """Coerce a text value to ``spec.type``; non-text values pass through untouched.

    Raises :class:`InputError` with a single message when the text cannot be coerced. The
    offending value is quoted in the message — except for a ``secret: true`` input, whose value
    is never printed: the message shows :data:`SECRET_PLACEHOLDER` instead.
    """
    if not isinstance(value, str):
        return value
    kind = spec.type
    text = value.strip()
    shown = SECRET_PLACEHOLDER if spec.secret else repr(value)
    if kind == "string":
        return value
    if kind == "integer":
        try:
            return int(text)
        except ValueError:
            raise InputError(f"input {name!r}: expected an integer, got {shown}") from None
    if kind == "number":
        try:
            return float(text)
        except ValueError:
            raise InputError(f"input {name!r}: expected a number, got {shown}") from None
    if kind == "boolean":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise InputError(f"input {name!r}: expected a boolean (true/false/yes/no/1/0), got {shown}")
    if kind == "array":
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise InputError(f"input {name!r}: invalid JSON array: {exc}") from None
            if not isinstance(parsed, list):
                raise InputError(f"input {name!r}: expected a JSON array, got {shown}")
            return parsed
        return [_coerce_item(value, spec, name=name)]
    if kind == "object":
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise InputError(f"input {name!r}: invalid JSON object: {exc}") from None
        if not isinstance(parsed, dict):
            raise InputError(f"input {name!r}: expected a JSON object, got {shown}")
        return parsed
    return value


def _coerce_item(value: str, spec: InputSpec, *, name: str) -> Any:
    item_type = (spec.items or {}).get("type") if isinstance(spec.items, dict) else None
    if isinstance(item_type, str) and item_type != "string":
        item_spec = InputSpec(type=item_type, secret=spec.secret)  # type: ignore[arg-type]
        return coerce_input(value, item_spec, name=f"{name}[]")
    return value


def split_cli_pairs(pairs: Sequence[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """``["k=v", ...]`` → ``([(k, v), ...], [error message per malformed pair])``."""
    out: list[tuple[str, str]] = []
    bad: list[str] = []
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            bad.append(f"invalid --input {pair!r}: expected NAME=VALUE")
            continue
        out.append((key.strip(), value))
    return out, bad


def parse_cli_pairs(pairs: Sequence[str]) -> list[tuple[str, str]]:
    """``["k=v", ...]`` → ``[(k, v), ...]``; raises :class:`InputError` on a malformed pair."""
    out, bad = split_cli_pairs(pairs)
    if bad:
        raise InputError(bad, hint="example: --input issue=123 --input tags=a --input tags=b")
    return out


def read_inputs_file(path: Path) -> dict[str, Any]:
    """Read a ``.json`` / ``.yaml`` / ``.yml`` inputs file into a mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise InputError(f"inputs file {str(path)!r} not found") from None
    except OSError as exc:
        raise InputError(f"cannot read inputs file {str(path)!r}: {exc.strerror or exc}") from None
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise InputError(f"{path}: invalid JSON: {exc}") from None
    else:
        try:
            data = load_yaml(text, source=str(path))
        except LoaderError as exc:
            raise InputError(str(exc), hint=exc.hint) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InputError(f"{path}: inputs file must be a mapping of name → value")
    return {str(k): v for k, v in data.items()}


def resolve_inputs(
    workflow: Workflow,
    *,
    cli_pairs: Sequence[str] = (),
    inputs_file: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the run inputs for ``workflow`` (see module docstring for precedence).

    Unknown names get a did-you-mean; every missing required input is reported together; an
    input whose given value is rejected is reported once as invalid (never also as missing); the
    final dict is validated against :func:`inputs_to_json_schema`.
    """
    environ = os.environ if env is None else env
    specs = workflow.inputs
    errors: list[str] = []
    values: dict[str, Any] = {}
    problems: dict[str, list[str]] = {}

    def problem(name: str, messages: Sequence[str]) -> None:
        errors.extend(messages)
        problems.setdefault(name, []).extend(messages)

    def unknown(name: str, source: str) -> None:
        hint = suggest(name, set(specs))
        msg = f"{source}: unknown input {name!r}"
        if specs:
            msg += f"; did you mean {hint!r}?" if hint else f" (declared: {', '.join(specs)})"
        else:
            msg += " (this workflow declares no inputs)"
        errors.append(msg)

    # 1. CLI pairs (highest precedence; repeated array inputs append)
    cli_values: dict[str, Any] = {}
    seen_cli: set[str] = set()
    repeated: list[str] = []
    pairs, bad_pairs = split_cli_pairs(cli_pairs)
    errors.extend(bad_pairs)
    for name, raw in pairs:
        spec = specs.get(name)
        if spec is None:
            unknown(name, "--input")
            continue
        if spec.type != "array" and name in seen_cli:
            if name not in repeated:
                repeated.append(name)
                errors.append(
                    f"--input: input {name!r} given more than once "
                    "(only array inputs may be repeated)"
                )
            continue
        seen_cli.add(name)
        try:
            coerced = coerce_input(raw, spec, name=name)
        except InputError as exc:
            problem(name, exc.errors)
            continue
        if spec.type == "array" and name in cli_values and isinstance(cli_values[name], list):
            cli_values[name] = [*cli_values[name], *coerced]
        else:
            cli_values[name] = coerced
    values.update(cli_values)

    # 2. inputs file
    if inputs_file is not None:
        try:
            file_values = read_inputs_file(inputs_file)
        except InputError as exc:
            # an unreadable file is the one problem worth reporting (everything else follows)
            raise InputError(exc.errors, hint=exc.hint, partial=values) from None
        for name, raw in file_values.items():
            spec = specs.get(name)
            if spec is None:
                unknown(name, str(inputs_file))
                continue
            if name in values:
                continue
            try:
                values[name] = coerce_input(raw, spec, name=name)
            except InputError as exc:
                problem(name, exc.errors)

    # 3. environment, 4. defaults
    missing: list[str] = []
    for name, spec in specs.items():
        if name in values or name in problems:
            # a value was given (even an invalid one): the input is not missing, and a lower-
            # precedence source must not silently stand in for the rejected value
            continue
        env_value = environ.get(env_var_name(name))
        if env_value is not None:
            try:
                values[name] = coerce_input(env_value, spec, name=name)
            except InputError as exc:
                problem(name, [f"{env_var_name(name)}: {e}" for e in exc.errors])
            continue
        if spec.has_default:
            values[name] = spec.default
        elif spec.required:
            missing.append(name)
            problems.setdefault(name, []).append("missing (required)")
    if missing:
        errors.append(
            "missing required input(s): "
            + ", ".join(missing)
            + " (pass --input <name>=<value>, an --inputs-file, or "
            + ", ".join(env_var_name(n) for n in missing)
            + ")"
        )
    if errors:
        good = {k: v for k, v in values.items() if k not in problems}
        raise InputError(errors, partial=good, problems=problems)

    # 5. schema validation
    schema = inputs_to_json_schema(specs)
    validator = jsonschema.Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(values), key=lambda e: list(e.path))
    if schema_errors:
        for err in schema_errors:
            name = str(err.path[0]) if err.path else "inputs"
            problems.setdefault(name, []).append(_schema_message(err, specs))
        good = {k: v for k, v in values.items() if k not in problems}
        raise InputError(
            [_schema_message(e, specs) for e in schema_errors], partial=good, problems=problems
        )
    return values


def _schema_message(err: jsonschema.ValidationError, specs: Mapping[str, InputSpec]) -> str:
    where = ".".join(str(p) for p in err.path) or "inputs"
    top = str(err.path[0]) if err.path else None
    spec = specs.get(top) if top is not None else None
    if spec is not None and spec.secret:
        # a secret value is never printed, not even when the schema rejects it
        shown = repr(err.instance)
        message = (
            err.message.replace(shown, SECRET_PLACEHOLDER)
            if shown in err.message
            else f"{SECRET_PLACEHOLDER} does not satisfy {err.validator}"
        )
        return f"input {where!r}: {message}"
    return f"input {where!r}: {err.message}"


# --------------------------------------------------------------------------------------------------
# secret inputs
# --------------------------------------------------------------------------------------------------


def secret_input_names(workflow: Workflow) -> tuple[str, ...]:
    """Names of the inputs declared ``secret: true`` (declaration order)."""
    return tuple(name for name, spec in workflow.inputs.items() if spec.secret)


def split_secret_inputs(
    values: Mapping[str, Any], secret_names: Iterable[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(public, secrets)`` — the resolved inputs split by name (absent secrets stay absent)."""
    names = set(secret_names)
    public = {k: v for k, v in values.items() if k not in names}
    secrets = {k: v for k, v in values.items() if k in names}
    return public, secrets


def redact_inputs(values: Mapping[str, Any], secret_names: Iterable[str]) -> dict[str, Any]:
    """A copy of ``values`` with every given secret replaced by :data:`SECRET_PLACEHOLDER`.

    An optional secret that was not given is simply absent (undefined in templates), exactly like
    any other absent optional input — so ``run.json`` also records *which* secrets were supplied.
    """
    names = set(secret_names)
    return {k: (SECRET_PLACEHOLDER if k in names else v) for k, v in values.items()}


def resolve_resume_secrets(
    workflow: Workflow,
    recorded_inputs: Mapping[str, Any],
    *,
    cli_pairs: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Re-obtain the secret inputs of a run being resumed (``resume``/``approve``/``reject``/
    ``run --resume``).

    Secrets are never persisted, so every secret that was given at launch (recorded as
    :data:`SECRET_PLACEHOLDER` in ``recorded_inputs``) must be supplied again: ``--input
    name=value`` (accepted on resume **only** for secret inputs — any other name is the usual
    "inputs are fixed per run" error), else the environment variable ``RAYSPEC_INPUT_<NAME>``.
    A secret that is still missing is an :class:`InputError` (exit 2) listing every missing name.
    An optional secret that was *not* given at launch is not required, but may be supplied now.
    """
    environ = os.environ if env is None else env
    specs = workflow.inputs
    secret_names = set(secret_input_names(workflow))
    errors: list[str] = []
    values: dict[str, Any] = {}
    pairs, bad_pairs = split_cli_pairs(cli_pairs)
    errors.extend(bad_pairs)
    for name, raw in pairs:
        if name not in secret_names:
            listed = ", ".join(sorted(secret_names)) or "none declared"
            errors.append(
                f"inputs are fixed per run; --input {name!r} is not accepted on resume "
                f"(only secret inputs may be supplied again: {listed})"
            )
            continue
        try:
            values[name] = coerce_input(raw, specs[name], name=name)
        except InputError as exc:
            errors.extend(exc.errors)
    if errors:
        raise InputError(errors)
    missing: list[str] = []
    for name in secret_input_names(workflow):
        if name in values:
            continue
        env_value = environ.get(env_var_name(name))
        if env_value is not None:
            values[name] = coerce_input(env_value, specs[name], name=name)
        elif recorded_inputs.get(name) == SECRET_PLACEHOLDER:
            missing.append(name)
    if missing:
        raise InputError(
            "missing secret input(s): "
            + ", ".join(missing)
            + " — pass "
            + " ".join(f"--input {n}=…" for n in missing)
            + " or set "
            + ", ".join(env_var_name(n) for n in missing)
        )
    return values


__all__ = [
    "ENV_PREFIX",
    "SECRET_PLACEHOLDER",
    "coerce_input",
    "env_var_name",
    "parse_cli_pairs",
    "read_inputs_file",
    "redact_inputs",
    "resolve_inputs",
    "resolve_resume_secrets",
    "secret_input_names",
    "split_cli_pairs",
    "split_secret_inputs",
]
