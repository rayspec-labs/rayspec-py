# SPDX-License-Identifier: Apache-2.0
"""``load_workflow``: YAML file → :class:`ResolvedWorkflow`.

Boundary: this module reads files (workflow, includes, agent files, prompt/instructions files),
expands ``include:`` steps at load time, resolves every prompt step's agent (name chain, shallow
merge, tiers/aliases) and computes the source hash. It does **not** validate graph semantics or
provider capabilities — that is :mod:`rayspec.loader.validate`.
"""

from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from rayspec.config.model import TIER_NAMES, Config
from rayspec.config.paths import rayspec_home
from rayspec.config.settings import load_config
from rayspec.errors import LoaderError
from rayspec.loader.discovery import (
    YAML_SUFFIXES,
    WorkflowRef,
    discover_agents,
    discover_workflows,
    project_rayspec_dir,
)
from rayspec.loader.yaml import LineMap, load_yaml_with_lines
from rayspec.schema import (
    AgentDef,
    AgentOverride,
    Defaults,
    EachStep,
    IncludeStep,
    InputSpec,
    LoopStep,
    McpServerDef,
    PromptStep,
    StepModel,
    ToolsSpec,
    Workflow,
    parse_agent_def,
    parse_workflow,
)
from rayspec.schema.base import suggest
from rayspec.schema.errors import SchemaError, expand_schema_errors

#: Maximum nesting of ``include:`` steps.
MAX_INCLUDE_DEPTH = 8
#: Provider ids known without consulting the registry.
BUILTIN_PROVIDERS: frozenset[str] = frozenset({"claude", "codex", "stub"})
#: Model used when an agent names none.
DEFAULT_MODEL_TIER = "medium"

YamlKeys = tuple[str | int, ...]


# --------------------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadedFile:
    """A file whose text contributes to the workflow (prompt file, instructions file)."""

    path: Path
    text: str


@dataclass(frozen=True, slots=True)
class StepLocation:
    """Where a step lives: the YAML file and the key path of its mapping inside it."""

    file: Path
    label: str
    keys: YamlKeys
    lines: LineMap = field(repr=False, compare=False, hash=False)

    def line_of(self, *fields: str | int) -> int | None:
        """Line of ``<step>.<fields...>`` (or of the step mapping itself)."""
        return self.lines.get((*self.keys, *fields))

    def location(self, *fields: str | int) -> str | None:
        """``<label>:<line>`` for ``<step>.<fields...>`` when the line is known."""
        line = self.line_of(*fields)
        return None if line is None else f"{self.label}:{line}"


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    """An agent after name lookup, merge and tier/alias resolution — what a provider receives."""

    key: str
    name: str
    provider: str
    model: str | None
    effort: str | None
    access: str
    instructions: str | None
    instructions_mode: str
    max_turns: int | None
    budget_usd: float | None
    tools: ToolsSpec
    thinking: bool | None
    #: ``warn`` (record them) or ``fail`` (a refused tool call fails the step)
    on_denial: str
    mcp: dict[str, McpServerDef]
    provider_options: dict[str, dict[str, Any]]
    source: str
    yaml_path: str
    locations: Mapping[str, str] = field(default_factory=dict)
    raw_model: str | None = None

    def location(self, field_name: str) -> str | None:
        """``<file>:<line>`` of the YAML that set ``field_name`` (when known)."""
        return self.locations.get(field_name)

    def field_path(self, field_name: str) -> str:
        """YAML path of a field, e.g. ``agents.implementer.max_turns``."""
        return f"{self.yaml_path}.{field_name}"


@dataclass(slots=True)
class IncludedBody:
    """The expanded body of an ``include:`` step (lexically scoped; inner ids are not visible)."""

    workflow_name: str
    path: Path
    inputs_binding: dict[str, Any]
    inputs: dict[str, InputSpec]
    steps: list[StepModel]
    outputs: dict[str, Any]
    defaults: Defaults
    description: str = ""


