# Runs, checkpoints and resume

Files are the checkpoint. Everything rayspec knows about a run lives in one directory and
`rayspec run --resume` rebuilds the run from it.

## Where runs live

```
$RAYSPEC_HOME/                                  (~/.rayspec)
  config.yaml, .env, workflows/, agents/
  projects/<slug>/
    runs/<run-id>/                              the run store (below)
    worktrees/<workflow>-<shortid>/             default isolation (see isolation.md)
    source.git/                                 bare clone for --repo <url>
    locks/                                      per-workdir lock files
```

The **slug** is `host/owner/repo` from `git remote get-url origin` (e.g.
`github.com/rayspec-labs/rayspec-py`), else `local/<dirname>-<sha1(abspath)[:8]>`; a run with
`--repo <source>` uses the source's slug (for a `file://`/unrecognised URL
`local/<name>-<sha1(url)[:8]>`, the bare clone's project), see [isolation.md](isolation.md#--repo). The
**run id** is `YYYYMMDD-HHMMSS-<4 base32 chars>` (UTC, time-sortable); commands accept any
unique prefix.

## The run directory

```
runs/<run-id>/
  run.json                      RunRecord — rewritten atomically after every step
  events.jsonl                  lifecycle events, one JSON object per line
  audit.jsonl                   the local ledger, only with RAYSPEC_AUDIT_LOG=1 (see below)
  steps/<path>/                 one directory per executed step (build[2]/implement → steps/build[2]/implement/)
    output.txt | output.json    the step's output (written BEFORE the record that points at it)
    prompt.txt                  prompt steps: the rendered prompt: body, written BEFORE the provider call (StepRecord.prompt_ref; `rayspec explain --full`; a write that fails is a warning, never a failed step)
    stream.jsonl                agent events / shell stdout+stderr lines, with the attempt number
    stdout.log, stderr.log      shell/python: attempt 1 starts the file, later attempts append under "--- attempt N ---"
    context.json                the template context the step saw (what RAYSPEC_CONTEXT points at)
  artifacts/                    yours (RAYSPEC_ARTIFACTS_DIR)
    <step path>/<declared path> a copy of every file a step promised under `artifacts:`
  tmp/                          scratch (spill files of oversized {{ }} values)
```

### `run.json`

```json
{
  "schema": 1,
  "run_id": "20260820-125859-ikd7",
  "workflow_name": "review", "workflow_path": ".rayspec/workflows/review.yaml",
  "workflow_hash": "e66903bd…",           // sha256 over the workflow + includes + agent files + prompt/instructions files
  "project_slug": "local/demo-cad85336", "project_root": "/Users/me/demo",
  "inputs": {"target": ".", "strictness": "strict", "token": "<secret>"},   // a secret: true input is stored as "<secret>"
  "secret_inputs": ["token"],             // names of the secret: true inputs (their values are never persisted)
  "stubs_path": null,                     // --stubs file (absolute path) or --stubs-from donor ("run:<id>"), reused by resume
  "status": "succeeded", "reason": null,
  "created_at": "…", "started_at": "…", "ended_at": "…",
  "actor": {"id": "me@example.com", "source": "git", "ci": null,   // who launched the run: RAYSPEC_ACTOR,
             "provider_accounts": {}},                            // else git user.email, else the OS user
  "resume_count": 0, "pid": null, "pid_started_at": null, "host": "mbp",   // pid_started_at: start time of the
  "dry_run": false,                       // pid's process (`ps -o lstart=`, for cancel); dry_run: a --dry-run rehearsal (stub providers)
  "workspace": {"isolation": "worktree", "workdir": "…/worktrees/review-ikd7",   // head_sha: tip of the workdir at the
                "branch": "rayspec/review-ikd7", "base_branch": "main", "base_sha": "…", "head_sha": "…"},   // last record write (pause/end/resume)
  "pause": null,                          // {token, step, message, requested_at,
                                          //  decision{approved, comment, by, decided_at, actor}}
  "outputs": {"verdict": "approve", "summary": "…"},
  "cost_source": "none",                  // run level: provider | table | partial | none (see "Failures, retries and timeouts")
  "toolchain": {"rayspec": "1.0.0", "python": "3.12.8", "platform": "macOS-15.5-arm64",   // what was in effect at
                "providers": {"claude": {"sdk_version": "0.2.142", "cli_version": "2.1.0",  // run start; an
                                         "cli_path": "/usr/local/bin/claude"}},             // unreachable provider
                "models": {"agents.reviewer": "claude-haiku-4-5"}},                         // gets an "error" entry
  "steps": {
    "review": {"path": "review", "id": "review", "kind": "prompt", "status": "succeeded", "attempts": 1,
               "started_at": "…", "ended_at": "…", "duration_ms": 4, "ok": true, "exit_code": null,
               "approved": null, "output_ref": "steps/review/output.json", "output_kind": "json",
               "output_sha256": "…", "session_ref": {"provider": "stub", "id": "stub:review:1"},
               "provider": "stub", "model": "haiku",
               "usage": {"input": 13, "cached_input": 0, "cache_write": 0, "output": 10, "reasoning": 0},
               "cost_usd": null, "cost_source": "none", "error": null, "skip_reason": null,
               "tolerated": false, "iteration": null, "item_index": null, "item_sha256": null,
               "loop": null, "each": null, "fingerprint": "…"}
  }
}
```

