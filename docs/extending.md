# Extending rayspec

rayspec is built around a few seams. Everything on this page is a documented entry point: a
separate package can add a command, a provider, a run store, an event sink or an approval prompt
without forking rayspec and without rayspec knowing the package exists. The authoritative surface
list is `CONTRACTS.md` at the repository root.

| entry-point group | what it adds | how it is selected |
|---|---|---|
| `rayspec.cli_plugins` | a CLI command | it appears in `rayspec --help` |
| `rayspec.providers` | an agent provider | `provider: <id>` on an agent |
| `rayspec.stores` | a run store | `create_store("<id>", …)` when embedding |
| `rayspec.sinks` | an event sink | `extensions.sinks: [<id>]` in `config.yaml` |
| `rayspec.approvals` | an approval prompt | `extensions.approval: <id>` in `config.yaml` |

`rayspec plugins` lists what is installed under each group, which distribution and version it
came from, and — for anything that was refused — why. It is the first command to run when a
command, store or sink shows up that you did not write.

The rules are the same for every group: **builtin ids can never be overridden**, entry points are
visited in name order, a programmatic registration wins over an entry point, and anything that
fails to load, has the wrong type or collides is skipped with a `RuntimeWarning` instead of
breaking the CLI. An id is first-come: if two installed distributions publish the same one, the
first keeps it and the second is refused and listed as `skipped` in `rayspec plugins` — nothing
is ever silently replaced, so an unexpected implementation always has a row explaining itself.

## A worked example: one command and one sink

A complete package. Three files, nothing rayspec-specific in the build.

```toml
# pyproject.toml
[project]
name = "acme-rayspec"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["rayspec", "typer"]

[project.entry-points."rayspec.cli_plugins"]
acme = "acme_rayspec.cli:register"          # value = module:callable

[project.entry-points."rayspec.sinks"]
acme-log = "acme_rayspec.sink:SINK"         # value = module:REGISTRATION

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```python
# acme_rayspec/cli.py — the same shape as any builtin command module
import json
from pathlib import Path

import typer

from rayspec.cli import _runs_common as runs_common


def register(app: typer.Typer) -> None:
    """Called once at startup with rayspec's Typer app."""

    @app.command("acme-runs")
    def acme_runs(
        root: Path = typer.Option(None, "--root", help="Project root."),
        json_: bool = typer.Option(False, "--json", help="Print as JSON."),
    ) -> None:
        """List this project's runs the acme way."""
        ctx = runs_common.make_runs_context(root)
        rows = [
            {"run_id": r.run_id, "status": r.status.value, "workflow": r.workflow_name}
            for r in ctx.store.list_runs(limit=10)
        ]
        if json_:
            typer.echo(json.dumps(rows))
            return
        for row in rows:
            typer.echo(f"{row['run_id']} {row['status']} {row['workflow']}")
```

```python
# acme_rayspec/sink.py — an observer of one run
from pathlib import Path

from rayspec.events.model import RunEvent, StreamRecord
from rayspec.registry import SinkContext, SinkRegistration