GraphKind = Literal["root", "loop", "each", "include"]


@dataclass(frozen=True, slots=True)
class GraphView:
    """One sibling list (the root steps or a composite's body) with its path prefix."""

    kind: GraphKind
    prefix: str
    steps: tuple[StepModel, ...]
    parent_path: str | None = None
    parent: StepModel | None = None

    def path_of(self, step: StepModel) -> str:
        return f"{self.prefix}{step.id}"


@dataclass(slots=True)
class ResolvedWorkflow:
    """A workflow with includes expanded, agents resolved and all contributing files hashed."""

    workflow: Workflow
    path: Path
    label: str
    base_dir: Path
    hash: str
    agents: dict[str, ResolvedAgent]
    step_agents: dict[str, str]
    includes: dict[str, IncludedBody]
    prompt_files: dict[str, LoadedFile]
    source_files: list[Path]
    warnings: list[str]
    step_locations: dict[str, StepLocation]

    # -- navigation ---------------------------------------------------------------------------

    def graphs(self) -> list[GraphView]:
        """Every sibling list, root first, then bodies in document order (includes expanded)."""
        out: list[GraphView] = []

        def walk(steps: list[StepModel], prefix: str, kind: GraphKind, parent: StepModel | None):
            parent_path = prefix[:-1] if prefix else None
            out.append(GraphView(kind, prefix, tuple(steps), parent_path, parent))
            for step in steps:
                path = f"{prefix}{step.id}"
                if isinstance(step, LoopStep):
                    walk(step.loop.steps, f"{path}/", "loop", step)
                elif isinstance(step, EachStep):
                    walk(step.steps, f"{path}/", "each", step)
                elif isinstance(step, IncludeStep) and path in self.includes:
                    walk(self.includes[path].steps, f"{path}/", "include", step)

        walk(list(self.workflow.steps), "", "root", None)
        return out

    def all_steps(self) -> list[tuple[str, StepModel]]:
        """``(path, step)`` for every step including bodies and included bodies."""
        return [(g.path_of(s), s) for g in self.graphs() for s in g.steps]

    def step(self, path: str) -> StepModel:
        """The step model at ``path``; raises ``KeyError`` when unknown."""
        for p, s in self.all_steps():
            if p == path:
                return s
        raise KeyError(path)

    def agent_for(self, step_path: str) -> ResolvedAgent:
        """The resolved agent of the prompt step at ``step_path``."""
        return self.agents[self.step_agents[step_path]]

    def prompt_text(self, step_path: str) -> str | None:
        """Prompt text of a prompt step (inline ``prompt:`` or the ``prompt_file:`` contents)."""
        step = self.step(step_path)
        if not isinstance(step, PromptStep):
            return None
        if step.prompt is not None:
            return step.prompt
        loaded = self.prompt_files.get(step_path)
        return None if loaded is None else loaded.text

    def location_of(self, step_path: str, *fields: str | int) -> str | None:
        """``<file>:<line>`` of ``steps.<path>.<fields...>`` when known."""
        loc = self.step_locations.get(step_path)
        return None if loc is None else loc.location(*fields)


# --------------------------------------------------------------------------------------------------
# Internal document model
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Doc:
    """One parsed workflow document (main or included)."""

    workflow: Workflow
    path: Path
    label: str
    base_dir: Path
    lines: LineMap
    include_path: str  # "" for the main document, "<include step path>/" otherwise


@dataclass(slots=True)
class _AgentSource:
    """Where a (partial) agent definition came from."""

    definition: AgentDef
    description: str
    file: Path
    label: str
    keys: YamlKeys
    lines: LineMap
    base_dir: Path
    yaml_path: str
    #: identity of the definition's origin (``<include path>agents.<name>``, ``file:<label>``,
    #: ``provider:<id>``, ``inline:<step path>``, ``override:<step path>``) — distinct origins
    #: never share a key in ``ResolvedWorkflow.agents``.
    key: str

    def location(self, field_name: str) -> str | None:
        line = self.lines.get((*self.keys, field_name))
        return None if line is None else f"{self.label}:{line}"


