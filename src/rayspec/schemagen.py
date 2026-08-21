# SPDX-License-Identifier: Apache-2.0
"""The published JSON Schemas (2020-12), generated from the Pydantic models.

Module boundary: the single source of the four schemas rayspec publishes —

* ``workflow`` — a ``.rayspec/workflows/<name>.yaml`` document (editor completion),
* ``run`` — a ``run.json`` record,
* ``events`` — one line of ``events.jsonl``,
* ``stream`` — one line of ``steps/<path>/stream.jsonl``.

``scripts/gen_schemas.py`` writes them to ``schemas/`` and ``rayspec schema`` prints them; both
are thin front ends over this module, so a checked-in file can never drift from the models.

Every ``$id`` is derived from :data:`SCHEMA_BASE_URL` — one constant, so a repository or project
rename stays a one-line edit.

The workflow schema is an **editor aid, not the validator**: it is generated from the models and
then relaxed where a ``BeforeValidator`` accepts spellings that the model's own type does not
describe (``timeout: 30m``, ``budget_usd: "$1.50"``, ``approve: <message>``,
``env: {PORT: 8080}``). ``rayspec
validate`` remains authoritative — it also checks the graph, references, includes, agents and
provider capabilities, none of which a JSON Schema can express.
"""

from __future__ import annotations

import json
from typing import Any

from rayspec.events.model import RunEvent, StreamRecord
from rayspec.schema import SCHEMA_VERSION, Workflow
from rayspec.store.model import RunRecord

#: Where the published schemas live. Every ``$id`` is this + ``<kind>.schema.json`` (the
#: project name was settled late, so a rename must stay a one-line edit).
SCHEMA_BASE_URL = "https://raw.githubusercontent.com/rayspec-labs/rayspec-py/main/schemas/"

#: JSON Schema dialect of every generated document.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: The published kinds, in the order ``scripts/gen_schemas.py`` writes them.
SCHEMA_KINDS: tuple[str, ...] = ("workflow", "run", "events", "stream")

#: One-line description of what each schema validates (the ``description`` of the document and
#: the ``rayspec schema`` listing).
SCHEMA_SUBJECTS: dict[str, str] = {
    "workflow": "A rayspec workflow document (.rayspec/workflows/<name>.yaml).",
    "run": "One rayspec run record (<run dir>/run.json).",
    "events": "One line of a rayspec run's event log (<run dir>/events.jsonl).",
    "stream": "One line of a step's agent/shell transcript (steps/<path>/stream.jsonl).",
}

#: Duration fields accept seconds or an ``h/m/s/ms`` string (``schema.common.parse_duration``).
_DURATION_PATTERN = r"^(?=.*\d)\s*(?:\d+(?:\.\d+)?h)?\s*(?:\d+(?:\.\d+)?m(?!s))?\s*(?:\d+(?:\.\d+)?s)?\s*(?:\d+ms)?\s*$"  # noqa: E501
#: ``defaults.budget_usd`` (``schema.workflow.parse_money``): ``1.5``, ``"1.50"``, ``"$1.50"``.
_MONEY_PATTERN = r"^\s*\$?\s*\d+(?:\.\d+)?\s*(?:[Uu][Ss][Dd])?\s*$"
#: ``defaults.max_tokens`` (``schema.workflow.parse_token_count``): ``1500``, ``"500k"``, ``1.5M``.
_TOKENS_PATTERN = r"^\s*\d+(?:_\d+)*(?:\.\d+)?\s*[kKmM]?\s*$"
#: One ``artifacts:`` entry (``schema.steps.validate_artifact_path``): a relative file path, no
#: ``~``, no ``..`` segment, no template syntax, no control characters, not a directory.
_ARTIFACT_PATTERN = (
    r"^(?!~)(?!/)(?!.*\{[{%])(?!.*(?:^|/)\.\.(?:/|$))[^\x00-\x1f\x7f]*[^/\x00-\x1f\x7f]$"
)


def _duration(*, positive: bool, nullable: bool) -> dict[str, Any]:
    number: dict[str, Any] = {"type": "number"}
    number["exclusiveMinimum" if positive else "minimum"] = 0
    options: list[dict[str, Any]] = [number, {"type": "string", "pattern": _DURATION_PATTERN}]
    if nullable:
        options.append({"type": "null"})
    return {"anyOf": options, "description": "Seconds (90) or a duration string ('90s', '1h30m')."}


def _money() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "number", "exclusiveMinimum": 0},
            {"type": "string", "pattern": _MONEY_PATTERN},
            {"type": "null"},
        ],
        "description": "A USD amount (1.5) or a string ('$1.50', '12 USD').",
    }


def _tokens() -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "integer", "exclusiveMinimum": 0},
            {"type": "string", "pattern": _TOKENS_PATTERN},
            {"type": "null"},
        ],
        "description": "A token count (500000) or a string ('500k', '1.5M').",
    }


def _artifacts() -> dict[str, Any]:
    """``StepBase.artifacts``: the item type is a plain ``str`` in the model, so the published
    schema would accept ``/etc/passwd`` and only the loader would object."""
    return {
        "type": "array",
        "items": {"type": "string", "pattern": _ARTIFACT_PATTERN},
        "description": (
            "Files the step promises to write, relative to its working directory "
            "(e.g. 'build/report.md'). Not templated: put what varies in the step's cwd:."
        ),
    }