`toolchain` records what produced the run — the rayspec and Python versions, the platform, one
entry per provider that the workflow's agents resolve to (`sdk_version`, `cli_version`,
`cli_path`, or `error` when the provider could not be reached) and the literal model id each
resolved agent used (`null` = the provider's default). It is captured once, at the run's first
start, from `Provider.healthcheck(probe=False)`, and is never re-captured on resume, so it keeps
describing the toolchain the run began with; `rayspec show` prints it as the `toolchain:` block
and `run.json` files written before it existed simply have no field. A dry run records the stub
that stood in for the provider.

`pid` is set while the run is `running` (and kept on `paused`) and cleared on every other final
status; `pid_started_at` is the start time of that process (the `ps -o lstart= -p <pid>` string
as printed under `LC_ALL=C TZ=UTC` — fixed so the launching and the cancelling shell's locale or
timezone cannot make it differ — or the `/proc/<pid>/stat` start time on Linux when `ps` is
missing or cannot report it), recorded at launch and on
every resume so `rayspec cancel` can tell the run's own process from a reused pid (missing in
records written before it existed). `error` is `{type, message, transient}`; `loop` is `{iterations, converged}`; `each` is
`{total, succeeded, failed}`; `fingerprint` is a sha256 of the rendered prompt/script plus the
resolved agent (used by `--resume --force`). Unknown keys are ignored when loading (forward
compatible).

### `events.jsonl`

Each line is `{"type", "run_id", "ts", "step_path", "data"}`:

