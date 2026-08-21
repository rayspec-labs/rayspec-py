# SPDX-License-Identifier: Apache-2.0
"""Step models. Exactly one *kind key* per step selects the model (callable discriminator)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BeforeValidator,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from rayspec.schema.agent import AgentDef, AgentOverride
from rayspec.schema.base import StrictModel, suggest
from rayspec.schema.common import Duration, Identifier, JoinPolicy, PositiveDuration
from rayspec.schema.errors import schema_error_from_validation

#: kind key → kind name
KIND_KEYS: dict[str, str] = {
    "prompt": "prompt",
    "prompt_file": "prompt",
    "shell": "shell",
    "python": "python",
    "loop": "loop",
    "each": "each",
    "approve": "approve",
    "include": "include",
    "stop": "stop",
}
KINDS: tuple[str, ...] = ("prompt", "shell", "python", "loop", "each", "approve", "include", "stop")


class RetryPolicy(StrictModel):
    """``attempts`` is the TOTAL number of attempts (1 = no retry). ``delay`` doubles each retry."""

    attempts: int = Field(ge=1, le=10)
    delay: Duration = 3.0
    on_error: Literal["transient", "all"] = "transient"


#: Engine default when a ``prompt:`` step has ``retry: None`` (shell/python default to no retry).
DEFAULT_PROMPT_RETRY = RetryPolicy(attempts=3, delay=3.0, on_error="transient")


class StepBase(StrictModel):
    """Fields common to every step kind."""

    kind: ClassVar[str] = "base"

    id: Identifier
    description: str = ""
    needs: list[Identifier] = Field(default_factory=list)
    when: str | None = None
    join: JoinPolicy = "all"
    timeout: PositiveDuration | None = None
    always_run: bool = False
    allow_failure: bool = False

    @classmethod
    def _what(cls) -> str:
        return f"{cls.kind} step"

    @classmethod
    def _unknown_key_message(cls, key: str, data: dict[str, Any]) -> str:
        valid_on = _FIELD_KINDS.get(key)
        if valid_on and cls.kind not in valid_on:
            kinds = ", ".join(sorted(valid_on))
            return f"field {key!r} is not valid on {cls.kind} steps (valid on: {kinds})"
        return super()._unknown_key_message(key, data)


def _coerce_env(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(v, bool):
                out[k] = "true" if v else "false"
            elif isinstance(v, int | float):
                out[k] = str(v)
            else:
                out[k] = v
        return out
    return value


EnvMap = Annotated[dict[str, str], BeforeValidator(_coerce_env)]


class LeafStep(StepBase):
    """Steps that actually execute something (prompt/shell/python).

    ``retry=None`` means "kind default": :data:`DEFAULT_PROMPT_RETRY` for prompt steps, no retry
    for shell/python.
    """

    retry: RetryPolicy | None = None
    env: EnvMap = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None


def _agent_ref_tag(value: Any) -> str | None:
    if isinstance(value, str):
        return "agent:name"
    if isinstance(value, dict):
        return "agent:override" if "extends" in value else "agent:inline"
    if isinstance(value, AgentOverride):
        return "agent:override"
    if isinstance(value, AgentDef):
        return "agent:inline"
    return None


AgentRef = Annotated[
    Annotated[str, Tag("agent:name")]
    | Annotated[AgentOverride, Tag("agent:override")]
    | Annotated[AgentDef, Tag("agent:inline")],
    Discriminator(_agent_ref_tag),
]


class PromptStep(LeafStep):
    kind: ClassVar[str] = "prompt"

    prompt: str | None = None
    prompt_file: str | None = None
    agent: AgentRef | None = None
    session: Identifier | None = None

    @model_validator(mode="before")
    @classmethod
    def _agent_ref_shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and "agent" in data:
            ref = data["agent"]
            if ref is not None and not isinstance(ref, str | dict | AgentDef):
                raise PydanticCustomError(
                    "agent_ref",
                    "agent must be a name, {{extends: <name>, ...}} or an inline agent mapping",
                )
        return data

    @model_validator(mode="after")
    def _prompt_xor_file(self) -> PromptStep:
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("set exactly one of 'prompt' or 'prompt_file'")
        return self


class ShellStep(LeafStep):
    kind: ClassVar[str] = "shell"

    shell: str
    interpreter: Literal["bash", "sh"] = "bash"
    cwd: str | None = None


class PythonStep(LeafStep):
    kind: ClassVar[str] = "python"

    python: str
    deps: list[str] = Field(default_factory=list)
    cwd: str | None = None


class LoopSpec(StrictModel):
    steps: list[Step]
    max_iterations: int = Field(ge=1)
    until: str | None = None
    on_exhausted: Literal["fail", "continue"] = "fail"

    @classmethod
    def _what(cls) -> str:
        return "loop"


class LoopStep(StepBase):
    kind: ClassVar[str] = "loop"

    loop: LoopSpec


class EachStep(StepBase):
    kind: ClassVar[str] = "each"

    each: str
    as_: Identifier = Field(default="item", alias="as")
    steps: list[Step]
    max_parallel: int | None = Field(default=None, ge=1)
    on_failure: Literal["fail", "continue"] = "fail"


class ApproveSpec(StrictModel):
    message: str
    on_reject: Literal["cancel", "continue", "fail"] = "cancel"

    @classmethod
    def _what(cls) -> str:
        return "approve"


def _approve_shorthand(value: Any) -> Any:
    if isinstance(value, str):
        return {"message": value}
    if isinstance(value, dict | ApproveSpec):
        return value
    raise PydanticCustomError(
        "approve_shape", "approve must be a message string or a mapping {message, on_reject}"
    )


class ApproveStep(StepBase):
    kind: ClassVar[str] = "approve"

    approve: Annotated[ApproveSpec, BeforeValidator(_approve_shorthand)]


class IncludeStep(StepBase):
    kind: ClassVar[str] = "include"

    include: str
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")


class StopSpec(StrictModel):
    status: Literal["succeeded", "failed", "cancelled"] = "cancelled"
    reason: str | None = None

    @classmethod
    def _what(cls) -> str:
        return "stop"


class StopStep(StepBase):
    kind: ClassVar[str] = "stop"

    stop: StopSpec


STEP_MODELS: dict[str, type[StepBase]] = {
    "prompt": PromptStep,
    "shell": ShellStep,
    "python": PythonStep,
    "loop": LoopStep,
    "each": EachStep,
    "approve": ApproveStep,
    "include": IncludeStep,
    "stop": StopStep,
}


def _all_field_names(model: type[StepBase]) -> set[str]:
    names: set[str] = set()
    for fname, finfo in model.model_fields.items():
        names.add(finfo.alias or fname)
    return names


#: field name → set of kinds on which it is valid (used for "not valid on X steps" hints)
_FIELD_KINDS: dict[str, set[str]] = {}
for _kind, _model in STEP_MODELS.items():
    for _fname in _all_field_names(_model):
        _FIELD_KINDS.setdefault(_fname, set()).add(_kind)


def _check_one_kind(value: Any) -> Any:
    if isinstance(value, StepBase):
        return value
    if not isinstance(value, dict):
        raise PydanticCustomError(
            "step_shape",
            "a step must be a mapping with an id and exactly one kind key, got {got}",
            {"got": type(value).__name__},
        )
    found = sorted(k for k in value if k in KIND_KEYS)
    kinds = {KIND_KEYS[k] for k in found}
    sid = repr(value.get("id", "?"))
    if not found:
        raise PydanticCustomError(
            "step_kind_missing",
            "step {id} has no kind key; add exactly one of: {valid}",
            {"id": sid, "valid": ", ".join(KIND_KEYS)},
        )
    if len(kinds) > 1:
        raise PydanticCustomError(
            "step_kind_ambiguous",
            "step {id} has multiple kind keys ({found}); exactly one is required",
            {"id": sid, "found": ", ".join(found)},
        )
    unknown = [k for k in value if isinstance(k, str) and k not in _FIELD_KINDS]
    if unknown:
        kind = next(iter(kinds))
        messages = []
        for key in unknown:
            hint = suggest(key, _all_field_names(STEP_MODELS[kind]))
            msg = f"unknown field {key!r} for {kind} step"
            if hint:
                msg += f"; did you mean {hint!r}?"
            messages.append(msg)
        raise PydanticCustomError("unknown_field", "{message}", {"message": "; ".join(messages)})
    return value


def _step_kind(value: Any) -> str | None:
    if isinstance(value, dict):
        kinds = {KIND_KEYS[k] for k in value if k in KIND_KEYS}
        return next(iter(kinds)) if len(kinds) == 1 else None
    kind = getattr(value, "kind", None)
    return kind if isinstance(kind, str) else None


StepModel = (
    PromptStep | ShellStep | PythonStep | LoopStep | EachStep | ApproveStep | IncludeStep | StopStep
)

_StepUnion = (
    Annotated[PromptStep, Tag("prompt")]
    | Annotated[ShellStep, Tag("shell")]
    | Annotated[PythonStep, Tag("python")]
    | Annotated[LoopStep, Tag("loop")]
    | Annotated[EachStep, Tag("each")]
    | Annotated[ApproveStep, Tag("approve")]
    | Annotated[IncludeStep, Tag("include")]
    | Annotated[StopStep, Tag("stop")]
)
Step = Annotated[
    Annotated[_StepUnion, Discriminator(_step_kind)],
    BeforeValidator(_check_one_kind),
]

LoopSpec.model_rebuild()
LoopStep.model_rebuild()
EachStep.model_rebuild()

_STEP_ADAPTER: TypeAdapter[StepModel] = TypeAdapter(Step)


def parse_step(data: Any, *, source: str | None = None) -> StepModel:
    """Validate one step mapping into its model, raising :class:`SchemaError` on problems."""
    try:
        return _STEP_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise schema_error_from_validation(exc, data, source=source) from None


def iter_steps(steps: Sequence[StepModel]) -> list[tuple[str, StepModel]]:
    """Yield ``(path, step)`` for every step including nested bodies (loop/each)."""
    out: list[tuple[str, StepModel]] = []

    def walk(items: Sequence[StepModel], prefix: str) -> None:
        for step in items:
            path = f"{prefix}{step.id}"
            out.append((path, step))
            if isinstance(step, LoopStep):
                walk(step.loop.steps, f"{path}/")
            elif isinstance(step, EachStep):
                walk(step.steps, f"{path}/")

    walk(steps, "")
    return out


__all__ = [
    "DEFAULT_PROMPT_RETRY",
    "KINDS",
    "KIND_KEYS",
    "STEP_MODELS",
    "AgentRef",
    "ApproveSpec",
    "ApproveStep",
    "EachStep",
    "IncludeStep",
    "LeafStep",
    "LoopSpec",
    "LoopStep",
    "PromptStep",
    "PythonStep",
    "RetryPolicy",
    "ShellStep",
    "Step",
    "StepBase",
    "StepModel",
    "StopSpec",
    "StopStep",
    "iter_steps",
    "parse_step",
]