def base_dir_for(path: Path) -> Path:
    """The ``.rayspec``-like directory a file's relative references resolve against."""
    if path.parent.name in {"workflows", "agents"}:
        return path.parent.parent
    return path.parent


def _looks_like_path(text: str) -> bool:
    return text.endswith(YAML_SUFFIXES) or os.sep in text or "/" in text or text.startswith(".")


def _read_text(path: Path, *, what: str, location: str | None = None) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LoaderError(f"{what} not found: {path}", location=location) from None
    except OSError as exc:
        raise LoaderError(f"cannot read {what} {path}: {exc}", location=location) from None


class _Loader:
    def __init__(
        self,
        *,
        project_root: Path,
        home: Path,
        config: Config,
        known_providers: Collection[str],
    ):
        self.project_root = project_root
        self.home = home
        self.config = config
        self.known_providers = set(known_providers)
        self.agents: dict[str, ResolvedAgent] = {}
        self.step_agents: dict[str, str] = {}
        self.includes: dict[str, IncludedBody] = {}
        self.prompt_files: dict[str, LoadedFile] = {}
        self.source_files: dict[Path, bytes] = {}
        self.warnings: list[str] = []
        self.step_locations: dict[str, StepLocation] = {}
        self._agent_file_cache: dict[Path, _AgentSource] = {}
        self._workflow_refs: list[WorkflowRef] | None = None
        self._agent_refs: dict[str, dict[str, Path]] | None = None

    # -- labels & files -----------------------------------------------------------------------

    def label(self, path: Path) -> str:
        try:
            return path.relative_to(self.project_root).as_posix()
        except ValueError:
            pass
        try:
            return "~/.rayspec/" + path.relative_to(self.home).as_posix()
        except ValueError:
            return str(path)

    def _remember(self, path: Path, data: bytes | str) -> None:
        self.source_files[path] = data.encode("utf-8") if isinstance(data, str) else data

    def read_file(self, path: Path, *, what: str, location: str | None = None) -> str:
        text = _read_text(path, what=what, location=location)
        self._remember(path, text)
        return text

    # -- documents ----------------------------------------------------------------------------

    def load_doc(self, path: Path, include_path: str) -> _Doc:
        label = self.label(path)
        text = self.read_file(path, what="workflow")
        data, lines = load_yaml_with_lines(text, source=label)
        if not isinstance(data, dict):
            raise LoaderError(f"{label}: workflow must be a mapping, got {type(data).__name__}")
        try:
            workflow = parse_workflow(data, source=label)
        except SchemaError as exc:  # every problem of the document, each with its line
            raise expand_schema_errors(exc, data, parse_workflow, lines=lines) from None
        return _Doc(workflow, path, label, base_dir_for(path), lines, include_path)

    def workflow_refs(self) -> list[WorkflowRef]:
        if self._workflow_refs is None:
            self._workflow_refs = discover_workflows(self.project_root, home=self.home)
        return self._workflow_refs

    def resolve_workflow_path(
        self, target: str | Path | WorkflowRef, *, search_dirs: Sequence[Path] = ()
    ) -> Path:
        """Resolve a name (discovery), a path (absolute or relative to ``search_dirs``) or a ref."""
        if isinstance(target, WorkflowRef):
            return target.path
        if isinstance(target, Path):
            return target.expanduser().resolve()
        if _looks_like_path(target):
            candidate = Path(target).expanduser()
            if not candidate.is_absolute():
                for base in search_dirs:
                    if (base / candidate).is_file():
                        return (base / candidate).resolve()
            if candidate.is_file():
                return candidate.resolve()
            raise LoaderError(f"workflow file not found: {target}")
        refs = self.workflow_refs()
        for ref in refs:
            if ref.name == target:
                return ref.path
        hint = suggest(target, [r.name for r in refs])
        message = f"unknown workflow {target!r}"
        if hint:
            message += f"; did you mean {hint!r}?"
        raise LoaderError(message, hint="run 'rayspec workflows' to list discovered workflows")

    # -- steps --------------------------------------------------------------------------------

    def process_steps(
        self,
        steps: list[StepModel],
        doc: _Doc,
        *,
        prefix: str,
        keys: YamlKeys,
        chain: tuple[Path, ...],
    ) -> None:
        for index, step in enumerate(steps):
            path = f"{prefix}{step.id}"
            step_keys: YamlKeys = (*keys, index)
            self.step_locations[path] = StepLocation(doc.path, doc.label, step_keys, doc.lines)
            if isinstance(step, PromptStep):
                self._process_prompt(step, path, step_keys, doc)
            elif isinstance(step, LoopStep):
                self.process_steps(
                    step.loop.steps,
                    doc,
                    prefix=f"{path}/",
                    keys=(*step_keys, "loop", "steps"),
                    chain=chain,
                )
            elif isinstance(step, EachStep):
                self.process_steps(
                    step.steps, doc, prefix=f"{path}/", keys=(*step_keys, "steps"), chain=chain
                )
            elif isinstance(step, IncludeStep):
                self._process_include(step, path, step_keys, doc, chain)

    def _process_prompt(self, step: PromptStep, path: str, keys: YamlKeys, doc: _Doc) -> None:
        if step.prompt_file is not None:
            file = (doc.base_dir / step.prompt_file).resolve()
            location = self.step_locations[path].location("prompt_file")
            text = self.read_file(file, what="prompt_file", location=location)
            self.prompt_files[path] = LoadedFile(file, text)
        agent = self.resolve_step_agent(step, path, keys, doc)
        self.agents.setdefault(agent.key, agent)
        self.step_agents[path] = agent.key

    def _process_include(
        self, step: IncludeStep, path: str, keys: YamlKeys, doc: _Doc, chain: tuple[Path, ...]
    ) -> None:
        location = self.step_locations[path].location("include")
        try:
            target = self.resolve_workflow_path(
                step.include, search_dirs=(doc.path.parent, doc.base_dir)
            )
        except LoaderError as exc:
            raise LoaderError(
                f"{location or doc.label}: step {step.id!r}: {exc}",
                hint=exc.hint,
                location=location,
            ) from None
        if target in chain:
            cycle = " -> ".join(self.label(p) for p in (*chain, target))
            raise LoaderError(f"include cycle: {cycle}", location=location)
        if len(chain) > MAX_INCLUDE_DEPTH:
            raise LoaderError(
                f"include depth exceeds {MAX_INCLUDE_DEPTH} at step {step.id!r} "
                f"(chain: {' -> '.join(self.label(p) for p in chain)})",
                location=location,
            )
        sub = self.load_doc(target, include_path=f"{path}/")
        body = IncludedBody(
            workflow_name=sub.workflow.name,
            path=target,
            inputs_binding=dict(step.with_),
            inputs=dict(sub.workflow.inputs),
            steps=list(sub.workflow.steps),
            outputs=dict(sub.workflow.outputs),
            defaults=sub.workflow.defaults,
            description=sub.workflow.description,
        )
        self.includes[path] = body
        self.process_steps(
            body.steps, sub, prefix=f"{path}/", keys=("steps",), chain=(*chain, target)
        )

    # -- agents -------------------------------------------------------------------------------

    def agent_file_index(self) -> dict[str, dict[str, Path]]:
        """``{scope: {name: path}}`` for ``.rayspec/agents`` and ``<home>/agents``."""
        if self._agent_refs is None:
            index: dict[str, dict[str, Path]] = {"project": {}, "user": {}}
            for ref in discover_agents(self.project_root, home=self.home):
                index[ref.scope][ref.name] = ref.path
            # discover_agents already applies project-over-user precedence; also index shadowed
            # user files so the chain is explicit.
            user_dir = self.home / "agents"
            if user_dir.is_dir():
                for p in user_dir.iterdir():
                    if p.is_file() and p.suffix in YAML_SUFFIXES:
                        index["user"].setdefault(p.stem, p)
            self._agent_refs = index
        return self._agent_refs

    def _agent_from_file(self, path: Path) -> _AgentSource:
        cached = self._agent_file_cache.get(path)
        if cached is not None:
            return cached
        label = self.label(path)
        text = self.read_file(path, what="agent file")
        data, lines = load_yaml_with_lines(text, source=label)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise LoaderError(f"{label}: agent file must be a mapping, got {type(data).__name__}")
        try:
            definition = parse_agent_def(data, source=label)
        except SchemaError as exc:  # every problem of the document, each with its line
            raise expand_schema_errors(exc, data, parse_agent_def, lines=lines) from None
        src = _AgentSource(
            definition=definition,
            description=label,
            file=path,
            label=label,
            keys=(),
            lines=lines,
            base_dir=base_dir_for(path),
            yaml_path=f"agents.{path.stem}",
            key=f"file:{label}",
        )
        self._agent_file_cache[path] = src
        return src

    def lookup_agent(self, name: str, doc: _Doc, *, where: str) -> _AgentSource:
        """Named lookup: workflow ``agents:`` > project agent file > user agent file > provider."""
        if name in doc.workflow.agents:
            return _AgentSource(
                definition=doc.workflow.agents[name],
                description=f"agents.{name} ({doc.label})",
                file=doc.path,
                label=doc.label,
                keys=("agents", name),
                lines=doc.lines,
                base_dir=doc.base_dir,
                yaml_path=f"agents.{name}",
                key=f"{doc.include_path}agents.{name}",
            )
        index = self.agent_file_index()
        for scope in ("project", "user"):
            path = index[scope].get(name)
            if path is not None:
                return self._agent_from_file(path)
        if name in self.known_providers:
            return _AgentSource(
                definition=AgentDef(provider=name),
                description=f"provider {name!r} default agent",
                file=doc.path,
                label=doc.label,
                keys=(),
                lines={},
                base_dir=doc.base_dir,
                yaml_path=f"agents.{name}",
                key=f"provider:{name}",
            )
        candidates = (
            set(doc.workflow.agents)
            | set(index["project"])
            | set(index["user"])
            | self.known_providers
        )
        hint = suggest(name, candidates)
        message = f"{where}: unknown agent {name!r}"
        if hint:
            message += f"; did you mean {hint!r}?"
        raise LoaderError(
            message,
            hint=(
                "define it under agents:, as .rayspec/agents/<name>.yaml, or use a provider id "
                f"({', '.join(sorted(self.known_providers))})"
            ),
        )

    def resolve_step_agent(
        self, step: PromptStep, path: str, keys: YamlKeys, doc: _Doc
    ) -> ResolvedAgent:
        where = f"{doc.label}: step {step.id!r}"
        ref = step.agent
        if ref is None:
            if doc.workflow.defaults.agent is not None:
                src = self.lookup_agent(
                    doc.workflow.defaults.agent, doc, where=f"{doc.label}: defaults.agent"
                )
                return self._finalize(src, doc, key=src.key, name=src_name(src))
            provider = self.config.default_provider
            src = _AgentSource(
                definition=AgentDef(provider=provider),
                description=f"config default_provider {provider!r}",
                file=doc.path,
                label=doc.label,
                keys=(),
                lines={},
                base_dir=doc.base_dir,
                yaml_path=f"agents.{provider}",
                key=f"provider:{provider}",
            )
            return self._finalize(src, doc, key=src.key, name=provider)
        if isinstance(ref, str):
            src = self.lookup_agent(ref, doc, where=where)
            return self._finalize(src, doc, key=src.key, name=src_name(src))
        step_src = _AgentSource(
            definition=ref,
            description=f"steps.{step.id}.agent ({doc.label})",
            file=doc.path,
            label=doc.label,
            keys=(*keys, "agent"),
            lines=doc.lines,
            base_dir=doc.base_dir,
            yaml_path=f"steps.{step.id}.agent",
            key=f"inline:{path}",
        )
        if isinstance(ref, AgentOverride):
            base = self.lookup_agent(ref.extends, doc, where=f"{where}: agent.extends")
            merged = self._merge(base, step_src, key=f"override:{path}")
            return self._finalize(
                merged,
                doc,
                key=merged.key,
                name=f"{step.id} (extends {ref.extends})",
                locations_from=(base, step_src),
            )
        return self._finalize(step_src, doc, key=step_src.key, name=f"{step.id} (inline)")

    @staticmethod
    def _merge(base: _AgentSource, override: _AgentSource, *, key: str) -> _AgentSource:
        """Shallow merge: only the override's set fields apply (tools/options replace wholesale)."""
        updates: dict[str, Any] = {
            name: getattr(override.definition, name)
            for name in override.definition.model_fields_set
            if name != "extends"
        }
        if "instructions" in updates:
            updates.setdefault("instructions_file", None)
        if "instructions_file" in updates:
            updates.setdefault("instructions", None)
        base_data = base.definition.model_dump()
        base_data.update(updates)
        merged = AgentDef.model_validate(base_data)
        return _AgentSource(
            definition=merged,
            description=f"{override.description} extends {base.description}",
            file=override.file,
            label=override.label,
            keys=override.keys,
            lines=override.lines,
            base_dir=override.base_dir,
            yaml_path=override.yaml_path,
            key=key,
        )

    def _finalize(
        self,
        src: _AgentSource,
        doc: _Doc,
        *,
        key: str,
        name: str,
        locations_from: tuple[_AgentSource, ...] | None = None,
    ) -> ResolvedAgent:
        definition = src.definition
        sources = locations_from or (src,)
        locations: dict[str, str] = {}
        for s in sources:
            for field_name in s.definition.model_fields_set:
                loc = s.location(field_name)
                if loc is not None:
                    locations[field_name] = loc
        provider = definition.provider or self.config.default_provider
        raw_model = definition.model
        model: str | None = raw_model if raw_model is not None else DEFAULT_MODEL_TIER
        effort = definition.effort
        if model.startswith("@"):
            alias = self.config.aliases.get(model)
            if alias is None:
                known = ", ".join(sorted(self.config.aliases)) or "none configured"
                raise LoaderError(
                    f"{src.description}: unknown model alias {model!r} (aliases: {known})",
                    location=locations.get("model"),
                )
            if alias.provider and definition.provider and alias.provider != definition.provider:
                raise LoaderError(
                    f"{src.description}: model alias {model!r} pins provider "
                    f"{alias.provider!r} but the agent sets provider {definition.provider!r}",
                    hint="drop provider: (the alias decides) or use a literal model id",
                    location=locations.get("model") or locations.get("provider"),
                )
            if alias.provider:
                provider = alias.provider
            model = alias.model
            effort = effort or alias.effort
        elif model in TIER_NAMES:
            tier = self.config.resolve_tier(provider, model)
            if tier is None:
                self.warnings.append(
                    f"{src.description}: no model tier {model!r} configured for provider "
                    f"{provider!r}; the provider default model will be used "
                    "(set tiers.<provider>.<tier> in config.yaml)"
                )
                model = None
            else:
                model = tier.model
                effort = effort or tier.effort
        instructions = definition.instructions
        if definition.instructions_file is not None:
            inst_src = next(
                (
                    s
                    for s in reversed(sources)
                    if "instructions_file" in s.definition.model_fields_set
                ),
                src,
            )
            file = (inst_src.base_dir / definition.instructions_file).resolve()
            instructions = self.read_file(
                file, what="instructions_file", location=locations.get("instructions_file")
            )
        return ResolvedAgent(
            key=key,
            name=name,
            provider=provider,
            model=model,
            effort=effort,
            access=definition.access,
            instructions=instructions,
            instructions_mode=definition.instructions_mode,
            max_turns=definition.max_turns,
            budget_usd=definition.budget_usd,
            tools=definition.tools,
            thinking=definition.thinking,
            on_denial=definition.on_denial,
            mcp=dict(definition.mcp),
            provider_options=dict(definition.provider_options),
            source=src.description,
            yaml_path=src.yaml_path,
            locations=locations,
            raw_model=raw_model,
        )

    # -- hash ---------------------------------------------------------------------------------

    def compute_hash(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.source_files, key=self.label):
            digest.update(self.label(path).encode("utf-8"))
            digest.update(b"\0")
            digest.update(self.source_files[path])
            digest.update(b"\0")
        return digest.hexdigest()