def _env_map() -> dict[str, Any]:
    """``EnvMap``: ``_coerce_env`` str-coerces bools and numbers, so the schema must accept them."""
    return {
        "type": "object",
        "additionalProperties": {
            "anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]
        },
        "description": "Environment variables; numbers and booleans are coerced to text.",
    }


def _workflow_patches() -> dict[str, dict[str, Any]]:
    """``<$def>.<property>`` → replacement subschema for the relaxations named in the module
    docstring. Every path is asserted to exist, so a model rename fails the generator loudly
    instead of silently dropping a relaxation."""
    patches: dict[str, dict[str, Any]] = {
        "Workflow.rayspec": {
            "const": SCHEMA_VERSION,
            "description": f"Schema version — must be {SCHEMA_VERSION}.",
        },
        "Defaults.timeout": _duration(positive=False, nullable=True),
        "Defaults.timeout_total": _duration(positive=True, nullable=True),
        "Defaults.budget_usd": _money(),
        "Defaults.max_tokens": _tokens(),
        "RetryPolicy.delay": _duration(positive=False, nullable=False),
        "ApproveStep.approve": {
            "anyOf": [
                {"type": "string", "description": "Shorthand for {message: <text>}."},
                {"$ref": "#/$defs/ApproveSpec"},
            ]
        },
    }
    for step in ("Prompt", "Shell", "Python", "Loop", "Each", "Approve", "Include", "Stop"):
        patches[f"{step}Step.timeout"] = _duration(positive=True, nullable=True)
    for step in ("Prompt", "Shell", "Python"):
        patches[f"{step}Step.env"] = _env_map()
    for step in ("Prompt", "Shell", "Python", "Loop", "Each", "Approve", "Include", "Stop"):
        patches[f"{step}Step.artifacts"] = _artifacts()
    return patches


def _apply_patches(schema: dict[str, Any], patches: dict[str, dict[str, Any]]) -> None:
    defs = schema.get("$defs", {})
    for path, replacement in patches.items():
        name, _, prop = path.partition(".")
        target = schema if name == "Workflow" else defs.get(name)
        if not isinstance(target, dict) or prop not in target.get("properties", {}):
            raise RuntimeError(
                f"cannot patch {path!r}: no such property in the generated schema — the model "
                "changed; update rayspec.schemagen"
            )
        keep = {
            k: v
            for k, v in target["properties"][prop].items()
            if k in {"default", "title", "description"}
        }
        target["properties"][prop] = {**keep, **replacement}


def schema_id(kind: str) -> str:
    """The ``$id`` of one published schema."""
    if kind not in SCHEMA_KINDS:
        raise ValueError(f"unknown schema kind {kind!r}; known: {', '.join(SCHEMA_KINDS)}")
    return f"{SCHEMA_BASE_URL}{kind}.schema.json"


def build_schema(kind: str) -> dict[str, Any]:
    """The JSON Schema of one published kind, generated from the live Pydantic models."""
    schema_id(kind)  # validates the kind
    if kind == "workflow":
        schema = Workflow.model_json_schema(mode="validation")
        _apply_patches(schema, _workflow_patches())
    elif kind == "run":
        schema = RunRecord.model_json_schema(mode="serialization", by_alias=True)
    elif kind == "events":
        schema = RunEvent.model_json_schema(mode="serialization")
    else:
        schema = StreamRecord.model_json_schema(mode="serialization")
    head = {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": schema_id(kind),
        "title": schema.pop("title", kind),
        "description": SCHEMA_SUBJECTS[kind],
    }
    schema.pop("description", None)
    return {**head, **schema}


def build_all() -> dict[str, dict[str, Any]]:
    """``{kind: schema}`` for every published kind."""
    return {kind: build_schema(kind) for kind in SCHEMA_KINDS}


def schema_text(kind: str) -> str:
    """The exact text of the checked-in ``schemas/<kind>.schema.json`` (2-space, LF, final NL)."""
    return json.dumps(build_schema(kind), indent=2, ensure_ascii=False) + "\n"


def schema_filename(kind: str) -> str:
    """File name of one published schema."""
    schema_id(kind)
    return f"{kind}.schema.json"


#: Comment prefix of the ``yaml-language-server`` modeline (how an existing one is recognised).
MODELINE_PREFIX = "# yaml-language-server: $schema="


def modeline(kind: str = "workflow", *, url: str | None = None) -> str:
    """The ``yaml-language-server`` modeline that gives an editor completion on a document.

    ``url`` overrides the published location — point it at a local copy (``rayspec schema
    workflow --out .rayspec/``) when the published URL is unreachable.
    """
    return f"{MODELINE_PREFIX}{url or schema_id(kind)}"


__all__ = [
    "JSON_SCHEMA_DIALECT",
    "MODELINE_PREFIX",
    "SCHEMA_BASE_URL",
    "SCHEMA_KINDS",
    "SCHEMA_SUBJECTS",
    "build_all",
    "build_schema",
    "modeline",
    "schema_filename",
    "schema_id",
    "schema_text",
]