class AcmeLogSink:
    """Appends one line per lifecycle event to a file named in config.yaml."""

    def __init__(self, context: SinkContext) -> None:
        self.path = Path(context.settings.get("path", "acme.log"))

    async def emit(self, event: RunEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{event.ts.isoformat()} {event.type.value} {event.step_path or '-'}\n")

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        pass  # the per-step transcript; ignore it or write it too

    async def aclose(self) -> None:
        pass


#: The entry point points HERE, not at the class: the id is part of the registration.
SINK = SinkRegistration(id="acme-log", display_name="Acme log", factory=AcmeLogSink)
```

Install it (`uv pip install -e .` next to rayspec) and it is live:

```bash
rayspec plugins                 # acme-rayspec 0.1.0 — ok, adds acme-runs
rayspec acme-runs --json        # the new command
```

The sink is opt-in, because a sink observes every run:

```yaml
# .rayspec/config.yaml (or ~/.rayspec/config.yaml)
extensions:
  sinks: [acme-log]             # added NEXT TO the console/--json sink, in this order
  settings:
    acme-log: {path: /tmp/acme.log}
```

`settings` is the mapping the factory is handed as `context.settings` — the same idea as
`providers.<id>` for a provider. An id that names nothing fails the run before it starts, with
the usual did-you-mean; so does a factory that raises — validate your settings in `__init__` and
the message you raise is what the user reads (`sink 'acme-log' failed to build: …`, exit 2).

## Adding a command

The entry-point value resolves to a callable `register(app: typer.Typer) -> None` — exactly what
`rayspec/cli/commands/<name>.py` exposes, so a third-party command module is literally the same
code as a builtin one, and the same `typer` version is already installed.

What the loader guarantees, so that an installed plugin can never make rayspec unusable:

- builtin commands are registered first and are never shadowed: a plugin command whose name is
  already taken is removed again and reported with a `RuntimeWarning` naming the plugin and the
  name it wanted;
- `register()` is handed rayspec's live command table, and the builtin entries in it are
  protected by identity: reordering them is harmless, and a plugin that *removes* one is rolled
  back whole (its own commands go with it) and reported — installing a package can never take a
  rayspec command away;
- a plugin that fails to import, is not callable, or raises inside `register()` is skipped —
  anything it managed to add before raising is rolled back;
- the root callback belongs to rayspec: a plugin that replaces it (`@app.callback()`) has the
  replacement dropped, so `--version` and the global help keep working;
- `rayspec --help` therefore still exits 0 with a broken plugin installed, and `rayspec plugins`
  shows what happened.

Keep the work in `register()` to registering: it runs on **every** rayspec invocation. Import
your heavy modules inside the command body, the way the builtin commands do — the scan costs
about two milliseconds when nothing is installed, and nothing is imported at all when the group
is empty.

Useful pieces of the CLI layer, all public: `rayspec.cli._runs_common` (project/home/config
resolution, run lookup with friendly errors, formatting), `rayspec.cli.commands._loader_common`
(`make_context`, `fail`, `err_console`) and `rayspec.cli._docs.docs_url` for hints that cite a
page.

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
not a terminal; `--quiet` swaps it for a problems-only line sink), `JsonStdoutSink` (`--json`),
`CollectingSink` (tests), `NullSink`, `MultiSink` (fan-out).

Registered ids: `console`, `json`, `quiet`, `null` — plus anything installed under
`rayspec.sinks`. `extensions.sinks` in `config.yaml` names the ones a run adds — including the
half of a run that happens after a pause, so `rayspec approve`/`reject`/`resume` deliver the
remaining events and the final `run.finished` to the same sinks `rayspec run` did; when embedding,
`rayspec.events.create_sink(id, SinkContext(...))` resolves one and
`Runner(sinks=MultiSink([...]))` takes whatever you built yourself. Persistence is not a sink:
the store owns `events.jsonl` / `stream.jsonl`.

**Redaction is not your problem.** Every sink of a run — builtin or plugin — is wrapped in
`rayspec.redact.RedactingSink` where the CLI assembles them, so a declared secret is already
replaced by `[REDACTED:<name>]` in the events and stream records your sink sees. Do not try to
redact again, and do not read secrets out of the environment to "un-redact" anything.

A sink may run a command; it may not open a socket. Notification sinks are therefore `exec:`-shaped
— the engine spawns a process and never makes a network call itself (`docs/constitution.md`).
`examples/notify_webhook` delivers a webhook from a `shell:` step, which is the idiom.

## Approval prompts

`rayspec.engine.approval.ApprovalPrompt` is `async (ApprovalRequest) -> ApprovalAnswer | None`:
return `ApprovalAnswer(approved=…, comment=…)` to decide, `None` to pause the run (exit 3) so a
human can `rayspec approve`/`reject` it later. The builtin is the terminal panel
(`ConsoleApprovalPrompt`, id `console`); `extensions.approval: <id>` in `config.yaml` swaps it
for an installed one — a policy engine, a prompt that shells out to a paging tool, a queue.

```python
from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.registry import ApprovalContext, ApprovalRegistration


class PolicyApproval:
    def __init__(self, context: ApprovalContext) -> None:
        self.allow = set(context.settings.get("auto_approve", []))

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        if request.step_path in self.allow:
            return ApprovalAnswer(approved=True, comment="approved by policy")
        return None                      # pause; a human decides with `rayspec approve`


APPROVAL = ApprovalRegistration(id="policy", display_name="Policy", factory=PolicyApproval)
```

A prompt is only ever asked when the run is interactive (a TTY, no `--yes`/`--no-interactive`);
otherwise the gate pauses without asking anyone — but the id is resolved either way, so a typo in
`config.yaml` fails the run with did-you-mean on a machine without a TTY as well. `context.console`
is the CLI's `rich` console (stderr) when there is one, so a prompt can render a panel without
building its own.

Keep it fast and cancellable: it runs inside the run's task group.

## Run stores

`rayspec.store.base.RunStore` is the persistence protocol (`create`, `save`, `load`, `list_runs`,
`write_output`, `read_output`, `append_event`, `append_stream`, …); `FileRunStore(root)` is the
builtin (layout in [runs-and-resume.md](runs-and-resume.md)). Register another one under
`rayspec.stores` with a `StoreRegistration`, whose factory is handed a `StoreContext`
(`root`, `home`, `project_slug`, `settings`), and resolve it with
`rayspec.store.create_store(id, context)`; `Runner(store=…)` accepts any implementation.

```python
from rayspec.registry import StoreContext, StoreRegistration


class SqliteRunStore:
    def __init__(self, context: StoreContext) -> None:
        self.db = context.root / "runs.sqlite"
    # ... the RunStore protocol


STORE = StoreRegistration(id="sqlite", display_name="SQLite run store", factory=SqliteRunStore)
```

**Where redaction sits.** A store persists everything a run produces, so this seam is the one
place a plugin could leak a secret. It cannot: `create_store` never hands a third-party store the
raw payload. Every store that did not come from the builtin table is returned wrapped in
`rayspec.store.redacting.RedactingStore`, which applies the run's `Redactor` to the record, the
outputs, the prompt, the events and the stream records (buffering across chunk boundaries, so a
secret split over two deltas is caught) *before* the wrapped store sees them. Assign the run's
redactor to the returned store's `redactor` attribute exactly as the CLI does for the builtin
one; a store implementation itself never has to redact anything, and cannot un-redact what it was
given. Members that are not part of that reviewed surface are not forwarded at all — a store
write added later has to be implemented on the boundary, which is what keeps the rule true over
time. The builtin `FileRunStore` is the exception, and only because it applies the same
`Redactor` closer to the bytes (a JSON output is redacted on the parsed value, which the wrapper
cannot do for a store it does not control).

Today the seam is for **embedding**: the run-management commands (`runs`, `show`, `logs`,
`resume`, …) read `$RAYSPEC_HOME/projects/<slug>/runs/` directly, so a run written to a different
backend is not listed by them yet. `config.yaml` therefore has no `store:` key — a store plugin
is selected in the code that builds the `Runner`.

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
`workspace`, `RunOptions(dry_run=…, yes=…, fail_fast=…, resume=…)` and `resume_run_id`. All three
of `store`, `sinks` and `approval_prompt` can be resolved by id instead of constructed by hand —
`rayspec.store.create_store`, `rayspec.events.create_sink`,
`rayspec.registry.create_approval` — which is what lets another package drive the same engine
with its own persistence and observers.

Registering programmatically (no packaging involved) works too, and takes precedence over an
entry point of the same id:

```python
from rayspec.registry import SinkRegistration, register_sink

register_sink(SinkRegistration(id="mine", display_name="Mine", factory=MySink))
```

## Roadmap

Planned, in rough order: `rayspec pick`, runtime child runs (a `workflow:` step) and run-level
`defaults.timeout_total`.

Shipped since this page was written: `secret: true` inputs, the run-level `defaults.budget_usd` /
`defaults.max_tokens` circuit breaker, third-party providers, and the command/store/sink/approval
entry points described above.
