# Extending rayspec

rayspec is built around a few seams. This page shows the ones that are usable today and names
the ones that exist as protocols but have no plug-in mechanism yet. The authoritative surface
list is `CONTRACTS.md` at the repository root.

## Adding a provider (entry points)

A provider is a `ProviderRegistration` exposed through the entry-point group
`rayspec.providers`. Its **capabilities are static data** so `rayspec validate`/`plan`/`providers`
never import your SDK; the `factory` is called lazily by `rayspec run`.

```toml
# pyproject.toml of your plugin
[project.entry-points."rayspec.providers"]
acme = "acme_rayspec:REGISTRATION"     # the entry-point NAME must equal the provider id
```

```python
# acme_rayspec/__init__.py
from collections.abc import Mapping
from typing import Any

from rayspec.providers.base import (
    AccessLevel, AgentEvent, AgentRequest, AgentResult, EmitFn, ProviderCapabilities,
    ProviderHealth, ProviderRegistration, Usage,
)

CAPABILITIES = ProviderCapabilities(
    structured_output="best_effort",          # enforced | best_effort | none
    session_resume=False, session_fork=False,
    instructions_modes=frozenset({"append"}),
    access_levels=frozenset({AccessLevel.READ_ONLY, AccessLevel.WORKSPACE_WRITE}),
    tool_groups=frozenset({"read", "edit", "shell"}),
    raw_tool_names=False, max_turns=True, budget_usd=False, cost_reporting=False,
    effort_levels=frozenset({"low", "medium", "high"}),
)


class AcmeProvider:
    id = "acme"
    capabilities = CAPABILITIES

    def __init__(self, settings: Mapping[str, Any]) -> None:   # config.yaml → providers.acme
        self.settings = dict(settings)

    async def open(self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int) -> None:
        ...                                                     # per-run resources

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        await emit(AgentEvent(kind="text_delta", text="hello"))
        return AgentResult(status="success", text="hello", usage=Usage(input=10, output=2))

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True, sdk_version="1.0")

    async def aclose(self) -> None:
        ...


REGISTRATION = ProviderRegistration(
    id="acme", display_name="Acme Agents", capabilities=CAPABILITIES, factory=AcmeProvider
)
```

Rules the registry enforces: builtin ids (`claude`, `codex`, `stub`) cannot be overridden; a
programmatic `rayspec.providers.registry.register(registration)` wins over an entry point of the
same id; an entry point that fails to load, is not a `ProviderRegistration`, or registers a
different id than its name is skipped with a `RuntimeWarning`. Once installed, `rayspec providers`
lists it as `plugin`, agents can say `provider: acme`, and `providers.acme` in `config.yaml` is
passed to the factory.

Contract details worth knowing:

- `run()` returns an `AgentResult` for *result-level* outcomes (`error`, `timeout`, `max_turns`,
  `budget`) and raises `ProviderError(transient=…, kind=…, hint=…)` only for infrastructure
  failures (CLI missing → `ProviderNotInstalledError`, transport death, auth); transient errors
  are retried by the step's retry policy;
- honour `req.timeout_s` and anyio cancellation (the engine cancels through anyio only — use
  `anyio.fail_after`, `CancelScope(shield=True)` for cleanup; no raw `asyncio` primitives);
- emit events with `AgentEvent(kind=…)`; `ts` may stay `0.0` (the recorder stamps it);
- with `structured_output="enforced"` put the parsed value into `AgentResult.structured`; with
  `best_effort` the engine appends a JSON instruction to the prompt and extracts the JSON itself;
- translate `req.tools` with `rayspec.providers._tools.translate_tools(...)` if you want the
  neutral vocabulary handled for you (it returns the entries addressed to your id plus warnings
  and errors);
- report usage as `Usage(input, cached_input, cache_write, output, reasoning)` and
  `cost_usd`/`cost_source="provider"` when your SDK knows the price; otherwise users configure
  `pricing:`.

Tests: `tests/providers/` shows the pattern — fake SDK objects, no network; the stub provider
(`rayspec.providers.stub`) is a complete reference implementation.

## Step kinds

The step kinds are a closed Pydantic union (`rayspec.schema.steps.STEP_MODELS`) and the executors
are looked up by kind (`rayspec.engine.executors.default_executors()`); the `Runner(executors=
{kind: fn})` parameter lets tests and embedders override an executor for an existing kind
(`async (step, scope, ctx, record, attempt) -> StepOutcome`). There is **no** plug-in mechanism
for *new* kinds — by design: the [constitution](constitution.md) keeps the step schema to the
governance set, and new behaviour belongs in `shell:`/`python:` steps or on the agent.

## Event sinks

`rayspec.events.base.EventSink` is the observer protocol (`emit(RunEvent)`,
`emit_stream(step_path, StreamRecord)`, `aclose()`); sinks never raise into the engine — and
should one raise anyway (a closed stdout, Rich's `SystemExit` on a broken pipe) the engine drops
its sinks and finishes the run with the store as the only observer. Built in: `QuietConsoleSink`
(one line per event; subclass and override `format_<event>()` to change a line), `ConsoleSink`
(the Rich Live tree `rayspec run` shows on a TTY; it degrades to one line per step when stdout is
not a terminal; `--quiet` swaps it for a problems-only line sink), `JsonStdoutSink` (`--json`), `CollectingSink` (tests), `NullSink`, `MultiSink`
(fan-out).
When embedding (`rayspec.engine.runner.Runner(sinks=MultiSink([...]))`) you can add your own; the
CLI has no flag to load sinks from plugins yet. Persistence is not a sink: the store owns
`events.jsonl` / `stream.jsonl`.

## Run stores

`rayspec.store.base.RunStore` is the persistence protocol (`create`, `save`, `load`, `list_runs`,
`write_output`, `read_output`, `append_event`, `append_stream`, …); `FileRunStore(root)` is the
only implementation (layout in [runs-and-resume.md](runs-and-resume.md)). `Runner(store=…)`
accepts any implementation; the CLI always uses the file store under `$RAYSPEC_HOME`. A SQLite
store is a post-v1 seam.

## Embedding the engine

```python
from pathlib import Path
from rayspec.engine.runner import Runner
from rayspec.loader import load_workflow, resolve_inputs, validate_workflow
from rayspec.store.file import FileRunStore

rw = load_workflow("review", project_root=Path("."))
report = validate_workflow(rw)              # never raises; check report.errors
values = resolve_inputs(rw.workflow, cli_pairs=["target=src"])
result = Runner(rw, inputs=values, store=FileRunStore(Path("/tmp/rayspec-store")),
                project_root=Path(".")).run_sync()
print(result.status, result.exit_code, result.outputs)
```

`Runner` also accepts injected `providers={"claude": StubProvider(...)}`, `sinks`, an
`approval_prompt` (`async (ApprovalRequest) -> ApprovalAnswer | None`, `None` = pause),
`workspace`, `RunOptions(dry_run=…, yes=…, fail_fast=…, resume=…)` and `resume_run_id`.

## Roadmap

Planned, in rough order: third-party providers via entry points, `rayspec pick`, runtime child
runs (a `workflow:` step) and run-level `defaults.timeout_total`.

Shipped since this page was written: `secret: true` inputs (1.0.0) and the run-level
`defaults.budget_usd` / `defaults.max_tokens` circuit breaker.

A sink may run a command; it may not open a socket. Notification sinks are therefore `exec:`-shaped
— the engine spawns a process and never makes a network call itself (`docs/constitution.md`).