def src_name(src: _AgentSource) -> str:
    """Display name of a named agent source (``agents.<name>`` → ``<name>``)."""
    return src.yaml_path.split(".", 1)[1]


def default_known_providers(config: Config) -> set[str]:
    """Builtin provider ids + those mentioned in config + registry ids when importable."""
    known = set(BUILTIN_PROVIDERS) | set(config.tiers) | set(config.providers)
    # the registry is a parallel scope: import lazily; only its *absence* is tolerated
    registry = import_optional("rayspec.providers.registry")
    if registry is not None:
        known.update(reg.id for reg in registry.list_registrations())
    return known


def import_optional(name: str) -> ModuleType | None:
    """Import ``name``; ``None`` only when that module itself is missing (bugs inside propagate)."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name or (exc.name and name.startswith(exc.name + ".")):
            return None
        raise


def load_workflow(
    ref_or_path: str | Path | WorkflowRef,
    *,
    project_root: Path,
    home: Path | None = None,
    config: Config | None = None,
    known_providers: Iterable[str] | None = None,
) -> ResolvedWorkflow:
    """Load a workflow by discovered name, file path or :class:`WorkflowRef`.

    Expands ``include:`` steps (lexical scope, cycle/depth checked), resolves every prompt step's
    agent (``agent:`` > ``defaults.agent`` > bare provider > ``config.default_provider``; named
    lookup ``agents:`` > ``.rayspec/agents/`` > ``~/.rayspec/agents/``; shallow merge for
    ``extends``; tiers and ``@aliases`` from ``config``), reads ``prompt_file`` /
    ``instructions_file`` contents and hashes every contributing file.
    """
    home = rayspec_home() if home is None else home
    config = load_config(project_root, home=home) if config is None else config
    known = set(known_providers) if known_providers is not None else default_known_providers(config)
    loader = _Loader(project_root=project_root, home=home, config=config, known_providers=known)
    path = loader.resolve_workflow_path(ref_or_path, search_dirs=(Path.cwd(), project_root))
    doc = loader.load_doc(path, include_path="")
    loader.process_steps(doc.workflow.steps, doc, prefix="", keys=("steps",), chain=(path,))
    return ResolvedWorkflow(
        workflow=doc.workflow,
        path=path,
        label=doc.label,
        base_dir=doc.base_dir,
        hash=loader.compute_hash(),
        agents=loader.agents,
        step_agents=loader.step_agents,
        includes=loader.includes,
        prompt_files=loader.prompt_files,
        source_files=sorted(loader.source_files, key=loader.label),
        warnings=loader.warnings,
        step_locations=loader.step_locations,
    )


__all__ = [
    "BUILTIN_PROVIDERS",
    "DEFAULT_MODEL_TIER",
    "MAX_INCLUDE_DEPTH",
    "GraphKind",
    "GraphView",
    "IncludedBody",
    "LoadedFile",
    "ResolvedAgent",
    "ResolvedWorkflow",
    "StepLocation",
    "base_dir_for",
    "default_known_providers",
    "import_optional",
    "load_workflow",
    "project_rayspec_dir",
]