| type | data |
|---|---|
| `run.started` / `run.resumed` | `workflow`, `dry_run`, `resume_count`, `workdir` |
| `workspace.created` | `workdir`, `branch`, `base_sha` |
| `step.started` | `kind`, `attempt` |
| `step.retry` | `attempt` (the next one), `delay_s`, `error` |
| `step.finished` | `status`, `duration_ms`, `usage`, `cost_usd`, `error`, `skip_reason`, `tolerated`; plus `reused: true` on a resume replay, `dry_run: true` for skipped shell/python, `cost_source` when not `none` |
| `loop.iteration` | `n`, `max` |
| `each.item` | `index`, `total` |
| `run.paused` | `token`, `step`, `message` |
| `run.decision` | `approved`, `comment`, `by` (`--yes`, `dry-run`, `tty`, or the stored decision's author) |
| `run.finished` | `status`, `reason`, `usage`, `cost_usd`, `outputs`; plus `cost_source` when not `none` |
| `warning` | `message` |

Agent deltas, tool calls, command output and shell stdout/stderr are **not** in `events.jsonl`;
they are per-step `stream.jsonl` records (`{kind, ts, attempt, text, name, call_id, nested, data}`).
`rayspec run --json` prints both streams interleaved on stdout (stream records wrapped as
`{"type": "stream", "step_path", "record"}`); the final summary object is the last stdout line
(see [cli.md](cli.md#rayspec-run)).

### Who ran it

`run.json` carries an `actor`: **who** set the run going. It is resolved once, at the run's first
start, and never rewritten — a resume by somebody else leaves it naming whoever launched it.

| field | what it is |
|---|---|
| `id` | the identity itself |
| `source` | where it came from: `env` (`RAYSPEC_ACTOR`), `git` (the workdir's `git config user.email`), `os` (the operating-system user), `unknown` |
| `ci` | the CI system detected from the environment (`github-actions`, `gitlab-ci`, `buildkite`, `circleci`, `azure-pipelines`, `jenkins`, `teamcity`, or the generic `ci`), else `null` |
| `provider_accounts` | provider id → the account the environment **named** (`ANTHROPIC_ACCOUNT`, `OPENAI_ORG_ID`/`OPENAI_ORGANIZATION`) |

Resolution order is `RAYSPEC_ACTOR` > `git config user.email` in the run's workdir > the OS user;
set `RAYSPEC_ACTOR` in a scheduler or a CI job so an unattended run is not attributed to whatever
service account happens to own the process.

It is an **identity, not a credential and not a permission**. rayspec never reads a token, key or
password to build it — a provider *account* comes only from a variable that names one, never from
the one that carries the secret — and nothing in rayspec grants an authorisation because of it.

Every decision carries its own actor too, next to `by`:

- `by` says which door the decision came through: `cli` (`rayspec approve`/`reject`), `tty` (the
  terminal prompt of the run itself), `--yes`, `dry-run`;
- `actor` says whose hand it was. For a decision recorded by `rayspec approve`/`reject` it is
  whoever ran that command — often not the person who launched the run. For a gate answered in the
  run's own terminal it is the run's actor.

`pause.decision` holds it while the run is paused, and the `run.decision` event in `events.jsonl`
keeps it afterwards (the pause slot is cleared the moment the gate consumes the decision, the event
log is not).

### The local audit log

`RAYSPEC_AUDIT_LOG=1` adds `audit.jsonl` to the run directory: one line per fact, in the order the
store learned it.

```json
{"ts": "2026-08-21T09:14:02+00:00", "kind": "command", "step": "build[1]/implement",
 "detail": "pytest -q", "data": {"attempt": 1}}
```

| `kind` | one row per |
|---|---|
| `run` | the run being created (with the actor), started/resumed, its workspace, a pause, the final status |
| `step` | a step starting, retrying, finishing |
| `command` | a command an agent started |
| `tool` | a tool an agent called (arguments in `data.input`, capped) |
| `file` | a file an agent reported changing |
| `warning` | a warning or error, from the engine or from an agent |
| `approval` | a decision — `data` carries `approved`, `comment`, `by` and `actor` |

`detail` is the one-line summary and is capped; `data` carries the structured extras. Progress
events (loop iterations, `each` items) are deliberately absent: they say how far a run got, not what
it did. The file is written **through the run store**, so the redactor covers it like everything
else under the run directory — over the row's values rather than its serialised text, so a numeric
secret becomes the marker instead of corrupting the JSON.

Two honest limits. The ledger is **append-only in behaviour, not tamper-evident**: rows are only
ever appended, and nothing about the file proves it was not edited afterwards — anybody who can read
a run directory can also write to it. And it is **local**: one file per run, on one machine, for one
user. `rayspec audit <run>` renders the same rows straight from `events.jsonl` and the step streams,
so you get the ledger whether or not the file was enabled.

### Declared artifacts

A step can name the files it promises to write:

```yaml
- id: report
  shell: "mkdir -p build && ./scripts/report.sh > build/report.md"
  artifacts: [build/report.md]
```

- Paths are relative to the step's **working directory** (its `cwd:` for `shell:`/`python:`,
  otherwise the run's workdir). Absolute paths, `~`, `..` and control characters are refused when
  the workflow is loaded, with the file and line of the step. So is `{{ … }}`: an entry is a
  literal file name, **not a template**. When the name varies per `each:` item, keep the file
  name fixed and template the step's `cwd:` instead (that one *is* rendered per item):

  ```yaml
  - id: fan
    each: "['api', 'web']"
    as: name
    steps:
      - id: build
        shell: "mkdir -p out/{{ name }} && ./build.sh > out/{{ name }}/report.md"
        cwd: "out/{{ name }}"
        artifacts: [report.md]
  ```
- They are checked once the step has **succeeded**: a declared file that is missing, is not a
  regular file (a directory, a FIFO, a socket, a device node), or resolves outside the working
  directory (a symlink) **fails the step**, with a reason naming the path. A file outside the
  run's **workspace** is refused too, even when the step's `cwd:` points there: `cwd:` is
  rendered at run time and may name any directory on the machine, and `artifacts:` is a promise
  about the workspace, not a way to copy arbitrary files into the run directory. That is the whole point — a promise that can be broken silently is
  not worth declaring. The step's own output is kept, so you can still read what it printed.
- Every artifact is copied into the run directory (`artifacts/<step path>/<declared path>`,
  `0600`, redacted like every other file the store writes) and recorded on the step as
  `{path, ref, sha256, size}`. The run stays readable after the worktree is gone. Keep them
  small: a build tree belongs in the workspace, not in the run directory.
- Only the **path** is recorded. The content of an artifact is never read into a record, an
  event, a template context or a step output — a downstream step that wants the content reads
  the file.
- A resumed step that is replayed from the cache keeps the artifacts it was recorded with (they
  are not re-checked), and a `--dry-run` checks nothing at all: nothing was really produced.

## Recording a run as a stub script

A finished run dir is a complete, credential-free fixture: `rayspec runs stubs <run> -o
stubs.yaml` turns its `prompt:` step answers, token usage and failures into a
[stub script](providers.md#record--replay), and `rayspec run <wf> --dry-run --stubs stubs.yaml`
(or `--stubs-from <run>`, which skips the file) replays the same run offline at zero cost — the
fastest way to reproduce an engine bug, freeze a regression fixture or hand a colleague a bug
report they can actually run. Loop iterations that answered differently become a `sequence:`
under the body's glob key, parallel `each` items keep their indexed keys, and a run launched
with `secret: true` inputs is refused (exit 2) rather than having its prompts copied into a file.
A run whose workflow changed since it was recorded gets a warning: the recorded keys may no
longer name steps that exist. `--stubs-from` records its donor run in `run.json`
(`stubs_path: "run:<run id>"`), so a replay that pauses keeps replaying the same answers when it
is resumed or approved.

## Durability rules

- `run.json` is written to a temp file, fsynced and renamed: a crash leaves the previous version.
- Every succeeded step writes its output file first, then its record, then `run.json`
  (write-ahead). A record whose output file is missing is not reusable.
- JSONL appends are flushed per line; a torn trailing line after a crash is tolerated by readers.
- Ctrl-C: the first SIGINT/SIGTERM cancels the run through anyio (running steps become
  `interrupted`, the SDK subprocesses are shut down, `run.json` is flushed, exit 130); a second
  SIGINT flushes synchronously and hard-exits.

## Resume

```
rayspec run <workflow> --resume <run-id or prefix> [--force] [--yes] [--no-interactive] …
```

Resume re-executes the workflow **from the top** with a reuse cache:

- a `prompt`/`shell`/`python`/`approve` record is **replayed** (event `step.finished` with
  `reused: true`) iff it is `succeeded` (or `failed` + `tolerated`), its output file exists and the
  step is not `always_run: true`;
- `failed`, `interrupted`, `running`, `paused`, `skipped` records re-run; `attempts` keep counting;
- composites (`loop`, `each`, `include`) always re-run their bodies, which replay naturally
  (`until` is re-evaluated from stored outputs; an `each` item whose `item_sha256` changed is
  re-run with a warning; a different item count yields new paths); `stop:` always re-runs;
- inputs come from `run.json` — `--inputs-file` is refused and `--input` is accepted for
  **secret inputs only**: a `secret: true` value is never persisted (`run.json` holds
  `"<secret>"`), so every secret that was given at launch must be supplied again by each resume
  entry — `rayspec resume|approve|reject` and `run --resume` all take `--input name=value` (a
  non-secret name is the usual `inputs are fixed per run` error, exit 2), else read
  `RAYSPEC_INPUT_<NAME>` from the environment, else exit 2 `missing secret input(s): token — pass
  --input token=… or set RAYSPEC_INPUT_TOKEN` before anything is written. An optional secret that
  was not given at launch is not required; supplied now, it is recorded as `"<secret>"` from then
  on and exported like the others. Leaf fingerprints hash the `<secret>` placeholder, so cached
  shell/python steps are replayed whatever value is supplied now;
- a run launched with `--stubs` recorded the file's absolute path (`stubs_path`): `resume`,
  `approve`, `reject` and `run --resume` load it again (missing/unreadable ⇒ exit 2, hint `pass
  --stubs <path>`); `--stubs PATH` on any of them overrides and becomes the recorded path. A
  `--dry-run` record resumes as a dry run through `rayspec resume|approve|reject` (they have no
  `--dry-run` flag of their own); `run --resume` keeps the flags you give it, so resuming a
  `--dry-run --stubs` record of a workflow with a non-stub agent *without* `--dry-run` is refused
  before anything is written — exit 2 `run <id> was launched with --dry-run --stubs <path>; its
  recorded stubs file requires --dry-run (… would run for real)`, hint `pass --dry-run to resume
  it as a dry run (rayspec resume does so automatically), or switch the agents to provider: stub`;
- the workflow hash must match; otherwise the resume is **refused** (exit 2, `pass --force`)
  unless `--force`, in which case a leaf whose `fingerprint` (rendered prompt/script + agent)
  changed is re-run with a warning and the rest is reused. Every entry point applies this guard
  **first** — `rayspec resume` (with or without `--no-interactive`/`--yes`, TTY or not),
  `approve`, `reject` and `run --resume` give the same answer before reporting a pending gate or
  writing anything (a CI job polling a paused run learns that the workflow drifted instead of
  "still paused");
- leaf fingerprints are compared on **every** resume, not only after a hash mismatch (an
  interrupted `--force` resume has already stamped the new hash; a later plain resume must still
  notice a step whose upstream output changed);
- a run whose `run.json` still says `running` with a live `pid` on this host — or recorded on
  another host (shared `RAYSPEC_HOME`) — is refused (hint: stop it first, or `--force`); a run of
  another workflow is always refused (`rayspec run <other> --resume <id>`);
- the workdir path lock is taken again for the resume (see [isolation.md](isolation.md#locks));
- the workspace is rebuilt from the stored `workspace` block (same worktree path; the run branch
  stays checked out **in that worktree**, never in your clone — see
  [isolation.md](isolation.md#the-branch-lives-in-the-worktree)). If the worktree directory is
  gone, recreate it yourself from the branch (`git worktree add <path> <branch>`) — automatic
  recreation is on the roadmap;
- `resume_count` increments and `run.resumed` is emitted.

After Ctrl-C, partial `stream.jsonl` records and any changes in the worktree are left alone.

## Approval gates

An `approve:` step is a human gate. Flow per gate (`steps/<path>`, attempt `n`):

1. `--yes` or `--dry-run` → approved automatically (`decision.by` = `--yes` / `dry-run`).
2. A stored decision for this exact gate (`pause.decision` with token `<path>#<attempt>`) →
   consumed (this is what `rayspec approve <run> [comment]` / `rayspec reject <run> [reason]`
   write, with `by: cli`, before resuming in-process).
3. Otherwise the run **quiesces**: no new leaf step starts anywhere and the gate waits until every
   running leaf has finished (several ready gates are handled one at a time). Then:
   - stdin is a terminal and neither `--no-interactive` nor `--yes` was given → a panel with the
     message, every `needs` step's status/duration/cost and last 15 output lines, `git status
     --short` / `git diff --stat` of the workdir and the run totals; keys `[a]pprove`, `[r]eject`,
     `[v]iew` (full outputs), `[d]iff` (`git diff`), `[p]ause`, plus an optional comment;
   - otherwise (no TTY, `--no-interactive`, or `[p]ause` / Ctrl-C at the prompt) → the gate is
     recorded `paused` with `pause: {token: "<path>#<n>", step, message}`, `run.paused` is
     emitted, the run status is `paused` and the exit code is **3**.
4. Reject: `on_reject: cancel` (default) → the gate is `rejected`, running siblings are cancelled,
   pending ones skipped (`stopped`), the run ends `cancelled` (exit 4); `continue` → the gate
   succeeds with `approved: false`; `fail` → the gate fails. The approver's comment is the step
   output (`''` when empty).

Continue a paused run with `rayspec resume <run>` (or `rayspec run <wf> --resume <run>`): after
the workflow-hash check (a changed workflow is exit 2 whatever the flags, see above), on a
terminal the gate asks again; with `--yes` it approves; with `--no-interactive` (or no TTY) it
prints the approve/reject hint and exits 3 again. `rayspec approve <run> [comment]` /
`rayspec reject <run> [reason]` decide without a terminal: they record `pause.decision` and resume
in-process (exit code = how the run ends). Whichever way the gate is answered, `run.pause` is
cleared once the decision is recorded — a finished run never reports a pending gate. The summary
printed after a pause (`decide with: rayspec approve <run> [comment] · rayspec reject <run>
[reason] · rayspec resume <run>`) names these commands; a cancelled run (`rayspec cancel`,
`stop:`, reject) is not resumable without `--force`.

## Failures, retries and timeouts

- A leaf attempt that fails with a *transient* error (rate limits, 5xx, transport resets, the
  stub's `transient: true`) is retried per the step's `retry` policy (prompt default: 3 total
  attempts, 3 s doubling); `on_error: all` also retries non-transient failures and timeouts.
  `step.retry` events carry the delay and the error. A step that succeeds after a retry records
  **no** error (`error: null`, console `✓ … succeeded`, `show` prints the output); the failed
  attempts' errors live in the `step.retry` events and the stream.
- `timeout` (or `defaults.timeout`) bounds each attempt of a leaf; shell/python subprocess groups
  are SIGTERMed, then SIGKILLed after 2 s; agent subprocesses/threads are interrupted through the
  SDKs. A timeout is `error.type: timeout`.
- The run status is `failed` when any untolerated step failed, was interrupted or rejected
  (`reason` names the first one) or when a run-level cap tripped (below); `succeeded` only when
  every step succeeded/was tolerated/was skipped and `outputs:` rendered. A failing `outputs:` template turns a successful run into
  `failed` with `reason: outputs: …`.
- **Interrupted attempts** (Ctrl-C, a sibling's `stop:`/pause, the per-attempt `timeout`): the
  attempt records whatever usage the adapter had reported so far — Codex: the last
  `thread/tokenUsage` total of the turn; Claude: the usage of every assistant message the CLI
  had completed (the message in flight is not billed to the record) — and prices it from the
  `pricing:` table when there is an entry; it shows up on the `interrupted`/`failed` record, in
  the `step.finished` event and in the run totals, and it stays counted after a resume (the
  re-run step's `attempts`, `usage` and `cost_usd` continue from the previous record). When the
  adapter had reported nothing yet (interrupted during start-up, or a provider whose SDK
  surfaces no usage before the result) the attempt's usage is **unknown**, not zero: the record
  carries `usage_unknown: true` (also on `step.finished`), the step's usage is a lower bound and
  the `rayspec run` footer prints `tokens: ≥N (usage of 1 step unknown)`. A provider error the
  SDK *raised* (auth failure, CLI not installed, a 429 before any token was billed) is a plain
  failed attempt: it keeps whatever usage the stream reported before the error, else zero — it
  is not marked unknown. A leaf that is interrupted while it still queues for a `max_parallel`
  slot never started its attempt: it is not counted as one and keeps the usage its record
  already carried.
- Token usage and cost are summed over every step attempt; `rayspec run` prints them, the
  `run.finished` event and the `--json` summary object (last stdout line, see [cli.md](cli.md#rayspec-run))
  carry `usage`, `cost_usd` and the run-level `cost_source` (also `run.json`): `provider` — every
  step with tokens reported a provider cost (`$0.12`); `table` — at least one step cost is a
  pricing-table estimate and none is unknown (`~$0.12`); `partial` — at least one step has
  tokens but no cost at all (an unpriced provider without a `pricing:` entry), so the sum is a
  lower bound (`≥$0.12`); `none` — no cost anywhere (only the token count is shown). Per step,
  `cost_source` stays `provider` / `table` / `none`.
- After a worktree run the text summary prints `worktree: <path> (branch <name>, checked out
  there)` and a one-line hint (`cd <path>` · `rayspec worktrees list|clean` · `git worktree remove
  <path>`); the `--json` summary carries the same path/branch under `workspace`.

## Run-level budget (circuit breaker)

`defaults.budget_usd` and `defaults.max_tokens` cap a **whole run** ([schema.md](schema.md#defaults)):

```yaml
defaults:
  budget_usd: 5          # or "$5.00"
  max_tokens: 2M         # or 2000000 / "500k"
```

- After every leaf step finishes, between retry attempts, and again when a leaf takes its
  `max_parallel` slot, the engine sums the tokens
  (input + output) and the cost of every step of this run — provider-reported cost, else the
  pricing-table estimate (`~$`); a step without any known cost counts 0 towards `budget_usd`
  (tokens are always known).
- The first time a cap is exceeded a `warning` event is emitted and the breaker **trips**: no new
  step starts anywhere (pending steps — leaves, composites, gates — are recorded `skipped` with
  `skip_reason: budget_exceeded`; a leaf that was already queued for a `max_parallel` slot is
  re-checked when it gets one and skips too; a loop starts no further iteration; a failed attempt
  is not retried), **running steps finish** (drain, no cancellation), and the run ends `failed` with
  `reason: budget exceeded (cost ~$5.120 > budget_usd $5.000)` / `(tokens 2,104,331 >
  max_tokens 2,000,000)` — exit **1**. Composites whose body hit the cap fail with a `budget` /
  `body` error naming it. As in every drain, `join: always` steps still run (whatever their kind:
  a `join: always` prompt step spends tokens after the trip — that is what the author asked for).
- **Resume**: replayed records count towards the cap again, so resuming with the same cap trips
  immediately — nothing new runs, but every step that finished before is still **replayed**, not
  skipped (a replay is free; the cache is never lost to a trip). Raise the cap in the workflow and
  resume with `--force` (the hash changed; leaf fingerprints do not include `defaults`, so finished
  steps are still reused): `rayspec resume <run> --force`.
- `rayspec plan` prints the caps next to the isolation (`budget_usd $5.00  max_tokens
  2,000,000`; `--json`: `budget_usd`, `max_tokens`).

### Wall-clock cap (`timeout_total`)

`defaults.timeout_total` is the same breaker measured in time instead of money or tokens:

```yaml
defaults:
  timeout_total: 2h        # or "90m" / 5400
```

- The clock starts when the run starts and **keeps counting across resumes**: it is measured
  from `run.json`'s original `started_at`, which a resume entry never rewrites. `2h` therefore
  means two hours of *run*, not two hours per attempt — including the time a run spent waiting
  at an approval gate. Resuming a run that has already used its budget of time starts nothing
  and ends `failed` right away; finished steps are still replayed.
- The cap is checked when a step finishes **and when a step takes its `max_parallel` slot**, so
  it never cancels anything: a step that is running when the clock runs out is allowed to finish,
  and every step that had not started yet is skipped (`skip_reason: budget_exceeded`, the same
  drain as above) — including one that was already queued behind `max_parallel`, which is why a
  fan-out cannot keep launching work for hours after a `2h` cap ran out. It is a *circuit
  breaker*, not a kill switch — use `timeout:` (per attempt, per step) or `stop:` if you need
  one.
- The run ends `failed` with
  `reason: time limit exceeded (elapsed 2h 4m > timeout_total 2h 0m)` — exit **1**. That also
  holds when the drain reaches a `stop:` step: a tripped cap outranks the stop's own status, so
  a `join: always` `stop: {status: succeeded}` cleanup step cannot report a capped run as
  successful (its `outputs:` are not published either). Raise the
  cap and resume with `--force` (the workflow hash changed) to continue where it stopped.

## Security notes

The run store is sensitive data: `run.json` holds the inputs (except `secret: true` inputs,
stored as `"<secret>"`), `steps/<path>/context.json` the full template context every step saw,
`steps/<path>/prompt.txt` the exact prompt every agent step was sent,
`stream.jsonl` the complete agent transcript, `stdout.log`/`stderr.log` whatever a script printed,
and `outputs` are in clear text. So:

- **Permissions.** Everything a run writes under `$RAYSPEC_HOME` is private regardless of the
  umask: the directories created on the way (`$RAYSPEC_HOME`, `projects/<slug>/`, `locks/`,
  `runs/<id>/`, `steps/<path>/`, `artifacts/<step path>/`) are `0700`; `run.json`,
  `events.jsonl`, `output.txt|json`,
  `prompt.txt`, `stream.jsonl`, `steps/<path>/context.json`, `stdout.log`/`stderr.log`, the
  copies of declared `artifacts:` and the workdir lock
  files (`locks/*.lock`) are `0600`. Directories that already exist (a `~/.rayspec` you created
  by hand, an older store) are never re-chmodded — tighten them yourself if you share the
  machine: `chmod -R go-rwx ~/.rayspec`. The one remaining umask-mode writer is
  `worktrees/` (git checkouts and their registry; no run data). All writers share two helpers,
  `rayspec.store.file.secure_mkdir` / `open_private` (the latter refuses a symlink at the path).
- **Clear-text inputs and outputs.** Declare credentials as `secret: true` inputs
  ([schema.md](schema.md#secret-inputs)): the value never enters `run.json`, `context.json`,
  events, prompts or outputs and reaches `shell:`/`python:` steps as `RAYSPEC_INPUT_<NAME>` only
  (no template may name it elsewhere — a load-time error); or hand them to steps through the
  environment (`~/.rayspec/.env`, your shell). Ordinary inputs and all outputs are persisted (in
  `run.json`, `events.jsonl` `run.finished`, every later step's `context.json`, and on the console
  / in `show`) — and so is whatever a script *prints*, secret or not. Agent steps cannot receive
  secret inputs in v1.
- **Project `.rayspec/.env` trust.** A checkout's `.rayspec/.env` is controlled by whoever pushed
  the repository and can redirect credentials (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `*_PROXY`)
  or reconfigure git (`GIT_CONFIG_*`, `GIT_SSH_COMMAND`). Inspection commands (`doctor`,
  `validate`, `plan`, `workflows`, `agents`, `runs`, `costs`, `show`, `logs`, …) never load it; only
  `run`, `resume`, `approve` and `reject` apply it — into the process environment that reaches
  step, provider and git subprocesses — and they say so on stderr (`env: loaded N variables from
  .rayspec/.env (project)`). `rayspec doctor` lists the file (`project .env` row) so you can read
  it before the first run. `~/.rayspec/.env` (yours) is loaded by every project command.
- **`cancel` pid check.** A `running` record keeps the engine's pid; after a crash or reboot the
  pid may belong to an unrelated process. `rayspec cancel` signals only a process whose start
  time equals the recorded `pid_started_at` (exact; older records without the field skip this)
  *and* whose command line contains a rayspec execution command (`rayspec run|resume|approve|
  reject`) and names this run (id or workflow, as whole words) — anything else is refused with
  exit 2; `rayspec cancel <run> --mark` finalizes such a stale record as cancelled without
  signalling.
- **Untrusted text on the terminal.** Agent output, step output and input values are printed by
  the console, `show`, `logs` and the approval panel with control characters and terminal escape
  sequences removed (no title changes, screen clears, colour spoofing or hyperlinks from a
  model's answer; Rich markup in them is shown literally). `rayspec logs --raw` is the unescaped
  escape hatch for debugging.

## Roadmap

Automatic worktree recreation on resume (today: `git worktree add <path> <branch>` by hand when
the directory is gone).
