# CLI reference

`rayspec` is a Typer app. Every command accepts `--help`; `rayspec --version` / `-V` prints the
version (so does `rayspec version`). Commands that read a project take `--root <dir>` (the
directory containing `.rayspec/`; default: walk up from the cwd to the first `.rayspec/`, then
`.git`, else the cwd; a `--root` that is not a directory is a usage error). `RAYSPEC_HOME`
(default `~/.rayspec`) holds user-level workflows/agents, `config.yaml`, `.env` and every
project's runs and worktrees. The commands that read a project (`run`, `validate`, `plan`,
`workflows`, `agents`, `doctor`) and the run-management commands (`runs`, `costs`, `show`, `logs`,
`resume`, `approve`, `reject`, `cancel`) first load `~/.rayspec/.env` into the environment
(existing variables are never overridden) and merge `~/.rayspec/config.yaml` +
`.rayspec/config.yaml`. The **project** `.rayspec/.env` is a credential surface controlled by
whoever pushed the checkout (`ANTHROPIC_BASE_URL`, `GIT_CONFIG_*`, …), so only the commands that
execute steps apply it — `run`, `resume`, `approve`, `reject` — and they print one dim
`env: loaded N variables from .rayspec/.env (project): NAME, NAME` line on stderr when they do,
naming the variables (project wins over the home file there too); the inspection commands
(`doctor`, `validate`, `plan`, `workflows`, `agents`, `providers`, `runs`, `costs`, `show`,
`logs`, `worktrees`, `projects`) never load it (`rayspec doctor` lists both files instead, in a
`home .env` and a `project .env` row). Neither file may supply an **identity**: a `RAYSPEC_ACTOR`
loaded from either is refused with a `warning:` on stderr and kept only as `actor.declared_id`,
because a workflow step can write both — see
[runs-and-resume.md](runs-and-resume.md#who-ran-it). `providers`, `projects *`,
`worktrees *` and `version` do **not** load any `.env` (`worktrees` reads `config.yaml` only to
resolve `--repo`), so variables they need — e.g. `RAYSPEC_HOME` or git credentials — must come
from the shell. A malformed `config.yaml` or `.env` (either layer: YAML syntax, an unsafe tag, a
non-mapping document, a wrong field type, an unreadable file) is a usage error on every command:
`error: <path>:<line>: <problem>` on stderr, exit 2, never a traceback (`doctor` reports it as
the failed `config` check instead).

## Exit codes

| code | meaning |
|---|---|
| `0` | succeeded |
| `1` | the run failed |
| `2` | usage error: unknown workflow, validation errors, bad inputs, `--stubs` without `--dry-run` when an agent is not `provider: stub`, store/workspace errors |
| `3` | the run paused: an `approve:` gate is waiting for a person, **or** an operational ceiling stopped it (`policy.budget`, `max_consecutive_failures` — see [runs-and-resume.md](runs-and-resume.md#operational-limits-policy-not-workflow)). `run.json`'s `pause.reason` tells them apart (`approval` · `budget` · `failures`), and the console footer says what to type next. They are not answered the same way: `approve` on a ceiling pause **waives** it |
| `4` | the run was cancelled (`stop:` or a rejected gate) |
| `130` | interrupted (Ctrl-C / SIGTERM) |

Errors go to stderr as `error: <message>` plus an optional `hint:` line — including input and
validation errors of `run`/`plan` (one `error:` line each; with `--json` they become one object
`{"error": "input errors" | "validation errors", "errors": [...]}` on stdout). `rayspec run`
prints its `warnings:` block on stderr in both text and `--json` mode, so stdout stays the run's
own output. Hints that point at documentation quote a full URL
(`https://github.com/rayspec-labs/rayspec-py/blob/main/docs/…`) or `rayspec <cmd> --help` —
never a repo-relative path, which a `uv tool install` user does not have on disk.

Every command that renders a listing or a report takes `--output table|json`: `table` is the human
rendering (the default), `json` the machine-readable one documented in that command's section.
`--json` is the older spelling of `--output json` and keeps working everywhere it ever did; the
two never disagree, because passing both with different values (`--json --output table`) is a
usage error — `error: --json and --output table disagree`, exit 2 — rather than one of them
silently winning. (`rayspec runs stubs -o/--output PATH` predates the flag and still means "write
the script to this file"; that command has no `--json`.)

## Commands

### `rayspec run`

```
rayspec run <workflow> [--input NAME=VALUE]... [--inputs-file PATH] [--root DIR]
            [--dry-run] [--stubs PATH] [--stubs-from RUN_ID] [--stubs-init PATH] [--exec-shell]
            [--yes] [--approve-class NAME] [--no-interactive]
            [--json | --output FORMAT] [--quiet] [--verbose]
            [--allow-unsupported] [--fail-fast] [--resume RUN_ID] [--force]
            [--worktree | --no-worktree] [--base BRANCH] [--repo SOURCE]
            [--locked | --no-locked] [--wait-slot DURATION]
```

Load, validate, resolve inputs, prepare the workspace and run (or resume) a workflow. `<workflow>`
is a discovered name (`rayspec workflows`) or a file path.

| Option | Effect |
|---|---|
| `--input`, `-i NAME=VALUE` | set an input (repeatable; repeat an array input to append); with `--resume` accepted only for `secret: true` inputs (they are never persisted and must be supplied again — else `RAYSPEC_INPUT_<NAME>`; any other name is `inputs are fixed per run`, exit 2) |
| `--inputs-file PATH` | YAML/JSON mapping of inputs (lower precedence than `--input`) |
| `--dry-run` | every provider becomes the scripted `stub`; shell/python steps succeed with `''` (or a minimal `output_schema` instance) without running; gates auto-approve; no worktree is created (`isolation: none`) unless `--exec-shell` |
| `--stubs PATH` | stub script (YAML) for `--dry-run`, or for a real run whose prompt agents are all `provider: stub` (exit 2 when a non-stub agent would run for real); a missing/unreadable or malformed file is a usage error, exit 2, as is a `steps:` key that carries an `expect:` block but matches no prompt step of the workflow (a stale assertion never fires). The absolute path is recorded in `run.json` (`stubs_path`) and reused by `resume`/`approve`/`reject` and `run --resume` (an explicit `--stubs` overrides it) |
| `--stubs-from RUN_ID` | replay a stored run's recorded answers instead of a `--stubs` file (run id or unique prefix, resolved in the current project first and then in every project under `RAYSPEC_HOME`) — the in-memory equivalent of `rayspec runs stubs <run> -o f.yaml` followed by `--stubs f.yaml`. Mutually exclusive with `--stubs`; an unknown/ambiguous id or a run with secret inputs is exit 2. The donor run — not a file — is recorded in `run.json` as `stubs_path: "run:<run id>"`, so `resume`/`approve`/`reject` and `run --resume` rebuild the same script from it (a donor that was deleted is exit 2 naming it; an explicit `--stubs`/`--stubs-from` on the resume entry overrides it) |
| `--stubs-init PATH` | write a stub scaffold (one entry per prompt step) and exit; refuses to overwrite an existing file unless `--force` |
| `--exec-shell` | run shell/python steps for real inside `--dry-run` (worktree isolation applies again) |
| `--yes`, `-y` | auto-approve every gate (`decision.by: "--yes"`) — except gates whose [approval class](runs-and-resume.md#approval-classes) is `allow_yes: false` (no operator policy is read yet, so today no class is) |
| `--approve-class NAME` | pre-approve gates of one [approval class](runs-and-resume.md#approval-classes) (repeatable, `decision.by: "--approve-class"`); gates of every other class still ask. A class marked `allow_yes: false` is never pre-approved, and a name no gate uses pre-approves nothing (the run pauses as it would have) |
| `--no-interactive` | never prompt; a gate pauses the run (exit 3) |
| `--json` | JSONL events on stdout followed by **the final summary object as the last stdout line** (shapes below; `rayspec run … --json \| tail -1 \| jq .exit_code`); warnings and errors go to stderr. `--json` does not imply `--no-interactive`: on a terminal an `approve:` step still prompts (on stderr) — pass `--no-interactive` (pause, exit 3) or `--yes` for unattended pipelines |
| `--quiet` | only run-level lines, warnings, retries and non-green step finishes |
| `--verbose` | also print `step.started` lines |
| `--allow-unsupported` | downgrade provider-capability mismatches to warnings |
| `--fail-fast` | on a failure, cancel running siblings instead of letting them finish (drain) |
| `--resume RUN_ID` | resume a run (unique prefix accepted) of **this** workflow in the current project's store; inputs come from `run.json` (`--inputs-file` is refused; `--input` only re-supplies secret inputs — exit 2 `missing secret input(s): token — pass --input token=… or set RAYSPEC_INPUT_TOKEN` when one is missing); the other flags are **yours**, not the record's — a `--dry-run --stubs` record of a workflow with a non-stub agent needs `--dry-run` again (else exit 2 `run <id> was launched with --dry-run --stubs <path>; its recorded stubs file requires --dry-run …`, hint `pass --dry-run …`; `rayspec resume` inherits the dry run instead); refused when the run belongs to another workflow (always), the workflow hash changed (unless `--force`), the run still has a live pid or is recorded as running on another host (unless `--force`); cannot be combined with `--repo` — use [`rayspec resume`](#rayspec-resume), which finds the run in any project |
| `--force` | resume despite a changed workflow (steps whose fingerprint changed re-run) or a recorded live pid; overwrite an existing `--stubs-init` file |
| `--worktree` / `--no-worktree` | override the workflow's `isolation:` |
| `--base BRANCH` | base ref for the worktree (default: current branch; `origin/HEAD` for URL repos) |
| `--repo SOURCE` | run against a local path, a registered project name or a git URL ([isolation.md](isolation.md#--repo)) |
| `--locked` / `--no-locked` | refuse to run when an agent resolves to a different model or effort than `.rayspec/rayspec.lock` pins ([`rayspec lock`](#rayspec-lock)); the error names the agent, the pinned id and the resolved one. **On by default under `CI`** (any `CI` value other than empty/`0`/`false`/`no`/`off`), off otherwise; `--no-locked` opts out again. A missing lockfile is also refused — "nothing to check" must not read as "everything is fine" |
| `--wait-slot DURATION` | when the host's run slots for this workflow's providers are all taken (`policy.max_concurrent_runs`), queue instead of failing: a duration (`--wait-slot 30m`, `--wait-slot 1h30m`), a bare number of seconds (`--wait-slot 90`), or `forever` — the only spelling that waits indefinitely. `--wait-slot 0` does **not** wait (the default); a negative duration is a usage error. Otherwise exit 2, naming the run that holds the slot. A `--dry-run` takes no slot |

On **stderr**, before the run starts, comes the policy line — `policy: .rayspec/policy.yaml`, or
`policy: none in force (searched …)` — followed by the `warnings:` block, so neither ends up in
piped stdout. Console output on stdout is one line per finished step
(`✓ review succeeded 4.1s · 1.2k tok · $0.03`;
a step replayed from the resume cache prints `↺ review reused (4.1s)`), plus run/workspace/
decision lines and the final `■ run <id> <status>` line, then a summary: the `outputs:` table,
the worktree path and branch, the `decide with: rayspec approve|reject|resume <id>` hint when
the run paused, a footer `tokens: N · cost: $X · run dir: …` (tokens/cost omitted when zero/
unknown) and, after a failure or interrupt, `hint: rayspec logs <id> --step <path> · rayspec
resume <id>`. On a terminal an `approve:` step prompts with `[a]pprove [r]eject [v]iew [d]iff
[p]ause`. Closing stdout early (`rayspec run … | head`) stops the console output but never
changes the run's status — the store keeps every event.

`--json` shapes — **stdout** is pure JSONL, one event or stream record per line (`ts` is ISO-8601
UTC with microseconds), ending with the `run.finished` event and then the summary object:

```json
{"type": "step.finished", "run_id": "20260820-132644-h2nx", "ts": "2026-08-20T13:26:44.930795Z",
 "step_path": "review", "data": {"status": "succeeded", "duration_ms": 1,
 "usage": {"input": 13, "cached_input": 0, "cache_write": 0, "output": 10, "reasoning": 0},
 "cost_usd": null, "error": null, "skip_reason": null, "tolerated": false}}
{"type": "stream", "step_path": "review", "record": {"kind": "text_delta", "ts": "2026-08-20T13:26:44.930412Z",
 "attempt": 1, "text": "Looks", "name": null, "call_id": null, "nested": false, "data": {}}}
{"type": "run.finished", "run_id": "20260820-132644-h2nx", "ts": "2026-08-20T13:26:44.931102Z",
 "step_path": null, "data": {"status": "succeeded", "reason": null, "usage": {"input": 13, "cached_input": 0,
 "cache_write": 0, "output": 10, "reasoning": 0}, "cost_usd": null, "outputs": {"verdict": "approve", "summary": "…"}}}
```

The summary object is printed **once, on stdout**, after the last event — it is the last stdout
line, so `rayspec run … --json | tail -1 | jq .exit_code` works (the same holds for
`resume`/`approve`/`reject --json`):

```json
{"run_id": "20260820-132644-h2nx", "status": "succeeded", "exit_code": 0, "reason": null,
 "outputs": {"verdict": "approve", "summary": "…"}, "usage": {"input": 13, "cached_input": 0, "cache_write": 0,
 "output": 10, "reasoning": 0}, "cost_usd": null, "cost_source": "none", "run_dir": "/Users/me/.rayspec/projects/local/demo-cad85336/runs/20260820-132644-h2nx",
 "workspace": {"isolation": "none", "workdir": "/Users/me/demo", "branch": "main"}, "pause": null}
```

Event types: `run.started` · `run.resumed` · `run.paused` · `run.decision` · `run.finished` ·
`step.started` · `step.retry` · `step.finished` · `loop.iteration` · `each.item` ·
`workspace.created` · `warning` (data keys in [runs-and-resume.md](runs-and-resume.md#eventsjsonl)).
Stream records wrap agent events (`text_delta`, `tool_call`, …) and shell output
(`stdout`, `stderr`, `exit`).

### `rayspec validate`

```
rayspec validate [names...] [--root DIR] [--allow-unsupported] [--locked | --no-locked]
                 [--json | --output FORMAT]
```

Load and validate workflows (schema, graph, references, templates, provider capabilities);
default: every discovered workflow. Prints `OK`/`FAILED` per workflow with errors and warnings —
one `  - ` bullet per problem (a schema error with several unknown fields / bad identifiers is
several bullets, each prefixed with the file; the summary counts them), rendered as plain text so
regexes such as `^[a-z][a-z0-9_]*$` and `[...]` in messages survive — exit 2 when any workflow has
errors. A name that is neither a discovered workflow nor a file is `error: unknown workflow
'<name>'` (exit 2). An empty project prints `no workflows found (nothing to validate)` plus the
`rayspec init` hint (exit 0). `--allow-unsupported` turns capability mismatches into warnings.
`--locked` / `--no-locked` additionally checks each workflow against
[`.rayspec/rayspec.lock`](#rayspec-lock); a drifted agent is an **error** here, not a warning
(on by default under `CI`).
Under each status line comes the policy line — `policy: .rayspec/policy.yaml,
~/.rayspec/policy.yaml`, or `policy: none in force (searched <path>, <path>)` when no layer was
found, so a `policy.yaml` that is not being read is visible rather than assumed (policy is
discovered against `--root`, see [policy.md](policy.md)). A workflow with `secret: true` inputs
gets a dim marker line too — `secret inputs: token (secret; env-only, never persisted)`.
`--json`: `[{name, path, ok, errors: [...], warnings: [...], secret_inputs: [...],
policy: {layers, searched}, problems: [...]}]` (exit 2 when any entry has errors); `errors` is
one string per problem and `path` is the workflow's label
(`.rayspec/workflows/<name>.yaml`) even when the file fails to load (`null` only when the target
is neither a discovered name nor a file). `problems` is the same list as objects — `{path, line,
location, field, message, hint}`, one per problem, `path` never `null` — so a schema mistake can
be jumped to in an editor. Include bodies are validated as closed scopes (only
their own steps and the `inputs` bound by `with:` are visible), exactly as the engine runs them.

### `rayspec lock`

```
rayspec lock [NAMES...] [--check] [--root DIR] [--json | --output FORMAT]
```

Write `.rayspec/rayspec.lock`, pinning the literal model id and effort every agent of the named
workflows (default: all of them) resolves to today. Commit the file.

`model: sonnet` is a tier, `@fast` is an alias and an unset `model:` is the provider's default —
all three mean "whatever this resolves to *today*". Between the review of a change and its merge
a provider can change what that is, and nothing in the run record would have said so. The
lockfile is what makes that visible, and [`run`](#rayspec-run) / [`plan`](#rayspec-plan) /
[`validate`](#rayspec-validate) `--locked` is what makes it fatal.

| Option | Effect |
|---|---|
| `--check` | report drift and exit 1; never write the file (what a CI job runs) |
| `--json` | `{"path", "workflows": {name: {agent key: {provider, model, effort}}}, "drift": [...], "checked": bool}` |
| `--root DIR` | project root |

Exit codes: `0` written / in sync · `1` `--check` found drift · `2` usage (unknown workflow, a
workflow that does not load, an unreadable lockfile).

```console
$ rayspec lock
wrote .rayspec/rayspec.lock (2 workflow(s), 3 agent(s))

$ rayspec lock --check
error: review_pr: agent 'agents.reviewer' resolves to model 'claude-opus-4-9' but the lockfile pins 'claude-sonnet-4-6'
hint: run `rayspec lock` to re-pin
```

Agents are keyed the way `run.json`'s `toolchain.models` keys them (`agents.reviewer`,
`file:.rayspec/agents/x.yaml`, `inline:<step path>`), so a record and the lockfile talk about the
same agents. An agent that resolves to no literal model id (the provider's own default) is
recorded with `model: null` and named on stdout: rayspec cannot pin what it never sees.

### `rayspec schema`

```
rayspec schema [workflow|run|events|stream] [--out DIR]
```

Print (or write) the published JSON Schemas — the same documents that are checked in under
[`schemas/`](../schemas). With no argument it lists the four kinds, what each one validates and
its `$id`. With a kind it prints that schema to stdout (JSON Schema 2020-12). `--out DIR` writes
the file(s) into `DIR` (created when missing) and prints an editor modeline pointing at the local
copy; without a kind it writes all four. An unknown kind is `error: unknown schema '<kind>'`
(exit 2) with a did-you-mean.

| Kind | Validates |
|---|---|
| `workflow` | a `.rayspec/workflows/<name>.yaml` document (editor completion) |
| `run` | a run record, `<run dir>/run.json` |
| `events` | one line of `<run dir>/events.jsonl` |
| `stream` | one line of `<run dir>/steps/<path>/stream.jsonl` |

The schemas are generated from the Pydantic models (`scripts/gen_schemas.py --check` fails when
the checked-in copies drift). The workflow schema is an editor aid, not the validator: it is
relaxed where a field accepts more spellings than its type says (`timeout: 30m`,
`budget_usd: "$1.50"`, `approve: <message>`), and it knows nothing about the graph, references,
includes or provider capabilities — `rayspec validate` stays authoritative. See
[schema.md → Editor support](schema.md#editor-support).

### `rayspec plan`

```
rayspec plan <workflow> [--input NAME=VALUE]... [--inputs-file PATH] [--root DIR]
             [--allow-unsupported] [--locked | --no-locked] [--json | --output FORMAT]
rayspec plan <workflow> --render [--step PATH] [--stubs FILE] [--json | --output FORMAT]
rayspec plan <workflow> --risk [--json | --output FORMAT]
```

Show what a run would do without executing: the workflow hash and isolation, the run-level caps the
workflow set (`budget_usd $1.50  max_tokens 500,000  timeout_total 2h 0m` — all three or none: they
are one circuit breaker, and naming two of them reads as "there is no third"), the policy layers in
force (`policy: …`, exactly as `rayspec validate` prints it), inputs with their
resolved values — each input on its own line: the value, `missing (required)`, `undefined`
(optional without a default) or `'<raw>' (invalid: <why>)`; one problem per input (a required
input whose value was rejected is `invalid`, never also `missing`) and one bad input never hides
the good ones; a `secret: true` input prints `token = <secret>  (string, secret)` — the value
itself is never shown, and a rejected one is reported as `'<secret>' (invalid: …)` — the resolved agents (provider, model with the tier or alias it came from, effort, access,
which steps use it, where it was defined), the steps in topological order (bodies indented, with
needs/join/when and per-kind detail) and the capability report (unsupported features,
structured-output mode and cost source per provider: `reported by the provider`, `estimated from
the pricing table (~$)`, or — for a provider without cost reporting whose models have no
[pricing](providers.md#pricing) entry — the nudge `tokens only — add pricing.<model> for estimates
(<docs URL>#pricing)`, naming only the unpriced models when some are priced or disabled;
models disabled with a `null` pricing entry are listed as `pricing disabled (null) for <model>`
without a nudge). Exit 2 on validation or input errors.
`--json`: `{workflow, path, hash, isolation, budget_usd, max_tokens, timeout_total, description,
inputs: {name: {name, type, value, state: ok|missing|invalid|undefined, problem, secret}},
input_errors, agents: [{name, provider, model,
effort, access, used_by, source}], steps: [{path, kind, needs, join, when, depth, detail}],
providers: {id: {structured_output, cost_reporting, cost: provider|table|none, priced_models,
unpriced_models, disabled_models, pricing_error?}}, policy: {layers, searched}, errors, warnings,
unsupported}` (a secret input's `value` is `"<secret>"`, `secret: true`; the three cap keys are
always present, `null` when the cap is unset, and `timeout_total` is in seconds).

#### `--render`: see what the agent will receive

`rayspec plan <workflow> --render` prints the **rendered bodies** instead of the plan: every
`prompt:` step's prompt and every `shell:`/`python:` step's script, exactly as they would be
handed over, with no token spent and no credential needed. Upstream step outputs come from
`--stubs <file>` (the same YAML stub script `rayspec run --dry-run --stubs` takes — only its
`steps:` entries apply, since a `match:` entry keys on a prompt that does not exist yet) and
otherwise from a visible `<path output>` placeholder, so a missing value is never an empty
string. `${RAYSPEC_V<n>}` slots are printed below the script next to their values (rayspec never
splices a value into a script), followed by the step's own rendered `env:` — where a
`secret: true` input shows as `<secret>`, never its value.

Loop bodies render as iteration 1 and `each` bodies as item 0 (the `each:` expression is
evaluated against the same stubbed context); `--step <path>` renders one step only, naming it
the way `rayspec show` does (`assess`, `build/implement`). Passing `--step` or `--stubs` without
`--render` is a usage error.

```
rayspec plan review_pr --render --step assess --stubs .rayspec/dryrun/stubs.yaml
```

Options:

- `--risk` — Report what the run would be allowed to do (runs nothing).
- `--render` — Show the rendered prompt/script bodies instead of the plan.
- `--step` `<path>` — With `--render`: render only this step path.
- `--stubs` `<file>` — With `--render`: stub script supplying the upstream step outputs.

Warnings (loader warnings, `defaults.on_unsupported: warn` findings, the capability warning)
are printed under `--render` exactly as they are without it.

With `--json` the usual plan payload gains `stubs` (the file, or `null`) and `render`:
`[{path, def_path, kind, agent, model, provider, text, env, step_env, error, warnings}]`, where
`path` is the *record* path the preview bound (`build[1]/echo`, `fan[0]/work`) and `def_path`
the definition path.

#### `--risk`: what the run would be allowed to do

`rayspec plan <workflow> --risk` reports what a run of this workflow could reach, read off the
workflow document itself. It is meant to be read **before** approving a run — by the person about
to type `rayspec approve`, or in review of a workflow somebody else wrote.

It **runs nothing**: no step body is executed, no provider is contacted, no socket is opened and
no file is written. The price of reading rather than running is that the analysis is textual — a
body is matched as written, before templates are rendered, so a command assembled at run time is
not seen. What the report cannot read it says out loud rather than passing over: a templated body
is a `templated-body` finding, an agent that may run commands is an `agent-tools` one, and a run
with no findings at all prints what was *not* covered instead of declaring the workflow safe.

Findings, worst first, each with where it is, the evidence, and what to do about it:

| Category | Severity | What it means |
|---|---|---|
| `agent-access` | high | an agent runs with `access: full` — it may read and write outside the workspace |
| `mcp-command` | high | an agent's MCP server is a program started on this machine |
| `shell-pipe-to-shell` | high | a body pipes something downloaded into a shell |
| `shell-push` | high | a body runs `git push` / `git merge` / `git rebase` / `gh pr merge` |
| `shell-force` | high | `--force`, `git reset --hard`, `git clean -f` |
| `shell-delete` | high | `rm -rf`, `git branch -D`, `find … -delete` |
| `shell-publish` | high | `npm publish`, `twine upload`, `cargo publish`, `docker push`, `gh release create`, … |
| `shell-privilege` | high | `sudo`, `chown`, `chmod 777` |
| `outside-workspace` | high | a `cwd:` outside the workspace, or a body naming `~/`, `$HOME`, `Path.home()`, an absolute path (outside `/dev` and `/tmp`) or a `../` escape |
| `mcp-remote` | medium | an agent's MCP server is reached over the network |
| `agent-tools` | medium | an agent may run `shell` or `edit` tools — what it does is its own decision and is not in this report |
| `shell-network` | medium | `curl`, `wget`, `ssh`, `rsync`, `requests`, `socket`, … in a command position |
| `shell-install` | medium | the step installs code it did not bring with it |
| `shell-credentials` | medium | `gh auth`, `docker login`, `aws configure`, … |
| `python-process` | medium | a `python:` body shells out, so what it runs cannot be read off the workflow |
| `reject-ignored` | medium | a gate with `on_reject: continue` — rejecting it does not stop the run |
| `self-approving-gate` | medium | a gate with `auto_if:` that no [approval class](runs-and-resume.md#approval-classes) holds shut |
| `templated-body` | medium | a `shell:`/`python:` body or a `cwd:` assembled at run time — what it runs is not what is written |
| `unheld-class` | medium | a gate names an [approval class](runs-and-resume.md#approval-classes) that nothing in force defines, so the name holds nothing |
| `no-isolation` | low | `isolation: none` — steps run in the project directory itself, not in a worktree |
| `waivable-gate` | low | a gate `--yes` approves — it names no class, or its class is not marked `allow_yes: false` |

A gate whose class is marked `allow_yes: false` is *not* reported: it is a real gate. A gate that
names a class nothing defines is reported as `unheld-class` — the name reads like a lock and is
not one.

```
$ rayspec plan release_check --risk
risk report release_check  .rayspec/workflows/release_check.yaml
  1 high · 8 medium · 1 low

  high   publish  shell-push
         shell: git push origin "{{ inputs.tag }}"
         → a shared branch is changed; put the step behind an approve: gate with a class the
           policy marks allow_yes: false

  medium gate  unheld-class
         class release (not held)
         → no operator policy in force defines approval class 'release', so naming it restricts
           nothing; the rule that would hold this gate is allow_yes: false for it
```

The report is advisory: it never changes the exit code, which stays 0 unless the workflow has
validation or input errors (2). `--json` adds `risk: [{severity, category, where, detail,
advice}]` to the usual plan payload, which is the form to gate a pipeline on. `--risk` and
`--render` are different views and are refused together.

### `rayspec test`

```
rayspec test [<workflow>] [--case ID] [-k PATTERN] [--junit FILE] [--json | --output FORMAT]
             [--exec-shell] [--root DIR]
```

Run the project's declarative workflow cases: every case is a **dry run against the stub
provider**, so a suite needs no network and no worktree, starts no subprocess and is given no
credentials — the project's `.rayspec/.env` is deliberately *not* loaded for this command. It is
the edit → check loop after changing a prompt, a `when:` or a stub, and it is safe to run against
a checkout you have not read. Cases are discovered from the layouts below (see [testing.md](testing.md)
for the file format):

- `.rayspec/tests/<workflow>/<case>.yaml` — one case per file; the directory names the workflow
  and the file stem names the case. Suite name: `tests/<workflow>`.
- `checks.yaml` at the **project root** — the project's own suite, and where the file lands when
  the project is a scaffolded example (`rayspec init --from <name>` writes it). Suite name:
  `checks`. It is read only when it is a mapping whose `checks:` key holds a list of case
  mappings, so a `checks.yaml` that belongs to another tool — `checks: {lint: true}`,
  `checks: [{name: lint, cmd: ruff}]` — is passed over instead of reported as broken.
- `examples/<name>/checks.yaml` (each example being a self-contained project) and
  `.rayspec/dryrun/checks.yaml` for the project's own workflows — a mapping with a `checks:`
  list. Suite names: the example directory / `dogfood`.

One line per case is printed while it runs (`ok tests/build:happy (0.04s)`), then the four-line
block of every unmet expectation and a `24 passed, 1 failed in 2.2s` summary:

```
expect.outputs.verdict: output 'verdict' is 'fix', expected 'patch'
  reason: (none)
  fix: update the expectation, or fix the template/stubs (rayspec logs 20260821-080147-sccz)
  at examples/fix_issue/checks.yaml:9
```

A passing case deletes the run it created (a suite must not bury the project's real runs); a
failing case keeps it, so `rayspec logs <run_id>` and `rayspec show <run_id>` explain it.

The positional `<workflow>` keeps only cases of that workflow (or of a suite with that name),
`--case ID` only that case id, `-k` / `--select PATTERN` only cases whose `<suite>:<case>` contains
the substring; the filters combine. `--exec-shell` makes `shell:`/`python:` steps of every case
execute for real instead of being simulated — **it is the only thing that can start a subprocess,
and only you can pass it**. A case file's `exec_shell: true` is a declaration that the case wants
real execution; without the flag the command refuses to run at all (exit 2, naming the case's
`file:line`) rather than letting a checked-in data file widen what the command does. Note that
`--exec-shell` takes no workdir lock, so do not run it beside a real `rayspec run` on the same
checkout, and that a case is bound by the same [approval classes](runs-and-resume.md#approval-classes)
a run is: a gate whose class may not be approved automatically pauses the case and fails it rather
than running the step behind it. `--junit FILE` writes a JUnit XML report (one `<testsuite>` per suite, the four-line
blocks as the `<failure>` text) — written whether cases pass, fail, or the suite could not start
(a usage error becomes one erroring `<testcase>`), so a CI publish step always has a file.
`--json` prints one object on stdout: `{passed, failed, duration_s, cases: [{suite, case, ok,
status, run_id, run_dir, duration_s, failures: [{field, summary, detail, fix, location}]}]}`.

Exit `0` when every case passed, `1` when any failed, `2` for a usage error — a filter that
matches nothing (the known `<suite>:<case>` names are listed), no cases at all, a case that needs
`--exec-shell`, or a malformed case file (`error: <file>:<line>: unknown field 'statuss' for
expect; did you mean 'status'?`).

### `rayspec workflows`

```
rayspec workflows [--root DIR] [--json | --output FORMAT]
```

List workflows from `.rayspec/workflows/` and `~/.rayspec/workflows/` (project wins on a name
clash): name, scope, description, path. A file that does not parse shows `(parse error — see
rayspec validate)` in the table and one short `error:` line below it; an empty project prints
`no workflows found …` and the hint to run `rayspec init` (or create
`.rayspec/workflows/<name>.yaml`; the examples are linked by URL). `--json`: `[{name, scope,
description, path, error}]` (`error` set when the file does not parse).

### `rayspec agents`

```
rayspec agents [--root DIR] [--json | --output FORMAT]
```

List agent files (`.rayspec/agents/*.yaml`, `~/.rayspec/agents/*.yaml`) with the provider, model
and effort they *resolve* to under the merged `config.yaml` — the same rules `plan` applies: an
`@alias` that pins a provider shows `codex (via @fast)`, an agent without `provider:` shows
`claude (default)`, the model column shows the resolved model with its alias or tier in
parentheses (`gpt-5.4 (@fast)`, `sonnet (medium)`; `(provider default) (large)` when the tier has
no model for that provider; an unknown alias or an alias that pins another provider than the
agent is flagged in red, with no model/effort — the loader refuses such an agent) — plus
access and path.
`--json`: `[{name, scope, path, provider, model, effort, access, error, resolved: {provider,
model, effort, via, provider_from: agent|alias|default, problem}}]` (`provider`/`model`/`effort`
are the raw file values; `resolved` is `null` when the file does not parse). Agents defined
inline under a workflow's `agents:` are shown by `rayspec plan`, not here.

### `rayspec providers`

```
rayspec providers [--json | --output FORMAT]
```

The provider registry (builtins and entry-point plugins) and the capability matrix
([providers.md](providers.md)). `--json`: `[{id, display_name, builtin, capabilities: {...}}]`.

### `rayspec plugins`

```
rayspec plugins [--output table|json] [--json]
```

Every installed rayspec plugin: the commands, run stores, event sinks, approval prompts and
providers other packages publish through entry points, with the distribution and version each
comes from and whether it was loaded or skipped (and why). Use it when a command, store or sink
appears that you did not write. The second table lists the ids that are registered right now —
what `extensions:` in `config.yaml` may name. `--json`: `{plugins: [{group, name, value,
distribution, version, status, detail}], registered: {stores, sinks, approvals}}`. Writing one:
[extending.md](extending.md).

### `rayspec trust add`

```
rayspec trust add <workflow>... [--root DIR]
```

Record each workflow's current hash in `.rayspec/trusted.yaml`. The hash covers every file that
contributed to the resolved workflow — the document, every `include:`d body, every agent file and
every `prompt_file`/`instructions_file` — so trust is a statement about what will actually run.
Adding a workflow that is already listed replaces its entry (`updated`). See
[policy.md](policy.md).

### `rayspec trust list`

```
rayspec trust list [--root DIR] [--json | --output FORMAT]
```

The trust list with, per entry, whether the workflow still hashes to what was trusted:
`current`, `changed` (it was edited since) or `missing` (it no longer loads). `--json`:
`[{workflow, hash, added, status}]`.

### `rayspec trust remove`

```
rayspec trust remove <workflow>... [--root DIR]
```

Drop workflows from the trust list (the file is deleted when the last entry goes). Exit 2 when a
name is not listed.

### `rayspec trust check`

```
rayspec trust check [<workflow>...] [--root DIR] [--json | --output FORMAT]
```

Exit 0 only when every named workflow — with no arguments, every discovered workflow — is listed
at its current hash; exit 1 otherwise, naming what changed. This is the gate a scheduled job puts
in front of `rayspec run`. `--json`: `[{workflow, name, hash, trusted, problem}]`. Setting
`trust: {require: true}` in `policy.yaml` applies the same gate to every command that loads a
workflow.

### `rayspec projects add`

```
rayspec projects add <name> <source> [--base BRANCH]
```

Register (or update) a project for `--repo <name>`: `<source>` is a local checkout path (stored
absolute; must exist) or a git URL; `--base` is its default worktree base. Written to
`~/.rayspec/config.yaml` under `projects:`.

### `rayspec projects list`

```
rayspec projects list [--json | --output FORMAT]
```

Registered projects. `--json`: `[{name, source, base}]`.

### `rayspec projects remove`

```
rayspec projects remove <name>
```

Unregister a project (its bare clone and worktrees are kept). Exit 2 when the name is unknown.

### `rayspec worktrees list`

```
rayspec worktrees list [--root DIR] [--repo SOURCE] [--json | --output FORMAT]
```

Worktrees on `rayspec/*` branches of the project (branch, age, `dirty`/`merged`/`gone`/`locked`
state, path). `--repo` selects another project (path, registered name or URL); a non-git
`--root` is an error. `--json`: `[{path, branch, head_sha, created_at, age_s, dirty, merged,
prunable, locked}]`.

### `rayspec worktrees clean`

```
rayspec worktrees clean [--root DIR] [--repo SOURCE] [--older-than AGE] [--merged]
                        [--merged-into REF] [--force] [--dry-run] [--json | --output FORMAT]
```

Remove rayspec worktrees and their branches (`git worktree remove` + `git branch -D`). **Safe by
default**: only merged, clean and unlocked worktrees go; the rest are listed as skipped with a
reason (`unmerged commits (use --force)`, `dirty (use --force)`, `locked (use --force)`,
`younger than …`, `not merged`). `--older-than` takes `7d`, `12h`, `30m`, `2w`, `1d12h`;
`--merged` keeps only merged ones; `--merged-into REF` changes the ref that decides "merged"
(default `origin/HEAD`, else `HEAD`); `--force` also removes unmerged, dirty and locked worktrees;
`--dry-run` reports without removing. `--json`: `{dry_run, removed: [...], skipped: [{..., reason}]}`.

### `rayspec runs`

```
rayspec runs [OPTIONS]
```

List runs newest first (by `created_at`, then id) — by default those of the current project (the slug of `--root`/cwd), or every project under `RAYSPEC_HOME` with `--all` (every store under `projects/`, however deep the slug) — with status (`succeeded (dry)` marks a `--dry-run`), workflow, start time, duration, steps, tokens and cost. Run ids may be abbreviated to a unique prefix everywhere below.

The **steps** column is `done/total`: *done* = steps the engine resolved — succeeded, failed with `allow_failure`, or skipped (`when:` false, upstream failed/skipped) — so a finished run reads `n/n`; *total* = the recorded steps plus, for every run that may still continue or be resumed (running/paused/interrupted/failed/cancelled — everything but succeeded), the workflow's planned steps (root steps and `include:` bodies; loop/each iterations are counted as they happen), so a run paused at the gate of a 3-step workflow reads `1/3` instead of `1/2` and a 3-step workflow that failed at step 2 reads `1/3`. When the workflow cannot be loaded any more (old record, file gone) the total falls back to the recorded steps. `rayspec show` adds the breakdown `(n ok · m skipped)`.

The **cost** column carries the run-level cost source: `$0.12` when every priced step reported a provider cost, `~$0.12` when any step cost is a [pricing-table](providers.md#pricing) estimate, `≥$0.12` when some steps have tokens but no price at all (an unpriced provider and no pricing entry — the sum is a lower bound; `show` says how many steps are unpriced), `-` when no cost is known at all (tokens are never shown as cost; the `tokens` column has them). The same marker appears on the step lines, the `■ run` line and the totals of the run console, in `show` and in the approval panel.

Outside a rayspec project (no `.rayspec/` directory and no git repository at or above the cwd / `--root`) nothing is listed and no project slug is minted: `not inside a rayspec project (no .rayspec/ or git repo at or above <dir>) — hint: rayspec runs --all` goes to stderr, exit 0 (`--json` prints `[]`). Every string that comes from `run.json` (workflow names, slugs, reasons) is rendered as plain text — never as Rich markup, never with terminal escape sequences.

Options:

- `--all` / `-a` — List runs of every project under RAYSPEC_HOME.
- `--limit` / `-n` `<int range>` — [x>=1]  Show at most N runs.
- `--json` / `--output json` — Machine-readable output: `[{run_id, workflow, status, reason, project_slug, created_at, started_at, ended_at, duration_ms, steps_done, steps_total, steps_ok, steps_skipped, tokens, usage{input, cached_input, cache_write, output, reasoning}, cost_usd, cost_source ("provider" | "table" | "partial" | "none"), resume_count, dry_run, pid, host, workspace{…}, pause{…}|null}]`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

These are the options of the **listing**. `--root` is the only one a subcommand honours before the
subcommand name (`rayspec runs --root X diff a b`); `--all`, `--limit`, `--json` and `--output`
there are a usage error (exit 2, `--json belongs to the rayspec runs listing…`) rather than
silently dropped — put them after the subcommand: `rayspec runs diff a b --json`.

### `rayspec runs stubs`

```
rayspec runs stubs <run> [-o PATH] [--redact] [--force] [--root DIR]
```

Write a [stub script](providers.md#stub-stub) from a stored run: every `prompt:` step's answer, token
usage and failure, keyed the way the engine names records. This is record & replay — a run dir is
the cheapest realistic fixture there is, and replaying it costs nothing and needs no credentials:

```console
$ rayspec run fix_issue --input issue=412          # a real run, once
$ rayspec runs stubs 20260821-101500 -o .rayspec/dryrun/fix_issue.stubs.yaml
$ rayspec run fix_issue --dry-run --stubs .rayspec/dryrun/fix_issue.stubs.yaml
$ rayspec run fix_issue --dry-run --stubs-from 20260821-101500   # same, without the file
```

Keys follow the run-time step paths: a top-level step is its id, a `loop:`/`each:` body step is a
glob (`build[*]/implement`), an `include:` body step is `block/step`. Iterations that answered the
same thing stay one entry; a **loop** body whose iterations differ becomes a `sequence:` under the
glob (the n-th iteration gets the n-th item, and the last item repeats if the loop runs longer next
time); an **`each`** body whose items differ keeps its own indexed keys (`fan[0]/patch`,
`fan[1]/patch`) — items run in parallel, so a sequence would hand answers to whichever item called
first. Only `prompt:` steps are recorded (shell/python/approve steps and composites are not agent
calls), and steps that never got an answer (skipped, never started, still running, paused,
interrupted, rejected) are left out. A failed step is recorded as `fail: {kind, message,
transient}`, so the replay fails the same way — and a body whose recording contains a *transient*
failure keeps its indexed keys instead of a `sequence:`, because the engine's retry would consume
the next sequence item. A step that claims an output rayspec cannot read (a pruned run dir) is
exit 2 naming the step: a recording must never contain an entry with no answer, which would
replay as the stub's built-in default and look faithful. An error type the stub cannot express
(an engine-level `rejected`, `exit`, …) is recorded as `fail: {kind: api}` with a stderr note.

When the workflow's hash has moved since the run, a stderr warning says so — recorded keys may no
longer match the steps that exist today. A run whose inputs were declared `secret: true` is
**refused** (exit 2, naming the inputs): its prompts and outputs may quote the secret and a stub
script is a plain file meant to be committed.

Options:

- `--output` / `-o` `<path>` — Write the script here instead of stdout.
- `--redact` — **Refused** (exit 2), and permanently so: a recording command is never given
  secret values. A redactor replaces only values it is *given*, a run's are never persisted, and
  asking you for them in order to write a file you are meant to commit is not a trade rayspec
  makes — exact-match redaction cannot promise that a value a step transformed is gone. A run
  with secret inputs is refused outright; a run without them has nothing to redact.
- `--force` — Overwrite an existing file.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec runs diff`

```
rayspec runs diff <a> <b> [--json | --output FORMAT] [--exit-code] [--outputs] [--steps] [--across-projects] [--root DIR]
```

Compare two runs **of one workflow** — after changing a prompt, a model or an agent, see what
actually moved. The report has a header (status, duration, tokens, cost with the run-level
marker, and the delta of each), the steps that differ, the iteration/item counts of any
`loop:`/`each:` step whose counts moved, and the workflow outputs that differ.

A step is *changed* when its status, its stored output hash (`output_sha256`) or its
`fingerprint` moved; a step recorded by only one of the runs is *added* / *removed* (a loop that
ran one iteration longer shows up exactly there). Duration, tokens and cost are **reported but
never counted as a difference** — two identical runs of a real agent differ there every time,
which would make `--exit-code` useless.

```console
$ rayspec runs diff 20260821-1015 20260821-1102 --exit-code
runs diff — workflow fix_issue
          a: 20260821-101500-k3fa  b: 20260821-110200-p8zq  delta
status    succeeded                failed                   changed
duration  1m35s                    52.0s                    (-43000ms)
tokens    8.5k tok                 6.2k tok                 (-2300)
```

Refusals (exit 2, nothing is guessed): comparing runs of two different workflows names both runs
and both workflow names; comparing runs of two different **projects** names both slugs (a run id
prefix resolves home-wide, so two same-named workflows of two unrelated repos would otherwise
compare "cleanly" and report every step as drift) — pass `--across-projects` if that is what you
mean, and the header gains a `project` row; an unknown or ambiguous run id is the usual lookup
error. When the two
runs recorded different `workflow_hash` values the report says so — the workflow itself changed
between them, so a moved step may be a moved *definition*.

Options:

- `--json` / `--output json` — Machine-readable output: `{workflow, a{run_id, status, reason, created_at, dry_run, workflow_hash, project_slug}, b{…}, workflow_hash_changed, status{a, b, changed}, duration_ms{a, b, delta}, tokens{a, b, delta}, cost_usd{a, b, delta}, steps: [{path, kind, change ("added"|"removed"|"changed"|"same"), reasons, status{a,b}, output{a,b,changed}, fingerprint{a,b,changed}, duration_ms{a,b,delta}, tokens{a,b,delta}, cost_usd{a,b,delta}}], loops: [{path, kind, a, b, changed}], outputs: {name: {a, b, changed}}, changed}`.
- `--exit-code` — Exit 1 when anything differs (a CI gate: `rayspec runs diff base head --exit-code`).
- `--outputs` — Also print a unified diff of every changed step's stored output. When either run
  was launched with `secret: true` inputs, a stderr note says the stored outputs are printed as
  recorded (rayspec does not redact them yet — `rayspec show`/`logs` behave the same).
- `--across-projects` — Compare two runs that belong to different projects (rare; off by default).
- `--steps` — List unchanged steps (and unchanged loop counts and outputs) too.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec costs`

```
rayspec costs [--since WHEN] [--workflow NAME] [--output table|json] [--json] [--root DIR]
```

Add the per-run figures of `rayspec runs` up, grouped by workflow, so "what has this project cost
me" does not mean reading every run by hand. It is read-only — it lists the runs of one store and
writes nothing — and deliberately **per project and per user**: the scope is
`$RAYSPEC_HOME/projects/<slug>` for the project of `--root`/cwd, the same store `rayspec runs`
lists without `--all`.

```console
$ rayspec costs --since 30d
project local/rayspec-py-69756f71 · since 2026-07-22 14:52:40 UTC
workflow           runs      tokens    cost  cost source
review_pr            12    1.3M tok  ≥$4.12  9 table · 3 unknown
implement_feature     4  820.0k tok   $2.30  4 provider
docs_sync             2   40.0k tok       -  2 unknown
total                18    2.2M tok  ≥$6.42  4 provider · 9 table · 5 unknown
5 of 18 runs have no recorded cost (dry runs, an unpriced provider, no pricing entry)
totals marked ≥ are a lower bound
```

The **cost** column carries the same markers as everywhere else in the CLI — `$0.12`
provider-reported, `~$0.12` estimated from the [pricing table](providers.md#pricing), `≥$0.12` a
lower bound, `-` nothing known — folded from the runs in the group the way a run folds its steps:
any estimate in the mix makes the group an estimate, and anything missing makes it a lower bound.
The **tokens** column carries `≥` too, because a step that was interrupted before the provider
reported any usage leaves the run's token count a lower bound (`usage_unknown`, the same figure
`rayspec run` prints as `tokens: ≥N`; see
[runs-and-resume.md](runs-and-resume.md#failures-retries-and-timeouts)). When nothing at all was
reported the cell reads `unknown` rather than `-`, which would claim the run used no tokens.

The **cost source** column is the breakdown that keeps the marker honest: one count per run,
`provider`/`table`/`partial` for the runs whose cost is known and **`unknown`** for the runs that
carry no cost at all (a `--dry-run`, a provider without cost reporting and no pricing entry, a run
that never reached a priced step). A fifth bucket, **`none`**, only shows up for a legacy record:
a `run.json` written before the cost source was recorded carries a cost without saying where it
came from, and it is reported as it was stored rather than guessed at.

A run whose cost is unknown is **counted and shown as unknown — never dropped, never counted as
zero**. That is the one property this command is built around: the run counts of the groups always
add up to the `total` row, a group in which nothing is priced prints its run count next to an empty
cost, and a total that is missing anything is marked `≥` and explained in the lines below the
table. A total that quietly omits runs would be worse than no total at all — which is why a run
record the store cannot read is not simply skipped: it is counted, the total is marked `≥`, and a
line below the table **names** the runs it is missing. Two ways a record goes unread and they are
one fact for a sum: a `run.json` that does not parse (truncated, hand-edited) and one that is not
there at all — the second is the quieter of the two, because a run directory without a `run.json`
is invisible to `rayspec runs` as well, which is why the ids are named here rather than pointed
at another listing — and why the line ends at the run directory rather than at `rayspec show
<id>`, which cannot show that run either:

```
1 run record could not be read and is not in these totals: 20260818-100000-cccc — its run.json is
missing or unparseable (~/.rayspec/projects/<slug>/runs/20260818-100000-cccc)
```

A run directory that has no `run.json` *yet* is not one of these: the store creates the directory
and writes the record into it a moment later, so a `costs` that lands in that window finds the
store's own staging file there and leaves the run alone (for half a minute — a staging file left
behind by a killed process must not silence its run for good).

With `--since`, only the lost runs whose own id places them **inside** the window are reported: a
rayspec run id starts with its UTC timestamp, so a record that cannot be read can still be dated.
Nothing else about it can — its workflow is only in the record — so `--workflow` keeps counting
it: a roll-up that quietly dropped a run that might have been one of this workflow's is exactly
the failure above.

Runs that are still `running` or `paused` are summed like any other — their cost so far is real
money — but a line below the table says how many, because those figures are not final: an approval
gate can sit for days, and two invocations while a run is in flight will not agree.

Groups are ordered most expensive first, then by name (an unpriced group sorts last — its cost is
unknown, not zero, so it sorts after a group that really did cost `$0.00`). Tokens are summed
independently of cost, so a group with no price still shows what it consumed. Everything that comes
out of `run.json` (workflow names, the project slug) is rendered as plain text, never as Rich
markup.

`--since` takes what a person types: a window back from now — `45s`, `90m`, `24h`, `7d`, `2w`
(decimals like `1.5h` work) — or an absolute ISO-8601 date/timestamp — `2026-08-01`,
`2026-08-01T06:30:00`, `2026-08-01T06:30:00Z`, `2026-08-01T06:30:00+02:00`. A bare date means
midnight, a timestamp without an offset is read as UTC. The cutoff is compared against the run's
`created_at` (the field `rayspec runs` orders by) and is **inclusive**: a run created exactly at
the cutoff is in scope. Anything else — `yesterday`, `7 days`, a negative window — is a usage
error (exit 2) naming the forms that work.

With no runs in scope the command prints one line (`no runs for project <slug> · workflow 'x' …`)
and exits 0 — an unknown `--workflow` name is a filter that matched nothing, not an error, because
a workflow may have been renamed since the runs it produced. `--json` always prints the full object,
with `runs: 0` and `cost_usd: null`. Outside a rayspec project (no `.rayspec/` and no git repository
at or above the cwd / `--root`) nothing is summed and no project slug is claimed — neither on disk
nor in the output, where `project` is `null`: a stderr notice points at `--root`, exit 0.

Options:

- `--since` `<when>` — Only runs created at or after this point: a window (`7d`, `24h`, `90m`) or a date (`2026-08-01`). Inclusive at the cutoff.
- `--workflow` `<name>` — Only runs of this workflow (exact recorded name).
- `--json` — Machine-readable output: one object `{project (the slug, or null outside a project), since (ISO cutoff | null), workflow (the filter | null), runs, runs_unknown_cost, runs_partial_cost, runs_usage_unknown, runs_in_flight, runs_unreadable, runs_unreadable_ids, tokens, usage{input, cached_input, cache_write, output, reasoning}, cost_usd (null when nothing is priced), cost_source ("provider" | "table" | "partial" | "none"), cost_sources{provider?, table?, partial?, none?, unknown?}, first_run_at, last_run_at, workflows: [{workflow, runs, runs_unknown_cost, runs_partial_cost, runs_usage_unknown, runs_in_flight, tokens, usage{…}, cost_usd, cost_source, cost_sources{…}, first_run_at, last_run_at}]}`. The top-level figures are the total over exactly the runs in `workflows`, so `sum(w.runs for w in workflows) == runs`; `cost_sources` counts every run once (zero buckets are omitted); `since` is what was asked for, `first_run_at`/`last_run_at` what the store actually holds. The four `runs_*` counters are why a figure may be a lower bound: runs with no cost at all, priced runs holding a step with tokens but no price, runs whose usage was cut off, and runs still running or paused. `runs_unreadable` and `runs_unreadable_ids` (top level only — an unreadable record cannot be attributed to a workflow) count and name the runs the store could not read (a `run.json` that does not parse, or one that is missing entirely); anything above zero means the totals are missing runs. With `--since` only the ids the window contains are listed (a run id carries its UTC timestamp); `--workflow` cannot filter them, because a record that cannot be read does not say which workflow it belonged to.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

This command is per-project and per-user by design. Roll-ups across projects, teams, repositories
or tags — and export formats aimed at chargeback — are deliberately out of scope; `--json` is the
seam for anything of that shape.

### `rayspec show`

```
rayspec show [OPTIONS] {run}    # --output table|json (--json is the older spelling)
```

Show one run: header (status — with a `dry run` marker for `--dry-run` records —, workflow, inputs (a `secret: true` input shows `"<secret>"` — the value is not in `run.json`), timing, `steps: done/total done (n ok · m skipped)` — see the `runs` column above —, tokens, cost with the run-level marker and, for a partial cost, a dim `(n steps unpriced)` note, `pid … (alive)` for a live run / `(exited)` for a paused one), workspace (isolation, workdir, branch, base/head sha), the step tree (nested paths like `build[2]/review`) with status, attempts, duration, tokens, cost and an output preview, an `artifacts:` table (step, file, size, sha256, stored ref) listing the files the steps promised and delivered — omitted when there are none —, a `warnings:` block (provider warnings streamed by the steps — e.g. a Claude rate-limit notice — and engine `warning` events, each as `<step>: <message>`; omitted when there are none), the rendered outputs, and the pause state (gate, token, decision) when the run is paused. Everything that comes out of `run.json`, an output file or a stream (inputs, outputs, previews, errors, reasons, messages) is untrusted text: it is printed as plain text with control characters and terminal escape sequences (colours, title changes, screen clears, hyperlinks) removed, and never parsed as Rich markup.

Options:

- `--json` — Machine-readable output: the `runs` row plus `run_dir, inputs, outputs, workflow_path, workflow_hash, project_root, steps: [record fields + tokens, output_preview], artifacts: [{step, path, ref, sha256, size}], warnings: [str]` and `record` (the raw `run.json`).
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec logs`

```
rayspec logs [OPTIONS] {run}
```

Print a run's lifecycle event log (`events.jsonl`). `--step PATH` prints that step's stream (`stream.jsonl`: agent text, tool calls, commands, usage) instead; `--stream` interleaves every step's stream into the log; `--follow` keeps tailing while the run is live.

A step stream is rendered for reading: text deltas are joined until the completed text arrives, reasoning deltas are joined per block and printed as whole `thinking:` lines (one per line of thought, never cut mid-word), tool calls (`⚙ Read({"p": …})`) and results (`  → first line …`), commands, stdout/stderr, `warning:`/`error:` lines, the session id and usage. Internal SDK plumbing (`raw status`, `raw thinking_tokens`, other unknown record kinds) is hidden unless `--verbose` (or `--json`, which prints every record as stored). An invalid `--step` value is one line — `error: invalid step path '../x': bad segment '..'`; an absolute path says `absolute paths are not step paths`. All rendered text is untrusted: control characters and terminal escape sequences are removed before printing (`--json` output was always safe, being JSON-escaped); `--raw` prints the stored text unescaped for debugging — pipe it through `cat -v` rather than straight to a terminal.

Options:

- `--follow` / `-f` — tails a live run.
- `--step` `<str>` — Show this step's stream (e.g. build[1]/implement).
- `--stream` — Interleave every step's stream into the log.
- `--verbose` — Also show internal/raw SDK records of a step stream.
- `--raw` — Print stored text unescaped (control characters and escape sequences included) — debugging only.
- `--json` / `--output json` — Machine-readable output: raw JSONL — events as stored in `events.jsonl`, stream records as `{"type": "stream", "step_path": …, "record": {…}}`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec audit`

```
rayspec audit [OPTIONS] {run}    # --output table|json (--json is the older spelling)
```

Answer "what did that agent actually **do**?" in one screen. The ledger is one line per fact the run left behind, oldest first: the run itself (created, started/resumed, workspace, paused, finished), every step, every command an agent started, every tool it called, every file it reported changing, every warning, and every approval with the identity behind it. An approval row names both halves — `approved by alice@example.com (cli)` — because `by` says which door the decision came through (`cli` for `rayspec approve`/`reject`, `tty` for the run's own prompt, `--yes`, `dry-run`) and the actor says whose hand it was. The header names the run, its status, the **actor** (`run.json`'s `actor`: who launched it, where the identity came from, the CI system and any provider account) and the workdir/branch; a `--dry-run` rehearsal is marked `dry run — nothing was executed`, because it called no provider and ran no shell body. Where a `.env` file tried to supply a `RAYSPEC_ACTOR`, both the header and the approval row say so — `approved by you (cli) — a .env declared 'security-team@corp.invalid', which is not an identity` — because that value was refused (a workflow step can write those files: see [runs-and-resume.md](runs-and-resume.md#which-sources-are-allowed-and-why)). Two honest limits: the header's actor is read straight out of `run.json`, while the approval row is re-resolved from the decision, so on a record somebody edited after the fact the two can disagree — and the decision row's guarantee is about the moment it was *recorded*, not about the file afterwards.

The rows are derived from the run's own `run.json` (its creation, which is where the actor is recorded), `events.jsonl` and per-step `stream.jsonl` — the same derivation an enabled `audit.jsonl` stores (see [runs-and-resume.md](runs-and-resume.md#the-local-audit-log)), so a rendered ledger and a stored one always agree. A step whose stream cannot be read is a `warning` row saying so, never a silently empty step. Reading only: the command never writes to the run directory, never re-runs anything and never contacts a network service. It is a report over the files of **one** run on **this** machine — it proves nothing about them (anybody who can read a run directory can also edit it) and knows nothing about other runs, projects or people. Every cell is untrusted text, printed with control characters and terminal escape sequences removed.

Options:

- `--commands` — Only what was executed: every command an agent ran — a Codex `command_start`, or any tool call that carries a command line, which is how the Claude adapter reports a `Bash` call — plus the `shell:`/`python:` steps, which are rayspec running a command itself. A step row names the step and its kind, not its body: the rendered body is not kept in the run directory, and `rayspec explain {run} {step}` re-renders it from the workflow.
- `--json` / `--output json` — Machine-readable output: `{run_id, workflow, status, dry_run, actor, workdir, branch, rows: [{ts, kind, step, detail, data}]}`, where `kind` is `run`, `step`, `command`, `tool`, `file`, `warning` or `approval`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec explain`

```
rayspec explain [OPTIONS] {run} {step}
```

Answer "why did this step run, skip or fail?" for one step of a finished (or unfinished) run.
`{step}` is a step path as it appears in `rayspec show` — `assess`, `build[2]/implement`,
`fan[0]/patch`. The screen has one block per question, and blocks with nothing to say are
omitted:

- **status** — final status, `skip_reason`, `(tolerated)`, the error, attempts, duration, tokens
  and cost, plus where the step is defined (`file:line`);
- **cap** — only for a step the run-level circuit breaker skipped: the cap that actually fired.
  `skip_reason: budget_exceeded` names the *breaker*, and `budget_usd`, `max_tokens` and
  `timeout_total` are one breaker sharing that one reason, so this line says which of them was
  over (`cap  time limit exceeded (elapsed 2h 4m > timeout_total 2h 0m)`) — every cap that was
  over, not just the first. When the run did not end on the breaker (it was interrupted or
  paused, or a cap was raised since), the caps are re-checked against the totals and timestamps
  the run record still holds and the line says so: `cap  budget exceeded (…) (recomputed from
  the run record)`. A recomputed line answers "which cap is this run over", not "which cap
  fired"; `--json` carries the same distinction as `cap.source` (`run.reason` | `recomputed`);
- **join** — every `needs` with its recorded outcome (and what the join table *counts* it as: a
  tolerated failure counts as succeeded) and the decision that followed. A step whose skip reason
  says the run itself was being torn down (`run_failed`, `stopped`, `budget_exceeded`) is
  re-evaluated the way the scheduler saw it, and the row says so: `decision skip (run_failed) —
  the run was already draining`;
- **when** — the expression, its value re-evaluated in the step's own scope, and each operand
  with the value it had (`steps.assess.output = LGTM`);
- **retries** — one line per `step.retry` event: attempt, delay and the error that caused it;
- **agent** — the resolved agent *after* the merge (provider, model with the tier/alias it came
  from, effort, access, tools, where it was defined) next to the provider/model actually
  recorded, and the session id;
- **env** — the step's `env:` mapping, rendered (a `secret: true` input stays `<secret>`);
- **prompt / script** — for a `prompt:` step the rendered `prompt:` body the provider received,
  read back from `steps/<path>/prompt.txt` (the agent's rendered `instructions` — the system
  prompt — are not persisted and are not shown); for `shell:`/`python:` the rendered script with
  its `${RAYSPEC_V<n>}` slots and their values. A value over 64 KiB is not inlined into a
  preview: its slot reads `<N bytes — too large to inline here; read it in the producing step's
  output file under the run dir>`. Only the first 20 lines are shown without `--full`.
- **fingerprint / output** — the step fingerprint, whether the run *replayed* the step from a
  previous one (`reused`), and the output file.

A step whose record is missing (the run never reached it) is still explained from the workflow,
with a warning saying the sections are re-evaluated rather than replayed. A workflow file that
changed since the run is flagged the same way (`workflow 't' changed since this run (hash … → …)`)
— the records are still the records, but anything re-evaluated comes from the file as it is now. Values re-evaluated
now — the `when:` operands, a script, a prompt that was not persisted — use the run's stored
inputs and outputs; `env.*` is this process's environment, because no run records its own, and
a re-evaluated template that actually reads `env.*` says so with a warning.
Nothing is executed and nothing is written. Exit 2 for an unknown run or step path.

Options:

- `--full` — Print the whole persisted prompt/script instead of the first 20 lines (control
  characters are stripped on the way to the terminal, like everywhere else).
- `--json` / `--output json` — Machine-readable output: `{run_id, workflow, step, def_path, kind, location,
  status, skip_reason, tolerated, attempts, error, exit_code, approved, duration_ms, tokens,
  cost_usd, cost_source, usage_unknown, cap: {reason, knobs, source} | null, join: {join, needs: [{step, status, counts_as,
  skip_reason, tolerated}], decision, skip_reason}, when: {expression, value, error, operands:
  [{reference, value, error}]}, retries: [{attempt, delay_s, error}], agent, env, rendered:
  {kind, source, text, env}, fingerprint, reused, output_ref, output_kind, prompt_ref,
  warnings}`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec eval`

```
rayspec eval [OPTIONS] {run} {EXPR}
```

Evaluate a Jinja expression against a stored run's context — a read-only REPL for the
expressions you write in `when:`, `until:` and `each:`. The expression sees exactly what the
chosen step saw: `inputs`, `steps` (lexically scoped), `run`, `project`, `env`, plus `iteration`
inside a loop body and the `as:` item inside an `each:` body. Undefined references fail with the
same hint the engine raises ("step 'lint' was skipped (when_false) — guard with
steps.lint.status == 'succeeded'"), never a traceback. Exit 2 on an unknown run/step, a syntax
error or an evaluation error. A workflow file that changed since the run is reported as a
warning (the stored outputs are still the stored outputs; the scope comes from the file).

The value is printed as text if it is text, otherwise as JSON (`true`/`false`/`null` for the
scalars without a text form, indent 2 for mappings and lists). Nothing is executed: no step
runs, no provider is created and the run directory is never written to.

```
rayspec eval 20260820-1 "steps.assess.output.verdict == 'fix'"
rayspec eval 20260820-1 "iteration.prev.implement.output | length" --step build[2]/implement
```

Options:

- `--step` `<path>` — Evaluate in this step's scope (record path, e.g. `build[2]/implement`; a
  definition path such as `build/implement` reads as iteration 1 / item 0). Without it the
  expression is evaluated in the run's root scope.
- `--shell` — Render `{{ expr }}` the way a `shell:` body would: the substituted
  `${RAYSPEC_V<n>}` reference on the first line, the slot values below it.
- `--json` / `--output json` — Machine-readable output: `{run_id, step, expr, warnings, value, type}` — or
  `{run_id, step, expr, warnings, shell, env}` with `--shell`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec resume`

```
rayspec resume [OPTIONS] {run}
```

Resume a paused, failed or interrupted run: the run is re-executed from the top with a reuse cache (steps that succeeded — or failed with `allow_failure` — and still have their output file *and* whose fingerprint — rendered prompt/script + agent — is unchanged are replayed, not re-run). Refuses (exit 2) a run that already succeeded or was cancelled (unless `--force`), a run whose workflow file changed since the run (unless `--force`), a run with a live pid and a run recorded as running on another host (unless `--force`). A pending gate is re-asked on a TTY (`--yes` auto-approves, `--no-interactive` pauses again with exit 3). With `--json` the JSONL events **and** the final summary object are printed on stdout (the summary is the last line), exactly like `rayspec run --json`.

`resume` finds the run wherever it was recorded, and from that point on everything project-scoped is read for the **run's** project rather than the directory you typed the command in: its workflow, the `config.yaml` that decides what a `model:` tier or an `@alias` resolves to, `.rayspec/rayspec.lock`, `secrets:`, the operator policy and the spend ledger. (`<project>/.rayspec/.env` is the exception and stays the caller's — it is a credential file controlled by whoever pushed the checkout.) The same holds for [`rayspec approve`](#rayspec-approve) and [`rayspec reject`](#rayspec-reject).

Two things are not in `run.json` and come from the command line again — both are checked **before** anything is written, after the workflow-hash guard and after the pending-gate pointer (a run paused at a gate, non-TTY without `--yes`, exits 3 pointing at `approve`/`reject` first — that is what such a run needs):

- **Secret inputs** (`secret: true`, [schema.md](schema.md#secret-inputs)) are never persisted: every secret that was given at launch must be supplied again with `--input NAME=VALUE` (accepted on resume for secret inputs only — any other name is `inputs are fixed per run`, exit 2) or through `RAYSPEC_INPUT_<NAME>` in the environment; otherwise exit 2 `missing secret input(s): token — pass --input token=… or set RAYSPEC_INPUT_TOKEN`. An optional secret that was not given at launch is not required (but may be supplied now — it is then recorded as `<secret>` and exported like the others).
- **The stub script**: a run launched with `--stubs` recorded the file's absolute path (`stubs_path`); `resume` loads it again (a missing/unreadable file is exit 2 with the hint `pass --stubs <path>`), `--stubs PATH` overrides it (and becomes the recorded path). A run launched with `--stubs-from <run>` recorded that donor run instead (`stubs_path: "run:<run id>"`) and rebuilds its answers from the store — a replay that pauses at an approval gate finishes with the recorded answers, never with the stub provider's default. A `--dry-run` record resumes as a dry run (stub providers, shell/python skipped), so its stubs keep applying; the rule holds as on `run` (a non-stub agent may only be scripted in a dry run).

Options:

- `--force` — Resume even if the workflow changed, the run already finished, or its pid/host cannot be verified.
- `--yes` / `-y` — Auto-approve gates (except gates whose [approval class](runs-and-resume.md#approval-classes) is `allow_yes: false`).
- `--approve-class` `<name>` — Pre-approve gates of one approval class (repeatable). Given, it also lifts the "still paused" short-circuit: the run is resumed so the flag can answer the pending gate, instead of exiting 3 with the approve/reject pointer.
- `--no-interactive` — Never prompt; pause at gates (exit 3).
- `--json` / `--output json` — Machine-readable output.
- `--quiet` — Only problems and run-level lines.
- `--verbose` — Also show step starts.
- `--input` / `-i` `NAME=VALUE` — Re-supply a secret input (repeatable; secret inputs only).
- `--stubs` `<path>` — Stub script for the resumed run (default: the file recorded at launch).
- `--locked` / `--no-locked` — Check the resumed workflow against `.rayspec/rayspec.lock` (see [`rayspec run`](#rayspec-run)); on by default under `CI`. A resume is where an unattended job spends the second half of a run, and the workflow hash does not cover a tier that was re-pointed in `config.yaml` — so the lockfile is checked here too — the one in the run's own project, against the models its own `config.yaml` resolves.
- `--wait-slot` `DURATION` — Queue for a free host run slot instead of failing (same spellings as [`rayspec run`](#rayspec-run)). A resume takes a slot because it starts the same agents.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec approve`

```
rayspec approve [OPTIONS] {run} [comment]
```

Record an approval for the pending gate of a *paused* run and resume it in-process; the optional comment becomes the gate step's output. On a run an operational ceiling paused (`pause.reason` `budget` or `failures`) there is no gate to answer: `approve` there means "run it anyway" and **waives** the ceiling for that run — a spending waiver does not touch the failure breaker, and closing the breaker does not waive a spend. `rayspec resume` re-evaluates the ceiling instead, which is usually what an unattended job wants. Refuses if the run is not paused or the workflow changed (`--force`). Secret inputs and the stub script are re-obtained exactly like [`rayspec resume`](#rayspec-resume) (`--input NAME=VALUE` for secret inputs / `RAYSPEC_INPUT_<NAME>`; the recorded `--stubs` file, or `--stubs PATH`) — all checked before the decision is written. Exits with the resumed run's exit code. `--json` prints the JSONL events and the summary object (last line) on stdout.

Options:

- `--json` / `--output json` — Machine-readable output.
- `--quiet` — Only problems and run-level lines.
- `--force` — Resume even if the workflow changed.
- `--input` / `-i` `NAME=VALUE` — Re-supply a secret input (repeatable; secret inputs only).
- `--stubs` `<path>` — Stub script for the resumed run (default: the file recorded at launch).
- `--locked` / `--no-locked` — Check the workflow against `.rayspec/rayspec.lock` before resuming (on by default under `CI`).
- `--wait-slot` `DURATION` — Queue for a free host run slot instead of failing.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec reject`

```
rayspec reject [OPTIONS] {run} [reason]
```

Record a rejection for the pending gate of a *paused* run and resume it; the optional reason becomes the gate's output. A run an operational ceiling paused has nothing to reject — the ceiling is unchanged, so the run simply pauses on it again. What happens next is the gate's `on_reject`: `cancel` (default) ends the run as cancelled (exit 4), `continue` proceeds with `approved: false`, `fail` fails the step. Secret inputs and the stub script are re-obtained exactly like [`rayspec resume`](#rayspec-resume). `--json` prints the JSONL events and the summary object (last line) on stdout.

Options:

- `--json` / `--output json` — Machine-readable output.
- `--quiet` — Only problems and run-level lines.
- `--force` — Resume even if the workflow changed.
- `--input` / `-i` `NAME=VALUE` — Re-supply a secret input (repeatable; secret inputs only).
- `--stubs` `<path>` — Stub script for the resumed run (default: the file recorded at launch).
- `--locked` / `--no-locked` — Check the workflow against `.rayspec/rayspec.lock` before resuming (on by default under `CI`).
- `--wait-slot` `DURATION` — Queue for a free host run slot instead of failing.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec cancel`

```
rayspec cancel [OPTIONS] {run}
```

Interrupt a live run (sends SIGINT to its recorded pid; asks first unless `--yes` or `--json`; without a terminal to answer on it refuses with exit 2 and the `--yes` hint) or mark a paused run as cancelled and clear its workdir path lock best effort (the lock is held by the engine while a run executes; a paused run has already released it, so this only cleans a stale file). `--force` also marks a record that claims to be running on another host.

Before signalling, the recorded pid is verified to be *this run's* rayspec process, twice. Exact check: the engine records the start time of its process next to `pid` in `run.json` (`pid_started_at`, the output of `ps -o lstart= -p <pid>` run under `LC_ALL=C TZ=UTC`, so the string does not depend on the locale or timezone of whoever launched or cancels the run — the `/proc/<pid>/stat` start time on Linux when `ps` is missing or cannot report it — refreshed on every resume because the pid changes); the live process must report exactly that string, so two runs of the *same* workflow, or a pid reused by another run of it after a crash, are told apart. Command-line check (also for records written before `pid_started_at` existed, which have only this one): the command line (`ps -o command= -p <pid>`) must contain a rayspec execution command (`rayspec run|resume|approve|reject`, whole tokens — a process that merely mentions `rayspec` in an argument does not count) and name the run as a whole word — the run id (`rayspec resume <id>`, `rayspec run --resume <id>`) or the workflow name / file (`rayspec run <workflow>`; `gate` does not match `gate2`). A pid that fails either check — typically pid reuse after a crash or reboot left a `running` record behind, or an edited `run.json` — is never signalled: `error: pid <n> is not a rayspec run process (stale record?) — use `rayspec cancel --mark` to mark the run cancelled without signalling`, exit 2 (also with `--yes`/`--json`). `--mark` finalizes the record as cancelled (`pid` cleared, reason, `run.finished`) without sending any signal — for stale records, whatever the pid now is.

Options:

- `--yes` / `-y` — Do not ask before interrupting a live run.
- `--force` — Mark a running record cancelled even if it belongs to another host.
- `--mark` — Mark the record cancelled without signalling any process (stale record, pid reused by another program); no confirmation prompt.
- `--json` / `--output json` — Machine-readable output (implies no confirmation prompt): `{run_id, action: "signalled", pid, status}` for a live run, `{run_id, action: "cancelled", pid: null, status, lock_released}` for a paused/dead one, `{run_id, action: "marked", pid: null, status, lock_released}` with `--mark`.
- `--root` `<path>` — Project root (the directory containing .rayspec/). Default: walk up from the cwd.

### `rayspec init`

```
rayspec init [--kind code|content | --from EXAMPLE] [--force] [--no-skill] [--root DIR]
```

Scaffold a project: `.rayspec/{workflows/example.yaml, agents/reviewer.yaml, prompts/*.md,
config.yaml, stubs/example.yaml}` from the templates packaged with rayspec, plus the **rayspec
skill for coding agents** in `.claude/skills/rayspec/` (`SKILL.md` + `references/*.md`, the same
files `rayspec skill install` writes — see [agent-skill.md](agent-skill.md); `--no-skill` opts
out), then print the next steps (`doctor`, `validate`, `plan example`, `run example --dry-run
--stubs .rayspec/stubs/example.yaml`, a real run, and "open a fresh Claude Code session here —
the skill loads automatically"). `--root DIR` is the directory that receives `.rayspec/` and
`.claude/skills/rayspec/` (default: the cwd — `init` does **not** walk up to an enclosing
project). Files that already exist — scaffold and skill alike — are kept and listed as
`exists … (skipped; use --force to overwrite)`; `--force` overwrites them — keeping the mode of
the file it replaces (a `config.yaml` you chmodded to `0600` stays `0600`) and refusing a target
that is a *symbolic link*, which is an error like a directory in the way: a scaffold writes files
inside the project it scaffolds, and replacing a link (or writing through it) would do neither.
When every file
already exists (nothing written) a `warning: nothing written …` line goes to stderr, but the
exit code stays 0. The default `code` scaffold's `files` step runs
`git ls-files`, so when the target is not inside a git checkout (no `.git` at or above it) `init`
prints `warning: <dir> is not a git repository — … run \`git init\` here or use \`rayspec init
--kind content\`` on stderr and still exits 0; `--kind content` needs no git and stays silent. Exit 2 (`error: cannot write the scaffold: …` on
stderr, no traceback) for an unknown `--kind`, a `--root` that is not a directory, a *directory*
where a template file goes (e.g. `.rayspec/config.yaml/`), a symlink where `--force` would
write, or any other filesystem error
(`error: cannot write the skill: … (the .rayspec/ scaffold was written; re-run with --no-skill
to skip the skill)` for the skill files — the scaffold is complete at that point). Nothing is
written outside `.rayspec/` and `.claude/skills/rayspec/`; runs live under `RAYSPEC_HOME`.

Switching kinds is per file: `rayspec init --kind content` over an untouched `code` scaffold adds
only the files `code` does not ship (`prompts/draft.md`) and keeps the rest, which leaves a mixed
project; `--force --kind content` replaces the shared files but leaves the `code`-only ones
(`prompts/review.md`) behind. Both cases print a stderr `warning:` naming the old kind and the
files that stay (the detection compares `workflows/example.yaml` with the packaged templates, so
an edited workflow is never flagged); delete the listed files by hand if they are unused.

| `--kind` | example workflow | notes |
|---|---|---|
| `code` (default) | `files` (shell: `git ls-files`) → `review` (read-only `reviewer` agent, `prompt_file`, `output_schema`) → `verdict`/`summary` outputs | `isolation: none` (a review only reads; delete the line for a worktree per run) |
| `content` | `draft` (inline `writer` agent) → `review` (`reviewer` agent, `output_schema`) → `draft`/`verdict`/`notes` outputs | `isolation: none`, no shell/python steps — for projects that are not code checkouts |

Both kinds ship the same `config.yaml` (commented `tiers`/`aliases`/`pricing`/`providers`
blocks) and a `stubs/example.yaml` that makes `rayspec run example --dry-run --stubs
.rayspec/stubs/example.yaml` succeed without any login (its header comment links the stub file
format in [providers.md](providers.md#stub-stub) by URL).

#### Starting from an example

```
rayspec init --from hello_review
```

`--from EXAMPLE` copies one of the example projects packaged with rayspec into the target
directory instead of the generic template: its `.rayspec/` tree, its stub scripts and its
`README.md`, all verbatim. The examples ship inside the wheel, so this works from a
`uv tool install rayspec` with no checkout of the repository. `--from` and `--kind` are mutually
exclusive (an example brings its own workflow), and an unknown name is a usage error (exit 2)
that lists every example with its description and a `did you mean …?` when the name is close
(a build with no corpus at all says `error: no examples are packaged with this build` instead):

```
$ rayspec init --from helo_review
error: unknown example 'helo_review'; did you mean 'hello_review'?
hint: available examples (rayspec init --from <name>):
  fix_issue         Triage a GitHub issue, fix it in a self-healing loop until the tests …
  hello_review      Review a file or directory with a single prompt step — the smallest …
  …
```

An example is applied whole or not at all — and so is the generic scaffold. Every file is written
to a temporary name beside its target first and moved into place only once all of them are ready,
so a write that fails (a directory where a file goes, a full disk, a read-only tree) leaves the
directory exactly as it was: no half-written project, and no `.rayspec/` that would make a
directory look like a rayspec project. When the target directory already holds one of its
files with *different* content — a `config.yaml` from a plain `rayspec init`, another example's
`stubs.yaml`, a workflow you edited — `--from` refuses (exit 2, naming those files) instead of
writing the rest around them: a kept `config.yaml` or stub file belongs to something else, and
the commands `init` prints next then fail. `--force` replaces them, and an empty directory is the
other way out; re-running the same example over an untouched copy writes nothing and stays exit 0.
The example's own `README.md` is the exception — an existing README is kept (a stderr `warning:`
says so, and the `open README.md` next step is dropped), so trying an example inside a repository
that already has one still works.

The next steps `--from` prints end with the example's own scripted dry run — the exact command
(inputs included) that this repository asserts green on every commit, so a fresh directory has a
working run before any credentials exist. A scenario that supplies a `secret: true` input is
never rendered: `-i NAME=VALUE` is the channel for a secret, and the printed line would put its
value on the terminal and in your shell history.

```
next steps:
  rayspec validate                        # schema, graph, references, capabilities
  rayspec run hello_review -i target=src/ -i focus=style --dry-run --stubs stubs.yaml   # scripted agents, no login needed
  open README.md                          # what this example shows, and a real run
```

The catalogue is whatever the build ships (`rayspec init --from ''` prints it); today that is
`fix_issue`, `hello_review`, `notify_webhook`, `pr_review`, `release_check`, `review_sweep`,
`secret_via_tool`, `triage_fanout` and `unsupported_demo` — see [examples.md](examples.md).

### `rayspec new workflow`

```
rayspec new workflow <name> [--agent NAME] [--description TEXT] [--force] [--root DIR]
```

Add one workflow to a project that already exists — `rayspec init` is for creating the project,
`new` for growing it. Writes `.rayspec/workflows/<name>.yaml` (one read-only agent step, one
`target` input, `isolation: none`) and prints `rayspec validate <name>` / `plan` / `run <name>
--dry-run`; the fresh workflow validates and dry-runs without any login. `<name>` is both the
`name:` and the file name, so it must be a workflow identifier (`^[a-z][a-z0-9_]*$`, not a
reserved context root such as `run`); anything else is a usage error naming the rule. `--agent
NAME` references `.rayspec/agents/<NAME>.yaml` (or `~/.rayspec/agents/<NAME>.yaml`) instead of
writing an inline agent into the workflow; that file has to exist, because a workflow pointing at
a missing agent fails the `rayspec validate <name>` the command itself prints next — an unknown
name is exit 2 with a `did you mean …?` and a pointer at `rayspec new agent <name>`.
`--description TEXT` fills the `description:` (quoted for YAML when it needs to be).
An existing file is never touched: `error: .rayspec/workflows/<name>.yaml already exists` +
`hint: pass --force to overwrite it`, exit 2. Without `--root` the project is found the way every
project command finds it (walk up to the first `.rayspec/`, then `.git`, else the cwd). With
`--root` that directory **is** the project and is not a place to start walking up from: a
directory with no `.rayspec/` is exit 2 pointing at `rayspec init`, so a mistyped `--root` is an
error instead of a file written into the enclosing project.

### `rayspec new agent`

```
rayspec new agent <name> [--force] [--root DIR]
```

The same for `.rayspec/agents/<name>.yaml`: a reusable `provider`/`model`/`access`/`instructions`
agent that any workflow of the project can reference as `agent: <name>` (or extend per step with
`agent: { extends: <name>, model: large }`). `--force`, `--root`, the name rule and the
`already exists` refusal behave exactly as for `new workflow`. `rayspec new` without a subcommand
prints the help and exits 2.

### `rayspec doctor`

```
rayspec doctor [--probe] [--provider ID]... [--json | --output FORMAT] [--root DIR]
```

Environment and provider health in one table: Python and rayspec versions, `RAYSPEC_HOME`
(exists/writable), the merged `config.yaml`, project detection (`--root` or walk up from the
cwd; workflows found), `git` and `uv` on `PATH`, and per provider the SDK version, the CLI
binary (claude: the SDK's bundled `claude`, then `PATH`, then the known install locations;
codex: `providers.codex.codex_bin` from config, else the bundled runtime from
`openai-codex-cli-bin`), a tolerant `-v`/`--version` probe, and the auth row: `ok` via
`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`; for Claude also `ok` when the
`claude` CLI's own claude.ai login is found — `~/.claude/.credentials.json` (`CLAUDE_CONFIG_DIR`
honoured) or, on macOS, the `Claude Code-credentials` keychain item (existence only: the file is
never read and the keychain secret never requested; the lookup is bounded and skipped when
`security` is missing); for Codex `info` when `~/.codex/auth.json` exists (`verify with --probe`);
otherwise `warn login state unknown` with a login hint. The Codex hint is runnable as written:
`run \`codex login\`` when `codex` is on `PATH`, else `run \`<bundled path> login\`` (the bundled
binary is not on `PATH`), or set `OPENAI_API_KEY`. `~/.rayspec/.env` is loaded first, so keys kept
there count; the project `.rayspec/.env` is **not** loaded (it is a credential surface of the
checkout — only `run`/`resume`/`approve`/`reject` apply it) and shows up as an `info` row
`project .env: <path> (N vars, applied only by run/resume/approve/reject)` when the file
exists. A provider that reports no USD cost (Codex) gets a
`<id> pricing` row: `info` with the nudge `tokens only — add pricing.<model> for estimates` when no
[pricing](providers.md#pricing) table exists, `warn` when a table exists but misses one of the
provider's tier/alias models (or is malformed), `ok` when every configured model is priced;
models disabled with a `null` entry are listed as `pricing disabled (null) for <model>` (no
nudge; `info` when every model is disabled).

`--probe` additionally instantiates every registered provider (or only the `--provider` ids; the
option repeats) and runs its `healthcheck(probe=True)` — a one-turn "Reply with exactly OK"
(Claude: no tools, `max_turns=1`; Codex: read-only, deny-all, ephemeral; Codex also checks the
login via `account()`). Probes need a login and can take up to two minutes per provider; a probe
that exceeds the outer 180 s bound is reported as a failed `<id>.probe` check (`probe timed out`),
and the adapter is closed with a 15 s bound — a genuinely hung CLI child may still keep the
process alive for a moment after the table is printed. A successful probe is the verification
the auth row asked for: a `warn`/`info` `<id> auth` row turns `ok` (`probe OK`) and its hint
disappears.

Exit code: `0` when every *required* check passes (Python ≥ 3.11, `RAYSPEC_HOME`, config, `git`,
each provider's SDK import and CLI binary), `1` otherwise. A failed probe is a required failure
only for a provider that is *configured* on this machine — requested explicitly with
`--provider <id>`, or its auth row found credentials (`ok`/`info`; providers without an auth row,
like `stub`, always count). A provider with no credentials at all that was merely probed by
default (e.g. Codex on a Claude-only box) gets a `warn` probe row instead, with the hint to scope
the probe (`rayspec doctor --probe --provider claude`; only providers with an auth row are
listed) or to log in, and the exit code stays `0`. A failed probe of a required provider reuses
the auth row's login hint without its `verify with --probe` clause.
Auth hints, the pricing row, the CLI version probe, project detection and `uv` are informational
(`warn`/`info`).
`--json`: `{"ok": bool, "exit_code": 0|1, "checks": [{id, label, status: ok|warn|fail|info,
detail, required, hint}]}` (keys in that order).

### `rayspec skill install`

```
rayspec skill install [--global] [--force] [--root DIR]
```

Write the packaged rayspec skill for coding agents ([agent-skill.md](agent-skill.md):
`SKILL.md` + `references/{concepts,schema,templating,cli,providers,examples}.md`) to
`<project>/.claude/skills/rayspec/` — `--root DIR` names the project; default: the nearest
directory with `.rayspec/`, then `.git`, else the cwd — or, with `--global`, to
`~/.claude/skills/rayspec/` for every project of this user. Same idempotence as `init`: one line
per file (`created` / `overwrote` / `exists … (skipped; use --force to overwrite)`), a summary
line with the target path, a stderr `warning: nothing written …` when every file existed (exit
stays 0), and the hint `open a fresh Claude Code session in <dir> — the rayspec skill loads
automatically`. `--force` overwrites (how you update after upgrading rayspec). Exit 2 (`error:
cannot write the skill: …`, no traceback) for a `--root` that is not a directory, a directory
where a skill file goes, or any other filesystem error; `--global` together with `--root` is a
usage error (exit 2, `--global and --root are mutually exclusive`).

### `rayspec skill show`

```
rayspec skill show [--root DIR] [--json | --output FORMAT]
```

Print the packaged skill (its directory, the rayspec version and a 12-hex-digit content digest
that identifies the skill's files, the file count) and the state of the project install
(`<root>/.claude/skills/rayspec`, `--root` as for `install`) and the global install
(`~/.claude/skills/rayspec`): `not installed`, `digest … — up to date` (byte-identical to the
packaged skill) or `digest … — differs from the packaged skill (… rayspec skill install
--force …)`. `--json`: `{packaged: {path, rayspec_version, digest, files}, project: {path,
state: missing|current|stale, digest}, global: {…}}`. Exit 0.

### `rayspec skill path`

```
rayspec skill path
```

Print the packaged skill directory (the one `install` copies; it holds `SKILL.md` and
`references/`). Exit 0.

### `rayspec completion`

```
rayspec completion <bash|zsh|fish>
rayspec completion --values workflows|runs [--root DIR]
```

Print a shell-completion script to source, and nothing else — installing it is your decision:

```sh
rayspec completion zsh  > ~/.zsh/completions/_rayspec     # then: compinit
rayspec completion bash > ~/.local/share/bash-completion/completions/rayspec
rayspec completion fish > ~/.config/fish/completions/rayspec.fish
```

The script completes commands, sub-commands and options, plus the two argument slots that save
real typing: a **workflow name** after `run`, `plan`, `validate` and `test`, and a **run id**
after `show`, `logs`, `resume`, `approve`, `reject`, `cancel`, `eval`, `explain`, `runs diff` and
`runs stubs`. It gets those from `rayspec completion --values workflows|runs`, which prints one
candidate per line for the project the shell is standing in (run ids: the 50 newest). That
lookup never fails and never prints a diagnostic — no project, an unreadable `config.yaml` or a
half-written workflow simply yields nothing, so a broken checkout can never inject an error
message into the candidate list. `rayspec completion` with no shell, or with a shell that is not
one of the three, is a usage error (exit 2) naming the supported ones; `--values` together with a
shell is a usage error too.

There is deliberately no `--install-completion`: Typer's version of it appends a `source` line to
`~/.bashrc` / `~/.zshrc` as a side effect of a flag, which is not something a workflow runner
should do to your dotfiles. `rayspec completion <shell>` prints the same script (Typer's, plus
the workflow/run-id wrapper) and leaves the placement to you.

### `rayspec version`

```
rayspec version
```

Print `rayspec <version>` (same as `rayspec --version` / `-V`).

## Roadmap

`tests/docs/test_cli_reference.py` keeps this page and the Typer app in lockstep: a change that
adds a command must add its `### \`rayspec <cmd>\`` section here in the same change, or the gate
fails.

Every command of the design is in this build, including `init`,
`doctor [--probe]` and the `skill` group; the `--json` summary object of `run` is the last stdout line and the Rich
live step tree is wired into `run` on a TTY (one line per finished step with `--quiet` / non-TTY).
`rayspec run <wf> --resume <run-id>` remains an alternative to `rayspec resume <run-id>` for runs
of the current project.
