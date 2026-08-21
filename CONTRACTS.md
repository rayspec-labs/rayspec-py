# CONTRACTS — module boundaries and public surfaces

This file is the working agreement between the modules of rayspec. If you need to change a
contract, change it here **first**, in the same pull request, and call it out in the PR
description. The design document that motivates everything is summarised in
`docs/constitution.md`.

## Package layout and ownership

```
src/rayspec/
  schema/      (frozen)  Pydantic models: Workflow, steps, agents, inputs, Duration, statuses
  providers/base.py (frozen)  neutral provider contract + capabilities
  engine/paths.py   (frozen)  StepPath
  store/model.py, store/base.py (frozen)  RunRecord/StepRecord + RunStore protocol
  events/model.py, events/base.py (frozen)  RunEvent/StreamRecord + EventSink protocol

  loader/      YAML → Workflow, includes, agent resolution, validation, capability check
  config/      RAYSPEC_HOME, Config model, config.yaml merge, .env loading
  templating/  Jinja environments, Scope/context, filters, expressions
  providers/capabilities.py, registry.py, stub.py, pricing.py, _tools.py + cli/commands/providers.py
  store/file.py, events/sinks/*.py  the file-backed run store + the built-in event sinks
  engine/*     runtime, graph, scheduler, executors, structured, runner + cli/commands/run.py
  providers/claude.py, providers/codex.py  the two shipped provider adapters
  workspace/   project slug, git, worktrees, --repo, path lock + cli/commands/{worktrees,projects}.py
  cli/         shared; each command is a module in cli/commands/<name>.py exposing
               register(app). app.py auto-discovers them — never edit app.py.
  cli/_runs_common.py + cli/commands/{runs,show,logs,resume,approve,reject,cancel}.py
  cli/commands/{init,doctor}.py + cli/templates/<kind>/**
  cli/commands/{new,completion}.py + cli/templates/new/** + the packaged examples corpus
  cli/_docs.py  DOCS_BASE + docs_url(rel) — the only way a hint cites a doc
  secrets/      SecretProvider protocol + the env/file/cmd sources behind
               `config.secrets`; redact.py  the one Redactor every writer goes through
  loader/secrets.py  where a `secret: true` input may appear (the placement rules)
  engine/context_rebuild.py + cli/commands/{explain,eval}.py + plan --render  the read-only
               half of the engine: rebuild a step's template
               context from a stored run (or from stubs) and render its body — no run, no provider
  skill/        package data `skill/rayspec/{SKILL.md,references/*.md}` +
               install/compare helpers; cli/commands/skill.py + cli/commands/_skill_common.py
               (presentation helpers shared with init.py; scripts/gen_skill.py generates the
               references from docs/ and mirrors the dir to .claude/skills/rayspec/)
  testing/      spec.py (Case/Expect/StepExpect + discovery), runner.py
               (one case through Runner + StubProvider + CollectingSink), report.py (the
               four-line failure, JUnit, --json) + cli/commands/test.py; scripts/check_examples.py
               keeps only the coverage matrix and one CLI contract smoke per suite
  registry.py   store/sink/approval registries (entry points + builtins) +
               store/redacting.py (the redaction boundary a plugin store sits behind) +
               cli/plugins.py (the `rayspec.cli_plugins` group) + cli/commands/plugins.py
```

"Frozen" means: additive changes only (new optional fields, new helpers), never renames or
semantic changes without updating every consumer in the same PR.

## Dependency rules

- `schema` depends on nothing inside rayspec except `schema.*`.
- `providers/base.py` depends on nothing inside rayspec. **No SDK imports.**
- `rayspec/errors.py` is the exception root (`RayspecError`, `UnsupportedFeatureError`,
  `LoaderError`, `InputError`); every rayspec-raised error derives from it (`SchemaError`,
  `ProviderError`, …).
- `providers/base.py` may import only `rayspec.errors`.
- `store`, `events`, `engine/paths.py` depend on `schema`, `providers/base.py` and each other's
  models only (`store/base.py` imports `events/model.py`). `engine/paths.py` is a leaf module
  (schema only) that `providers/*` may import for glob-safe step-path matching; nothing else
  under `engine/` may be imported by `providers/`.
- `loader` → `schema`, `config`, `providers/base.py` + `providers/registry.py` (capabilities only,
  imported lazily; never import `providers/claude.py` or `providers/codex.py`), `templating`
  (compile-only helpers via the `TemplateChecker` protocol).
- `config` → `schema` (StrictModel) and `loader/yaml.py` (lazy import inside `load_config`).
- `templating` → `schema` (status enums, identifiers) only.
- `engine` → everything above; never imports concrete providers directly (uses the registry).
- `workspace` → `config` (registered projects), `errors`, and — only in `workspace/registry.py`,
  lazily — `loader/yaml.py` (the same YAML reader `config` uses for `config.yaml`); shells out to
  `git`. Nothing under `workspace/` imports the engine, providers, templating or the store —
  except the mode helpers `rayspec.store.file.secure_mkdir` / `open_private`.
- `cli` → everything; no business logic in the CLI layer.
- Concurrency: anyio only (ruff `TID251` enforces the banned asyncio APIs).

## Public surfaces

### schema (frozen)
```python
from rayspec.schema import (
    Workflow,
    parse_workflow,
    parse_step,
    SchemaError,
    StepBase,
    LeafStep,
    PromptStep,
    ShellStep,
    PythonStep,
    LoopStep,
    EachStep,
    ApproveStep,
    IncludeStep,
    StopStep,
    AgentDef,
    AgentOverride,
    ToolsSpec,
    InputSpec,
    inputs_to_json_schema,
    Duration,
    parse_duration,
    Identifier,
    StepStatus,
    RunStatus,
    KINDS,
    KIND_KEYS,
    iter_steps,
)
# StrictModel.parse(data, source=...) -> model | raises SchemaError(errors: list[str])
```
Additive: `Defaults.budget_usd: float | None` (positive; `rayspec.schema.workflow.
parse_money` accepts `1.5`, `"1.50"`, `"$1.50"`, `"12 USD"`) and `Defaults.max_tokens: int | None`
(positive; `parse_token_count` accepts `1500`, `"500k"`, `"1.5M"`, `"1_000_000"`; a fraction
that is not a whole number of tokens — `"1.5"`, `"1.0005k"` — is rejected, never rounded) — the
run-level circuit breaker caps (Duration-like `BeforeValidator` parsing, the `parse_*` functions
carry the `> 0` check; `Money` / `TokenCount` annotated types exported from
`rayspec.schema.workflow`). Root workflow only; included bodies' values are
ignored by the engine.

Additive: `Defaults.timeout_total: PositiveDuration | None = None` — the run-level
WALL-CLOCK cap, the third circuit breaker beside `budget_usd`/`max_tokens` (same `Duration`
parsing as `defaults.timeout`, `> 0`). Measured from `RunRecord.started_at`, which a resume
never rewrites: the cap is per RUN, not per attempt. Root workflow only.

Additive: `StepBase.artifacts: list[ArtifactPath] = []` (every kind) — the files a step
promises to write, relative to its working directory. `schema.steps.validate_artifact_path`
refuses an empty path, an absolute one, `~`, a `..` segment, a control character, a directory
path and TEMPLATE SYNTAX (`{{` / `{%`) at LOAD time (house error format, `file:line` from the
loader's line map), and returns the path in normal form (`./a.txt` → `a.txt`), so the recorded
path and the store's ref always name the same file. An entry is a literal file name: the field
is deliberately NOT rendered — `cwd:` on the same step is, which is how a per-item name is
expressed — and the published schema carries the same rule as an `items.pattern`
(`schemagen._ARTIFACT_PATTERN`). `ArtifactPath` /
`validate_artifact_path` are exported from `rayspec.schema.steps`.

Additive: `InputSpec.secret: bool = False` — the value is never persisted (stored
and printed as `"<secret>"`) and reaches `shell:`/`python:` steps only as `RAYSPEC_INPUT_<NAME>` or
through their `env:` mapping; `secret: true` + `default` is a schema error; `to_json_schema()`
does not emit `secret` (a rayspec marker, not JSON Schema).

### loader
```python
from rayspec.loader import (
    discover_workflows,  # (project_root: Path, *, home: Path | None = None) -> list[WorkflowRef]
    discover_agents,  # (project_root, *, home=None) -> list[AgentFileRef(name, path, scope)]
    find_project_root,  # (start: Path | None = None) -> Path  # nearest dir with .rayspec/ (then .git, then start)
    load_workflow,  # (ref_or_path_or_name, *, project_root, home=None, config=None,
    #  known_providers=None) -> ResolvedWorkflow   (raises LoaderError / SchemaError)
    validate_workflow,  # (resolved, *, capabilities_for=None, template_checker=None,
    #  on_unsupported="error", provider_ids=()) -> ValidationReport   (never raises)
    resolve_inputs,  # (workflow, *, cli_pairs=(), inputs_file=None, env=None) -> dict   (raises InputError)
    topological_order,  # (steps) -> list[StepModel]  stable Kahn order of one sibling list
    load_yaml,  # (text, *, source) -> Any   # strict SafeLoader variant (raises LoaderError)
    load_yaml_with_lines,  # (text, *, source) -> (data, LineMap)  LineMap: tuple[key|index, ...] -> 1-based line
    WorkflowRef,  # (name = file stem, path, scope: "project"|"user", description, error: str|None)
    ResolvedWorkflow,
    ResolvedAgent,
    IncludedBody,
    GraphView,
    LoadedFile,
    StepLocation,
    ValidationReport,
    TemplateChecker,
)
# ResolvedWorkflow: .workflow (Workflow; include steps stay IncludeStep, bodies live in .includes),
#   .path, .label (".rayspec/workflows/x.yaml"), .base_dir (the .rayspec dir), .hash (sha256 over
#   every contributing file: workflow + includes + agent files + prompt/instructions files),
#   .agents: dict[key, ResolvedAgent] (keys are opaque and origin-scoped: "<include path>agents.<name>",
#   "file:<label>", "provider:<id>", "inline:<step path>", "override:<step path>" — a main-document
#   agents.foo never collides with an included document's or an agent file's foo),
#   .step_agents: dict[step path, agent key] (prompt steps only),
#   .includes: dict[include step path, IncludedBody], .prompt_files: dict[step path, LoadedFile(path, text)],
#   .source_files: list[Path], .warnings: list[str], .step_locations: dict[step path, StepLocation]
#   helpers: .graphs() -> [GraphView(kind, prefix, steps, parent_path, parent)] root first, bodies
#   (loop/each/include) after; .all_steps() -> [(path, step)]; .step(path); .agent_for(step_path);
#   .prompt_text(step_path) (prompt or prompt_file contents); .location_of(step_path, *fields) -> "file:line"
# Step paths: "build/implement" (loop body), "fan/patch" (each body), "review/lint" (include body).
# ResolvedAgent: key, name, provider, model (tier/alias resolved; None = provider default), effort,
#   access, instructions (file contents read), instructions_mode, max_turns, budget_usd, tools: ToolsSpec,
#   thinking, mcp, provider_options, source (human description), yaml_path ("agents.x" / "steps.<id>.agent"),
#   locations: {field: "file:line"}, raw_model; .location(field), .field_path(field)
# IncludedBody: workflow_name, path, inputs_binding (= with:), inputs (declared InputSpecs), steps,
#   outputs, defaults, description — lexical scope: inner steps are NOT addressable from outside; the
#   include step's output is its outputs map.
# ValidationReport: .errors: list[str], .warnings: list[str], .unsupported: list[UnsupportedFeatureError], .ok
#   Unsupported entries are rendered with str(UnsupportedFeatureError) (fixed 4-line format) into
#   .errors, or into .warnings when on_unsupported == "warn" or workflow defaults.on_unsupported == "warn".
#   Effort aliases (caps.effort_aliases) are a warning + the ResolvedAgent in .agents is rewritten.
#   capabilities_for(provider_id) -> ProviderCapabilities | None (None → warning, checks skipped);
#   capabilities_for=None skips capability checks silently (the CLI prints the registry warning).
```
`TemplateChecker` protocol (implemented by `templating`; `validate_workflow(template_checker=None)`
skips compile/reference checks):
```python
class TemplateChecker(Protocol):
    def compile_template(
        self, text: str, *, where: str
    ) -> object: ...  # raise on error (RayspecError preferred)
    def compile_expr(self, text: str, *, where: str) -> object: ...
    def references(self, text: str) -> Iterable[Any]  # Ref objects (root/name/attr_path) or (root, name[, attrs]) tuples:
        ...
        # free references of a template OR expression; each item is indexable: [0]=root, [1]=name,
        # optional [2]=attr path tuple (e.g. ("output", "verdict")) — a NamedTuple Ref(root, name, attr_path) fits.
```
Reference rules enforced when a checker is given: `steps.X` must be a transitive ancestor (same
graph) or an ancestor of an enclosing composite (loop/each/include body steps are not visible from
outside); `until` sees the whole body; `inputs.Y` must be declared (included workflow's inputs inside
its body); `iteration.*` only inside a loop, `iteration.prev.Z` a body sibling; `each.*`/`<as>` only
inside an each body; `steps.<include>.output.<k>` must be one of the include's outputs.
Lints (always on): `{{`/`{%` in `when`/`until`/`each` → error; `${{` in shell bodies → error.

Agent resolution: step `agent` (name | {extends} | inline) > `defaults.agent` > bare provider name >
`config.default_provider`; named lookup: workflow `agents:` > `.rayspec/agents/<name>.yaml` >
`~/.rayspec/agents/<name>.yaml`; shallow merge over top-level keys, only `model_fields_set` of an
override applies; `tools`/`provider_options` replace wholesale. An unset `model` resolves to the
provider's `medium` tier (tiers/aliases from config); `session:` targets must resolve to the same
provider. Model: literal id, tier (`small|medium|large` → `config.resolve_tier(provider, tier)`,
built-in `DEFAULT_TIERS` fallback, unknown → warning + `model=None`) or `@alias` (may also pin
provider/effort; unknown → LoaderError). An alias that pins a provider different from the agent's
explicitly set `provider` is a `LoaderError` (drop `provider:` or use a literal model id); an unset
provider takes the alias's. `prompt_file` / `instructions_file` resolve relative to the
`.rayspec/` dir (or `~/.rayspec/`) of the file that set them. Includes: name (discovery) or a path
relative to the including file; cycles and depth > 8 are `LoaderError`s; `with:` is validated
against the included inputs (unknown/required/type-coercible) by `validate_workflow`.
Inputs (`resolve_inputs`): `--input k=v` > inputs file (yaml/json) > `RAYSPEC_INPUT_<NAME>` > default;
text coerced by type (integer/number, boolean true/false/yes/no/1/0, array/object JSON, repeated
`--input k=v` appends for arrays, repeating a scalar is an error); unknown → did-you-mean; all
missing required together; final jsonschema validation. `rayspec.loader.inputs.env_var_name(name)`.
Secret inputs — `rayspec.loader.inputs`: `SECRET_PLACEHOLDER = "<secret>"`,
`secret_input_names(workflow) -> tuple[str, ...]`, `split_secret_inputs(values, names) ->
(public, secrets)`, `redact_inputs(values, names)` (given secrets → placeholder; absent optional
secrets stay absent), `resolve_resume_secrets(workflow, recorded_inputs, *, cli_pairs=(),
env=None) -> dict` (`--input` for secret names only — a non-secret name raises `InputError("inputs
are fixed per run; …")` —, else `RAYSPEC_INPUT_<NAME>`; every secret recorded as the placeholder
that is still missing → `InputError("missing secret input(s): a, b — pass --input a=… --input
b=… or set RAYSPEC_INPUT_A, RAYSPEC_INPUT_B")`). `validate_workflow`: a secret input may be
named ONLY by a shell/python `env:` value (`_check_template(..., secret_ok=True)`); every other
template/expression (prompt bodies, prompt `env:`, agent `instructions`, shell/python bodies,
`cwd`, `when`/`until`/`each`, `outputs`, `approve.message`, `stop.reason`, include `with:`) reports
`loader.validate.secret_reference_message(name)` (`inputs.x is declared secret: true — secret
inputs can only reach shell/python steps via RAYSPEC_INPUT_X (or a shell/python env: mapping); …`);
an included workflow declaring a secret input is an error at `steps.<path>.with`; `inputs` used
as a whole (a `Ref` with `name=None`: `inputs | tojson`, `inputs.get(..)`, `inputs.items()`,
`inputs[expr]`) outside a `secret_ok` template reports `secret_whole_inputs_message(names)`
(`inputs is used as a whole while inputs.x is declared secret: true — …`) whenever the scope
declares a secret. `coerce_input` / the jsonschema message print `<secret>` instead of the
rejected value of a `secret: true` input (never echoed, not even in `plan` rows). Expression
references (`when`/`until`/`each`) are collected with `references(text, kind="expr")` (they were
parsed as text before and yielded nothing).
Strict YAML (`load_yaml`): booleans only `true/false` spellings, no sexagesimal, no leading-zero
octal (`0123` stays a string, `0o17` is octal), no timestamps (dates stay strings), duplicate keys
are errors.

### config
```python
from rayspec.config import (
    rayspec_home,  # (environ=None) -> Path   $RAYSPEC_HOME or ~/.rayspec
    load_config,  # (project_root, *, home=None) -> Config   ~/.rayspec/config.yaml then .rayspec/config.yaml
    load_env,  # (project_root, *, home=None, environ=None, override=False) -> dict applied
    merge_config_data,
    parse_env_text,
    Config,  # default_provider="claude", tiers: {provider: {small|medium|large: TierSpec(model, effort)}},
    # aliases: {"@name": AliasSpec(provider?, model, effort?)}, pricing: dict, providers: dict[str, dict],
    # projects: [ProjectSpec(name, source, base?)];  .resolve_tier(provider, tier) -> TierSpec | None
    DEFAULT_TIERS,
    TIER_NAMES,
    TierSpec,
    AliasSpec,
    ProjectSpec,
)
```
Merge: project wins, shallow per top-level key; `tiers` merged per provider+tier, `aliases` per alias.
`.env`: simple `KEY=VALUE` (`export`, quotes, `#` comments); project wins; never overrides an
already-set process variable unless `override=True`.

Additive: `ConfigError(LoaderError)` — the ONE exception `load_config`/
`load_env` raise for a malformed or unreadable `config.yaml`/`.env` (YAML syntax, unsafe tag,
non-mapping, wrong field type, IO/UTF-8), message `<path>[:<line>]: …` (+ `.hint`/`.location`);
the CLI boundary (`cli/commands/_loader_common.make_context`) prints `error: <msg>` and exits 2.
`load_env(..., include_project=False)`: the project `.rayspec/.env` is applied only on request —
`make_context(root, *, project_env: bool | None = None)` applies it for `EXECUTION_COMMANDS`
(`run`, `resume`, `approve`, `reject`; detected from the click context via `invoked_command()`,
`None` outside a CLI invocation) and prints `env: loaded N variables from .rayspec/.env (project)`
on stderr; every other command loads `~/.rayspec/.env` only. `project_env_info(project_root) ->
ProjectEnvInfo(path, count) | None` describes the project file (the `doctor` row).
`rayspec.textsafe`: `safe_text(s, *, keep_newlines=True) -> str` (C0/C1 controls,
CSI/OSC/DCS/SS3/Fe escapes removed; `\n`/`\t` kept or turned into spaces), `safe_markup(s) =
rich.markup.escape(safe_text(s))`. Every renderer of agent/step/input/output text (console sink,
approval panel, `show`, `logs`, run summary) goes through it; no rayspec imports (leaf module).

### errors (additive)
`rayspec.errors` gained `LoaderError(message, *, hint=None, location=None)` (YAML/include/agent/file
problems) and `InputError(errors: Sequence[str] | str, *, hint=None, partial=None, problems=None)`
(`.errors` list; additive: `.partial` = the inputs that did resolve, `.problems` =
`{name: [messages]}` for those that did not — `rayspec plan` shows good inputs next to bad ones),
both deriving from `RayspecError`. `UnsupportedFeatureError` gained an optional `field=` kwarg (+ `.field`): the
name shown in line 2 (`does not support \`<field>\``), defaulting to the last path segment.

### CLI — `workflows [--json]`, `agents [--json]`, `validate [names...] [--allow-unsupported] [--json]`
(exit 2 on errors; an unknown name is `error: unknown workflow`), `plan <workflow> [--input k=v]*
[--inputs-file f] [--allow-unsupported] [--json]` (input/validation errors as `error:` lines on
stderr, one JSON error object with `--json`; additive: the `--json` object carries
`providers: {id: {structured_output, cost_reporting, cost: provider|table|none, priced_models,
unpriced_models, disabled_models[, pricing_error]}}` and the human capability report prints the
cost source per provider incl. the `tokens only — add pricing.<model> …` nudge); all take `--root` (default: `find_project_root(cwd)`;
a `--root` that is not a directory exits 2). `rayspec --version`/`-V` prints the version. They import `rayspec.providers.registry` lazily (warning
"capability checks skipped (providers registry not available)" when absent) and use
`rayspec.templating.TemplateEngine()` as the `TemplateChecker` when importable, else `None`.

Additive semantics: `validate` prints one bullet per problem (a
`SchemaError` is split via `_loader_common.error_entries(exc)`: one `<file>: <message>` entry per
error; other errors stay one entry) and its `--json` row carries `path` = the target's label from
`_loader_common.workflow_label(target, ctx)` even when the file fails to load; `report_lines`
escapes every item (`rich.markup.escape`) so user text is never parsed as markup.
`agents --json` rows gain `resolved: {provider, model, effort, via, provider_from ∈
agent|alias|default, problem}` from `agents.resolve_fields(AgentDef, Config)` — the loader's
alias/tier rules (alias pins the provider; tiers resolve per provider) applied to one file; the
table shows `codex (via @fast)` / `claude (default)` and `gpt-5.4 (@fast)`. `workflows` and
`validate` print `EMPTY_PROJECT_HINT` (names `rayspec init` + the examples URL) on an empty project.
Every hint that cites documentation goes through `rayspec.cli._docs.docs_url(rel)`
(`DOCS_BASE = "https://github.com/rayspec-labs/rayspec-py/blob/main/"`) or says `rayspec <cmd>
--help` — never a bare repo-relative path; `_pricing_common.PRICING_DOCS` is that URL.
`loader.inputs.resolve_inputs`: an input whose given value is rejected is one problem (`invalid`)
— never also `missing (required)`, and no lower-precedence source stands in for it.

### templating
```python
from rayspec.templating import (
    TemplateEngine,        # TemplateEngine(*, spill_threshold=SPILL_THRESHOLD)   (64 KiB)
    #   .render_text(tpl, ctx) -> Any          text env; a template that is exactly one {{ expr }} keeps its
    #                                          Python type (incl. None), anything else returns str
    #   .render_str(tpl, ctx) -> str           text FIELDS (prompt, instructions, approve message, stop.reason,
    #                                          cwd): stringify_text(render_text(...)); None/undefined/callable
    #                                          -> TemplateRenderError (never a silent None or a repr)
    #   .render_value(value, ctx) -> Any       deep rendering: str = template, dict/list recursed, scalars pass
    #   .render_shell(body, ctx, *, spill_dir=None) -> RenderedScript(script, env: dict[str,str], spills: list[Path])
    #                                          {{ expr }} -> "${RAYSPEC_V<n>}" (+ env slot) | "$(cat '<spill>')" over 64 KiB
    #                                          spill_dir may be shared by parallel steps (collision-free names,
    #                                          made absolute); macro/call/filter/set-BLOCKS are compile errors
    #   .render_python(body, ctx, *, spill_dir=None) -> RenderedScript   {{ expr }} -> Python literal (repr of JSON-like)
    #   .eval_expr(expr, ctx) -> Any           undefined result -> TemplateRenderError
    #   .eval_bool(expr, ctx) -> bool          raises unless the result is exactly a bool
    #   .compile_template(tpl, *, where, kind="text"|"shell"|"python") / .compile_expr(expr, *, where)
    #                                          load time; raise TemplateCompileError(where, message, lineno)
    #   .references(text, *, kind="text"|"shell"|"python"|"expr") -> frozenset[Ref]
    #                                          method calls are not segments: steps.a.output.items() -> ("output",)
    Ref,                   # Ref(root, name: str | None, attr_path: tuple[str, ...]); roots: REFERENCE_ROOTS
    RenderedScript,        # frozen dataclass (script, env, spills); the engine adds export_env() itself
    Scope,                 # Scope(parent, steps: Mapping[str, StepView], variables=None); .child(), .lookup_step(),
                           #   .visible_steps(), .lookup_var(), .merged_vars(), .missing_step_hint()
    StepView,              # frozen dataclass: id, kind, status, output, ok, exit_code, stderr, duration_s, cost_usd,
                           #   usage, session, model, approved, iterations, converged, items, skip_reason, error,
                           #   tolerated, body_ids; .resolve(name) is the hint-bearing lookup; .to_json()
    build_context,         # (scope, *, inputs, run, project, iteration=None, each=None, item_var=None, item=None,
                           #   env=None) -> dict[str, Any]   (the ctx handed to render_*/eval_*)
    export_env,            # (ctx) -> {RAYSPEC_INPUT_<NAME>, RAYSPEC_RUN_ID, RAYSPEC_WORKDIR, RAYSPEC_ARTIFACTS_DIR,
                           #   RAYSPEC_STATE_DIR}
    write_context_file,    # (ctx, path) -> Path   JSON dump for RAYSPEC_CONTEXT (views -> dicts, undefined -> null;
                           #   the `env` root is NOT written — secrets never land in the run dir;
                           #   atomic: <path>.tmp + os.replace)
    RayspecUndefined,      # ChainableUndefined: chain on access, raise on use; .rayspec_hint
    TemplateCompileError,  # (where, message, lineno) ; TemplateRenderError(message, *, hint=None)
    TemplateRenderError,
    fromjson, regex_search, has_signal,   # the only non-builtin filters (has_signal also a test)
    has_braces, has_gha_syntax,           # pure lints for the loader (no engine needed)
    STEP_ATTRIBUTES, REFERENCE_ROOTS, SPILL_THRESHOLD, stringify_text, to_jsonable,
)
```
Context roots: `inputs`, `steps`, `run`, `project`, `iteration`, `each`, `<as>`, `env`.
Semantics fixed here (tests in `tests/templating/`):
- `StepView.resolve`: `output` of a non-succeeded step without output → undefined with the guard
  hint; `None` attributes → undefined with hint (so `| default` / `is defined` work); `status`
  is a plain `str`; `ok` is `record.ok`, else `status == 'succeeded'` — **except on a skipped
  step, where it is undefined carrying the same hint as `output`**; `usage`
  dataclass → dict (`StepView.to_json` therefore reports `"ok": null` for a skipped step).
  Text outputs remember their path: `.field` on them → "no output_schema (try | fromjson)".
- Attribute lookup on mappings is item-first (`inputs.items` is the input named `items`), then
  only `items`/`keys`/`values`/`get`; `steps.<id>` resolves innermost scope first; a body step seen
  from outside → "inside loop 'x'; use steps.x.output.<id>".
- `stringify_text` (text finalizer, `render_str`, shell env values) renders str/bool/number/
  composite/dataclass/path/date/enum only; `None`, undefined, callables (`{{ x.keys }}` without
  parentheses) and arbitrary objects raise `TemplateRenderError` naming the fix — no reprs leak.
- `iteration.prev` that is `None` is dropped from the context → chainable undefined
  (`iteration.prev.x.output | default('')` works). `each` without `item_var` exposes `item`.
- Shell/python bodies: comment delimiters `{{# … #}}`; `{% raw %}` for literal `{{`. Python
  `{{ }}` values must be JSON-like (no NaN/objects; mapping keys must be str). Spills:
  `<spill_dir>/v<n>-<random>` via `mkstemp` (shell: text, `$(cat '<abs path>')`; python: JSON,
  `json.loads(Path(...).read_text())`); `spill_dir` is made absolute and may be shared by parallel
  steps. `{% macro %}`, `{% call %}`, `{% filter %}` blocks and `{% set x %}…{% endset %}` are
  rejected at compile time for shell/python (they would re-substitute already substituted text);
  use `{% set x = expr %}` and inline filters.
- `render_value`: single `{{ expr }}` keeps type **including `None`**; the engine must reject
  `None` when str-coercing `env:` values. Text *fields* (prompt, instructions, approve message,
  stop.reason, cwd) go through `render_str`, which rejects `None`/undefined/callables.
- Every exception raised while rendering becomes `TemplateRenderError` (message names the fix).

### providers

```python
from rayspec.providers.capabilities import (
    CLAUDE_CAPABILITIES,  # ProviderCapabilities for the Claude adapter
    CODEX_CAPABILITIES,
    STUB_CAPABILITIES,  # everything on
    BUILTIN_CAPABILITIES,  # Mapping[id, ProviderCapabilities] for claude/codex/stub
    KNOWN_CLAUDE_TOOLS,  # frozenset[str] of current Claude Code tool names
    RENAMED_CLAUDE_TOOLS,  # {"Task": "Agent", "MultiEdit": "Edit", "BashOutput": "TaskOutput", "KillShell": "TaskStop"}
)
from rayspec.providers.registry import (
    ENTRY_POINT_GROUP,  # "rayspec.providers"; entry-point value = "module:REGISTRATION"
    BUILTIN_REGISTRATIONS,  # (claude, codex, stub) — lazy factories, no SDK import
    UnknownProviderError,  # RayspecError + LookupError; .hint carries did-you-mean / available ids
    get_registration,  # (provider_id) -> ProviderRegistration | raises UnknownProviderError
    list_registrations,  # () -> list[ProviderRegistration]  (builtins first, then plugins by id)
    create_provider,  # (provider_id, settings: Mapping | None = None) -> Provider
    #   builtin factories import rayspec.providers.{claude,codex,stub} lazily and raise
    #   ProviderNotInstalledError("provider adapter not available", hint=...) on ImportError,
    #   ProviderError(kind="provider", hint=...) on any other import-time exception
    register,  # (ProviderRegistration, *, replace=False) -> None   (programmatic; builtins immutable)
    #   precedence, order-independent: builtins > programmatic > entry points; replace=True only
    #   needed to re-register an id that was itself registered programmatically
    reset_registry,  # () -> None   (tests: forget cache + programmatic registrations)
)
from rayspec.providers.stub import (
    StubProvider,  # (settings: Mapping | None = None, *, script: StubScript | Mapping | None = None)
    #   settings keys: "script" (dict | StubScript), "script_path" (YAML file)
    #   .calls: list[AgentRequest]; .script; session_ref "stub:<step_path>:<n>"
    StubScript,  # .from_dict(data, *, source) / .from_yaml(text, *, source) / .from_file(path)
    #   .resolve(step_path, prompt) -> StubEntry | None   (exact -> glob -> match regex)
    #   .steps: tuple[StubEntry], .match: tuple[StubEntry], .defaults: StubDefaults
    StubEntry,  # .key, .outcome, .sequence, .prompt_regex, .outcome_for(n) -> StubOutcome
    #   n = calls that resolved to THIS entry (per-entry counter: a glob's sequence advances across
    #   every path it matches); session_ref "stub:<path>:<n>" and fail.times count per step path
    StubOutcome,  # text / output(+has_output) / fail / events / usage / latency_ms / status
    #   latency_ms > req.timeout_s -> status "timeout" after timeout_s/2 (engine owns the deadline)
    StubFailure,  # kind, message, transient, times (None = always), raise_error
    StubDefaults,  # latency_ms, usage
    StubScriptError,  # RayspecError: malformed script
    minimal_instance,  # (json_schema) -> minimal value (required fields, type defaults, first enum)
)
from rayspec.providers.pricing import (
    Price,  # (input, cached_input, output, cache_write=None)  USD per 1M tokens
    PriceTable,  # .from_config(mapping | None) / .lookup(model) -> Price | None / .cost_usd(model, usage)
    #   exact id first, then longest matching fnmatch glob; a null value disables pricing
    PricingConfigError,  # RayspecError
    cost_usd,  # (usage, price) -> float
    #   = (uncached*input + cached*cached_input + cache_write*(cw or input) + output*output) / 1e6
    format_cost,  # (cost_usd | None, cost_source, usage) -> "$0.12" | "~$0.12" | "12.3k tok"
    format_tokens,  # (int) -> "850 tok" | "12.3k tok" | "1.5M tok"
)
from rayspec.providers._tools import (
    translate_tools,  # (allow, deny, provider_id, capabilities) -> ToolTranslation   (adapters)
    #   also the original sketch form: (ToolsSpec | ToolPolicy, provider_id, *, capabilities=None)
    #   -> capabilities default to the registry's table for provider_id
    ToolTranslation,  # allow_native, deny_native, config_overrides, warnings, errors, .ok,
    #   allow_all_mcp / deny_all_mcp (bare `mcp` group; Claude adapter expands to mcp__<server>
    #   over req.mcp_servers — Claude Code has no MCP wildcard name)
    validate_tools,  # (ToolsSpec | ToolPolicy, provider_id, capabilities, *, access=None,
    #                   known_providers=None) -> list[str] error messages (loader helper; the
    #                   LOAD-TIME ENTRY POINT for the loader — never raises, bad access -> message)
    parse_tool_entry,  # (str) -> ToolEntry(kind: group|mcp|raw|invalid, ...)
    CLAUDE_GROUP_TOOLS,  # group -> native Claude names (read->Read,Glob,Grep; ...; mcp -> ())
)
```

Pricing additive: `providers.pricing.format_cost(cost_usd, cost_source, usage)` also
renders `partial` → `≥$0.12`; `cost_marker(source) -> "" | "~" | "≥"` is the single marker rule;
`combine_cost_sources(sources, *, unpriced=False)` folds per-step sources into the run level
(`provider` = only provider costs, `table` = any estimate, `partial` = `unpriced` steps next to
priced ones, `none` = no cost); `COST_SOURCES`.

Stub script shape (YAML/dict): ``steps: {<path|glob>: {output|text|sequence|fail|events|usage|
latency_ms|status}}``, ``match: [{prompt_regex, ...}]``, ``defaults: {latency_ms, usage}``.
Resolution: exact key → first matching glob in declaration order (not specificity) → first
``match`` regex → default. ``sequence`` advances per matched entry (glob entries see every loop
iteration); ``fail.times`` and ``session_ref`` count per step path.
Tool translation: Claude groups → native names (``mcp:<s>`` → ``mcp__<s>``, ``mcp:<s>/<t>`` →
``mcp__<s>__<t>``, bare ``mcp`` → ``allow_all_mcp``/``deny_all_mcp`` flag); raw ``<provider>:<Name>``
only for that provider (Claude: renamed with warning, unknown with warning, ``mcp__*`` unchecked);
Codex: ``deny: [web]`` → ``config_overrides {"web_search": "disabled"}``, anything else → error naming the
capability; stub/plugins: neutral spelling passed through. Raw entries addressed to another provider are
ignored silently. The CLI command ``rayspec providers [--json]`` prints the registry and capability matrix.

### providers/codex.py

```python
from rayspec.providers.codex import (
    CodexProvider,  # (settings: Mapping | None = None) — registry factory for provider id "codex"
    #   settings keys (config.providers.codex): approval_mode ("deny_all" default | "auto_review"),
    #   config (extra Codex config mapping merged into every thread, e.g. model_reasoning_summary),
    #   codex_bin (path overriding the bundled runtime), pricing (model/glob -> Price mapping, see
    #   providers.pricing; cost_source "table"), drain_s (seconds to wait for an interrupted turn to
    #   finish before the client is closed + recreated; default 10)
    #   .open(run_id, workdir, env, max_parallel): records the run context, raises the loop's
    #   default executor to max(32, 2*max_parallel+8) workers when smaller, creates .limiter
    #   (anyio.CapacityLimiter(workers - 4)); clients are created lazily per env signature
    #   .run(req, emit) -> AgentResult; .healthcheck(probe=False); .aclose() closes every client
    #   and forgets the per-thread usage totals
    #   .limiter, .executor_workers, .settings, .pricing (PriceTable)
    classify_turn_error,  # (TurnError | None) -> AgentError  (codex_error_info -> kind/transient)
    error_info_code,  # (TurnError | None) -> "serverOverloaded" | "httpConnectionFailed" | ... | None
    usage_from_breakdown,  # (TokenUsageBreakdown) -> Usage
    usage_delta,  # (current, previous) -> Usage  (field-wise, clamped at 0)
)
from rayspec.providers._schema import (
    for_openai_strict,  # (schema) -> (strict_schema, warnings): additionalProperties:false on every
    #   object + all properties required, recursively (properties/items/prefixItems/$defs/definitions/
    #   anyOf/oneOf/allOf/not/if-then-else); a warning per object whose additionalProperties was open.
    #   Only these two rules are applied: other keywords OpenAI strict mode rejects (e.g. format,
    #   pattern, minimum/maximum, minLength, default) pass through and surface as a badRequest turn
    #   error (status "error", kind "api", non-transient) — keep output_schema to types/enum/required
)
```
Request mapping: `thread_start(cwd=req.cwd, model=req.model, sandbox=read_only|workspace_write|
full_access per access, approval_mode=deny_all (or provider_options.codex.approval_mode), developer_
instructions (append) | base_instructions (replace), config={**settings.config, **provider_options.
codex.config, "mcp_servers": {name: {command,args,env} | {url,http_headers}} from req.mcp_servers,
"web_search": "disabled" when tools.deny has web}, ephemeral only via provider_options.codex.ephemeral
(probes))`; `resume_session` → `thread_resume(id, same kwargs)`, `+ fork_session` → `thread_fork`.
`thread.turn(prompt, output_schema=for_openai_strict(schema), effort=ReasoningEffort(effort with
max/ultra pass through), model=req.model)` — never `sandbox=` per turn. `req.provider_options` may be the full
`{codex: {...}}` mapping or already narrowed to the codex block (both accepted); keys:
`approval_mode`, `config`, `ephemeral`, `usage_baseline` (see usage). Loud failures
(`ProviderError` + hint): unsupported tool entries (anything but `deny: [web]`), unknown
`approval_mode`/`effort`, an http MCP spec without `url`, transport `sse` (codex speaks stdio and
streamable http only), a non-mapping `config.mcp_servers`.
Events: `turn/started`→session (text=thread id); `item/agentMessage/delta`→text_delta; completed
agentMessage→text (data.phase); `item/reasoning/*`→reasoning; commandExecution started/outputDelta/
completed→command_start/command_output/command_end (data exit_code,status,duration_ms,command,cwd);
fileChange→file_change per change (name=path, text=diff, data path/kind/diff/status); mcpToolCall/
webSearch→tool_call/tool_result (name `server/tool` | `web_search`); `turn/plan/updated`→plan;
`thread/tokenUsage/updated`→usage (data.usage = TOTAL delta vs the last total seen for that thread,
data.total, data.turn_total). Usage baseline for a resumed thread: the provider's own last total
when it saw the thread in this run, else `provider_options.codex.usage_baseline` (a usage-counter
mapping = the previous turn's `raw["usage_total"]`), else **inferred** as `total - last` of the
first update in the turn — exact only if that update is the turn's first model request (if the
app-server replays a carry-over update at turn start the turn is over-counted by `last`; not
verified against the live server — pass `usage_baseline` to be exact);
`error`→warning if will_retry else error; unknown methods/items→raw. Result: status completed→
`success` (also when our deadline fired but the turn completed during the drain:
`raw.deadline_exceeded=True` + a `warning` event), interrupted→`timeout` (our deadline fired;
error.kind "timeout") else `interrupted`, failed→`error` with `classify_turn_error` (transient:
serverOverloaded, internalServerError, httpConnectionFailed, responseStream*; fatal:
unauthorized→auth, usageLimitExceeded→budget, badRequest, contextWindowExceeded→model,
cyberPolicy, sandboxError→sandbox); `structured` = `json.loads(text)` when an output_schema was
given (invalid JSON → None); `session_ref` = thread id (None if the turn never started); `model` =
`req.model` as requested (the SDK handle does not expose the effective model); `cost_usd` from the
pricing table (`table`) else None; `raw` = {thread_id, turn_id, turn_status, turn_error,
timed_out, usage_total (cumulative thread total, see usage), deadline_exceeded?, turn_duration_ms?}.
Infrastructure: `FileNotFoundError` (bundled runtime) → `ProviderNotInstalledError`;
`TransportClosedError` → client poisoned+closed, `ProviderError(transient=True, kind="transport")`;
`ServerBusyError`/`is_retryable_error` → `ProviderError(transient=True, kind="api")`; other
`CodexError` → `ProviderError(kind="api")`; a `ValueError` from `thread_start`/`turn()` →
`ProviderError(kind="provider")`; anything else (emit/store failures, bugs) is re-raised untouched.
Cancellation/timeout = shielded driver: `thread_start`/`turn()` *and* the stream run in one child
task under `CancelScope(shield=True)`, so `req.timeout_s` covers thread start, turn start and the
stream; on timeout/cancel/emit failure → (start still in flight: wait ≤ `drain_s`, then
`codex.close()`) → `interrupt()` → drain ≤ `drain_s` → still hung ⇒ `codex.close()` (wakes the
blocked worker) + recreate the client; worst case `timeout_s + 2*drain_s`. External cancellation is
re-raised after the abort; an emit failure is re-raised after the turn was interrupted.
Healthcheck: sdk_version, bundled `codex` path,
`codex --version`, auth = `OPENAI_API_KEY` or `AsyncCodex().account()`; `probe=True` runs one
read-only, deny-all, ephemeral turn ("Reply with exactly OK"). Live smoke:
`RAYSPEC_LIVE=1 uv run pytest -m live tests/providers/test_codex_live.py`.

### store + events
Additive: `RunRecord.stubs_path: str | None = None` (absolute path of the `--stubs`
file given at launch; overridden by `--stubs` on a resume entry) and `RunRecord.secret_inputs:
tuple[str, ...] = ()` (names of the `secret: true` inputs; `RunRecord.inputs` holds
`"<secret>"` for the ones that were given). Older `run.json` files read as `None` / `()`.
```python
from rayspec.store import FileRunStore, RunStore  # + all store.model names, errors, WrittenOutput
from rayspec.store.file import (
    FileRunStore,  # (root: Path) -> RunStore; layout under <root>/runs/<run-id>/ (see below)
    WrittenOutput,  # frozen dataclass: output_ref (run-dir relative), path, kind, sha256, size
    StoreError,  # RayspecError subclass; base of the four below
    UnknownRunIdError,  # load/resolve/delete of a missing run (or no prefix match)
    AmbiguousRunIdError,  # .prefix, .candidates (newest first)
    RunExistsError,  # create() for an existing run id
    CorruptRunError,  # load() of a run.json that is not a valid RunRecord (list_runs skips it)
)
# FileRunStore — beyond the RunStore protocol:
#   run_dir(run_id)           -> Path (not created; ValueError on unsafe ids)
#   step_dir(run_id, path)    -> Path, created with parents; path validated via StepPath.parse
#                             (ValueError for the empty/root path)
#   exists(run_id)            -> bool
#   create(run)               mkdir skeleton (steps/, artifacts/, tmp/) + first save; RunExistsError
#   save(run)                 atomic: run.json.<pid>.<n>.tmp + fsync + os.replace; single writer
#                             (threading.Lock) — safe from anyio.to_thread / several tasks;
#                             ensures the skeleton too
#   load(run_id)              RunRecord.model_validate_json; unknown keys ignored;
#                             UnknownRunIdError (no run.json) | CorruptRunError (unparseable)
#   list_run_ids()            -> list[str] newest first (time-sortable ids)
#   list_runs(limit=None)     -> list[RunRecord] newest first; unreadable run.json skipped + warning
#   resolve_run_id(prefix)    -> full id (exact match wins) | UnknownRunIdError | AmbiguousRunIdError
#   delete_run(run_id)        rmtree; UnknownRunIdError if missing
#   write_output(run_id, path, content, *, kind)            -> output_ref ("steps/<path>/output.txt|json")
#   write_output_with_sha(run_id, path, content, *, kind)   -> WrittenOutput
#       kind "text": written verbatim, streamed in chunks (large outputs never copied twice)
#       kind "json": content must be a JSON document; stored pretty-printed (indent=2, trailing
#       newline) and hashed in that form; ValueError on invalid JSON (NaN/Infinity included) /
#       unknown kind. The stale output file of the other kind is removed. Written as
#       output.<kind>.<pid>.<n>.tmp + fsync + os.replace: a failed rewrite keeps the old output.
#   StepRecord.usage_unknown: bool = False (additive): at least one attempt of the
#       step was interrupted / timed out before the adapter reported any usage — the record's
#       usage is a lower bound, never "zero tokens"; step.finished data gains usage_unknown: true
#   RunRecord.cost_source (additive): "provider" | "table" | "partial" | "none" —
#       the run-level cost source the engine computes on every final status (see the engine
#       section: rayspec.engine.context.cost_source_of); older run.json files read as "none"
#   RunRecord.pid_started_at: str | None = None (additive): start time of the
#       process behind pid — the `ps -o lstart= -p <pid>` string as printed under LC_ALL=C TZ=UTC
#       (fixed env, so launch and cancel shells agree; Linux when ps is missing/fails: the
#       /proc/<pid>/stat starttime), from rayspec.engine.runner.process_start_time(pid); written
#       with pid at launch and refreshed on every resume; None in older records. rayspec cancel
#       compares it with the live process before the command-line heuristic
#   write_artifact(run_id, step_path, rel_path, source) -> WrittenArtifact (additive):
#       copies one declared artifact to artifacts/<step path>/<rel_path> and reports
#       (artifact_ref, path, source, sha256, size) of the STORED bytes. Same durability and
#       permissions as an output (tmp + fsync + os.replace, 0600 via a private-file open, 0700
#       dirs); a later attempt overwrites the copy. ``rel_path`` must be relative, without
#       ".." (ValueError otherwise — the schema already refuses those at load time, this is the
#       second lock). The SOURCE must be a regular file: it is opened non-blocking and checked
#       with fstat, so a FIFO/socket/device raises OSError instead of blocking the worker
#       thread forever (the engine refuses those first; this is the second lock on that door). Redaction covers arbitrary bytes: the file is streamed through a
#       StreamRedactor decoded/encoded with ``surrogateescape``, so a binary file round-trips
#       byte for byte unless it carried a secret (then the marker replaces it and the stored
#       bytes — and the sha — differ from the step's original, deliberately)
#   StepRecord.artifacts: list[ArtifactRef] = [] (additive): what the step declared
#       under ``artifacts:`` and delivered — ArtifactRef(path (as declared, normal form), ref
#       (run-dir relative copy, None when the store keeps none), sha256, size). sha256/size
#       always describe the STORED (redacted) bytes: a store without write_artifact reports the
#       digest of the bytes it WOULD have kept (engine.context._digest_of streams the file
#       through store.redactor), so the field is comparable across runs and no digest of a
#       secret is ever persisted. Only the PATH is
#       recorded: an artifact's CONTENT never enters a record, an event, a template context or
#       an output. Empty for older records and for steps that declared nothing
#   write_prompt(run_id, step_path, text) -> prompt_ref (additive): the rendered
#       prompt of a prompt: step -> "steps/<path>/prompt.txt"; same durability/permissions as an
#       output (tmp + fsync + os.replace, 0600 via open_private), overwritten by a later attempt,
#       read back with read_output. The prompt executor calls it WRITE-AHEAD (after rendering,
#       before the provider call) and stamps StepRecord.prompt_ref; a store without the method
#       simply has no copy, and a write that FAILS is reported with ctx.warn (a warning event on
#       the step, so `rayspec show`/the console see it) — neither ever fails the step
#   StepRecord.prompt_ref: str | None = None (additive): the ref above; None for
#       every non-prompt kind and for records written before 1.1
#   read_output(run_id, output_ref)  -> str; ValueError if the ref is absolute, has ".." or
#                                       resolves (symlinks) outside the run dir; FileNotFoundError
#   record_step(run, record, output=None, *, kind="text") -> WrittenOutput | None
#       write-ahead: output file -> record.output_ref/output_kind/output_sha256 ->
#       run.steps[record.path] = record -> save(run)
#   append_event(run_id, event)          -> events.jsonl  (one flushed line per call, locked)
#   append_stream(run_id, path, record)  -> steps/<path>/stream.jsonl (same guarantees)
#   read_events(run_id) -> Iterator[RunEvent]; read_stream(run_id, path) -> Iterator[StreamRecord]
#       a torn trailing line (crash mid-write) ends the iteration with a warning; an unreadable
#       middle line is skipped with a warning — neither raises
#   read_stream(run_id, path, *, kinds=None) (additive): with kinds={"warning", …}
#       only records of those kinds are yielded and only lines containing one of the kind names
#       as a JSON string are parsed (cheap scan of multi-MB transcripts; FileRunStore only)
# Layout: <root>/runs/<run-id>/run.json, events.jsonl, steps/<path>/{output.txt|output.json,
#   prompt.txt, stream.jsonl, stdout.log, stderr.log}, artifacts/[<path>/<declared>], tmp/
#   (path = StepPath.fs_path())
# Permissions: every directory the store creates — <root> and its missing parents
#   ($RAYSPEC_HOME, projects/<slug>) included — is 0700, every file it writes 0600, regardless
#   of the umask (PRIVATE_DIR_MODE / PRIVATE_FILE_MODE); pre-existing dirs are never chmodded.
#   secure_mkdir(path) (mkdir -p 0700 for the missing part) and open_private(path, "w"|"a"|"x")
#   (os.open 0600|O_NOFOLLOW|O_CLOEXEC + fdopen, text; a symlink at path raises OSError) are
#   exported and used by the other writers under $RAYSPEC_HOME: workspace.lock (locks/ dir +
#   *.lock 0600), templating.scope.write_context_file (context.json), executors._process
#   (stdout.log/stderr.log); also workspace.worktrees (worktrees/ dir and the
#   parent of a recreated worktree), workspace.repos (parent of source.git) and
#   workspace.registry (home dir + config.yaml written 0600 via a private temp file, rewrite
#   included). Git-owned content — the worktree checkout, the bare source.git — keeps git's modes.

from rayspec.events import (  # models + protocol + sinks (no JsonlSink: the store owns the files)
    # the sink names resolve lazily (module __getattr__): importing the models never loads rich
    EventType,
    RunEvent,
    StreamRecord,
    EventSink,
    JsonStdoutSink,  # (stream: TextIO, *, close_stream=False): events as RunEvent.to_json() lines;
    #                  stream records as {"type":"stream","step_path":...,"record":{...}} lines;
    #                  flush per line; IO errors logged once, never raised
    CollectingSink,  # .events: list[RunEvent]; .streams: list[(step_path, StreamRecord)]; .closed;
    #                  .events_of(EventType), .stream_for(path), .clear()
    NullSink,  # discards everything
    MultiSink,  # MultiSink([a, b]) or MultiSink(a, b): fan-out in order, exceptions logged,
    #                  aclose() closes every sink; .sinks: tuple[EventSink, ...]
    QuietConsoleSink,  # (console: rich.console.Console, *, show_started=False): one line per
    #                  step.finished/step.retry/run.*/workspace.created/warning event (duration ·
    #                  tokens · cost · error/skip reason); step.started only with show_started=True;
    #                  emit_stream is a no-op; override format_event(), any format_<event>() hook
    #                  (dispatched by name through the instance; return Text | None) or
    #                  emit_stream() for the Live tree
)
from rayspec.events.sinks.console import fmt_duration, fmt_tokens, fmt_cost, usage_total, error_text
# fmt_cost(usd, *, approx=False, source=None) -> "$0.12" | "~$0.12" (approx / source "table":
# price-table estimate) | "≥$0.12" (source "partial": some steps have tokens but no price); the
# marker is providers.pricing.cost_marker. The quiet sink reads the OPTIONAL step.finished /
# run.finished data key cost_source; when run.finished carries none the run line derives it from
# the step.finished events seen (QuietConsoleSink.derived_cost_source()).
# format_stream_warning(step, text) -> "⚠ <step>: <warning>": printed by QuietConsoleSink.
# emit_stream() for kind == "warning" stream records (the only stream output in quiet mode).
# Every string from event data (paths, kinds, errors, reasons, messages, workdir/branch, outputs)
# is rendered through rayspec.textsafe.safe_text.
```
Every sink logs to the ``rayspec.events`` logger and never raises into the engine.

### ConsoleSink — Rich Live tree
```python
from rayspec.events import ConsoleSink  # also rayspec.events.sinks / rayspec.events.sinks.console
from rayspec.events.sinks.console import (
    ConsoleSink,  # (console, *, quiet=False, verbose=False, tail_lines=None, live=None,
    #   clock=time.monotonic, refresh_per_second=8, summary=True, display=True, show_started=None,
    #   max_children=DEFAULT_MAX_CHILDREN)
    #   QuietConsoleSink subclass. Tree mode iff (live if live is not None else
    #   console.is_terminal and not dumb) and not quiet — else every event is a QuietConsoleSink
    #   line and emit_stream is a no-op (non-TTY / --quiet degrade; --json: do not build one on
    #   stdout — JsonStdoutSink owns it; a stderr console is fine). display=False keeps the tree
    #   model without a Live display (tests / embedding). If Live cannot start, the sink degrades
    #   to quiet lines for the rest of the run (never silent, never raises).
    #   .view (RunView)   .render(height=None) -> tree + footer renderable (height = terminal
    #     rows to budget against, as the Live display does)   .render_summary() -> Panel
    #   .tail_lines (6, 20 with verbose, 0 = no tail)   .max_children (8)   .tree_enabled
    #   .live_enabled   .is_live
    #   .emit(): model update under anyio.Lock + threading.Lock (the Live refresh thread reads the
    #     model); malformed event data is coerced, model errors logged once (never raised); Live
    #     starts lazily on the first event, is redrawn from the model on Rich's timer (never per
    #     event), stops on run.finished (final frame stays, then the summary panel is printed
    #     once when summary=True); later events degrade to quiet lines
    #   .emit_stream(): feeds the running step's tail (text_delta/text de-duplicated, tool_call
    #     "⚙ name arg", tool_result "↳ …", command_start "$ …", stdout/stderr lines, file_change
    #     "✎ path", warning/error "! …"; reasoning/plan only with verbose); unknown/finished
    #     steps ignored
    #   .aclose(): stops the display (idempotent)
    #   await .pause() / await .resume()  — stop/restart the display around a prompt; calls nest
    #     (depth-counted); pause() before any event keeps the display off until resume(), which
    #     starts it iff any event was received (also while suspended) and the run is not finished
    #   async with .suspended(): ...    — pause()+resume() context manager for approval prompts
    RunView,  # in-memory model: run_id, workflow, status, finished, roots/nodes (StepView by
    #   path), warnings, pause (step, message), decision (approved, comment), workdir/branch,
    #   usage/cost_usd/outputs (from run.finished), tokens_total/cost_total (accumulated),
    #   cost_source (run.finished's, else .derived_cost_source folded from step_cost_sources +
    #   unpriced_steps — provider|table|partial|none); stream "warning" records are appended
    #   to .warnings as "<step>: <text>" (footer);
    #   .get(path), .apply(event), .apply_stream(path, record, verbose=), .elapsed_ms(node)
    StepView,  # path, name, kind, status, attempt, duration_ms, usage, cost_usd, error,
    #   skip_reason, tolerated, iteration (n, max) | item (index, total) for the synthetic
    #   container nodes (``build[2]`` / ``fix_all[0]``, status derived from the body steps),
    #   children, .label, .is_done, .is_container, .tail_lines()
    render_view,  # (view, *, height=None, max_children=DEFAULT_MAX_CHILDREN) -> renderable
    render_tree,  # (view, *, max_children=..., tail_limit=None) -> rich Tree
    render_summary, render_step_line, render_run_line,  # pure renderers
    DEFAULT_TAIL_LINES, VERBOSE_TAIL_LINES, DEFAULT_MAX_CHILDREN,  # 6 / 20 / 8
)
```
Rendering rules: every tree line is exactly one terminal row (no wrap, ellipsis; embedded
newlines/ANSI escapes in tails, errors, warnings and messages are stripped). Running leaves show
their tail under the step line; finished steps collapse to one line (``✓ id (kind) duration
[attempt n] [· tokens · cost] [(tolerated)] [— error]``); loop iterations / each items are
sub-trees (``iteration n/max``, ``item i/total``). Every finished, clean subtree (iteration,
item, loop/each/include composite) collapses to its one line; a subtree with a failed /
tolerated / interrupted / cancelled / rejected / paused descendant stays expanded. Per node only
running/problem children plus the last ``max_children`` finished clean ones are listed, the rest
become ``… +N more``. The approval gate shows ``‖ approval required: <message>``; warnings (last
5) and the ``‖ paused at … (rayspec approve/reject <run>)`` / ``● decision`` line go under the
tree. **Height budget** (``render_view(height=)``, the Live display passes
``console.size.height``): the footer is always kept; the tree gets ``height - footer`` rows —
tails shrink (full → 2 → 0), then the children cap (→ 2 → 0), then as a last resort the tree is
cropped from the top (run line, ``… N lines hidden``, most recent rows). Elapsed times come from
the injectable ``clock`` so tests call ``render()`` directly (no timers).

### workspace
```python
from rayspec.workspace import (
    prepare_workspace,  # (project_root: Path, *, home: Path, workflow_name: str, run_id: str,
    #   isolation: Literal["worktree", "none"] = "worktree", base: str | None = None,
    #   repo_arg: str | None = None, config: Config | None = None) -> Workspace
    #   raises WorkspaceError (unknown isolation / bad --repo / bad --base) or GitError
    prepare_workspace_async,  # same signature, runs prepare_workspace on a worker thread
    Workspace,  # frozen dataclass: isolation ("worktree"|"none"), project_root, workdir, branch,
    #   base_branch, base_sha, head_sha  (names identical to run.json's workspace block / the
    #   engine's dataclass) + extras: slug (project slug), notice (one console line or None),
    #   source_root (the bare source.git for --repo <url>, else None)
    workspace_lock,  # (workspace, *, home, run_id) -> PathLock  (not yet acquired)
    PathLock,  # (home, slug, workdir, *, run_id); .acquire(blocking=False) -> self | raises
    #   WorkdirLockedError("<workdir> is already locked by run X (pid Y)"), .release() (idempotent),
    #   .held, .holder() -> LockHolder | None, context manager; file
    #   <home>/projects/<slug>/locks/<sha1(resolved workdir)>.lock holds JSON
    #   {run_id, pid, workdir, started_at} while held, is truncated on release, never unlinked;
    #   the kernel drops it when the holder dies. NotImplementedError without fcntl (Windows).
    WorkdirLockedError,  # WorkspaceError: .workdir, .run_id, .pid (of the holder, when readable)
    # rayspec.workspace.lock.remove_lock_file(home, slug, workdir) -> bool: unlink the lock file
    #   when nobody holds it (non-blocking acquire first); False for missing/held/OS error
    WorkspaceError,  # RayspecError root of the layer; GitError(message, args, returncode, stderr)
    Project,  # frozen: root, slug, name, is_git
    find_project_root,  # (cwd=None) -> Path   git toplevel, else cwd (resolved)
    project_slug,  # (root) -> "host/owner/repo" from origin, else "local/<dir>-<sha1(abspath)[:8]>"
    normalize_remote_url,  # (url) -> slug | None   git@h:o/r.git, ssh://[u@]h[:port]/o/r, https://h/o/r
    project_from_root, discover_project, project_dir,  # project_dir(home, slug) = <home>/projects/<slug>
    create_worktree,  # (project, *, home, workflow_name, run_id, base=None) -> Worktree(path, branch,
    #   base_branch, base_sha, head_sha); path <home>/projects/<slug>/worktrees/<wf>-<shortid>,
    #   branch rayspec/<wf>-<shortid> (shortid = last '-' segment of run_id; the full run id on a
    #   name collision); `git worktree add --no-track -b`; base defaults to the current branch (HEAD
    #   when detached, base_branch=None)
    recreate_worktree,  # (project, *, path, branch) -> Worktree   resume: re-attach an existing branch
    list_worktrees,  # (project, *, merged_into=None) -> [WorktreeInfo(path, branch, head_sha, created_at,
    #   dirty, merged, prunable, locked; .age)]  rayspec/* branches only; merged = ancestor of
    #   origin/HEAD when it resolves (else HEAD; unborn HEAD → never merged); an unknown explicit
    #   merged_into is one WorkspaceError; created_at = mtime of the worktree's .git pointer /
    #   admin gitdir file (heuristic: `git worktree move|repair` resets it)
    remove_worktree,  # (path, *, delete_branch=True, force=False, repo=None, branch=None)
    #   force passes `--force --force` (dirty AND locked worktrees)
    clean_worktrees,  # (project, *, older_than: timedelta|None, merged_only, force, dry_run,
    #   merged_into=None, home=None) -> CleanReport(removed, skipped: [(info, reason)], dry_run);
    #   with home the lock file of every removed worktree is unlinked unless held (the CLI
    #   passes rayspec_home())
    #   SAFE BY DEFAULT: without force only merged+clean+unlocked worktrees are removed; skipped
    #   reasons: "younger than …", "not merged" (merged_only), "unmerged commits (use --force)",
    #   "dirty (use --force)", "locked (use --force)", or the GitError text of a failed removal
    #   (the loop continues; the report is always complete)
    parse_age,  # ("7d" | "12h" | "30m" | "45s" | "2w" | "1d12h") -> timedelta
    resolve_source,  # (arg, config, *, home, cwd=None, fetch=True) -> RepoSource(kind "path"|"url", arg,
    #   root, slug, name, url, base, project_name); order: explicit path form → registered name →
    #   git URL → bare existing dir → WorkspaceError
    ensure_bare_source,  # (url, *, home, fetch=True) -> <home>/projects/<slug>/source.git
    #   `git init --bare` + `remote add origin` once (NO local refs/heads/* snapshot), then
    #   `git fetch --prune origin` + `remote set-head origin -a` on every use; fetch refspec
    #   +refs/heads/*:refs/remotes/origin/* so origin/HEAD works and local rayspec/* branches
    #   survive pruning. Never checked out: always a worktree.
    remote_tracking_ref,  # (root, ref) -> "origin/<ref>" when refs/remotes/origin/<ref> exists in the
    #   bare source, else ref unchanged; applied to the registered base and to --base for URL sources
    #   so `base: main` always means the freshly fetched origin/main (base_branch == "origin/main")
    is_git_url,
)
from rayspec.workspace.registry import add_project, list_projects, remove_project  # <home>/config.yaml
from rayspec.workspace import git  # run_git(args, cwd, *, check=True, env=None, timeout=None) -> GitResult,
#   current_branch, rev_parse, ref_exists, branch_exists, is_dirty, remote_url, remote_default_branch,
#   fetch_prune, is_ancestor, toplevel, common_dir, is_git_repo  (all sync; GitError on failure)
```
Semantics fixed here (tests in `tests/workspace/`):
- Default isolation is a worktree for every git project; `isolation="none"` runs in place
  (`workdir == project_root`, `branch`/`head_sha` filled, `base_*` None); a non-git directory
  always runs in place with `notice` set. `--repo <url>` forces a worktree (notice when
  `isolation="none"` was requested) and `project_root == workdir` (workflows load from the
  checkout); `--repo <path>` makes that path the project root; `--repo <name>` resolves
  `config.projects` (its `base` applies unless `--base` is given). Base precedence:
  `--base` > registered base > current branch (cwd/path) or `origin/HEAD` (url); for URL sources
  a bare branch name is mapped to `origin/<name>` (the fetched tip), never a local copy.
- Worktrees are kept after the run (the engine prints the branch); `rayspec worktrees
  list|clean [--older-than 7d] [--merged] [--merged-into REF] [--force] [--dry-run] [--json]
  [--root] [--repo]` removes with `git worktree remove` + `git branch -D`; unmerged (committed
  work), dirty and locked worktrees need `--force` — a bare `clean` never deletes unmerged work.
- A git repo without commits: worktree isolation raises `WorkspaceError("… has no commits yet")`
  (hint: commit or `--no-worktree`); in place it runs with `head_sha=None`. A `base` given for
  an in-place run (`isolation="none"` / non-git dir) is ignored and reported in `notice`.
- `rayspec projects add <name> <source> [--base]` (update in place when the name exists) |
  `list [--json]` | `remove <name>` rewrite `projects:` in `<home>/config.yaml` (other keys kept,
  comments not); local sources are stored absolute and must exist; names match
  `[A-Za-z0-9][A-Za-z0-9._-]*`.
- The engine owns lock lifecycle: `Runner(home=)` acquires `PathLock(home, slug, workdir,
  run_id=)` non-blocking before the run record is touched (every CLI entry point passes its
  home), releases it on every final status (pause included) and takes it again on resume; a held
  lock is an `EngineError` (exit 2) naming the holder. Pure dry runs (no `--exec-shell`) and
  platforms without `fcntl` run unlocked. `prepare_workspace` does not lock.
- Permissions (the same rule as the store's): every directory the workspace layer creates under
  `$RAYSPEC_HOME` — `projects/<slug>/worktrees/`, the parent of `source.git`, the home of
  `config.yaml` — is `0700` and `config.yaml` is written `0600` (`rayspec.store.file.secure_mkdir`
  / `open_private` — the one store import the workspace layer makes, shared with `lock.py`);
  pre-existing directories are never chmodded. What git creates (the worktree checkout, the
  bare `source.git`) keeps git's modes (the checkout is the user's own tree, meant to be used).
- A project root below the git top level (`packages/foo/.rayspec`): the worktree checks out the
  whole repository and `Workspace.workdir` is `<worktree>/<relative path>`; `project_root` stays
  the sub-directory (workflows load from there).

### engine — secret inputs and recorded stubs
Additive: `RunOptions.stubs_path: str | None`; `RunContext(...,
secret_inputs=)` + `RunContext.secret_inputs` (the real values; never in a template context,
record or event), `RunContext.secret_env(scope) -> {RAYSPEC_INPUT_<NAME>: value}` (every secret
recorded as `<secret>` in the ROOT record inputs `run.inputs` — the same in every scope, include
bodies included; `scope` is not consulted) and `RunContext.secret_context(scope)` (the template
context with the real values in place of the `<secret>` placeholders — used ONLY to render a
shell/python step's `env:` mapping; substitution in the root scope only, `scope.parent is None`);
`Runner` splits `inputs` into public + secret (`Runner.secret_inputs`), records `inputs` redacted
(`redact_inputs`) + `secret_inputs` + `stubs_path`, and on resume raises `ResumeError("missing
secret input(s): …")` when a secret recorded as `<secret>` is not supplied, records an optional
secret that was not given at launch but is supplied now as `<secret>` in `run.inputs` (so it is
exported from then on), updates `stubs_path` when `RunOptions.stubs_path` is given; root `ExecScope.inputs` are the redacted record inputs
(also on a fresh run). `executors._process.process_env(step, ctx, tctx, rendered, path, *,
scope=None)`: with `scope`, secrets are exported and the `env:` mapping rendered with
`secret_context`; `context.json`, `export_env` and the shell/python **fingerprints** see the
placeholder (the fingerprint hashes the env rendered from the redacted context — a cached step
is replayed whatever secret value is supplied on resume).

### providers/claude.py
```python
from rayspec.providers.claude import (
    ClaudeProvider,  # (settings: Mapping | None = None); id "claude", capabilities is CLAUDE_CAPABILITIES
    #   settings keys: "setting_sources" (list of user|project|local; default ["project"]; null = all),
    #                  "cli_path" (explicit claude binary; default bundled → PATH → known locations),
    #                  "env" (extra subprocess env; precedence: CLIENT_APP < settings.env < open(env) < req.env)
    #   construction does os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1") and raises
    #       ProviderError(kind="provider", hint names providers.claude.<key>) for malformed settings
    #   .open(run_id, workdir, env, max_parallel) just records them (env merged under req.env); aclose() no-op
    #   .run(req, emit): one query() subprocess per call, under contextlib.aclosing + anyio.fail_after(req.timeout_s)
    #   .healthcheck(probe=False): sdk_version, cli_path, cli_version (`claude -v`, 5 s per attempt, one
    #       retry on timeout → up to 10 s when the CLI hangs; an OSError such as ENOENT is not retried;
    #       None only when no attempt reports a version), auth "ok" iff
    #       ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN set OR cli_login_source() finds the claude CLI's
    #       own login, else "unknown" (never "missing"); ok iff the CLI
    #       is found AND reports a version (AND the probe passes); probe=True = 1-turn "Reply with
    #       exactly OK" (tools=[], max_turns=1, dontAsk, setting_sources=[]); never raises
    cli_login_source,  # () -> str | None  "claude.ai login (~/.claude/.credentials.json)"
    #   when <cli_config_dir()>/.credentials.json exists, "claude.ai login (macOS keychain)" when
    #   `security find-generic-password -s "Claude Code-credentials"` exits 0 (Darwin only, 5 s bound,
    #   no -w/-g: existence only, never the secret; no `security` / timeout / OSError → None)
    cli_config_dir,  # () -> Path  $CLAUDE_CONFIG_DIR or ~/.claude
    build_options,  # (provider, req, stderr_cb, *, run_env=None) -> (ClaudeAgentOptions, ToolTranslation)
    #   raises ProviderError (non-transient) for a missing cwd or an untranslatable tool policy
    find_cli,  # () -> str | None   bundled claude_agent_sdk/_bundled/claude → shutil.which → known paths
    #   (claude.exe + the SDK's Windows location list on Windows)
    cli_version_of,  # async (cli_path, *, timeout_s=CLI_VERSION_TIMEOUT_S (5.0), retries=CLI_VERSION_RETRIES (1))
    #   -> "x.y.z" | None (None only when every attempt times out / fails / prints no version)
    SDK_VERSION, CLI_BUNDLED_VERSION, CLI_VERSION_TIMEOUT_S, CLI_VERSION_RETRIES, TRANSIENT_API_STATUSES,
    STDERR_TAIL_LINES, DEFAULT_SETTING_SOURCES,
    ADAPTER_OWNED_OPTIONS, MERGED_OPTIONS, VALID_SETTING_SOURCES,
)
```
Option mapping: `instructions_mode: append` → `system_prompt={"type":"preset","preset":"claude_code","append":…}`,
`replace` → bare string (vanilla Claude; CLAUDE.md still via `setting_sources`); `read-only` →
`tools=["Read","Glob","Grep"(+WebFetch/WebSearch iff `web` allowed)]`, same `allowed_tools` (+ allowed `mcp__*`),
`permission_mode="dontAsk"` (allowed names not representable there — Bash/Edit/Agent/… — are dropped with a `warning`
event); `workspace-write` → `acceptEdits`, `allowed_tools=["Bash",*web,*explicitly allowed native names,*mcp]`,
`tools` untouched;
`full` → `bypassPermissions`; `tools.deny` → `disallowed_tools` in every mode; bare `mcp` → `mcp__<server>` over
`req.mcp_servers`; `strict_mcp_config=True` iff servers given; `thinking: True/False` → `{"type":"adaptive"}` /
`{"type":"disabled"}`; `effort` through `CLAUDE_CAPABILITIES.effort_aliases`; `output_schema` →
`output_format={"type":"json_schema","schema":…}` (the engine validates `AgentResult.structured`);
`resume_session`/`fork_session` → `resume`/`fork_session`; `include_partial_messages=True`; `stderr=` keeps the last
40 lines (in `ProviderError.hint` and `AgentResult.raw["stderr_tail"]`); `provider_options`: keys in
`ADAPTER_OWNED_OPTIONS` (`stderr cwd cli_path resume fork_session output_format include_partial_messages`) are
ignored with a warning event; `MERGED_OPTIONS` (`env`, `mcp_servers`) are merged UNDER the computed mapping (env
precedence: CLIENT_APP < settings.env < provider_options.env < open(env) < req.env; `req.mcp_servers` win on name
collision); every other `ClaudeAgentOptions` field is applied verbatim (unknown keys → warning event).
Events: init → `session` (data session_id/model/tools/cwd/permission_mode); `text_delta` / `reasoning` from
StreamEvent deltas; AssistantMessage `TextBlock`/`ThinkingBlock` → `text`/`reasoning` only when nothing was
streamed for that message; `ToolUseBlock` → `tool_call(name, call_id, data=input)`; `ToolResultBlock` →
`tool_result(call_id, name, text, data.is_error)`; `parent_tool_use_id` → `nested`; `<synthetic>` assistant
error → `error` event (not output); RateLimitEvent / permission_denials / tool warnings → `warning`; other
SystemMessage subtypes → `raw(name=subtype)`.
Result: status `aborted_*` → interrupted, `error_max_turns` → max_turns, `error_max_budget_usd` → budget,
`is_error` → error, else success; `timeout` (own deadline, no result frame seen) with partial text = last complete
assistant text + in-flight streamed deltas; a deadline that fires AFTER the result frame (slow shielded transport
teardown) folds the result normally and sets `raw["teardown_timed_out"]=True`; `AgentError.transient` iff
`api_error_status ∈ {408,409,429,500,502,503,504,529}` or synthetic `rate_limit|server_error`, never for
`authentication_failed` (kind auth) / `billing_error` (budget) / `invalid_request` (api); usage `input =
input_tokens + cache_read + cache_creation`, `cached_input`, `cache_write`, `output`; `cost_usd =
total_cost_usd` (`cost_source="provider"`, else `"none"`); `model` = max-`outputTokens` entry of `model_usage`,
else the last assistant/init model; `session_ref = session_id`. Infrastructure: `CLINotFoundError` →
`ProviderNotInstalledError`; `CLIConnectionError` (cwd missing, spawn failure) → `ProviderError(kind="transport",
transient=False)`; `ProcessError`/`CLIJSONDecodeError`/stream without result → transient transport error; other
`ClaudeSDKError` → `ProviderError(kind="provider", transient=False)`; any other `Exception` the SDK lets out of
`query()` (bare `Exception("Control request timeout: …")`, `RuntimeError` from resume materialization) →
`ProviderError(kind="transport", transient=True)`; cancellation is never caught. `ResultError` (a `ProcessError`
subclass, caught first): the error `ResultMessage` is normally yielded before it is raised and is folded as above;
a `ResultError` with NO prior `ResultMessage` (CLI error result while `initialize` is still in flight, e.g. a
refused `--resume`) is folded from `ResultError.data` into an `AgentResult(status="error")` (same classification:
non-transient unless `api_error_status`/synthetic say otherwise) — it is NOT a `ProviderError`, because the payload
is the CLI's real result and retrying would not help.

### engine — the runner
Uses: `RunStore`, `EventSink`, `Provider` (via registry), `TemplateEngine`, `ResolvedWorkflow`.
Exit codes: 0 succeeded · 1 failed · 2 usage error · 3 paused · 4 cancelled · 130 interrupted.

```python
from rayspec.engine.runner import (
    Runner,  # Runner(resolved, *, inputs, store, project_root, project_slug=None, project_name=None,
    #   sinks=None (NullSink), workspace=None (in place), options=None (RunOptions()),
    #   approval_prompt=None, engine=None (TemplateEngine()), providers=None ({id: Provider}
    #   injected instances, e.g. a StubProvider under "claude"), env=None (os.environ),
    #   run_id=None (new_run_id()), resume_run_id=None, price_table=None (PriceTable),
    #   handle_signals=True, executors=None ({kind: executor fn} overrides for tests),
    #   home=None (rayspec home: when given the workdir PathLock is held for the run))
    #   .run() -> RunResult (async; raises ResumeError before anything starts)
    #   .run_sync() -> RunResult   (anyio.run(..., backend="asyncio"))
    Workspace,  # dataclass(isolation="none", workdir=Path.cwd(), branch, base_branch, base_sha,
    #   head_sha); Workspace.in_place(root); .info() -> store.model.WorkspaceInfo. Built by the
    #   CLI (rayspec.workspace.prepare_workspace when importable, else in place with a notice).
    #   head_sha = tip of the workdir at the last record write: Runner._refresh_head_sha
    #   re-reads HEAD (rayspec.workspace.git.rev_parse, lazily) at pause, run end and resume
    #   start for git workdirs (branch or head_sha recorded); base_sha never changes.
    RunResult,  # run_id, status: RunStatus, exit_code, run_dir, workspace, outputs (rendered
    #   workflow outputs or None), reason, usage: Usage, cost_usd, cost_source ("provider" |
    #   "table" | "partial" | "none"), steps: {path: StepRecord},
    #   reused: [paths replayed from the resume cache], pause: PauseInfo | None, interrupted, record
    fallback_project_slug,  # (root) -> "local/<dirname>-<sha1(abspath)[:8]>"
)
from rayspec.engine.context import (
    RunOptions,  # dry_run, exec_shell, yes, interactive=True, fail_fast, force, resume,
    #   stub_script (StubScript | dict; dry run / --stubs), provider_settings ({id: settings})
    #   fail_fast is the --fail-fast FLAG only. The scheduler reads two derived properties:
    #     RunContext.fail_fast  = options.fail_fast OR defaults.on_step_failure == "fail_fast"
    #     RunContext.keep_going = defaults.on_step_failure == "continue" AND NOT options.fail_fast
    #   The flag may only ever TIGHTEN: it enables fail-fast and beats "continue", and never
    #   downgrades a workflow that asked for fail_fast. "drain" = 1.0.0 behaviour.
    #   keep_going relaxes draining caused by a FAILURE only — a pause/stop still halts new work,
    #   the failed step's dependents still skip (upstream_failed is decided before draining in
    #   join_decision), and the run still ends FAILED. It is GLOBAL: run_graph runs every sibling
    #   list, so the policy also applies inside each:/loop:/include: bodies (tested).
    #   NOT the same knob as each.on_failure: continue, which is per-ITEM (does a failed item
    #   fail the each step?) — see docs/schema.md under `each:`.
    #   DESIGN RULE (deliberate, not inherited from v1.0.0): the --fail-fast
    #   flag may only ever TIGHTEN. Revisit deliberately if that ever feels wrong.
    RunContext, ExecScope, StepOutcome, ProviderPool,  # internals shared by scheduler/executors
    cost_source_of,  # (records) -> "provider" | "table" | "partial" | "none" (pinned seam):
    #   none = no record has a cost; partial = some record has tokens but no cost (the sum is a
    #   lower bound, rendered "≥$"); table = an estimate is in the sum and nothing is unknown
    #   ("~$"); provider = every record with tokens reported a provider cost ("$"). Records
    #   without tokens and cost (shell/python/skipped) do not count.
    totals_of,  # (records) -> (Usage, cost_usd | None, cost_source); RunContext.run_totals()
    #   applies it to every record of the run (run.json cost_source, run.finished, RunResult),
    #   RunContext.budget_totals() to the accounted ones
)
from rayspec.engine.approval import (
    ApprovalPrompt,  # Protocol: async __call__(ApprovalRequest) -> ApprovalAnswer | None (None = pause)
    ApprovalRequest,  # run_id, step_path, message, attempt, workdir, needs: [ApprovalNeed], totals
    ApprovalAnswer,  # (approved: bool, comment: str = "")
    ApprovalNeed,  # path, status, duration_ms, cost_usd, tail (last 15 output lines), output (full
    #   text, capped at 200k chars, shown by [v]iew), cost_source ("none"|"provider"|"table";
    #   additive)
    ConsoleApprovalPrompt,  # the Rich implementation the CLI injects: panel (message, needs, git
    #   status --short / git diff --stat of the workdir, totals) + keys [a]pprove/[r]eject/[v]iew/
    #   [d]iff/[p]ause; git information is best effort (no repo / no git ⇒ omitted).
    #   every answer goes through clean_answer() (CSI/SS3/ESC sequences and control chars
    #   stripped — arrow keys never corrupt the key or the comment; a line that is only escape
    #   noise re-asks), enable_readline() imports readline best effort on a real TTY (stdin AND
    #   stdout) before the first prompt; durations render via humanize_duration (1.2s · 31m 52s ·
    #   1h 3m), costs via fmt_cost ($0.50 / ~$0.50 for table estimates / "—" unknown), the totals
    #   line via format_totals ("steps: 3 · tokens: 12.3k tok · cost: —"; the executor passes
    #   totals {steps, tokens, cost_usd, cost_source}) — never a raw None or raw seconds
    clean_answer, enable_readline, humanize_duration, fmt_cost, format_totals,
    git_summary, git_diff,  # (workdir) -> str, best effort, capped; used by the console prompt
)
from rayspec.engine.runtime import (
    Runtime,  # (max_parallel): leaf_limiter, leaf_permit() (gate + slot + active count),
    #   close_gate()/open_gate()/wait_quiesced()/active_leaves — approval quiesce;
    #   approval_lock (anyio.Lock): simultaneous gates quiesce/prompt/pause one at a time
    run_with_signals,  # (body, *, on_hard_exit=None, hard_exit=os._exit, handle_signals=True)
    #   -> SignalResult(value, interrupted, signal): first SIGINT/SIGTERM cancels the root scope,
    #   second SIGINT → on_hard_exit() + hard_exit(130); no-op outside the main thread
    configure_default_executor, default_executor_workers,  # max(32, 2*max_parallel+8); the pool
    #   installed on a loop is reused by later runs on that loop (or shut down when too small)
    installed_default_executor,  # (loop) -> ThreadPoolExecutor | None
    exit_code_for, EXIT_CODES, EXIT_SUCCEEDED, EXIT_FAILED, EXIT_USAGE, EXIT_PAUSED,
    EXIT_CANCELLED, EXIT_INTERRUPTED,  # 0 1 2 3 4 130
    unwrap_exception_group,
)
from rayspec.engine.graph import StepGraph, join_decision, JoinDecision  # the join truth table
from rayspec.engine.scheduler import run_graph, run_one, run_leaf, try_reuse, finish
from rayspec.engine.structured import run_structured, extract_json, validate_value
from rayspec.engine.errors import EngineError, GraphError, ResumeError, RunStopped, RunPaused
from rayspec.engine.executors import default_executors, fingerprint_of
#   executor signature: async (step, scope: ExecScope, ctx: RunContext, record: StepRecord,
#   attempt: int) -> StepOutcome; modules: prompt, shell, python, stop, approve, loop, each, include
```
Semantics fixed here (tests in `tests/engine/`):
- Interrupted / timed-out prompt attempts: the prompt executor wraps the adapter's `emit`
  in `executors.prompt.UsageTracker` — a `usage` AgentEvent's `data["turn_total"]` (cumulative
  usage of the attempt; Codex per `thread/tokenUsage` update, Claude per completed assistant
  message) or, without it, the summed `data["usage"]` deltas is what the attempt records when it
  is cancelled (Ctrl-C, sibling stop/pause, the per-attempt deadline), raises a `ProviderError`,
  or ends `timeout`/`interrupted` without a result usage; no report at all ⇒ `usage_unknown`
  for the cancelled / timed-out / `interrupted` paths only — a raised `ProviderError` without a
  prior usage report (auth failure, CLI missing, a 429 the SDK raised before a token was billed)
  records zero usage and is NOT `usage_unknown`. `run_leaf` starts an attempt (attempt count,
  per-attempt usage/cost reset) only once the leaf permit is held: a leaf cancelled while it
  queues for a `max_parallel` slot / the launch gate keeps the totals its record carries and
  does not count as an attempt. Every started attempt (including the cancelled one and the
  attempts of earlier runs, which `RunContext.new_record` carries over from the cache) is
  folded into the step totals — usage and cost are summed over every attempt across resumes.
  The `rayspec run` footer prints `tokens: ≥N (usage of n steps unknown)` when a record is
  `usage_unknown`.
- Step records: `skip_reason` ∈ `upstream_skipped | upstream_failed | run_failed | when_false |
  stopped | paused | failed | interrupted | budget_exceeded` (the last one = `context.
  BUDGET_SKIP_REASON`); interrupted siblings of a `stop:`/reject carry
  `skip_reason: stopped`; a composite whose body stopped is `interrupted` (`stopped`), whose body
  paused is `paused`. Control signals raised by several `each:` items concurrently collapse into
  one (first wins, a pause beats a stop; the other items are cancelled with reason
  `stopped`/`paused`) — never a failed composite.
- Approval: simultaneous gates are handled one at a time (`Runtime.approval_lock`); when a run is
  already pausing, a later gate is recorded `paused` too but `run.pause`/`run.paused` belong to
  the first gate (the later one asks again on resume). Ctrl-C at the prompt = pause: the gate
  record stays `paused` (never `interrupted`), run status `paused`, exit 3. `step.finished` data gains `reused: true` on a resume replay, `dry_run: true`
  for skipped shell/python, `cost_source` when not `none`.
- `run.started`/`run.resumed` data: `{workflow, dry_run, resume_count, workdir}`; `run.finished` adds
  `outputs` and `cost_source` (when not `none`); `run.decision` data `{approved, comment, by, step_path}` with `by` ∈ `--yes | dry-run |
  tty | <Decision.by>`; `warning` data `{message}`.
- Resume: only `prompt/shell/python/approve` records are replayed (composites re-run their bodies);
  `stop` always re-runs; `ResumeError` on hash mismatch without `--force`; a leaf whose
  `fingerprint` changed re-runs (warning event) on **every** resume, hash mismatch or not (an
  interrupted forced resume has already stamped the new hash). Inputs come from `run.json` on
  resume. A run whose `run.json` says `running` with a live `pid` on this host — or recorded on
  another host — is refused (`ResumeError`, hint `rayspec cancel <run>` / `--force`); a run of
  another workflow is always refused (`--force` never crosses workflows). Leaf fingerprints are stable for spilled (> 64 KiB) values
  (the spill path is replaced by a digest of its content).
- Dry run: shell/python steps succeed with `''` (or the minimal `output_schema` instance) unless
  `--exec-shell`; every provider id maps to the stub (`RunOptions.stub_script`); gates auto-approve.
- Approval token = `<path>#<attempt>`; a stored `pause.decision` is consumed only when
  `pause.step == path and pause.token == token` (the gate then does not bump `attempts`).
- `RunRecord.pid` is cleared on every final status except `paused`; `RunRecord.pid_started_at`
  (additive) is set with `pid` by `Runner._prepare_record` — fresh run and resume — from
  `engine.runner.process_start_time(os.getpid())` (`ps -o lstart=` under `LC_ALL=C TZ=UTC`, else
  `/proc/<pid>/stat` starttime; computed by `Runner.run` in a worker thread) and left in place
  afterwards (it describes the
  pid that last ran). `run.pause` is cleared when the
  gate that owns it reaches a decision by any path (stored decision, `--yes`, dry run, TTY), so
  `RunResult.pause` is only non-None for a run that is `paused`. `RunRecord.dry_run` (additive)
  records `--dry-run`.
- Declared `artifacts:`: `executors.artifacts.collect_artifacts(step, scope, ctx, outcome)` runs
  in `scheduler._execute` after the executor (`_dispatch`) and before `finish`, for EVERY kind.
  It is a no-op unless the step declared artifacts and SUCCEEDED in this run (a replayed record
  keeps its recorded artifacts; a dry run checks nothing — nothing was produced). Each declared
  path is resolved against the step's working directory (`artifact_dir`: the rendered `cwd:` of
  a shell/python step, else `ctx.workdir`); duplicates are collapsed (first occurrence wins,
  order preserved); a file that is missing, is not a REGULAR file (a
  directory, a FIFO, a socket, a device node — `Path.is_file()`, which resolves symlinks),
  resolves outside that directory (a planted symlink) or outside `ctx.workdir` (the run's
  workspace — `cwd:` is rendered at run time and may name any directory, so the workspace is
  what makes the "a file the step wrote in its own workspace" promise true)
  turns the succeeded outcome into `failed` with
  `ErrorInfo(type="artifact")` naming the path — never a retry (the leaf loop is already over)
  and the step's own output is kept. The kept files go through
  `RunContext.write_artifacts(record, [(declared, path)])` → `store.write_artifact` in a worker
  thread under the persistence lock, before `run.json` names them; a store without the method
  (or an `OSError` while copying) records the artifact with `ref=None` plus a `warning` — the
  promise was kept, so it is not a step failure.
- Sinks are observers: if a sink raises (`BrokenPipeError`, Rich's `SystemExit` after `| head`)
  `RunContext.emit` drops the sinks (`ctx.sinks_broken`) and the run continues; the store still
  receives every event and the run status is unaffected.
- `each:` accepts any iterable that is not `str`/`bytes`/`Mapping` (`.values()`, `.items()`,
  `range`, generators).
- `timeout:` on `approve:`/`stop:` steps is a validation error (it would be ignored); include
  bodies are validated as closed lexical scopes (no outer steps/`iteration`/`each`), as the
  engine runs them.
- Shell/python: `RAYSPEC_CONTEXT` (steps/<path>/context.json) and `RAYSPEC_STEP_PATH` are exported in
  addition to `export_env`; the process group is SIGTERM→(2 s)→SIGKILL'ed on cancel/timeout.
  The child env is `scrub_launcher_env(ctx.env)`: `LAUNCHER_ENV_VARS`
  (`VIRTUAL_ENV VIRTUAL_ENV_PROMPT UV_PROJECT_ENVIRONMENT PYTHONHOME`) and every variable whose
  value is a path inside rayspec's own venv (`launcher_venvs()` = `sys.prefix` when it is a venv +
  `$VIRTUAL_ENV`) are dropped — an `os.pathsep` list value (`PYTHONPATH`) loses only the entries
  inside the venv and is dropped only when none is left; `PATH` untouched — before `RAYSPEC_*`,
  the step `env:` and the slots are applied, so a step's explicit `env:` value always wins.
  `stdout.log`/`stderr.log` hold every attempt: attempt 1 starts the file, later attempts (retries,
  resumes) append under a `--- attempt N ---` line.
- Persistence: `run.json` saves and output files (fsync) are written in a worker thread under one
  `anyio.Lock` (ordered); events/streams (flush only) are appended inline.
- `when:` on a skipped upstream (a behaviour change after 1.0.0): `steps.x.output`
  **and** `steps.x.ok` both fail the step with the same guard hint ("step 'x' was skipped
  (when_false) — guard with `steps.x.status == 'succeeded'`"). A skipped step never answered, so
  `ok` is undefined rather than `False`; guard with `steps.x.status == 'succeeded'` or keep the
  old reading with `steps.x.ok | default(false)`. `steps/<path>/context.json` records `ok: null`
  for such a step. Deliberately narrow: only `SKIPPED` is treated as "never answered" — a
  `PENDING`/`RUNNING`/`PAUSED` step still resolves `ok` to `False` (pinned by
  `tests/templating/test_skipped_ok.py`). The same argument applies to those statuses; extending
  the rule is a separate change, not an oversight of this one.
- Run-level circuit breaker: `RunContext.check_budget(pending=None) -> reason |
  None` is called by `scheduler.finish` after every leaf outcome (fresh or replayed; the record
  path is added to `ctx.accounted_paths` first) and by `run_leaf` between attempts (with the
  in-flight record as `pending`). It measures `ctx.budget_totals()` — usage/cost/cost_source over
  the records accounted in THIS run (stale cache records of a resume do not count until they are
  replayed) — against the ROOT `defaults.budget_usd` (provider cost or table estimate; unknown
  cost = 0) / `defaults.max_tokens` (`Usage.total`) via `context.budget_reason(usage, cost,
  source, defaults)`; strictly greater trips. The first trip sets `ctx.budget_exceeded = "budget
  exceeded (cost ~$0.004 > budget_usd $0.003, tokens 2,000 > max_tokens 1,500)"` and emits one
  `warning`. While tripped: `run_graph.decide_and_launch` decides every pending step of every
  graph once its `needs` are terminal — `join: always` steps run as usual (drain semantics:
  nothing new except `join: always`, whatever their kind), any other step is REPLAYED from the
  resume cache when `try_reuse` accepts its record (a replay is free; a same-cap resume must not
  overwrite finished records) and otherwise recorded `skipped`/`budget_exceeded` (no new leaf,
  composite or gate starts; running steps drain); `run_leaf` does not retry a failed attempt,
  `run_loop` starts no further iteration (failed, error type `budget`), `loop.failed_body_step`
  treats a budget-skipped body step as a failing one (loop/each/include fail with
  `body_failure_message` = error | skip reason | status). `Runner._finalize`: after engine error /
  interrupted / paused / stopped, `ctx.budget_exceeded` ⇒ status `failed`, reason = the breaker
  reason, exit 1. The breaker outranks `ctx.stopped`: a capped run keeps draining, so it reaches
  a `join: always` `stop: {status: succeeded}` — and it must not report success (or publish
  `outputs:`) to its caller. Ranked below `paused`, which is not a final status. Resume replays count again (same cap ⇒ trips at once, finished steps stay
  replayed); raising the cap changes the hash ⇒ `--force`, leaves are reused (fingerprints exclude
  defaults).
  `rayspec plan` prints `budget_usd $X  max_tokens N` after the isolation and adds `budget_usd` /
  `max_tokens` to its `--json` payload.
- Wall-clock breaker: `defaults.timeout_total` joins the same circuit breaker.
  `RunContext.elapsed_s()` is `utcnow() - RunRecord.started_at` (the ORIGINAL start — a resume
  entry keeps it, so the cap measures the run, not the attempt, waiting at an approval gate
  included); `context.time_reason(elapsed_s, defaults)` renders `time limit exceeded (elapsed
  2h 4m > timeout_total 2h 0m)` (`engine.approval.humanize_duration` for both sides, strictly
  greater trips). `check_budget` evaluates the cost/token caps first and the clock second, so
  one reason wins and everything downstream (`ctx.budget_exceeded`, `BUDGET_SKIP_REASON`,
  the loop/each drain, `Runner._finalize` → `failed` + exit 1) is unchanged; the warning hint
  names the knob that tripped. `scheduler.finish` asks the breaker after EVERY step when
  `ctx.time_capped` (a shell-only run reports no usage), and `Runner.run` asks it once before
  the graph starts so a resumed run whose clock already expired starts nothing.
- The breaker is asked at TWO points, and both are load-bearing: when a step becomes ready
  (`run_graph`) and again inside `run_leaf` once the leaf holds its `max_parallel` permit
  (`join: always` exempt). The second one is what makes "no new step starts" true for a step
  that was ready before the cap tripped and then queued for a slot; it ASKS `check_budget`
  rather than reading `ctx.budget_exceeded`, because the wall clock can run out while nothing
  finishes. A first attempt is recorded `skipped`/`budget_exceeded` (`attempts` stays 0); a
  retry that loses the permit race keeps the failed outcome it already has.

### CLI `run` — `rayspec run <workflow> [--input k=v]* [--inputs-file f] [--root]
[--dry-run] [--stubs f] [--stubs-init f] [--exec-shell] [--yes] [--no-interactive] [--json] [--quiet]
[--verbose] [--allow-unsupported] [--fail-fast] [--resume <run-id|prefix>] [--force]
[--worktree/--no-worktree] [--base <branch>] [--repo <url|path|name>]`. Store root =
`$RAYSPEC_HOME/projects/<slug>` (slug from `rayspec.workspace.project_slug` when importable, else
`fallback_project_slug`; with `--repo` the slug the prepared workspace reports — the source's
slug, i.e. the bare clone's project dir for URL sources). `--json` prints JSONL events on stdout and a final summary object
(`run_id, status, exit_code, reason, outputs, usage {input, cached_input, cache_write, output,
reasoning}, cost_usd, cost_source, run_dir, workspace, pause`; `cost_source` ∈ `provider|table|partial|none`
is the run-level cost seam, additive). `--stubs` without `--dry-run` is allowed only
when every resolved agent of a prompt step is `provider: stub` (a real run of stub agents with
scripted answers; `run.py::non_stub_agents` names the offenders in the exit-2 error, hint
"pass --dry-run, or switch the agents to provider: stub"); `--stubs-init` writes run-time keys
(`build[*]/implement` globs for loop/each bodies, `block/step` for includes; `run.py::
stub_scaffold_keys`) and refuses to overwrite without `--force`; `--repo` + `--resume` is
a usage error. Text mode prints the `warnings:` block on stderr like `--json`.
`--stubs` records `str(path.resolve())` as `RunOptions.stubs_path` /
`RunRecord.stubs_path`; `run.py::load_stub_script(path, *, hint=None)` and
`run.py::refuse_stubs_for_real_agents(rw, *, dry_run, record=None)` are the shared loaders
(`load_stub_script -> StubScript`; with `record` the refusal names the recorded file: `run <id>
was launched with --dry-run --stubs <path>; its recorded stubs file requires --dry-run (…)`, hint
`pass --dry-run to resume it as a dry run (rayspec resume does so automatically), or switch the
agents to provider: stub` — `run --resume` keeps its own flags, it does NOT inherit `dry_run`); `--resume` accepts
`--input` for secret inputs only and reuses the recorded stubs file when `--stubs` is absent
(`resume.py::resume_secret_inputs(record, resolved, cli_pairs)` / `resume.py::resume_stub_script(
record, resolved, *, stubs, dry_run) -> (script, abs_path | None)` — exit 2 before anything is
written). `plan` rows gain `secret: bool` and print `<secret>` + `(type, secret)`; `validate`
prints `secret inputs: <names> (secret; env-only, never persisted)` and its `--json` row gains
`secret_inputs: [...]`.
Text mode: the console sink prints the final `■ run <id> <status>` line (once); the CLI summary adds
the outputs table, the worktree block (`run.py::worktree_lines`: `worktree: <path> (branch
<b>, checked out there)` + `hint: cd <path> · rayspec worktrees list|clean · git worktree remove
<path>`, soft-wrapped so paths stay whole), the `decide with: rayspec approve|reject|resume`
hint while paused, a `tokens: … · cost: … · run dir: …` footer (`cost:` renders the run-level
source, `$0.12` provider · `~$0.12` table · `≥$0.12` partial · omitted when none;
`run.py::cost_label`)
and a `rayspec logs <id> --step <path> (+n more) · rayspec resume <id>` hint after
failure/interrupt where `<path>` is a failed/interrupted LEAF (prompt/shell/python — the step
named in the run reason first; never a composite; `run.py::failed_leaf_paths`);
replays print `↺ <step> reused`.
`--quiet` prints only run-level lines, warnings, retries and non-green step finishes (failed,
tolerated, interrupted, paused…). The summary object is the **last stdout line** (after the
`run.finished` event) so `rayspec run … --json | tail -1 | jq .exit_code` works; Rich
console lines go to stderr. The Rich Live tree (`ConsoleSink`, see the events section) is wired in:
`run.py::_sinks` builds `ConsoleSink(console, verbose=verbose, summary=False)` unless
`--quiet`/`--json` (it auto-degrades to one line per step on a non-TTY; with `--json` only the
`JsonStdoutSink` is on stdout); the CLI keeps printing its own text summary (run dir, approve hint,
reused count) and the sink never prints its panel. `run.py::approval_prompt_for(sinks,
interactive=, prompt=None)` returns `None` (pause at gates) or a `SuspendingApprovalPrompt` that
runs the `ConsoleApprovalPrompt` inside `async with sink.suspended():` for every sink exposing
`suspended()`; `rayspec run` and `_runs_common.resume_run` (`resume`/`approve`/`reject`;
additive kwargs `inputs=` (re-supplied secrets), `stub_script: StubScript | None =`,
`stubs_path=`; the resumed run inherits `dry_run` from the record) both use
it. `print_summary`'s outputs table and `_loader_common.fail()` render run data as `rich.text.Text`
(never markup: `[stub] think` stays literal) and through
`rayspec.textsafe.safe_text` (ESC/CSI/OSC sequences and C0/C1 control characters stripped;
`safe_markup` = `rich.markup.escape(safe_text(s))`).

### CLI run management
Thin commands over the store and engine; shared glue in `rayspec.cli._runs_common` (store
factory `project_store(home, slug)` / `make_runs_context(root)` = `$RAYSPEC_HOME/projects/<slug>`
with the slug from `rayspec.cli.commands.run.project_slug_for`; `iter_project_stores(home)` —
walks `projects/` recursively (slugs may be deeper than `host/owner/repo`, e.g. GitLab subgroups),
treats every directory with a `runs/` child as a store and never descends into it, `worktrees/`,
`source.git/` or `locks/`; `find_run(ctx, ref)` → `(store, RunRecord)` resolving full ids and unique prefixes in the current
project first, then every project under the home (`UnknownRunIdError` / `AmbiguousRunIdError`
with candidates newest first; `lookup_run` prints them with exit 2); `fmt_duration/fmt_tokens/
fmt_cost` (`providers.pricing.format_cost`: `$0.12`, `~$0.12` for table prices, `-` when no cost
is known — tokens are never shown in a cost slot; listings have a `tokens` column) / `fmt_when` (relative within 30 days) / `run_duration_ms` /
`steps_progress(run, *, planned=None)` (done = succeeded, tolerated or skipped; total =
recorded paths ∪ `planned`) / `steps_detail` (`n ok · m skipped`) / `planned_step_paths(ctx, run, *, cache=None)`
(the workflow's static paths — root + `include:` bodies — for every run that may continue or be
resumed: running/paused/interrupted/failed/cancelled; `None` for succeeded runs or when the
workflow no longer loads — any loader exception is swallowed, a listing never fails on a broken
workflow; `cache` memoises per (project root, workflow) for one listing) / `unpriced_steps` / `run_cost_source`
(`provider|table|partial|none` via `combine_cost_sources`) / `pid_command_line(pid)` (`ps -o
command=`) / `pid_is_rayspec_run(run)` (command line has `rayspec run|resume|approve|reject` as whole tokens + run id / workflow name / file as a whole token) /
`run_row(run, *, planned=None)` (additive keys `steps_ok`, `steps_skipped`) / `step_row` / `output_preview`
(first line, JSON outputs compacted, `…` when cut) / `load_resolved_for(ctx, run)` (workflow by
recorded path, then by name) / `check_workflow_unchanged(run, resolved, force=)` (the engine's
hash rule as a `ResumeError`, applied before anything is persisted) / `resume_run(...,
resolved=None)` (builds the `Runner(resume_run_id=)` with the workspace from `run.json`, prints
the `run` summary, returns the exit code) / `pid_alive` (delegates to the engine's rule) /
`on_other_host` / `release_workdir_lock`). All commands take `--root` and honour `RAYSPEC_HOME`;
every `<run>` argument accepts a unique prefix. Human output renders everything that comes from
`run.json`/output files as plain text (never Rich markup). `--json` on `resume/approve/reject`
prints the JSONL events **and** the final summary object on stdout (Rich progress/errors on
stderr).

- `rayspec runs [--all] [--limit N] [--json]`: newest first by `created_at` then id (run id,
  workflow, status — `(dry)` for dry runs —, started, duration, steps done/total, tokens, cost;
  `--all` adds the project column and lists every project). JSON: list of `{run_id, workflow,
  status, reason, project_slug, created_at, started_at, ended_at, duration_ms, steps_done,
  steps_total, steps_ok, steps_skipped, tokens, usage{…}, cost_usd, cost_source, resume_count,
  dry_run, pid, host, workspace{…}, pause{…}|null}`. Exit 0 (empty list ⇒ "no runs …").
  Outside a project (no `.rayspec/` and no `.git` at or above the root; `runs.is_project_dir`)
  without `--all`: a stderr notice `not inside a rayspec project (…) — hint: rayspec runs --all`,
  no slug minted, exit 0, `[]` with `--json`.
- `rayspec show <run> [--json]`: header (status/reason, workflow, project, inputs, timings,
  `steps done/total (n ok · m skipped)`, tokens, cost with marker + `(n steps unpriced)` for
  partial, pid while running/paused, run dir), workspace block, per-step table (path,
  kind, status, attempts, duration, tokens, cost, output preview | error | skip reason),
  `warnings:` block (`show.collect_warnings`: `warning` events + stream `warning` records as
  `<step>: <message>`), outputs table, pause block with the `rayspec approve|reject|resume`
  hint. JSON = the `runs` row + `run_dir, inputs, outputs, workflow_path, workflow_hash,
  project_root, steps[]` (record fields + `tokens`, `output_preview`), `artifacts[]`
  (`{step, path, ref, sha256, size}` per delivered `artifacts:` entry, `show.artifact_rows`),
  `warnings[]` + `record`
  (raw `run.json`). The human view adds an `artifacts` table (step, file, size via
  `show.fmt_size`, sha256[:12], stored ref) after the steps table when any step recorded one.
  Exit 0 · 2 unknown/ambiguous. All run data is safe plain text.
- `rayspec logs <run> [--step <path>] [--follow] [--stream] [--verbose] [--raw] [--json]`:
  timestamped (UTC) event lines (`events.jsonl`, rendered like the quiet console sink, loop/each
  progress as generic lines); `--step` renders that step's `stream.jsonl` (text deltas buffered
  until the completed text, reasoning deltas joined per block into whole `thinking:` lines,
  tool calls/results, command start/output/end, shell stdout/stderr/exit, usage, session…;
  `raw`/unknown kinds only with `--verbose`); `--stream` interleaves every step's stream
  (prefixed `[path]`, ordered by timestamp); `--json` prints raw JSONL (events as stored;
  streams as `{"type":"stream","step_path":…,"record":{…}}`); `--follow` polls the files until
  `run.json` leaves `running` (final drain after the flip; Ctrl-C ⇒ 130). Rendered text goes
  through `safe_text` unless `--raw`; `logs.step_path_problem(step)` is the one-line
  `--step` error. Exit 0 · 2 unknown run/step.
- `rayspec resume <run> [--force] [--yes] [--no-interactive] [--json] [--quiet] [--verbose]`:
  engine resume in-process (reuse cache, hash mismatch ⇒ error unless `--force`, live pid ⇒
  error, exit 2 with the engine's hint; a succeeded or cancelled run ⇒ exit 2 "nothing to resume"
  unless `--force`). The hash guard runs FIRST (`cli/commands/resume.py::
  guard_workflow_unchanged` = `load_resolved_for` + `check_workflow_unchanged`, shared by
  `approve`/`reject`; `run --resume` applies `refuse_changed_workflow` to its loaded workflow):
  a changed workflow is exit 2 before the paused/non-TTY short-circuit below. A paused run with a pending gate: TTY ⇒ the gate re-asks
  (`ConsoleApprovalPrompt`); non-TTY / `--no-interactive` ⇒ prints the approve/reject hint and
  exits 3 without running; `--yes` auto-approves. The secret-inputs / stubs checks
  (`resume_secret_inputs` / `resume_stub_script`) run AFTER that short-circuit and before anything
  is written (`approve`/`reject` run them before the decision is recorded). Otherwise exit = the
  run's exit code.
- `rayspec approve <run> [comment]` / `rayspec reject <run> [reason]` (`--json --quiet --force`):
  require status `paused` (else exit 2 "is <status>, not paused"); the workflow must re-load and
  its hash must match `run.workflow_hash` unless `--force` (else exit 2 with the engine's
  wording/hint, nothing recorded); only then write `pause.decision{approved, comment, by:
  "cli"}` and resume non-interactively — the gate consumes the decision when its token matches.
  Exit = how the run ends (approve typically 0; reject with the default `on_reject: cancel` ⇒ 4).
- `rayspec cancel <run> [--yes] [--force] [--mark] [--json]`: live run (status `running`, pid
  alive on this host **and** `pid_is_rayspec_run` — else exit 2 "pid N is not a rayspec run
  process (stale record?) — use `rayspec cancel --mark` …"; when the record
  carries `pid_started_at` the live process's start time (`_runs_common.pid_start_time(pid)` =
  `engine.runner.process_start_time`) must equal it exactly — unknown ⇒ mismatch — and the
  command-line heuristic is the second check; records without the field use the heuristic
  alone) ⇒ confirmation (`--yes`/`--json`
  skip it; declined ⇒ exit 1; no terminal to answer on ⇒ exit 2 with the `--yes` hint), then
  SIGINT to the pid (the engine finalizes its own record; exit 0, JSON `{run_id, action:
  "signalled", pid, status}`); `--mark` ⇒ the running/paused record is finalized as cancelled
  without any signal (JSON `action: "marked"`); a `running` record whose `host` is another
  machine (shared `RAYSPEC_HOME`) ⇒ exit 2 "recorded as running on host X" unless `--force`;
  paused run or a `running` record whose process is gone ⇒ the record is marked `cancelled`
  (`pid` cleared, `ended_at`, reason, `run.finished{status: cancelled}` appended) and a stale
  workdir lock file cleared best effort — the engine releases the lock on pause, so this is
  housekeeping (JSON `{run_id, action: "cancelled", pid: null, status, lock_released}`); step records are left intact (a later `resume` behaves like any cancelled
  run). Any other status ⇒ exit 2 "nothing to cancel".

Exit codes across the group: 0 ok · 1 (run failed | cancel declined) · 2 usage/lookup error ·
3 paused · 4 cancelled · 130 interrupted — i.e. the engine table for anything that runs.

### CLI `runs` sub-app + record & replay

**`runs` is a Typer sub-app.** `cli/commands/runs.py` registers a `typer.Typer` group
via `app.add_typer(runs_app, name="runs")` whose callback is `invoke_without_command=True`, so the
**bare** `rayspec runs [--all|-a] [--limit|-n N] [--json] [--root]` keeps its earlier behaviour
byte for byte (same table, same JSON rows, same exit codes; only the `--help` usage line gains
`COMMAND [ARGS]...`). The listing body is `runs.list_runs(*, all_, limit, root, json_)`;
`runs.group_root(ctx, root)` resolves a `--root` given *before* the subcommand name (the callback
stashes it in `ctx.obj`) — it is the ONLY group option a subcommand honours, so `--all`/`--limit`/
`--json` before a subcommand name are a usage error (exit 2, "…belongs to the `rayspec runs`
listing") instead of being parsed and silently dropped. Subcommands live on `runs_app`.
Consequence for the shared CLI-surface tests: a group that is itself invokable counts as a command
(`tests/docs/test_cli_reference.py::cli_commands`) and carries its own options
(`tests/skill/test_skill_content.py`).

```python
from rayspec.cli.commands.runs import (
    collect_runs, group_root, is_project_dir, list_runs, register, runs_table,
    stub_script_text,           # (FileRunStore, RunRecord) -> stub-script YAML
    workflow_drift_warning,     # (RunsContext, RunRecord) -> str | None  (hash moved since the run)
    write_script,               # (Path, str) -> None  atomic write; any OSError is exit 2
)
from rayspec.cli.commands import _runs_diff   # StepDiff, LoopDiff, RunDiff, build_diff, render
```

Additive `cli/_runs_common.py` helpers: `recorded_calls(store, run) -> list[RecordedCall]`
(prompt steps only; a step that never got an answer — skipped, pending, running, paused,
interrupted, rejected — is left out, and a step that claims an output rayspec cannot read is
exit 2 naming the step and the missing ref: an entry without an answer would replay as the stub
provider's default), `recording_notes(run) -> list[str]` (one line per lossy substitution the
recording makes — today only an error type the stub cannot express, recorded as `kind: api`;
printed on stderr by `runs stubs` and `--stubs-from`), `stub_script_data(store, run) -> dict` (the
YAML-dumpable script), `secret_refusal(run) -> str | None` (the message for a run with
`secret_inputs`; `None` when recording is safe), `replay_source(ref, *, root) -> (StubScript,
run id)` (the `--stubs-from` resolver: run id or unique prefix through `make_runs_context`/
`lookup_run`, exit 2 on unknown/ambiguous ids and on secret inputs) and `replay_script(ref, *,
root) -> StubScript` (the script alone).

Additive `providers/stub.py`:

```python
from rayspec.providers.stub import (
    StubExpect,     # prompt_regex | prompt_contains | not_contains | access | model |
    #   output_schema: bool | session: "resumed"|"fresh";  .check(req) -> (reasons, offset),
    #   .failure_message(req, reasons, offset) -> str
    RecordedCall,   # step_path, text, output/has_output, usage, failure: StubFailure,
    #   sequential: bool;  .to_outcome() -> dict
    record_script,  # (Sequence[RecordedCall], *, defaults=None) -> {"steps": {...}, "defaults": {...}}
    glob_key,       # "build[2]/implement" -> "build[*]/implement"
    prompt_excerpt, # (prompt, *, around=None, limit=EXCERPT_LIMIT) -> str
    EXCERPT_LIMIT,  # 600
)
```
`StubOutcome` gains `expect: StubExpect | None` (entry level **or** a single `sequence` item; the
item's block replaces the entry's for that call, like every other outcome field). A mismatch
returns `AgentResult(status="error", error=AgentError(kind="stub_expectation", transient=False))`
whose message is one bullet per mismatch plus the rendered prompt, and emits an `error`
AgentEvent; `raw["expectation_failed"]` lists the reasons. Assertions run **before** `latency_ms`,
before `fail:` and before the scripted answer, so a mismatched request is never masked *by the
script itself*. It is still an ordinary step failure: the STEP's own `allow_failure: true` (and
`each.on_failure: continue`) tolerate a `stub_expectation` exactly like any other failure; the
behaviour is pinned by `tests/cli/test_runs_stub_expect.py`. What is guaranteed is that an
assertion cannot vanish silently: `rayspec run --stubs/--stubs-from` refuses (exit 2, naming the keys and listing
the workflow's prompt steps) any `steps:` key that carries an `expect:` block — on the entry or on
a sequence item — and matches no prompt step of the workflow, via
`stub.unmatched_expect_keys(script, known_paths)` / `stub.entry_expects(entry)` (indices and
globs are ignored on both sides) from `run.refuse_unmatched_expectations(rw, script)`.

Additive frozen-module change: `providers/base.py` `ErrorKind` gains `"stub_expectation"` (no
engine code switches on error kinds; `ErrorInfo.type` is a plain `str` in the store).

`record_script` keying rule: calls are grouped by `glob_key`. Identical outcomes stay one
entry under the glob; differing outcomes of an **ordered** body (every enclosing composite is a
`loop:`) become `sequence:` in index order; differing outcomes of a body with any **`each`**
ancestor are written as their own indexed keys (`fan[0]/patch`) — `each` items run in parallel, so
a sequence would hand answers to whichever item called first. `RecordedCall.sequential` carries
that decision, computed by `_runs_common` from the ancestors' recorded `kind`. A group in which
any call carries a **transient** failure also keeps its indexed keys: `sequence:` advances per
CALL and the engine retries a transient failure (`DEFAULT_PROMPT_RETRY`), so the retry would eat
the next iteration's answer and shift the loop by one.

CLI surface:
- `rayspec runs stubs <run> [-o PATH] [--redact] [--force] [--root]` — writes the recorded script
  to a file (refusing an existing one without `--force`; the write is atomic and any `OSError` is
  exit 2 `cannot write <path>: …`) or to stdout; prints `workflow_drift_warning` and
  `recording_notes` on stderr; exit 2 for a run with `secret_inputs` (naming them) and **always**
  for `--redact` (not available until the redactor ships — the flag never silently records an
  un-redacted script).
- `rayspec runs diff <a> <b> [--json] [--exit-code] [--outputs] [--steps] [--across-projects]
  [--root]` — two runs of
  ONE workflow. `RunDiff.changed` (what `--exit-code` returns 1 on) covers the run status, the set
  of recorded step paths, each step's status / `output_sha256` / `fingerprint`, and the workflow
  outputs. Duration, tokens and cost are reported but **never** set `changed`. Two different
  `workflow_name`s ⇒ exit 2 naming both runs and both workflows; two different `project_slug`s ⇒
  exit 2 naming both projects unless `--across-projects` is given (the human header then gains a
  `project` row); a missing run ⇒ the usual lookup error. With `--outputs`, a run with
  `secret_inputs` gets `_runs_common.secret_output_notice(run)` on stderr — this reader prints
  stored output text like `show`/`logs` do, so the Redactor must cover
  `_runs_diff.output_diff` too. JSON shape in `docs/cli.md#rayspec-runs-diff`.
- `rayspec run <wf> --dry-run --stubs-from <run>` — `RunOptions.stub_script` from a stored run
  instead of a file; mutually exclusive with `--stubs` (exit 2). The **donor run** is recorded in
  the existing `RunRecord.stubs_path` field as `run:<run id>` (`cli/commands/run.py`:
  `REPLAY_REF_PREFIX` / `replay_ref(stubs_path) -> run id | None`), so `resume`/`approve`/
  `reject` and `run --resume` rebuild the identical script through `load_stub_script` (a deleted
  donor is the usual lookup error, exit 2) instead of falling back to the stub provider's
  built-in default. Precedence on a resume entry: explicit `--stubs` > explicit `--stubs-from` >
  the recorded `stubs_path`. No new field: the `RunRecord.toolchain` reservation stands.

### CLI `costs` — the cost roll-up
`src/rayspec/cli/commands/costs.py` (`rayspec costs [--since WHEN] [--workflow NAME] [--json]
[--root]`). **Read-only consumer of the store**: it calls `store.list_runs()` and nothing else —
no record format changes, no field is added, nothing is ever written and no `projects/<slug>`
directory is created (`tests/cli/test_costs_cmd.py` hashes the whole `RAYSPEC_HOME` tree around
the invocations). The per-run arithmetic is *not* reimplemented: `RunRecord.total_cost_usd()` /
`total_usage()` for the numbers and `_runs_common.fmt_cost` / `run_cost_source` /
`unpriced_steps` / `fmt_tokens` / `usage_dict` / `fmt_stamp` for the rendering, so a roll-up can
never disagree with the `rayspec runs` lines it sums. **Nothing in scope is presented as complete
when it is not**: an unpriced run, a priced run holding an unpriced step, a step cut off before
it reported usage, a run still in flight and a `run.json` the store could not parse each get a
counter and a line below the table. Scope is one project (`make_runs_context`), and the
outside-a-project rule is `runs.is_project_dir` (stderr notice, exit 0, no slug minted).

- `parse_since(text, *, now=None) -> datetime` — aware UTC cutoff from a window
  (`45s|90m|24h|7d|2w`, decimals allowed) or an ISO-8601 date/timestamp (`2026-08-01`,
  `…T06:30:00`, `…Z`, `…+02:00`; naive = UTC). `ValueError` otherwise (a negative window
  included); the command turns it into exit 2 with `SINCE_HINT`.
- `select_runs(records, *, since, workflow)` — `created_at >= since` (**inclusive** at the
  cutoff) and an exact `workflow_name` match; newest first (the roll-up regroups and re-sorts —
  the order is for callers that reuse the helper to list what was summed).
- `cost_bucket(run)` — `unknown` when `total_cost_usd()` is `None`, else `run_cost_source(run)`.
  `BUCKETS = (*COST_SOURCES, "unknown")` is the fixed print order. The `none` bucket is reachable
  and is **not** remapped: a `run.json` written before `StepRecord.cost_source` existed carries a
  cost without a source, and it is reported as stored rather than guessed at (docs/cli.md names
  the bucket in prose).
- `aggregate(records, *, label, incomplete=False) -> CostGroup(label, runs, runs_unknown_cost,
  runs_partial_cost, runs_usage_unknown, runs_in_flight, usage, cost_usd, cost_source, buckets,
  first_run_at, last_run_at)` — every record is counted; `cost_usd` is `None` when nothing in the
  group is priced (never `0.0`); `cost_source = combine_cost_sources(sources of the priced runs,
  unpriced=…)`, i.e. an unpriced *run* makes the group a lower bound exactly the way an unpriced
  *step* makes a run one. Four counters, each a distinct reason a figure is not the whole truth:
  `runs_unknown_cost` (no cost at all), `runs_partial_cost` (a *priced* run holding a step with
  tokens and no price — `_runs_common.unpriced_steps`), `runs_usage_unknown` (a run holding a
  step with `StepRecord.usage_unknown`: an attempt cut off before the adapter reported any usage,
  which `unpriced_steps` cannot see because that step's `usage.total` is 0) and `runs_in_flight`
  (`status in IN_FLIGHT = {running, paused}`). `unpriced` is set by `runs_unknown_cost`,
  `runs_usage_unknown`, a run whose own source is `partial`, **or** `incomplete=True`;
  `runs_in_flight` is reported but does not move the marker. `CostGroup.partial` = the cost is a
  lower bound, `CostGroup.tokens_partial` = the token count is.
- `build_report(records, *, unreadable=0) -> CostReport(groups, total, runs_unreadable)` — grouped
  by workflow, most expensive first then by name; the sort key is tri-state
  (`(cost_usd is None, -cost, label)`) so an unpriced group sorts after a real `$0.00` one.
  Groups are never dropped: `sum(g.runs) == total.runs`. `unreadable` is passed as
  `incomplete=` to the total's fold, so a store the command could not read completely can never
  present an exact-looking sum. The command computes it as
  `len(store.list_run_ids()) - len(store.list_runs())` (`list_runs` swallows an unparseable
  `run.json` into a log warning; `list_run_ids` only lists dirs that have one).
- Presentation: `costs_table(report)` (workflow · runs · tokens · cost · cost source, total row
  last; the tokens cell is `≥…` when `tokens_partial`, `unknown` when nothing was reported at
  all and `-` only for a genuine zero), `scope_line`, `empty_notice`, `unreadable_notice(count)`,
  `partial_notices(report) -> list[str]` (one line per counter above, then the marker line — and
  the marker line is only printed for a marker that is on screen: `no cost is known for any run
  in scope` when `total.cost_usd is None`, `totals marked ≥ are a lower bound` when the total
  renders with `≥`, nothing otherwise), `group_payload` / `costs_payload`.
- `--json`: one object `{project, since, workflow, runs, runs_unknown_cost, runs_partial_cost,
  runs_usage_unknown, runs_in_flight, runs_unreadable, tokens, usage{…}, cost_usd, cost_source,
  cost_sources{…}, first_run_at, last_run_at, workflows: [{workflow, runs, runs_unknown_cost,
  runs_partial_cost, runs_usage_unknown, runs_in_flight, tokens, usage{…}, cost_usd, cost_source,
  cost_sources{…}, first_run_at, last_run_at}]}`. The top level is the total over exactly the runs
  in `workflows`; `cost_sources` counts every run once (zero buckets omitted); `runs_unreadable`
  is top level only (an unreadable record cannot be attributed to a workflow) and `project` is
  `null` outside a rayspec project — no slug is claimed there, on disk or in the output. Exit 0
  with `runs: 0` when nothing is in scope (an unknown `--workflow` is a filter that matched
  nothing, not an error) · exit 2 on a bad `--since`.
- Deliberately out of scope (a follow-up would be a new command, not a flag here): cross-project,
  per-team, per-repo, per-user or per-tag roll-ups and chargeback export formats. `--json` is the
  seam for those.

### CLI `init` + `doctor`
`src/rayspec/cli/commands/{init,doctor}.py`; scaffold templates are package data under
`src/rayspec/cli/templates/<kind>/**` (read with `importlib.resources`; one directory per kind).

- `rayspec init [--kind code|content | --from EXAMPLE] [--force] [--no-skill] [--root DIR]`
  copies the `<kind>` tree into `<root>/.rayspec/` (`workflows/example.yaml`, `agents/reviewer.yaml`,
  `prompts/*.md`, `config.yaml`, `stubs/example.yaml`; `workflows/agents/prompts/stubs` dirs
  always created) and — unless `--no-skill` — the packaged coding-agent
  skill into `<root>/.claude/skills/rayspec/` (`rayspec.skill.install_skill(project_skill_dir
  (root), force=)`, same per-file idempotence, printed through `_skill_common.
  print_install_result`; the `nothing written` warning counts both sets; the only write outside
  `.rayspec/`). `--root` defaults to the cwd (no walk-up). Existing files are kept (`exists …
  skipped; use --force`) unless `--force`; exit 0 (a stderr `warning: nothing written` when
  every file existed; a stderr `warning:` naming the old kind + its left-over files when an
  untouched scaffold of the other kind is detected via `workflows/example.yaml`), exit 2
  (`error: cannot write the scaffold: …`, no traceback) for an unknown kind, a `--root` that is
  not a directory, a directory at a template path (`IsADirectoryError`, also without `--force`)
  or any other `OSError`; exit 2 `error: cannot write the skill: … (the .rayspec/ scaffold was
  written; re-run with --no-skill to skip the skill)` when the scaffold succeeded but the skill
  write failed (e.g. `.claude` is a file). Prints the next steps (`doctor`,
  `validate`, `plan example`, `run example --dry-run --stubs .rayspec/stubs/example.yaml`, a real
  run, and — with the skill — `open a fresh Claude Code session here …`). Both kinds validate
  with the real loader (no warnings) and dry-run with their stubs; the
  `content` kind has `isolation: none` and no shell/python steps. Python surface:
  `TEMPLATE_KINDS`, `SCAFFOLD_FILES: {kind: (".rayspec/…", …)}`, `scaffold(root, *, kind="code",
  force=False) -> list[ScaffoldFile(relative, path, action ∈ created|overwritten|skipped)]`
  (raises `NotADirectoryError`/`IsADirectoryError`/`OSError`), `template_files(kind)`,
  `detect_kind(root) -> kind | None`, `orphan_files(old_kind, new_kind)`, `next_steps(kind, *,
  skill=True)` (additive keyword);
  `in_git_checkout(path) -> bool` (a `.git` dir *or* file at or above `path`),
  `GIT_DEPENDENT_KINDS = {"code"}`, `non_git_warning(target, kind) -> str | None` — the `code`
  scaffold outside a git checkout prints that stderr `warning:` (names `git init` and
  `rayspec init --kind content`), exit stays 0; `content` is silent.
- `rayspec init --from <example>` scaffolds one of the packaged **example projects** instead of a
  `--kind` template: every file of `examples/<name>/` except `checks.yaml` (repository test data),
  copied verbatim to the same relative path — `.rayspec/**` stays `.rayspec/**`, `stubs*.yaml` and
  `README.md` land at the root. Same `ScaffoldFile` actions, same `--force`/`--no-skill`/`--root`
  behaviour and the same `error: cannot write the scaffold: …` mapping as `scaffold()`; the
  kind-switch and non-git warnings do not apply. `--from` together with `--kind` is exit 2, and an
  unknown (or empty) name is exit 2 `error: unknown example '<n>'[; did you mean '<m>'?]` with a
  `hint:` listing every example and its first workflow's `description:` (truncated at 72 chars);
  with no corpus at all the error is `no examples are packaged with this build` instead.
- An example is applied **whole or not at all**. Before anything is written,
  `example_conflicts(root, name)` lists the files the target already holds with *different*
  content; a non-empty list without `--force` is exit 2 naming them, because writing the rest
  around a kept `config.yaml` or stub file leaves a project whose own printed next steps fail.
  Identical files are not conflicts (re-running the same example is idempotent) and neither are
  the documentation-only files of `EXAMPLE_OPTIONAL = {"README.md"}`: an existing README is kept,
  a stderr `warning:` says so, and `example_next_steps(..., readme=False)` drops the step that
  would open it.
- The corpus is package data: `pyproject.toml`'s wheel target lists `examples` in `only-include`
  and remaps it with `sources` (`"examples" = "rayspec/examples"`), so `--from` works from a bare
  `uv tool install rayspec`; in a source checkout `examples_root()` falls back to the repository
  directory four levels above the module (guarded by a sibling `pyproject.toml`). The remap goes
  through the ordinary file selection **on purpose**: `force-include` copies a directory verbatim,
  ignoring the VCS ignore rules and `exclude` alike, so a release cut from a checkout that has
  been used would publish its `.rayspec/.env` and `.rayspec/runs/`. The wheel target's `exclude`
  names those two paths under `examples/` as well, because `.gitignore` anchors them at the
  repository root only. `tests/cli/test_init_cmd.py` builds a wheel from a staged copy with that
  local state planted and asserts the corpus is in and the state is out.
- Python surface: `EXAMPLES_DIR`, `EXAMPLE_SKIP`, `EXAMPLE_OPTIONAL`, `examples_root() ->
  Traversable | None`, `example_names() -> tuple[str, ...]` (a directory with a `.rayspec/`),
  `example_files(name) -> [(relative posix path, resource)]` (raises `LookupError`),
  `example_conflicts(root, name) -> list[str]`, `scaffold_example(root, name, *, force=False) ->
  list[ScaffoldFile]`, `example_catalogue() -> [(name, description)]`, `unknown_example_hint()`,
  `example_dry_run(name) -> str | None`, `example_refuses_validation(name) -> bool` (the example
  ships a workflow `validate` is meant to reject — its `checks.yaml` says `validate: error`, and
  the printed `rayspec validate` step then carries "this example refuses on purpose"),
  `example_next_steps(name, *, skill=True, readme=True)`,
  `secret_inputs(root, example, workflow) -> frozenset[str] | None` (`None` = the declaration
  could not be read, so none of that scenario's inputs may be rendered).
  `example_dry_run` reads the example's `checks.yaml` and renders the **first scenario that
  scripts the agents** as a shell command (`rayspec run <wf> [-i k=v]* [--allow-unsupported]
  --dry-run --stubs <file>`, `shlex.quote`d; scenarios that declare `validate:`/`run: false`,
  carry a non-scalar input or would print the value of a `secret: true` input are skipped) — the
  printed next step is therefore a command the example's own checks assert green, and
  `tests/cli/test_init_cmd.py` runs the printed line for every example.
- `rayspec doctor [--probe] [--provider ID]... [--json] [--root DIR]` loads `.env` + config like
  the project commands (tolerant: a broken config is the failed `config` check, not a crash) and
  collects `Check(id, label, status ∈ ok|warn|fail|info, detail, required, hint)` rows in a
  `Report` (`.ok` = no required check failed, `.exit_code` 0|1, `.to_dict()` = `{ok, exit_code,
  checks[]}` = the `--json` shape). Check ids: `python` (≥ 3.11, required), `rayspec`, `home`
  (`RAYSPEC_HOME` exists+writable or creatable, required), `config` (required), `project`
  (`find_project_root` + workflow count; warn + `rayspec init` hint when no `.rayspec/`), `git`
  (required), `uv` (warn), `claude.sdk` / `codex.sdk` (import + version; required), `claude.cli`
  (config `providers.claude.cli_path` → `claude_agent_sdk/_bundled/claude` → `PATH` → the SDK's
  known locations; `-v` probe via `version_of`; required, `warn` when found but no version) /
  `codex.cli` (`providers.codex.codex_bin` → `codex_cli_bin.bundled_codex_path()`; `--version`),
  `claude.auth` (`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` ⇒ ok; else
  `doctor.claude_login_source()` — lazily `rayspec.providers.claude.cli_login_source()`, `None`
  when the adapter is not importable — ⇒ ok with the source as detail; else warn "login state
  unknown (… no ~/.claude/.credentials.json or the macOS keychain …)") / `codex.auth`
  (`OPENAI_API_KEY` ⇒ ok; `$CODEX_HOME/auth.json` present ⇒ info; else warn with
  `codex_login_hint(cli_path)`: `run \`codex login\`` when `codex` is on PATH, else `run
  \`<bundled path> login\``, or `OPENAI_API_KEY`), `<id>.pricing` (never required; only for providers whose capabilities say
  `cost_reporting=False`: `info` = no pricing table at all (nudge `tokens only — add
  pricing.<model> for estimates`) or every model disabled with `null`, `warn` = a table exists
  but misses one of the provider's tier/alias models or is malformed, `ok` = every configured
  model priced; `null` entries are `pricing disabled (null) for <model>`, no nudge), and with
  `--probe` one `<id>.probe` (required) per selected provider =
  `create_provider(id, config.providers[id]).healthcheck(probe=True)` + `aclose()` under
  `anyio.run` with an outer 180 s bound (`PROBE_TIMEOUT_S`; exceptions/timeouts become failed
  checks; `aclose()` is bounded by `CLOSE_TIMEOUT_S` = 15 s so a hung adapter cannot block the
  report). Probe policy (`apply_probe_policy(checks, *, explicit)`): a failed
  `<id>.probe` stays required only when `id` was requested with `--provider` or its `<id>.auth`
  row is `ok`/`info` (`CONFIGURED_AUTH_STATUSES`) or absent (no login needed); otherwise it
  becomes a non-required `warn` row whose hint says `rayspec doctor --probe --provider <others>`
  and how to log in (`login_hint_for`). A successful probe rewrites a non-ok `<id>.auth` row to
  `ok` / `probe OK (verified by --probe …)` with no hint. `--provider`
  restricts the provider sections and probes (unknown id ⇒ exit 2); default = every registration
  (`claude`, `codex`, `stub`, plugins). SDK modules are imported lazily through `importlib`
  (tests monkeypatch `sys.modules`, `shutil.which` and `doctor.version_of`). Python surface:
  `run_doctor(*, root, probe, providers) -> Report`, `environment_checks`, `claude_checks(settings)`,
  `codex_checks(settings)`, `pricing_check(provider_id, config) -> Check | None`,
  `pricing_checks(ids, config)`, `probe_checks(ids, config)`, `find_claude_cli`, `find_codex_cli`,
  `known_claude_locations`, `version_of(cmd, *, timeout_s=5)`, `parse_version`, `render_table`.

### CLI `new`
`src/rayspec/cli/commands/new.py`; templates are package data under `cli/templates/new/`
(`workflow.yaml`, `workflow_agent.yaml`, `agent.yaml`), rendered by literal `__NAME__` /
`__AGENT__` / `__DESCRIPTION__` substitution — never Jinja, because the documents themselves are
Jinja.

- `rayspec new workflow <name> [--agent NAME] [--description TEXT] [--force] [--root DIR]` writes
  `.rayspec/workflows/<name>.yaml`; `rayspec new agent <name> [--force] [--root DIR]` writes
  `.rayspec/agents/<name>.yaml`. `rayspec new` with no subcommand ⇒ help, exit 2. Both print
  `created`/`overwrote  <relative path>` plus a next-steps block, and both refuse an existing file:
  exit 2 `error: <relative path> already exists` + `hint: pass --force to overwrite it`.
- The project is `--root` **itself**, else `find_project_root(None)` — the project-command
  walk-up, unlike `init`'s cwd-only rule. `find_project_root` walks *up*, so an explicit `--root`
  is never fed to it: a `--root` without `.rayspec/` would otherwise add the file to an enclosing
  project and report it as a path relative to a root the user never named. Either way a directory
  without `.rayspec/` is exit 2 (`… is not a rayspec project (no .rayspec/ directory)`, hint
  `rayspec init`): `new` grows a project, it never creates one. `<name>` is checked with the
  loader's own validators (`validate_identifier` for a workflow, `validate_name` for an agent)
  before anything is written, so the file name and the document's `name:` cannot disagree.
- With `--agent NAME` the workflow references `.rayspec/agents/<NAME>.yaml` and ships no inline
  `agents:` block (a second template); without it the workflow carries one inline agent named
  `assistant`. The agent must already resolve — `agent_names(project)` = `discover_agents`, so
  the user scope (`<RAYSPEC_HOME>/agents/`) counts — and an unknown name is exit 2
  (`error: unknown agent '<n>'[; did you mean '<m>'?]`, hint `rayspec new agent <n>` /
  `rayspec agents`) with nothing written: the rendered workflow validates and dry-runs as written
  (`tests/cli/test_new_cmd.py`), which a reference to a missing agent would break.
- Python surface: `KINDS = {kind: (subdir, template file)}`, `DEFAULT_DESCRIPTION`,
  `workflow_text(name, *, agent=None, description="")`, `agent_text(name)`, `yaml_scalar(text)`
  (a one-line YAML scalar, quoted when it must be — a `--description` arrives from a shell),
  `write_new(root, kind, name, text, *, force=False) -> NewFile(relative, path, action ∈
  created|overwritten)` (raises `FileExistsError`/`IsADirectoryError`/`OSError`),
  `project_root_for(root)`, `agent_names(project) -> list[str]`.

### CLI `completion`
`src/rayspec/cli/commands/completion.py` owns **everything** about shell completion; `app.py`
keeps `add_completion=False` (Typer's `--install-completion` appends a `source` line to the user's
shell rc file, and `--show-completion` sniffs the shell through `shellingham`; both would also sit
in every `--help`).

- `rayspec completion <bash|zsh|fish>` prints a script to stdout and nothing else. No shell ⇒
  exit 2 `error: which shell? one of: bash, zsh, fish`; an unsupported shell ⇒ Typer's enum error,
  exit 2.
- `rayspec completion --values workflows|runs [--root DIR]` prints one candidate per line — what
  the emitted script calls back for. `workflow_values(root)` = `discover_workflows`,
  `run_values(root, *, limit=RUN_LIMIT=50)` = the project store's newest run ids. **Both return
  `[]` and print nothing on any failure**, with stderr redirected, because a completion callback's
  stdout is the shell's candidate list: no project, an unreadable `config.yaml` or a broken
  workflow must never inject a diagnostic there. `--values` together with a shell is exit 2.
- `enable_shell_completion()` calls Typer's private `completion_init()` and is invoked from
  `register(app)` **only when `COMPLETE_VAR = "_RAYSPEC_COMPLETE"` is set** — with
  `add_completion=False` Typer never registers its shell classes, so the protocol would answer
  `Shell bash not supported`; gating on the variable keeps that process-global registry untouched
  for every ordinary invocation. Returns `False` (and completion degrades to nothing) when the
  private module is gone.
- `completion_script(shell)` = Typer's own script for that shell plus a wrapper
  (`_rayspec_values_completion`, bound last so it wins) that answers the two argument slots Typer
  cannot know: `WORKFLOW_COMMANDS` (`run`, `plan`, `validate`, `test`) get workflow names,
  `RUN_COMMANDS` (`show`, `logs`, `resume`, `approve`, `reject`, `cancel`, `eval`, `explain`,
  `diff`, `stubs`) get run ids; anything else falls through to Typer. No command module declares
  an `autocompletion=` callback, so the completion seam stays in this one file.
  `tests/cli/test_completion_cmd.py` sources the emitted script in a real `bash` and drives the
  completion function non-interactively, and parses each script with its own shell.

### CLI presentation — `--output` / `--json`
`cli/commands/_loader_common.py`: `OutputFormat` (`table`/`json`), `OutputOption` (`--output`,
default `None`) and `resolve_output(output, json_) -> bool`. Every command that has `--json` takes
`output: OutputOption = None` and starts its body with `json_ = resolve_output(output, json_)`;
nothing else about those commands changed, so `--json` behaves exactly as it always did and is
documented as the older spelling of `--output json`. `--output` alone decides; `--json` alone
decides; both together are fine while they agree and exit 2
(`error: --json and --output table disagree`) when they do not — one of them silently winning
would print a table into a pipe that asked for JSON. `rayspec runs` counts `--output` with
`--json`/`--all`/`--limit` as a listing flag that a subcommand refuses. Two knowingly-open points:
`rayspec show` still takes `--json` alone, and `rayspec runs stubs -o/--output PATH` predates the
flag and keeps its own meaning (that command has no `--json`, so nothing is ambiguous).
`tests/cli/test_output_option.py` holds the gap list and asserts `--json` and `--output json` are
byte-identical per command.

### rayspec.skill + CLI `skill`
The Claude Code skill for coding agents ships as package data: `src/rayspec/skill/rayspec/`
holds the hand-written `SKILL.md` (frontmatter `name: rayspec`, `description:`) and
`references/{concepts,schema,templating,cli,providers,examples}.md` — verbatim copies of
`docs/<name>.md` with a three-line `<!-- Generated … -->` header and relative links rewritten
(sibling reference when the target is one of the six, else the `cli/_docs.py::DOCS_BASE` URL)
by `scripts/gen_skill.py [--check]`, which also mirrors the whole dir to
`.claude/skills/rayspec/` for this repository's own sessions (`--check` exits 1 on drift;
`tests/skill/test_skill_fresh.py` runs it, so editing `docs/<one of the six>.md` or `SKILL.md`
means re-running the script in the same PR). Nothing under `rayspec/skill/` imports the loader,
engine or providers; files are read via `importlib.resources` (works from a wheel — hatchling
needs no pyproject include).

- Python surface (`rayspec.skill`): `SKILL_NAME = "rayspec"`, `REFERENCE_NAMES` (the six),
  `SKILLS_SUBDIR = Path(".claude/skills")`, `skill_dir() -> Traversable` (the packaged dir),
  `skill_files() -> [(relative posix path, Traversable)]` (sorted; no `.py`/dotfiles),
  `content_digest() -> str` (12 hex of sha256 over `rel\0bytes\0` of every file, sorted — the
  skill's version identity; there is no version stamp inside the files, so a rayspec version bump
  alone never makes an installed copy stale), `install_skill(target, *, force=False) ->
  list[InstalledFile(relative, path, action ∈ created|overwritten|skipped)]` (`target` is the
  `…/skills/rayspec` dir itself; existing files kept unless `force`; raises
  `NotADirectoryError`/`IsADirectoryError`/`OSError` — callers map to `error:` + exit 2),
  `installed_state(target) -> InstalledState(path, state ∈ missing|current|stale, digest)`
  (`missing` = no `SKILL.md`; `current` = the digest over the files found there equals
  `content_digest()`; `stale` otherwise — an extra or edited file counts as stale),
  `project_skill_dir(root) = <root>/.claude/skills/rayspec`, `global_skill_dir(home=None) =
  ~/.claude/skills/rayspec` (`home` overrides `Path.home()`).
- `rayspec skill install [--global] [--force] [--root DIR]` (`cli/commands/skill.py`): target =
  `project_skill_dir(--root or find_project_root(cwd))` (nearest `.rayspec/` → `.git` → cwd —
  deliberately the project-command walk-up, not `init`'s cwd-only rule) or, with `--global`,
  `global_skill_dir()`; `--global` together with `--root` is exit 2 (`--global and --root are
  mutually exclusive`). Output: one line per file (`created` / `overwrote` / `exists …
  (skipped; use --force to overwrite)`), `<label> skill in <target>: n file(s) written[, m kept]`,
  a stderr `warning: nothing written — all n file(s) exist; use --force …` when nothing was
  written (exit stays 0), and the hint `open a fresh Claude Code session in <dir> — the
  rayspec skill loads automatically (rayspec skill show)`. Exit 2 `error: cannot write the
  skill: …` (no traceback) for a `--root` that is not a directory, a directory where a skill
  file goes, or any other `OSError`.
- `rayspec skill show [--root DIR] [--json]`: `packaged <dir>  rayspec <version>, digest <12hex>,
  <n> files`, then `project <path>  not installed | digest … — up to date | digest … — differs
  from the packaged skill (… rayspec skill install --force [--global] to refresh)` and the same
  `global` row. `--json` = `{packaged: {path, rayspec_version, digest, files[]}, project: {path,
  state: missing|current|stale, digest|null}, global: {…}}`. Exit 0.
- `rayspec skill path`: prints `skill_dir()`. `rayspec skill` with no subcommand ⇒ help, exit 2.
- `cli/commands/_skill_common.py` (underscore ⇒ not auto-discovered): `print_install_result
  (results, target, *, label)` and `session_hint(directory, *, global_install)` — used by both
  `skill install` and `init` so the two command modules stay independent plug-ins.
- Tests: `tests/skill/test_skill_content.py` checks `SKILL.md` against the real loader/CLI
  (every ```yaml fence parses with PyYAML and the strict loader and every `- id:` step under
  `steps:` via `parse_step`; both cheat-sheet workflows validate without warnings and dry-run;
  every command/flag of the CLI table exists on every command named in its row);
  `tests/skill/test_skill_secret_seam.py` verifies the skill's `secret: true` paragraph against
  the implementation.

### secrets + redact
Two new packages and one new loader module; nothing else moved.

- **`rayspec.secrets`** — where a secret value comes from, and nothing else (never persists,
  prints or logs one). `SecretProvider` protocol (`names() -> tuple[str, ...]`,
  `get(name) -> str | None`; `None` = not configured or optional-absent, `SecretError` =
  configured but unobtainable). `SecretError(RayspecError)` messages always start
  `secrets.<NAME>: ` and never contain a value; the CLI maps them to exit 2.
  `resolve_source(name, spec, *, env, base_dir) -> str | None` implements the three built-in
  sources: `env` (a variable), `file` (`~` expanded, relative to `base_dir` = the project root;
  **refused unless mode `0600` or tighter** — `… is mode 0644; it must not be readable by group
  or others`, hint `chmod 600 <path>`), `cmd` (a string is `shlex.split`, **no shell**; argv list
  accepted; `CMD_TIMEOUT_S = 30`; stdout only; non-zero exit names argv[0] and the exit code and
  **never the helper's stderr** — that goes to the debug log, and into the message only when
  `RAYSPEC_DEBUG` is set: one line, 200 characters). Values are `.strip()`ed; empty ⇒ error
  unless `required: false`.
  `ConfigSecretProvider(specs, *, env=None, base_dir=None)` is the lazy, memoised provider over
  a `config.secrets` table (`resolve_all()`, `describe() -> ((name, "env GH_TOKEN"), …)`);
  `provider_for(config, …)`, `resolve_config_secrets(config, …) -> {NAME: value}`,
  `describe_sources(specs)`, `secret_input_overlay(provider, names, *, env=None, problems=None)
  -> {RAYSPEC_INPUT_<NAME>: value}` (the env overlay handed to `resolve_inputs` /
  `resolve_resume_secrets`, which is what makes the precedence `--input`/`--inputs-file` >
  `config.secrets` > `RAYSPEC_INPUT_<NAME>` > `default` without either function knowing about
  sources; with `problems` given a `SecretError` is COLLECTED there instead of raised — a source
  that is briefly unavailable must never strand a paused run whose value the user has in hand),
  `used_config_secrets(provider, steps, names) -> {NAME: value}` (**lazy**: only the entries
  `loader.secrets.config_secrets_in_use` says the workflow can read are resolved, so an unused
  entry neither fails a run nor executes its `cmd:` helper),
  `build_redactor(config, {name: value}) -> Redactor`.
- **`rayspec.redact`** — pure text transformation. `Redactor.build({name: value}, *,
  detectors=()) -> Redactor` (`literals` longest-first, each value registered **twice** when its
  JSON-escaped form differs, because records are redacted as serialised JSON text;
  `MIN_REDACTABLE_LEN = 4` — shorter values are skipped and named in `.skipped`, which the CLI
  turns into a `warning:` line at run start and a `doctor` note; `bool(redactor)` is False when
  it would change nothing). `redact(text)`, `redact_obj(value)` (also replaces a **number** that
  IS a secret, so a JSON document stays well-formed), `REDACTION = "[REDACTED:{name}]"`,
  `NULL_REDACTOR` (the shared no-op). `StreamRedactor.feed(text)` holds back only the tail that
  could still GROW into a match — the longest suffix that is a proper prefix of a known value, or
  a detector shape in progress — so ordinary text is emitted immediately and a live log never
  lags; `redactor.hold` is the documented upper bound, not what is held. `flush()` returns the
  tail and MUST be called at the end of a stream. Detector patterns are bounded (`PEM_MAX_BODY =
  8192`, 4 KiB tokens) precisely so a shape split across two chunks is still caught. The
  concatenation of `feed`/`flush` equals the redaction of the concatenated input — only the
  chunk boundaries move. `RedactingSink(inner, redactor)` wraps any `EventSink` (event `data`,
  stream `text`/`data`/`name`; per-`(step_path, kind, attempt)` boundary buffer flushed on
  `step.finished`/`run.finished`/`aclose`; unknown attributes delegate to `inner`, so
  `suspended()` still works). Documented as **exact match, best effort**: it cannot catch a
  value an agent transformed — the load-time refusals remain the guarantee.
- **`rayspec.loader.secrets`** — the placement rules, called from `validate.py` at ONE marked
  site in `_check_refs` plus the include wording; plus `config_secrets_in_use(steps, names)`, the
  textual rule for which `config.secrets` entries a run can read (a name mentioned as a whole
  word in a `shell:`/`python:` body, `env:` or `cwd:`) that makes lazy resolution possible. `check_secret_reference(bare_root, normalized,
  inputs, *, secret_ok) -> SecretVerdict(message, stop)`; `secret_reference_message(name)`,
  `secret_whole_inputs_message(names)` (both byte-identical to the loader's original wording and still
  re-exported from `loader.validate`), `include_secret_input_message(workflow_name, names)`
  (now also names the `RAYSPEC_INPUT_<NAME>` the body already gets). **No rule was relaxed.**

Additive changes to existing modules:

- `config/model.py`: `SecretSourceSpec` (`env`/`file`/`cmd`/`required: bool = True`, exactly one
  source, `.kind`), `RedactSpec(detectors: list[str] = [])` with `resolved_detectors()` (`all`
  expands to `DETECTOR_NAMES = ("github", "openai", "aws", "jwt", "pem")`), `Config.secrets:
  dict[str, SecretSourceSpec] = {}`, `Config.redact: RedactSpec`. A `secrets:` KEY must be a
  usable environment variable name, must not start with `RAYSPEC_` and must not be one of
  `RESERVED_SECRET_NAMES` (`PATH`, `HOME`, `PWD`, `SHELL`, `IFS`, `LD_PRELOAD`,
  `LD_LIBRARY_PATH`, `PYTHONPATH`) — it becomes a step's environment variable, and shadowing one
  of those breaks the step in a way that never points back at the secret. `config/settings.py`:
  `_MERGE_DEPTH` gains `secrets: 1, redact: 1` (merged per key like `aliases`).
- `store/base.py` (frozen, ADDITIVE): `RunStore.redactor: Redactor = NULL_REDACTOR` — part of
  the protocol so the one writer that cannot go through the store (the subprocess pump writing
  `stdout.log`) reads it off the store it was handed, and a rename is a type error rather than a
  silent leak.
- `store/file.py`: `FileRunStore(root, *, redactor=NULL_REDACTOR)` and the mutable
  `store.redactor` attribute (the store is built before the run's secrets are known; the CLI
  assigns the real one at run start). Every writer redacts: `save` (the serialised `run.json`
  payload), `write_output_with_sha` (before hashing, so the sha is the file's — and for
  `kind="json"` the PARSED value is redacted, not the serialised text, so a secret that is a
  bare JSON token cannot turn a valid document into an invalid one), `append_event`,
  `append_stream` (boundary-safe per `(run, step, kind, attempt)`), and `record_step` through
  those two. `flush_streams(run_id, step_path=None)` writes the held-back tail, and
  **`append_event` calls it** on `step.finished` and on `run.finished`/`run.paused` — the events
  the engine emits for every step and every run — so a finished stream is always complete on
  disk and `stream.jsonl` reassembles to exactly what the step produced. **New writes must go
  through the store**: a writer that opens a file under the run dir directly is not covered.
- `engine/context.py`: `RunOptions.config_secrets: Mapping[str, str] = {}` (additive) — the
  resolved `config.secrets`, handed only to `shell:`/`python:` steps.
- `engine/executors/_process.py`: `process_env` adds `ctx.options.config_secrets` under their own
  names, below the step's own `env:` (never in `context.json`, `export_env` or the fingerprint);
  `_pump` redacts `stdout.log`/`stderr.log`, the captured chunks and the emitted stream records
  through a `StreamRedactor` built from `ctx.store.redactor`.
- `cli/commands/run.py`: `_sinks(..., redactor=NULL_REDACTOR)` wraps every sink in a
  `RedactingSink` — the ONE place redaction reaches the console/`--json`. The `run` command
  resolves the `config.secrets` entries this workflow reads (exit 2 with the source's message on
  failure), passes the overlay to `resolve_inputs`, installs the redactor on the store and the
  sinks, prints `warn_unredactable_secrets(out, redactor)` (the `.skipped` names) and puts the
  values in `RunOptions.config_secrets`. `cli/_runs_common.py::resume_run` does the same for
  every resume entry and takes the caller's provider as `secret_provider=` so a `cmd:` helper
  runs at most once per command.
- `cli/commands/resume.py`: `resume_secret_inputs(record, resolved, cli_pairs, *, provider=None)`
  — with a provider, a secret input with a `secrets:` entry is re-fetched instead of demanded;
  the provider is asked ONLY for names `--input` did not supply and a `SecretError` is reported
  next to the `missing secret input(s)` list rather than raised. `secret_provider_for(ctx,
  record)` builds the ONE provider a resume/approve/reject command uses, based at the RUN's
  project root, and it is threaded into `resume_run`.
- `cli/commands/show.py`: `pending_secret_inputs(run)` / `print_secret_inputs(out, run)` — the
  `secret inputs to re-supply:` block for any resumable run; `--json` gains
  `pending_secret_inputs: [...]`; names a `config.secrets` entry supplies are listed as
  `supplied by config.secrets:` instead of being asked for. `cli/commands/doctor.py`: a
  `secrets` row (`NAME ← env GH_TOKEN`, `(absent, optional)`, `(FAILED: …)`, `(too short to
  redact …)`) plus the detector state; never a value, absent entirely when `config.secrets` is
  empty, and `required=True` when it FAILS so `doctor`'s exit code matches what `run` will do.

Decided and **not** to be re-litigated:

- `env:` on a **`prompt:` step stays refused** for secret inputs. Verified live against both
  adapters on 2026-08-21 (claude-agent-sdk 0.2.142 / bundled CLI 2.1.237, openai-codex 0.147.0):
  both deliver the variable to the child and record nothing under `$RAYSPEC_HOME`, but the Codex
  CLI writes `~/.codex/shell_snapshots/<id>.<ts>.sh` (mode `0644`) containing a literal
  `export <NAME>=<value>` line that outlives the run and no redactor can reach.
  `tests/integration/test_secret_placement_live.py` (`RAYSPEC_LIVE=1`) is the reproduction and
  the tripwire: if Codex stops snapshotting, that test fails and the decision may be revisited.
- Binding a secret into an included workflow via `with:` **stays refused** — and is unnecessary:
  `RunContext.secret_env` ignores the scope on purpose, so every secret of the run already
  reaches an include body's `shell:`/`python:` steps as `RAYSPEC_INPUT_<NAME>`, and every
  `config.secrets` entry under its own name.
- Builtin detectors are **opt-in, default off**. A false positive in a run log is worse than the
  gap.
- Tests: `tests/secrets/**` (sources, redactor, store writers, the `_pump` writer, sinks,
  placement) — that package deliberately has **no** `__init__.py`, because `tests/` is on
  `sys.path` and a package named `secrets` there shadows the standard library's for every
  dependency that imports it. `tests/integration/test_e2e_secret_sources.py` is the end-to-end
  leak test; `tests/integration/test_e2e_secret_inputs.py::
  test_a_step_that_echoes_the_secret_no_longer_persists_it` is the inverted secret-input limit.

### schemagen + the published JSON schemas

```python
from rayspec.schemagen import (          # NEW leaf module: the published schemas, from the models
    SCHEMA_BASE_URL,   # "https://raw.githubusercontent.com/rayspec-labs/rayspec-py/main/schemas/"
    SCHEMA_KINDS,      # ("workflow", "run", "events", "stream")
    SCHEMA_SUBJECTS,   # kind -> one-line description (the `rayspec schema` listing)
    JSON_SCHEMA_DIALECT, MODELINE_PREFIX,
    schema_id,         # (kind) -> f"{SCHEMA_BASE_URL}{kind}.schema.json"   (ValueError on unknown)
    build_schema,      # (kind) -> dict   Workflow | RunRecord | RunEvent | StreamRecord
    build_all,         # () -> {kind: schema}
    schema_text,       # (kind) -> the exact bytes of schemas/<kind>.schema.json (indent=2 + "\n")
    schema_filename,   # (kind) -> "<kind>.schema.json"
    modeline,          # (kind="workflow", *, url=None) -> "# yaml-language-server: $schema=…"
)
```
Every `$id` derives from the single `SCHEMA_BASE_URL` constant (a rename stays a one-line
edit + one `scripts/gen_schemas.py` run). `run`/`events`/`stream` are `model_json_schema(
mode="serialization")` (run: `by_alias=True`, so `schema` is the version key) and stay open
(`run.json` ignores unknown keys — forward compatible). The **workflow** schema is generated in
validation mode and then relaxed by a patch table for the spellings a `BeforeValidator` accepts
but the field's type does not describe (`Defaults.timeout|budget_usd|max_tokens`,
`RetryPolicy.delay`, `<Kind>Step.timeout`, the `approve:` string shorthand, `rayspec: 1` as a
`const`); a patch whose path no longer exists raises at generation time, so a model rename fails
loudly instead of silently dropping a relaxation. It is an **editor aid, not the validator** —
`rayspec validate` stays authoritative for the graph, references, includes, agents and
capabilities.

`scripts/gen_schemas.py [--check] [--out DIR]` writes `schemas/{workflow,run,events,stream}.schema.json`
AND syncs the `# yaml-language-server: $schema=…` modeline into every packaged workflow
(`MODELINE_GLOBS`: `init` templates, `examples/*/.rayspec/workflows`, `.rayspec/workflows`);
`--check` exits 1 on any stale file or missing modeline and `tests/schema/test_gen_schemas.py`
runs it, so drift fails the normal gate. That test also validates every packaged workflow against
the generated workflow schema. `tests/events/_validating.py` `ValidatingSink(inner=None)`
(a `CollectingSink` subclass, so it drops into the engine harness) validates every
`RunEvent.to_json()` / `StreamRecord.to_json()` against the generated events/stream schema and
raises `SchemaViolation`; wired into three engine tests.

CLI: `rayspec schema [workflow|run|events|stream] [--out DIR]` — no argument lists the kinds with
their `$id`; a kind prints that schema; `--out` writes the file(s) (all four without a kind) and
prints a modeline for the local copy; an unknown kind is `error: unknown schema '<kind>'` (exit 2,
did-you-mean).

Additive to the frozen `schema` package (`schema/errors.py`):
```python
from rayspec.schema.errors import (
    SchemaProblem,             # frozen dataclass: field, message, loc, kind (pydantic error type),
    #                            hint (did-you-mean), line, source; .location "<file>:<line>",
    #                            .rendered() "<file>:<line>: <field>: <message>", .text(), .to_json()
    problems_from_validation,  # (ValidationError, data) -> [SchemaProblem]   (one per offending KEY)
    expand_schema_errors,      # (SchemaError, data, parse, *, lines=None, max_passes=6) -> SchemaError
    line_of,                   # (LineMap, loc) -> line of loc or of its nearest ancestor, else None
    truncation_problem,        # (dropped, *, source=None) -> the "… and N more problems" entry
    MAX_AGGREGATION_PASSES, MAX_PROBLEMS, UNKNOWN_FIELD,
)
# SchemaError(errors, *, source=None, problems=()) — `.problems` additive; `.errors` keeps the
# "<field>: <message>" entry shape but is now ONE ENTRY PER OFFENDING KEY (was one joined entry
# per pydantic error) and the entry's field path is the key, not the parent mapping:
#   parse_step({"id": "a", "prompt": "x", "allow_failur": True, "timeot": 3})
#   before: ["<root>: unknown field 'allow_failur' …; unknown field 'timeot' …"]   (len 1)
#   now:    ["allow_failur: …", "timeot: …"]                                       (len 2)
# `validate --json` `errors` and the reported "N error(s)" count change accordingly; a truncated
# report (> MAX_PROBLEMS) ends with a "… and N more problems (showing the first 50)" entry.
# str(exc) prints one problem per line as "<file>:<line>: <field>: <message>" when problems are
# known (so `plan`/`run`/`resume` agree with `validate`), else the old "<source>: <entry>".
```
`expand_schema_errors` re-parses the document with the rejected unknown keys removed until
nothing new appears (bounded by `max_passes` AND `MAX_PROBLEMS`, which caps the first pass too),
then stamps every problem with its `file:line`: a single typo at the top level no longer masks
every other mistake, because pydantic rejects the mapping before it validates any field. Only the
exact rejected KEY is ever pruned: `StrictModel._reject_unknown_keys` carries the offending keys
in the pydantic error's `ctx` (`unknown_keys`/`unknown_messages`, additive — the joined `msg` is
unchanged), so a key name is never recovered by re-parsing English, and a problem whose key could
not be identified is marked non-prunable. `line_of` returns `None` for a `loc` that is nowhere in
the line map instead of falling back to the root's line 1. `loader/loader.py` calls it at its two document parse sites (workflow file,
agent file) — the ONE aggregation call site each. `cli/commands/_loader_common.py`:
`error_entries(exc)` renders problems as `<file>:<line>: <field>: <message>`, `error_problems(exc,
*, path)` / `message_problems(messages, *, path)` build the `--json` objects. `validate --json`
rows gain `problems: [{path, line, location, field, message, hint}]` — one object per problem,
`path` never null (additive; `errors` unchanged).

Additive to the frozen `store/model.py`: `RunRecord.toolchain: dict[str, Any] | None = None`
— `{rayspec, python, platform, providers: {id: {sdk_version, cli_version, cli_path[, error]}},
models: {resolved agent key: literal model id | null}}`, captured once at the run's FIRST start (never on a
resume, so a record written before this field existed keeps `None`) by
`rayspec.engine.toolchain.capture_toolchain(ctx, *, timeout_s=TOOLCHAIN_TIMEOUT_S)` (NEW module)
from `Provider.healthcheck(probe=False)`, only for the providers the workflow's prompt-step agents
resolve to (in a dry run: the stub that stood in). Best effort: the providers are probed
CONCURRENTLY under one `timeout_s` and every failure becomes an `error` entry — the probe never
raises, hangs or changes a run. It runs AFTER `run.started` is emitted and takes its provider
instances from the additive `ProviderPool.peek(provider_id)` (create + cache, never `open()`), so
it neither opens a provider that no step uses nor puts a CLI subprocess on the critical path.
`None` in older records. `rayspec show` prints it as the
`toolchain:` block (`show.toolchain_lines(run)`); `show --json` already carries it inside `record`.
There is deliberately no `--strict-toolchain` resume guard.

### plugin entry points — `registry.py`, `cli/plugins.py`, `store/redacting.py`
Four entry-point groups, all with the fixed precedence of `providers/registry.py` (builtins can
never be shadowed; a programmatic registration beats an entry point; anything that fails to load,
has the wrong type, names an id different from its entry-point name, or claims an id another
installed distribution already took is skipped with a `RuntimeWarning` — never an exception into
the CLI. An id is first-come, so a second distribution never replaces the first: it is refused
with a `DiscoveryProblem` and `rayspec plugins` shows it as `skipped`):

| group | value | resolved by |
|---|---|---|
| `rayspec.cli_plugins` | `module:register` (a callable `register(app: typer.Typer) -> None`) | `rayspec.cli.app.build_app()` |
| `rayspec.stores` | `module:REGISTRATION` (`StoreRegistration`) | `rayspec.registry.create_store` |
| `rayspec.sinks` | `module:REGISTRATION` (`SinkRegistration`) | `rayspec.registry.create_sink` |
| `rayspec.approvals` | `module:REGISTRATION` (`ApprovalRegistration`) | `rayspec.registry.create_approval` |

```python
from rayspec.registry import (
    KIND_GROUPS, GROUP_KINDS,          # kind ("store"|"sink"|"approval") <-> entry-point group
    StoreContext,                      # (root, home, project_slug="", settings={})
    SinkContext,                       # (console=None, stream=None, verbose=False, quiet=False, settings={})
    #   stream = the stdout a stdout-shaped sink may write to; None under --json (the CLI owns it)
    ApprovalContext,                   # (console=None, interactive=True, settings={})
    StoreRegistration, SinkRegistration, ApprovalRegistration,   # (id, display_name, factory)
    BUILTIN_STORES,                    # file
    BUILTIN_SINKS,                     # console, json, quiet, null
    BUILTIN_APPROVALS,                 # console
    UnknownExtensionError,             # RayspecError + LookupError; .hint carries did-you-mean
    DiscoveryProblem, discovery_problems, is_registered, reset_registry,
    get_store, list_stores, register_store, create_store,        # + the sink/approval triples
)
# re-exported where they belong: rayspec.store.{create_store,list_stores,register_store,
#   StoreContext,StoreRegistration,RedactingStore} and rayspec.events.{create_sink,list_sinks,
#   register_sink,SinkContext,SinkRegistration} (the events ones stay lazy: importing the models
#   still must not load rich)
```
`rayspec.cli.app`: `build_app()` builds a fresh app (builtins by pkgutil, then plugins); the
module-level `app` is one of those. `rayspec.cli.plugins`: `CLI_ENTRY_POINT_GROUP`,
`PLUGIN_GROUPS` (the four above + `rayspec.providers`), `register_cli_plugins(app)`,
`loaded_cli_plugins()`, `reset_cli_plugins()`, `command_names(app)`, `installed_plugins()`
(`InstalledPlugin(group, name, value, distribution, version, status, detail)` — what
`rayspec plugins [--json]` prints). A CLI plugin may not shadow a builtin command name (the
command is removed again and reported; a plugin that had only part of what it registered
refused is still `ok` and `rayspec plugins` names what was dropped), may not replace the root
callback (the replacement is dropped), and anything it registered before raising is rolled back
— `rayspec --help` exits 0 with a broken plugin installed. `register()` is handed the live
`app.registered_commands` / `app.registered_groups`, so the builtin surface is protected by
object IDENTITY, not by position: the entries that existed before `register()` are put back in
their original order, and a plugin that removed one (`clear()`, `pop()`, a filtered rebuild) is
rolled back whole and reported. Cost: nothing is imported when the group is empty (~2 ms scan).

**Redaction boundary of the store seam.** `create_store` returns every non-builtin store wrapped
in `rayspec.store.redacting.RedactingStore`, which applies the run's `Redactor` to the record
(`create`/`save` — on the parsed value, so a secret that is a bare JSON token cannot make the
record unparseable), the outputs (`write_output`, `write_output_with_sha` — `json` on the parsed
value), the prompt (`write_prompt`), the events (`data`) and the stream records (a
`StreamRedactor` per `(run_id, step_path, kind, attempt)`, flushed on `step.finished`/
`run.finished`/`run.paused` exactly as `FileRunStore` does) BEFORE the wrapped store sees them —
so a plugin store never receives a secret and cannot persist one. Members outside the reviewed
surface are NOT forwarded: `READ_THROUGH` (root, runs_root, exists, resolve_run_id, list_run_ids,
read_events, read_stream, delete_run) passes through, `WRITE_THROUGH` (write_prompt,
write_output_with_sha) passes through a redacting wrapper and keeps `hasattr` answering for the
wrapped store, everything else raises `AttributeError` — a store write added later has to be
implemented on the boundary. The builtin `FileRunStore` is exempt because it applies the same
`Redactor` inside itself (closer to the bytes); the exemption is a property of the builtin table,
not a flag a registration can set. Sinks keep their existing boundary: the CLI wraps EVERY sink
of a run — configured plugins included — in `RedactingSink`.

Additive in `config`: `Config.extensions: ExtensionsSpec` (`sinks: list[str] = []`,
`approval: str | None = None`, `settings: dict[str, dict[str, Any]] = {}`,
`.settings_for(id)`), exported as `rayspec.config.ExtensionsSpec`; `_MERGE_DEPTH["extensions"]
= 2`, so the user-level and project-level blocks merge per key and per extension id (a `sinks:`
list is still replaced as a whole). `extensions.sinks` are
ADDITIONAL observers built next to the CLI's own sink (`cli/commands/run.py: configured_sinks`),
in configuration order; `extensions.approval` replaces the interactive prompt
(`configured_approval` — `None`, i.e. the builtin `ConsoleApprovalPrompt`, when unset, so the
default path is byte-identical to before; the factory is handed the CLI's stderr `Console` as
`ApprovalContext.console`); an unknown id is a usage error (exit 2) with did-you-mean before the
run starts, for both keys and whether or not the run is interactive — a non-interactive run
resolves the approval id it will not use, so a typo fails on a machine without a TTY too. A
factory that RAISES is the same kind of error: whatever it raises is reported as
`sink 'x' failed to build: ValueError: …` with exit 2, never as a traceback and never as exit 1
(the code that means the workflow failed).
`rayspec run` and the in-process resume behind `approve`/`reject`/`resume`
(`_runs_common.resume_run_in_process`) build sinks and prompt the same way, so a run that paused
at a gate delivers the rest of its events — `run.finished` included — to the configured sinks. There is deliberately no `store:` key: the run-management
commands read `$RAYSPEC_HOME/projects/<slug>/runs/` directly, so a non-file store is an
embedding seam (`Runner(store=create_store(...))`) until they go through the registry too.

### engine/context_rebuild + CLI `explain` / `eval` / `plan --render`
```python
from rayspec.engine import context_rebuild
#   from_run(run, resolved, *, store, engine=None, env=None) -> ContextRebuilder
#       views from the stored records + output files; inputs = run.inputs (secrets stay
#       "<secret>"); run.* from run_vars(run, run_dir); env = this process's environment
#   from_plan(resolved, *, inputs, project_root, script: StubScriptLike | None = None,
#       engine=None, env=None) -> ContextRebuilder   every upstream step "succeeded" with the
#       stub script's value or placeholder_output(path) == "<{path} output>"; secret inputs
#       redacted first. Precedence is StubScript.resolve's (exact key, then glob in
#       declaration order) — this module never re-derives it; an entry that came from `match:`
#       is dropped (it keys on a prompt that does not exist before the run).
#   StubScriptLike / StubEntryLike / StubOutcomeLike: the structural types of the consumed stub
#       surface (`script.match`, `script.resolve(path, prompt)`, `entry.outcome_for(n)`,
#       `outcome.has_output/.output/.text`) — `engine` never imports a concrete provider, and
#       stating the surface makes pyright fail if the stub provider reshapes it
#   ContextRebuilder.at(path=None) -> RebuiltContext(record_path: StepPath, def_path, step,
#       record, scope, context, inputs, warnings)   path = a record path ("build[2]/implement")
#       or a definition path ("build/implement" = iteration 1 / item 0); empty/None = the run's
#       root scope. `record_path` is the path that was actually BOUND (a definition path
#       resolves to "build[1]/implement" and finds that record), never the string as typed.
#       Raises ContextRebuildError (RayspecError) for an unknown path, one that descends into a
#       step without a body, or an [index] on a step that is not loop:/each:.
#   RebuiltContext.context is build_context(...) — what render_*/eval_* take.
#   render_body(engine, body, context, *, kind="prompt"|"shell"|"python") -> RenderedBody(
#       kind, text | None, env: dict[str, str] (the RAYSPEC_V<n> slots), error | None) — never raises
#   render_script(engine, body, context, *, kind="shell"|"python") -> RenderedScript   raises like
#       render_shell; a value over SPILL_THRESHOLD is rendered into a scratch dir and replaced by
#       oversize_placeholder(size) ("<N bytes — too large to inline here; read it in the producing
#       step's output file under the run dir>"), so a preview always shows the script and leaves
#       no temporary file behind (`eval --shell` uses it too)
#   env_reference_warning(engine, [(text, kind)]) -> ENV_IS_LOCAL_WARNING | None   the env.* root
#       of a re-evaluated template is this process's environment; nothing records a run's
#   render_step_env(engine, step, context) -> dict[str, str]   the step's env: mapping, rendered
#   view_of_record(record, step, *, store, run_id) -> StepView   output read back from the output
#       file (json parsed), stderr from stderr.log; `items` is not persisted and stays None
#   read_ref(store, run_id, ref) -> str | None; run_vars(run, run_dir) -> the run.* root
```
Rebuild rules (tests in `tests/engine/test_context_rebuild.py`): the scope chain is walked from
the root sibling list down the record path; every *recorded* sibling of a level is visible (a
step with no record is not), composites carry `body_ids` so the "inside loop 'x'" hint still
works; a loop level binds `iteration = {n, max, first, prev}` (`prev` = the previous iteration's
views, `None` on iteration 1), an each level binds `each = {index, total}` + the `as:` item, an
include body is a fresh lexical root with its own `with:`-bound inputs. `each` items and `with:`
bindings are **re-evaluated** in the parent scope (the engine persists neither); a failure
degrades to a placeholder + a warning, and a recorded `item_sha256` that no longer matches is
reported as a warning. A definition path without indices (`build/implement`) reads as iteration 1
/ item 0 — that is what `plan --render` previews.

CLI (all read-only: no provider is created, no step runs, nothing under the run dir is written):
- `rayspec explain <run> <step> [--full] [--json]` — status/skip_reason, the join row (each
  `needs` with its recorded status and what `join_decision` counts it as), the `when:`
  re-evaluated with every operand's value, the `step.retry` events, the resolved agent after
  merge vs. the recorded provider/model, the rendered `env:`, the persisted `prompt:` body from
  `steps/<path>/prompt.txt` (`--full` prints the whole persisted prompt — control characters
  stripped for the terminal; otherwise 20 lines. The agent's rendered `instructions` are
  not persisted and are not shown) or the rendered script + slots, and fingerprint / `reused`
  (from the `step.finished` event) / output ref. The retries and `reused` come from ONE pass
  over `events.jsonl`. A step with no record is still explained, with a warning that the
  sections were re-evaluated rather than replayed; a re-evaluated template that reads `env.*`
  adds `ENV_IS_LOCAL_WARNING`.
- `rayspec eval <run> '<expr>' [--step PATH] [--shell] [--json]` — one expression in that step's
  scope; undefined references print the engine's hint (exit 2), never a traceback. `--shell`
  renders `{{ expr }}` through the shell environment and shows the `${RAYSPEC_V<n>}` slot.
- `rayspec plan <wf> --render [--step PATH] [--stubs FILE] [--json]` — rendered bodies instead of
  the plan; `--json` adds `render: [{path, def_path, kind, agent, model, provider, text, env,
  step_env, error, warnings}]` and `stubs` to the plan payload, where `path` is the RECORD path
  the preview bound (`build[1]/echo`) and `def_path` the definition path. `--step`/`--stubs`
  without `--render` is a usage error (exit 2). The warnings the plain plan prints (loader,
  `on_unsupported: warn`, capability) are printed under `--render` too.
- Presentation helpers shared between the three commands live in `cli/commands/eval.py`
  (`format_value`, `value_type`, `echo_block`, `print_warning`) and are imported by
  `explain.py` / `plan.py`. Untrusted text is never interpolated into a Rich markup string: it
  is printed as a `rich.text.Text` through `safe_text`, or — when a markup string is
  unavoidable (`plan --render`) — through `safe_markup`.

### actor identity + `audit` + the push hook

```python
from rayspec.actor import (  # leaf module: identity resolution, no network, never raises
    resolve_actor,  # (*, env: Mapping | None = None) -> ActorInfo
    #   RAYSPEC_ACTOR > the user's own `git config user.email` > the OS user > "unknown";
    #   fills ActorInfo.ci (detect_ci) and .provider_accounts (provider_accounts).
    #   No workdir parameter, on purpose: every source must be one the RUN cannot write to.
    clean_identity,  # (str | None) -> str | None   safe_text, whitespace-collapsed, capped
    detect_ci,       # (env=None) -> "github-actions" | … | "ci" | None  (CI_ENV_MARKERS, in order)
    git_email,       # () -> str | None  GIT_SCOPES ("--global", then "--system") only — never
    #   a repository's config: a worktree shares .git/config with the repo it came from, so a
    #   shell step could name the actor of the next approval. (GitError/OSError/timeout -> None)
    os_user,         # (env=None) -> str | None      (getpass.getuser, then USER/LOGNAME/USERNAME)
    provider_accounts,  # (env=None) -> {provider id: account}  (PROVIDER_ACCOUNT_ENV only)
    ACTOR_ENV, MAX_ACTOR_LEN, GIT_TIMEOUT_S, GIT_SCOPES, CI_ENV_MARKERS, PROVIDER_ACCOUNT_ENV,
)
```
`ActorInfo` (additive, `store/model.py`): `id`, `source` (`env`|`git`|`os`|`unknown`), `ci: str |
None`, `provider_accounts: dict[str, str]`. It is an **identity, never a credential**:
`PROVIDER_ACCOUNT_ENV` names only variables that carry an *account* (`ANTHROPIC_ACCOUNT`,
`OPENAI_ORG_ID`/`OPENAI_ORGANIZATION`) — an API-key variable is never read, and its presence is
never recorded. Nothing in rayspec grants an authorisation because of an actor.

Additive record fields (`store/model.py`, frozen → additive only):
- `RunRecord.actor: ActorInfo | None = None` — who launched the run. Stamped by the runner on a
  FRESH record only (`Runner._prepare_record(pid_started_at, actor)`, resolved off the event loop
  like `pid_started_at`), so a resume never rewrites it. `None` in older records.
- `Decision.actor: ActorInfo | None = None` — who decided. `rayspec approve|reject`
  (`cli/commands/approve.py:record_decision`) stamps it; `by` still says which door the decision
  came through (`cli`/`tty`/`--yes`/`dry-run`). The `run.decision` event gains an optional `actor`
  key (the stored decision's actor, else `run.actor`) — `run.pause` is cleared the moment a gate
  consumes a decision, so the event log is the durable record of who approved.

```python
from rayspec.store.file import (
    AUDIT_JSONL,      # "audit.jsonl" — the optional local ledger in the run dir
    AUDIT_ENV,        # "RAYSPEC_AUDIT_LOG" — 1/true/yes/on turns it on (off by default)
    AUDIT_DETAIL_CAP, # 1000 characters of a row's ``detail``
    audit_log_enabled,      # (env=None) -> bool
    audit_entry_for_event,  # (RunEvent) -> {ts, kind, step, detail, data} | None
    audit_entry_for_stream, # (step_path, StreamRecord) -> row | None
)
# FileRunStore(root, *, redactor=NULL_REDACTOR, audit: bool | None = None)
#   audit: True/False pin the ledger on/off; None (default) asks AUDIT_ENV at write time
#   .read_audit(run_id) -> Iterator[dict]  torn trailing line ends it, bad middle line skipped
# Rows are appended by create() (kind "run", detail "created", data.actor), append_event()
#   and append_stream(); row kinds: run | step | command | tool | file | warning | approval.
#   Progress events (loop.iteration, each.item) produce no row. A stream row is derived from the
#   ORIGINAL record, before the boundary buffer, and the row is redacted with
#   Redactor.redact_obj (VALUES, not serialised text).
```
The ledger is a **log**: append-only in behaviour, no chain, no digest, nothing about the file
proves it was not edited. It is local to one run of one user on one machine. There is no export
format, no continuous export and no aggregation across runs, projects or people — deliberately.

```python
from rayspec.workspace.git import (
    PushOutcome,   # frozen: branch, remote, pushed: bool, reason: str | None
    push_branch,   # (workdir, branch, *, remote="origin", timeout=PUSH_TIMEOUT_S) -> PushOutcome
    #   `git push --set-upstream <remote> refs/heads/<b>:refs/heads/<b>`; NEVER forces; never
    #   raises — no branch / no remote / rejected / timeout / no git all come back as a reason
    push_remote,   # (env=None) -> str | None   RAYSPEC_PUSH_BRANCH: 1|true|yes|on -> "origin",
    #   any other non-empty value names the remote, unset/0/false/no/off -> None
    PUSH_ENV, DEFAULT_REMOTE, PUSH_TIMEOUT_S,  # "RAYSPEC_PUSH_BRANCH", "origin", 60.0
)
```
`Runner._publish_branch(ctx)` runs in `_finalize`, after `workspace.info()` and before
`run.finished` — so it fires on a pause and on every final status. It imports
`rayspec.workspace.git` through `import_optional` (like the workdir lock and `_refresh_head_sha`),
skips dry runs and `isolation == "none"`, and turns a failed push into `ctx.warn` only: the run's
status and exit code are unaffected. A successful push is silent.

```python
from rayspec.cli.commands.audit import (  # `rayspec audit <run> [--commands] [--json] [--root]`
    collect_rows,    # (store, run) -> rows from events.jsonl + every recorded step's stream.jsonl,
    #                  through the store's own row derivation, sorted by ts (ties keep read order)
    audit_payload,   # (store, run, *, commands) -> {run_id, workflow, status, actor, workdir,
    #                  branch, rows}
    is_command_row,  # a "command" row, or a step row whose data.kind is shell/python
    print_audit, rows_table, actor_line, ROW_STYLES, COMMAND_STEP_KINDS,
)
```
`rayspec audit` is **read-only**: it opens `run.json`, `events.jsonl` and the step streams through
the store and prints them. It never writes, never re-runs anything and never opens a socket. Every
cell goes through `rayspec.textsafe.safe_text`.

## Pinned semantics (settled early — do not re-litigate)

- **Identifiers**: step ids, `as:`, `session:` targets use `Identifier` (snake_case, not a reserved
  context root: `inputs steps run project env iteration each loop self true false none null`).
  Dict keys of `inputs:`/`agents:`/`outputs:` use `Name` (syntax only). `item` is NOT reserved
  (it is the default `as:`).
- **Retry**: `RetryPolicy.attempts` = TOTAL attempts (1 = no retry); `delay` doubles each retry.
  `retry: None` on a leaf step means the kind default: `DEFAULT_PROMPT_RETRY`
  (`attempts=3, delay=3s, on_error=transient`) for `prompt:`, none for `shell:`/`python:`.
  Timeouts count as transient only with `on_error: all`.
- **allow_failure** is valid on ANY step: the outcome is recorded as `failed` + `tolerated=True`
  (`ok=False`), joins treat it as satisfied, run status is unaffected.
- **StopSpec.status** defaults to `cancelled`. `timeout` must be > 0 (`PositiveDuration`).
- **Deep rendering** of `outputs:`, `with:`, `env:` values: a `str` is a template, dict/list are
  recursed, other scalars pass through; a template that is exactly one `{{ expr }}` keeps its
  Python type; `env:` values are str-coerced afterwards.
- **Every succeeded step writes an output file** (approve → the approver's comment, `''` if none;
  composites → JSON) or resume
  will re-run it. `StepRecord.reusable` only checks status/tolerated/`output_ref`; callers also
  check file existence and `always_run`.
- **run.json == `RunRecord.model_dump_json()`** (alias `schema`); unknown keys are ignored on
  load (forward compatible). `RunRecord.model_dump()` (python mode) is not JSON-serialisable.
- **Persistence vs observation**: the engine persists through the `RunStore`
  (`append_event` → events.jsonl, `append_stream` → steps/<path>/stream.jsonl, `save` → run.json).
  `EventSink`s are observers (console, json-stdout, collecting); there is no JSONL sink — the store
  owns the files. Executors write `stdout.log`/`stderr.log` into `store.step_dir()` directly.
- **AgentEvent.ts** is `time.time()` seconds; `0.0` = unset → `StreamRecord.from_agent_event`
  stamps now. `StreamRecord.kind` for shell steps: `stdout`, `stderr`, `exit`. A `usage`
  AgentEvent carries `data["usage"]` (this report's delta) and `data["turn_total"]` (cumulative
  usage of the attempt so far) as `{input, cached_input, cache_write, output, reasoning}` dicts
  — the engine records `turn_total` for an attempt cut off before its result.
- **RunEvent.data** keys: `step.started {kind, attempt}`, `step.retry {attempt, delay_s, error}`,
  `step.finished {status, duration_ms, usage, cost_usd, error, skip_reason, tolerated}`,
  `loop.iteration {n, max}`, `each.item {index, total}`, `run.finished {status, reason, usage,
  cost_usd, cost_source?}`, `run.paused {token, step, message}`, `workspace.created {workdir, branch, base_sha}`.
- **Agents**: `AgentDef` has `thinking: bool | None` (capability `thinking`) and
  `mcp: {name: McpServerDef}` (capability `mcp_servers`); `AgentRequest.thinking` mirrors it.
- **Provider.open(run_id, workdir, env, max_parallel)** acquires per-run resources.
- `parse_step`/`Workflow.steps` are typed as the `StepModel` union (narrow with `isinstance`).

## Examples and dogfood workflows

- `examples/<name>/` is a self-contained project: `.rayspec/{workflows,agents,prompts,config.yaml}`,
  a `stubs.yaml` for `rayspec run <wf> --dry-run --stubs stubs.yaml`, a `checks.yaml` (validate /
  plan / dry-run scenarios with expected status, exit code, outputs, step statuses; optional
  `env: {NAME: value|null}` pins or unsets process env per scenario) and a README.
  `examples/README.md` holds the coverage matrix (`| Capability | Examples | Notes |` tables) that
  `tests/examples/test_examples.py` parses — every required capability row must name an existing
  example, and every backticked token of a row must occur in the named examples' trees/READMEs
  (`unbacked_claims`).
- The repo's own workflows live in `.rayspec/workflows/` (`review_pr`, `fix_issue`,
  `implement_feature_tdd`, `docs_sync`, `release_check`) with agents in `.rayspec/agents/`,
  prompts in `.rayspec/prompts/` and dry-run checks + stubs in `.rayspec/dryrun/`.
- `scripts/check_examples.py [--only NAME] [--verbose] [--matrix]` runs every `checks.yaml`
  (examples + dogfood) through `rayspec.testing` under a temporary `RAYSPEC_HOME` and exits 1 on
  any failure (2 for an unknown `--only` suite or a malformed case file); it owns
  only the coverage matrix and one CLI contract smoke per suite (`rayspec run --dry-run --json`
  through the Typer app, checked with `json_stream_problems`) — the case format and the per-case
  driver live in `src/rayspec/testing/`. It exposes `discover_suites`, `run_check`, `load_checks`,
  `Check`/`Expect`/`Suite`/`CheckResult`/`CheckFileError`, `_invoke`, `json_stream_problems`,
  `parse_coverage_matrix`, `matrix_needles`, `unbacked_claims` for the tests. Examples use only features that exist on `main`; planned-but-missing features are listed
  in `examples/README.md` instead of invented.

## Testing conventions
- `uv run pytest` must be green; tests live under `tests/<area>/`.
- anyio pytest plugin (`pytestmark = pytest.mark.anyio`; `anyio_backend` fixture = "asyncio").
- No network in unit tests; real SDK calls only under `-m live`.
- Every module ships at least one end-to-end style test of its public surface.

### CLI ↔ workspace seam
`rayspec.cli.commands.run.prepare_workspace(*, project_root, home, workflow_name, run_id, isolation,
base, repo, config, notice) -> (engine Workspace, slug | None, project_root)` calls
`rayspec.workspace.prepare_workspace(project_root, home=, workflow_name=, run_id=, isolation=, base=,
repo_arg=, config=)` and forwards `Workspace.notice`. The CLI generates the run id (`new_run_id()`)
before preparing the workspace (branch `rayspec/<wf>-<shortid>` needs it) and passes it to
`Runner(run_id=)`. With `--repo`, the workspace is prepared **before** the workflow is loaded (the
resolved project root is where workflows come from; isolation = flag, default worktree). On
`--resume`, the workspace is rebuilt from the stored `run.json` (`workspace_from_record`) — the
runner overwrites `run.workspace` with what the CLI passes. Only the *absence* of the workspace
module degrades to in-place; a signature mismatch raises (covered by
`tests/integration/test_cli_workspace_seam.py`).

## `rayspec test` and the testing harness

`src/rayspec/testing/` is a **shipped** package (it is part of the wheel, so a project can run its
own suites in CI and import the pieces from pytest). It depends on loader + engine + providers +
store; nothing depends on it.

- **`spec.py`** — `Case`, `Expect`, `StepExpect` are `StrictModel`s; unknown keys are refused with
  a did-you-mean and the `file:line` of the offending key, and every problem of a file is reported
  together (`CaseFileError.errors`, one `<file>:<line>: <message>` entry each). Field names are the
  YAML keys, except `validate:` → `Case.validate_` (alias). `expect.steps` accepts a bare status
  string (`review: succeeded`) or a mapping `{status, skip_reason, output_regex, output_json}`;
  `output_json` is distinguished from "unset" through `model_fields_set`.
  A `run: false` / `validate: error` case that also carries an `expect:` block is **refused at
  load time** (`unreachable_expect(case)` names the reason, `UNREACHABLE_EXPECT_HINT` the fix) —
  those assertions are never evaluated, so the case would report `ok` whatever it claims.
  `discover_suites(root)` returns `Suite(name, root, checks_path, checks, locations, checks_label)`
  for
  `examples/<name>/checks.yaml` (rooted at the example), `.rayspec/dryrun/checks.yaml` (`dogfood`,
  rooted at the project) and `.rayspec/tests/<workflow>/<case>.yaml` (`tests/<workflow>`, rooted at
  the project; the directory names the workflow, the file stem the case). Discovery of a
  greenfield directory skips a document that `is_case_document()` recognises as *something else* —
  top-level keys a non-empty subset of `STUB_SCRIPT_KEYS` (`steps`/`match`/`defaults`) naming no
  case key, i.e. a stub script kept next to its case; everything else (empty document, a typo, bad
  YAML) is read as a case so its problems are reported. A case id repeated across the files of one
  directory is refused naming both files. `Suite.location(id)` is a `CaseLocation` whose
  `.of("expect", "status")` renders the `<file>:<line>` of any expectation, falling back to the
  closest known ancestor; for a directory suite `checks_path` is a *directory* and `checks_label`
  is its repo-relative rendering, which is what the fallback prints.
- **`runner.py`** — `run_case(suite, case, *, home, exec_shell=False, keep_run_dir=True)` →
  `CaseResult`; **never raises** — any unexpected exception becomes an `internal` failure carrying
  the traceback, so one broken case cannot lose a suite (or its `--junit` file). **`exec_shell` is
  the caller's authorisation and is never read from the case**: `case.exec_shell` is a declaration
  that `cli/commands/test.py` checks against `--exec-shell`, so a committed data file can never
  widen what the command does. It loads and validates the workflow (`validate: error` is
  satisfied by a load error *or* a validation error), then drives `Runner` with
  `RunOptions(dry_run=True, exec_shell=…, interactive=False, stub_script=…)`, a `CollectingSink`,
  `Workspace.in_place(suite.root)`, `handle_signals=False` and no `home=` (so no path lock). The
  store is `FileRunStore(home / "projects" / fallback_project_slug(suite.root))` — the project's
  ordinary store, so `rayspec logs <run_id>` explains a failure. `case_environment` clears
  `RAYSPEC_INPUT_*`, sets `RAYSPEC_HOME`/`NO_COLOR` and applies the case's `env:` (`null` unsets),
  restoring `os.environ` afterwards. With `keep_run_dir=False` a *passing* case deletes its run dir
  (what the CLI does); a failing one always keeps it.
- **`report.py`** — `Failure(field, summary, detail, fix, location)` renders the house four-line
  block (`<field>: <claim>` / `  <detail>` / `  fix: …` / `  at <file>:<line>`). `CaseResult` has
  `.ok`, `.failures`, `.status`, `.run_id`, `.run_dir`, `.duration_s`, `.events`, `.report()`.
  `junit_xml(results)` and `results_json(results)` are the `--junit` / `--json` shapes;
  `junit_error_xml(message, detail=…)` is the one-erroring-`<testcase>` document a usage exit
  writes (`errors="1"`), so `--junit` always produces a file.
- **`cli/commands/test.py`** — `rayspec test [<workflow>] [--case ID] [-k/--select PATTERN]
  [--junit FILE] [--json] [--exec-shell] [--root DIR]`; exit `0` all passed, `1` any failed, `2`
  usage (a filter matching nothing, no cases at all, a selected case declaring `exec_shell: true`
  without `--exec-shell`) or a malformed case file. `--junit` is written in **every** case,
  including a usage exit. It calls `make_context(project_env=False)`: a case is a dry run against
  the stub provider and needs no credentials, so the project's `.rayspec/.env` must not reach the
  process (it would otherwise be inherited by anything `--exec-shell` starts, and by the failure
  reporter's output).

### Golden run corpus (`tests/golden/`)
`tests/golden/<suite>/<case>/{events.jsonl,run.json,summary.json}` is the masked output of
`rayspec run --dry-run --json --stubs` for every runnable case. `_capture.py` masks by key
(`MASKED_KEYS`: run ids, timestamps, durations, pid, host, absolute paths, git branch, content
hashes — the placeholder keeps the JSON type, and a key is masked *unconditionally*, `None`
included, because `workspace.branch` is null on a detached-HEAD checkout) and by text substitution
over every string. `USAGE_KEYS` (`input`/`output`/`cached_input`/`cache_write`/`reasoning`) are
masked to `0` wherever a `usage:` mapping appears: the stub derives its default counts from the
prompt (`len(req.prompt) // 4`) and a prompt may embed `{{ run.workdir }}`, so an unmasked count
is a function of the checkout path's length. **The corpus must capture byte-identically from two
differently-named checkouts, detached HEAD included** — that is the acceptance test for a change
to the masking. A case with no committed corpus *skips*; a
deleted one is red (`MINIMUM_COVERED`), and a malformed case file anywhere in the repo is one
failing test, never a collection error.
`RAYSPEC_UPDATE_GOLDEN=1 uv run pytest tests/golden` regenerates. **A change to the `--json` event
stream, the summary object or `run.json` must show up as a diff in this corpus in the same PR** —
that is what it is for.

### Fault-injecting store (`tests/engine/_faulty_store.py`)
`FaultyStore(inner, fault=FaultPoint(method, n, when))` implements the `RunStore` protocol and
raises `StoreCrash` at the n-th `save`/`write_output`/`append_event`/`append_stream`. `when` is
`before` or `after` the write lands, or `torn` — only for the JSONL writers (`LINE_METHODS`) —
which appends the first half of the serialised line and dies, the only way to exercise the store's
promise that readers tolerate a torn trailing line; the file it tore is on `store.torn_path`. Once
fired it stays dead, so nothing is persisted afterwards. `enumerate_points(counts)` turns one clean
run's call counts into the crash points (16 whole-call points + one torn point per JSONL writer).
`tests/engine/test_resume_faults.py` asserts that resuming converges on the same status, per-step
statuses and outputs from all 18 of them, and that `read_events`/`read_stream` survive the torn
line. Note for stub authors: a `sequence:` entry counts calls
in the provider **instance**, so it is not replay-stable across a resume — key loop-body stubs by
record path (`build[2]/review`) when the run may be resumed.
