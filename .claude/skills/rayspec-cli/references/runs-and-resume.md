<!-- Generated from docs/runs-and-resume.md by scripts/gen_skill.py — do not edit here. -->
<!-- Canonical source: https://github.com/rayspec-labs/rayspec-py/blob/main/docs/runs-and-resume.md -->
<!-- Sibling references in this directory: cli.md · providers.md · testing.md · policy.md · runs-and-resume.md · isolation.md · ci.md -->

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
  cancel.json                   present only while a `rayspec cancel` is in flight (see "Cancelling a run")
  events.jsonl                  lifecycle events, one JSON object per line (capped at 16 MiB, see below)
  audit.jsonl                   the local ledger, only with RAYSPEC_AUDIT_LOG=1 (see below)
  detach-launch.log             `rayspec run --detach` only: the background child's stdout/stderr at boot
  detach-handshake.json         `rayspec run --detach` only, transient: the child's proof-of-life to the launcher
  steps/<path>/                 one directory per executed step (build[2]/implement → steps/build[2]/implement/)
    output.txt | output.json    the step's output (written BEFORE the record that points at it)
    prompt.txt                  prompt steps: the rendered prompt: body, written BEFORE the provider call (StepRecord.prompt_ref; `rayspec explain --full`; a write that fails is a warning, never a failed step)
    stream.jsonl                agent events / shell stdout+stderr lines, with the attempt number (capped at 16 MiB)
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
  "fail_fast": false,                     // --fail-fast as given at launch; restored by every resume entry (a resume may tighten it, never loosen it)
  "status": "succeeded", "reason": null,
  "created_at": "…", "started_at": "…", "ended_at": "…",
  "actor": {"id": "me", "source": "os", "ci": null,                // who launched the run: an exported
             "provider_accounts": {}, "declared_id": null},        // RAYSPEC_ACTOR, else the OS user. A
                                                                   // RAYSPEC_ACTOR from a .env is refused
                                                                   // and kept as declared_id (see below)
  "resume_count": 0, "pid": null, "pid_started_at": null, "host": "<host>",   // pid_started_at: start time of the
  "heartbeat_at": "2026-08-20T12:59:03Z",  // pid's process (`ps -o lstart=`, for cancel); last proof the run's
  "dry_run": false,                       // process was alive (see "Is this run still alive?"); dry_run: a --dry-run rehearsal (stub providers)
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

`heartbeat_at` is the last time the run's process proved it was still alive — moved by a periodic
timer for the life of the run (every 5–60s, derived from `defaults.timeout`: `min(60s, max(5s,
timeout / 10))`, 60s when the workflow states none) and, on top of that, right before and right
after every provider call, so a single long-running step still moves it between its own
`step.started`/`step.finished`. `null` in records written before the field existed and in a record
whose engine has not ticked yet; see "Is this run still alive?" below for what reads it.

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
| `run.paused` | `token`, `step`, `message`, `reason` (`approval` · `budget` · `failures`) |
| `run.decision` | `approved`, `comment`, `by` (`--yes`, `dry-run`, `tty`, or the stored decision's author) |
| `run.finished` | `status`, `reason`, `usage`, `cost_usd`, `outputs`; plus `cost_source` when not `none` |
| `warning` | `message` |

Agent deltas, tool calls, command output and shell stdout/stderr are **not** in `events.jsonl`;
they are per-step `stream.jsonl` records (`{kind, ts, attempt, text, name, call_id, nested, data}`).
`rayspec run --json` prints both streams interleaved on stdout (stream records wrapped as
`{"type": "stream", "step_path", "record"}`); the final summary object is the last stdout line
(see [cli.md](cli.md#rayspec-run)).

`events.jsonl` and each step's `stream.jsonl` are capped at 16 MiB so a long-running or
unattended (`--detach`ed) run nobody watches cannot fill the disk: once a file grows past three
times the cap, it is truncated from the **middle**, keeping the first third and the most recent
two thirds — never tearing a JSON line — so a run's start and its latest activity both survive
and only the noisy middle is dropped. This only ever affects a file that would otherwise grow into
the tens of megabytes; a normal run never notices it.

### Who ran it

`run.json` carries an `actor`: **who** set the run going. It is resolved once, at the run's first
start, and never rewritten — a resume by somebody else leaves it naming whoever launched it.

| field | what it is |
|---|---|
| `id` | the identity itself |
| `source` | where it came from: `env` (`RAYSPEC_ACTOR`), `os` (the operating-system user — see the row below for what that means exactly), `unknown` when nothing answered |
| `ci` | the CI system detected from the environment (`github-actions`, `gitlab-ci`, `buildkite`, `circleci`, `azure-pipelines`, `jenkins`, `teamcity`, or the generic `ci`), else `null` |
| `provider_accounts` | provider id → the account the environment **named** (`ANTHROPIC_ACCOUNT`, `OPENAI_ORG_ID`/`OPENAI_ORGANIZATION`) |
| `declared_id` | a `RAYSPEC_ACTOR` a `.env` file asked for and did **not** get, kept as a claim; `null` normally |

Resolution order is `RAYSPEC_ACTOR` > the OS user. Set `RAYSPEC_ACTOR` in the shell you launch a
run from, or in a scheduler or CI job, whenever you want the ledger to name a person or a bot
rather than the account that happens to own the process — an email address, a team name, anything
that identifies the hand.

#### Which sources are allowed, and why

**An identity is only evidence if the audited code could not have chosen it.** That is one rule
about the *source* of a value, not a list of attacks, and it is worth stating as a table because
the answer is not obvious for any of these:

| source | trusted for an identity? | why |
|---|---|---|
| the process environment **as the operator exported it** | yes | it lives in the process that launches the run or answers the gate; a step is a child of that process and cannot write it |
| the operating-system user | yes | the account database (POSIX `pwd`) for this process's uid — a fact about the process that no environment can influence |
| `$USER` / `$LOGNAME` / `$LNAME` / `$USERNAME`, **as the operator exported them** | yes, but only as a fallback | asked when the account database has no entry for the uid (a container started with `--user 1000:1000`), and read through the same subtraction as everything else — never with `getpass.getuser`, which reads them straight out of the live environment and ahead of the account database |
| a `RAYSPEC_ACTOR` rayspec loaded from `$RAYSPEC_HOME/.env` | **no** | `$RAYSPEC_HOME` is exported into every step: `printf 'RAYSPEC_ACTOR=…' > "$RAYSPEC_HOME/.env"` is one line, and the file is applied by *every* later command |
| a `RAYSPEC_ACTOR` rayspec loaded from `<project>/.rayspec/.env` | **no** | it is a file in the tree the run works in, applied by `run`/`resume`/`approve`/`reject` |
| `git config user.email`, any scope | **no** | a run's steps execute as you, with your `$HOME` and inside the repository the worktree came from, so `git config [--global] user.email …` is one command in one step |
| a `RAYSPEC_ACTOR` your **shell startup file** exports | yes — and this is the one gap, see [What this does not give you](#what-this-does-not-give-you) | by the time rayspec sees it, it is in the environment the operator handed over, and nothing can tell it from one the operator typed |

A run's `shell:` steps and its agents execute as you (`workspace-write` is a permission mode of
the agent, not an operating-system sandbox). So every "no" above is one line in one step away
from choosing the name stamped on your next `rayspec approve` — and it would be rendered as a
machine-derived identity, which reads as *more* trustworthy than a self-declared one, not less.

**How the rule is enforced**, because this is the part that has to keep holding: `.env` files
are copied into the process environment by exactly one function, and that function reports every
variable it applied, and from which file. Identity resolution then subtracts that set from the
environment before it looks at anything. So the guarantee is not "these two filenames are
special" — it is "rayspec never identifies you from a value rayspec itself put there", which
holds for a `.env` location added later, too. Every field of `actor` is resolved that way, not
only `id`: a planted `GITHUB_ACTIONS` would otherwise make a laptop run read as CI, and a planted
`ANTHROPIC_ACCOUNT` would put somebody else's team in the record.

A refused `RAYSPEC_ACTOR` is not swallowed. It is recorded as `declared_id` — a claim, next to
the identity that was actually used — and every command that loads the file says so on stderr:

```console
$ rayspec approve 20260821-2228-5xwm "LGTM"
warning: RAYSPEC_ACTOR in /home/you/.rayspec/.env is not used as an identity — a workflow step
can write that file. Export RAYSPEC_ACTOR in the shell that runs rayspec instead.
```

`rayspec audit` then shows both: `approved by you (cli) — a .env declared
'security-team@corp.invalid', which is not an identity`.

If you want a per-project identity, set `RAYSPEC_ACTOR` in your shell, your scheduler or your CI
job — that is a decision of yours, taken outside the run, which is exactly what makes it worth
recording. Putting it in a `.rayspec/.env` that is committed to the repository does not work, and
that is deliberate: the file is chosen by whoever pushed the checkout.

`.env` files keep working for everything else. They exist so a project can supply configuration
and they still do — `ANTHROPIC_BASE_URL`, an API key, whatever a step needs. The narrowing is to
*identity only*; nothing else about `.env` changed.

#### What this does not give you

Worth saying plainly, because a guarantee that is described wider than it is built is worse than
none:

- **It is not tamper-evidence.** The run directory is yours, so anything that can run as you can
  edit `run.json` after the fact. The rule above is about the moment a value is *recorded*: at
  that moment it came from the operator, not from the run. `rayspec audit` re-resolves the
  decision row from the record's actor, but its header line is read straight out of `run.json`.
- **It is not authentication.** Nobody proved they are `alice@example.com`; the operator's shell
  said so. An identity here names a hand for a log — it never grants a permission.
- **A `.env` a run wrote still supplies configuration.** `$RAYSPEC_HOME/.env` in particular is
  applied by every rayspec command, so a step that writes it changes the environment of your
  later commands (a proxy URL, say). That is the same trust boundary the file always had — it is
  a file on your machine — but the identity rule does not fix it, and this build does not either.
  Keep an eye on it the way you would on `~/.bashrc`; `rayspec doctor` lists both files.
- **The defence is per process, and a shell startup file is outside it.** A step runs as you, so
  it can append `export RAYSPEC_ACTOR=someone-else` to any file your shell reads at startup.
  Nothing happens to the run that did it, or to any process already running: rayspec never reads
  those files, and a variable that is not in a process cannot be read out of it. But your **next
  shell** exports it, and from that moment the value really is in the environment you handed over
  — indistinguishable, by construction, from you having typed it, and it will be recorded as
  `source: env` on everything you run from that shell.

  How much later that is depends on which file, and the reach is wider than the next window you
  open. `~/.zshenv` is read by zsh on **every** invocation, so a `zsh -c` inside a script or a cron
  job exports it too, with no terminal involved anywhere. `~/.zprofile` and `~/.bash_profile` are
  read by a login shell, which on macOS is what a new terminal window starts.

  This is not a hole the identity rule can close, and pretending otherwise would be the third
  guarantee this area described wider than it was built. Close it where it actually lives: run
  workflows in a container or a sandbox that has no write access to your dotfiles, or review those
  files the way you would review anything else that decides what your shell does. A spot check
  after an unfamiliar workflow:

  ```bash
  grep -r RAYSPEC_ACTOR ~/.zshenv ~/.zprofile ~/.zshrc ~/.zlogin \
                        ~/.bash_profile ~/.bashrc ~/.profile \
                        ~/.config/fish/config.fish ~/.config/fish/conf.d 2>/dev/null
  ```

  A spot check is all that is: a startup file can source another file, your shell may be neither
  zsh nor bash, and a login shell, an interactive shell and `sh -c` each read a different set. A
  list is an enumeration where the honest answer is that you cannot enumerate this. `rayspec audit
  <run> --commands` shows what the run actually executed, which is the check that does not depend
  on having guessed the list right.

It is an **identity, not a credential and not a permission**. rayspec never reads a token, key or
password to build it — a provider *account* comes only from a variable that names one, never from
the one that carries the secret — and nothing in rayspec grants an authorisation because of it.

Every decision carries its own actor too, next to `by`:

- `by` says which door the decision came through: `cli` (`rayspec approve`/`reject`), `tty` (the
  terminal prompt of the run itself), `--yes`, `dry-run`;
- `actor` says whose hand it was. It is resolved **in the process that answers the gate, at the
  moment it answers** — never copied from `run.json`. So it is whoever ran `rayspec approve`, or
  whoever answered the terminal prompt, or whoever ran the `rayspec resume --yes` that waived it.

That last one is why `actor` is not simply `run.actor`. The run's actor is resolved once, at the
run's first start, and a resume never rewrites it — so a gate answered two days later by a second
operator would otherwise be recorded as a decision by the person who launched the run.

`pause.decision` holds the decision while the run is paused, and the `run.decision` event in
`events.jsonl` keeps it afterwards (the pause slot is cleared the moment the gate consumes the
decision, the event log is not). The `actor` inside `pause.decision` is **not** what the gate
stamps on the decision: the run directory is yours, so any step running as you can write a
decision into a paused run's `run.json` and put any name on it. A stored decision supplies the
*answer*; the identity is asked for again in the process that consumes it. In the ordinary case
those are the same process (`rayspec approve` records the decision and resumes in-process) and the
two agree. When they do not, the ledger says so:

```console
warning: the decision found in the record for ok names 'ceo@corp.invalid'; a decision read back
from run.json is an answer, not evidence of whose hand it was — anything running as you can write
that file — so this gate is recorded as decided by 'you'
```

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
| `step` | a step starting, retrying, finishing (its kind, not its body — the rendered body is not stored with the run; `rayspec explain` re-renders it) |
| `command` | a command an agent ran — a `command_start` record, or a tool call carrying a command line (`data.tool` names the tool: the Claude adapter reports a shell command as a `Bash` tool call) |
| `tool` | a tool an agent called that ran nothing itself — an edit, a read, a search (arguments in `data.input`, capped) |
| `file` | a file an agent reported changing |
| `warning` | a warning or error, from the engine or from an agent |
| `approval` | a decision — `detail` is `approved by alice@example.com (cli)`, and `data` carries `approved`, `comment`, `by` and `actor` |

`detail` is the one-line summary and is capped; `data` carries the structured extras. Progress
events (loop iterations, `each` items) are deliberately absent: they say how far a run got, not what
it did. The file is written **through the run store**, so the redactor covers it like everything
else under the run directory — over the row's values rather than its serialised text, so a numeric
secret becomes the marker instead of corrupting the JSON.

The ledger can never cost a run anything: `run.json`, `events.jsonl` and each step's
`stream.jsonl` are written first, and a ledger write that fails (a full disk, a read-only mount)
is a warning in the log and nothing more.

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
  name fixed and template the step's `cwd:` instead (that one *is* rendered per item). A step's
  `cwd:` is resolved **before** its body runs, so the body cannot be what creates it — an earlier
  step is:

  ```yaml
  - id: fan
    each: "['api', 'web']"
    as: name
    steps:
      - id: prepare
        shell: "mkdir -p out/{{ name }}"
      - id: build
        needs: [prepare]
        cwd: "out/{{ name }}"                     # rendered per item: this is where the name varies
        shell: 'echo "built {{ name }}" > report.md'
        artifacts: [report.md]                    # a fixed name, relative to the cwd above
  ```

  The two files land in the run directory as `artifacts/fan[0]/build/report.md` and
  `artifacts/fan[1]/build/report.md`: the *step path* keeps the items apart, which is why the
  declared name does not have to.
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
  step is not `always_run: true`. A replay executes nothing — no shell body, no provider call —
  and [`rayspec audit`](cli.md#rayspec-audit) says so on the row (`succeeded (reused from the
  previous attempt — not re-executed)`), so the ledger of a resumed run never shows one step's
  work twice. `rayspec audit --commands` leaves the replay row out altogether, and keeps the row
  of the attempt that really ran the step — that view answers per row, so a step skipped in one
  attempt and executed in the next shows only the execution, and a step an attempt was killed
  mid-execution shows that attempt's `started` row and nothing a later attempt decided;
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
- a run launched with `--fail-fast` recorded that (`fail_fast`), and every entry restores it, so
  the second half of a run uses the failure policy the first half did. `rayspec resume
  --fail-fast` turns it on for a run launched without it (and is recorded in turn); the flag may
  only ever *tighten* — omitting it never turns a recorded one off, and `approve`/`reject` need
  no flag of their own. A run paused at an approval gate records the tightening even though that
  resume stops at the gate (exit 3), so `--fail-fast` first and `approve`/`reject` second is a
  usable order; `rayspec show` reports what was recorded. The workflow's own
  `defaults.on_step_failure` is in the file both halves read and needs no such treatment;
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

## Detached runs

```
rayspec run <workflow> --detach [...the usual flags]
```

`--detach` forks the run into the background and hands control back almost immediately: rayspec
rebuilds the child's command line from the options you passed (minus `--detach`, plus `--quiet
--no-interactive`), pre-creates the run directory, and launches a `setsid` child with stdin
closed and stdout/stderr redirected into `<run dir>/detach-launch.log`. The child hands back a
small handshake file just before it takes a host slot, and the launcher waits for that — with no
fixed deadline while the child is alive, so a `--wait-slot` queue or a slow `--repo` clone does
not make it give up — then prints the bare run id (or a `--json` object) and exits 0. A child
that dies before it starts is exit 2, with the tail of `detach-launch.log` — its own error — as
the hint. **The exit code is the launch succeeding, not the workflow's eventual outcome** — a
workflow that goes on to fail still reports `0` here, because at that point it has barely started;
check on it the same way you would any other run:

```
rayspec run build --detach                    # prints just the run id, e.g. 20260827-101500-h2nx
rayspec logs 20260827-101500-h2nx --follow     # watch it
rayspec runs                                    # or list it among everything else
rayspec cancel 20260827-101500-h2nx             # or stop it — see below
```

Detached is POSIX only (it needs `setsid`); on a platform without one this is a usage error before
anything runs. If the workdir is already known without preparing a workspace (`--no-worktree`, or
`defaults.isolation: none`), the launcher checks the workdir path lock itself before backgrounding
anything, so a conflict is reported synchronously (exit 2, naming the holder run) rather than
discovered later in a log file nobody is watching; a worktree's own lock is taken by the detached
child, exactly as a foreground run would.

The detached process is otherwise an entirely ordinary run — it goes through the same code path a
foreground `rayspec run` does, with one consequence worth knowing: it has no controlling terminal,
so an `approve:` gate it reaches cannot prompt anybody and **pauses** (exit 3 in the detached
process) exactly as it would for `--no-interactive` or any other non-TTY invocation. Nothing
detach-specific decides that — it falls out of the same "can this stdin answer a question?" check
every run makes. Plan for it: pre-authorise the gates you expect (`--yes`, `--approve-class`, a
[policy](policy.md#approval-classes)) or be ready to `rayspec approve`/`rayspec resume` the run
once it gets there.

## Cancelling a run

```
rayspec cancel <run-id or prefix> [--yes] [--now] [--mark] [--force]
```

Cancelling a **live** run is cooperative by default — rayspec does not signal the process.
`rayspec cancel` writes a small marker, `cancel.json` (`{reason, actor, requested_at}`), next to
`run.json` in the run directory (after confirming, unless `--yes`/`--json`); the run's own process
notices it at its next step boundary — the same points that check the run-level budget cap, never
in the middle of an attempt — lets any step already running finish, still runs `join: always`
cleanup steps, and then finalizes itself `cancelled` (exit 4). The flag is cleared when the run
finalizes or is resumed, so a cancelled run can be resumed later without the stale flag cancelling
it again. Nothing outside the run's own process ever touches its steps or its workdir, so there is
no window in which a signal can land mid-write. This needs no terminal on the run's side and works
identically for a foreground run or one started with `--detach` — `rayspec logs <id> --follow`
shows it happening either way.

A run deep inside one long provider call keeps running until that call returns; the cooperative
flag does not preempt it. When you need it to stop *now* — a wedged step, a single long sleep —
`rayspec cancel <id> --now` sends the run's process group a `SIGINT` (the same escape hatch a
foreground Ctrl-C gives you), which unwinds it through anyio at once. `--now` still verifies the
recorded pid really is this run's rayspec process first, so it never signals a pid a crash left
behind that something else has since reused.

Before writing the flag, `rayspec cancel` verifies the recorded `pid` really is *this run's*
rayspec process — the same check as before (its `pid_started_at`, then its command line — see
[Security notes](#security-notes)) — so a pid reused by an unrelated process after a crash is
never flagged. `--mark` finalizes a stale `running`/`paused` record as cancelled without writing
any flag or touching any process, for exactly that situation. A **paused** run, or a `running`
record whose process is confirmed gone, is simply marked cancelled outright (there is no process
left to cooperate with).

### Is this run still alive?

`rayspec runs`, `rayspec show`, `rayspec logs --follow` and `rayspec cancel` all correct a stored
`running` record before they act on it: if the recorded `pid` is no longer alive on this host, or
it is alive but `heartbeat_at` (see `run.json` above) has not moved in over three heartbeat
intervals, the record is rewritten to `interrupted` right there — not just displayed that way.
Before `heartbeat_at` existed, only a dead pid told a genuinely running process apart from a
`run.json` a crash left behind; a pid that got reused by something else on a busy machine could
leave a run reading `running` forever. A run recorded as running on **another** host (a shared
`RAYSPEC_HOME`) is left alone either way — its process cannot be checked from here, which is also
why `rayspec cancel` on it needs `--force`.

## Approval gates

An `approve:` step is a human gate. Flow per gate (`steps/<path>`, attempt `n`):

1. A stored decision for this exact gate (`pause.decision` with token `<path>#<attempt>`) →
   consumed (this is what `rayspec approve <run> [comment]` / `rayspec reject <run> [reason]`
   write, with `by: cli`, before resuming in-process).
2. An **automatic approval** this gate's [approval class](#approval-classes) permits → approved
   without asking anybody, with the path that did it recorded as `decision.by`. First match
   wins: `--yes` → `--dry-run` (`by: dry-run`) → `--approve-class <this gate's class>` →
   the gate's own `auto_if:` expression evaluating to `true` (`by: auto_if`).
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

### Approval classes

A gate can name a **class**; what that class permits is decided outside the workflow:

```yaml
- id: publish
  needs: [build]
  approve:
    message: "publish {{ inputs.version }} to the registry?"
    class: release
```

The split is the point. The workflow decides *that* a gate exists; the operator decides *how
strictly* it is held. A workflow can name a class but cannot define one, so it can never loosen
a rule an operator set — which is what makes it safe to leave a workflow running on a schedule
that is also allowed to publish a release.

The classes are defined in the `approvals:` block of a [`policy.yaml`](policy.md#approval-classes)
— the same three layers, discovered the same way, combined most-restrictive-wins:

```yaml
# .rayspec/policy.yaml
approvals:
  classes:
    release:
      allow_yes: false
```

Every command that can reach a gate reads them: `rayspec run`, `rayspec resume`, `rayspec
approve` / `rayspec reject`, `rayspec test`, and `rayspec plan` / `plan --risk` before anything
runs. A class **no** layer in force defines keeps the permissive default, and every one of those
commands except `rayspec test` says so rather than letting the name pass for a lock
(`steps.publish.approve.class: 'release' is not defined by the operator policy, so the gate is
not held`, or `…but no operator policy is in force…` when there is no file at all); `plan --risk`
reports such a gate as `unheld-class`. `rayspec test` enforces the rules and stays silent about
an unknown class: the harness has no warning surface, and a passing case discards its run
directory, so there is nowhere to put the notice. Validate the spelling with `rayspec plan
--risk` rather than reading a green suite as proof the class is held.

| Rule | What it forbids | What still works |
|---|---|---|
| *(none — the default)* | nothing | everything |
| `allow_yes: false` | **every** automatic approval: `--yes`, `--dry-run`, `--approve-class`, `auto_if`, and any combination of them | a human answering this one gate: the terminal prompt, or `rayspec approve <run>` / `rayspec reject <run>` |
| `require_tty: true` | the above, **and** a decision recorded out of band by `rayspec approve`/`rayspec reject` (it can be scripted), **and** a replacement prompt configured through `extensions.approval`, **and** asking at all from a process with no terminal | the built-in terminal prompt of the process running the workflow — reach it with `rayspec resume <run>` from a terminal |

Where the rules are checked is what makes them rules: in the executor that decides a gate, not
where a flag is parsed. So when a class is in force, no combination of `--yes`, `--dry-run`,
`--approve-class` and `auto_if` approves a gate it holds — nor does an environment variable or a
configuration key, because none of them is consulted there. `rayspec test` takes the same rules
(see below). What you get instead of an approval is a warning naming the class and the rule, and
a gate that goes on to ask a human.

The limit of the mechanism is the **name**. A class the rules in force do not define keeps the
permissive default: a workflow can no more invent a restriction than lift one, so a name that
does not match on both sides — a typo, or an edit to the workflow — leaves the gate open. It does
not leave it open quietly: `rayspec plan` before a run, `plan --risk` in review and the gate
itself each report that the class is not held.

`require_tty` cannot tell a person from a terminal. It refuses a decision recorded out of band,
refuses a prompt that is not the built-in one, and refuses to ask at all unless the process
really is attached to a terminal — checked when the gate is asked rather than taken from a flag.
But a pty wrapper (`script`, `expect`, `unbuffer`) looks exactly like a human to it. The rule
buys you "not from a pipe in a scheduled job", not "a person was there".

**Rejecting is never constrained by a class.** Refusing to approve is the fail-closed direction,
and a gate nobody can reject is a gate nobody can get out of.

`--approve-class <name>` pre-approves gates of one class for one invocation — `rayspec run
release_check --approve-class chore` answers the tidy-up gates and still stops at the release
one. It is repeatable, and it pre-approves nothing at all for a class whose rules say
`allow_yes: false`. A name no gate in the workflow uses simply pre-approves nothing: the run
pauses exactly as it would have.

`rayspec test` is governed by the same rules. A case is a dry run and a dry run approves gates,
but a class held shut is not waived by the mode a gate is reached in — and with `--exec-shell` a
gated `git push` really runs. A case that reaches such a gate pauses and fails, naming the gate,
instead of publishing.

### Approving by condition (`auto_if`)

`auto_if:` approves a gate without asking when its expression is true:

```yaml
- id: gate
  needs: [tests]
  approve:
    message: "tests passed — merge?"
    class: merge
    auto_if: steps.tests.output.failures == 0
```

It is an expression field like `when:` — a bare Jinja expression (no `{{ }}`), evaluated against
the same context, checked at load time, and never allowed to name a `secret: true` input. It
must evaluate to exactly `true` or `false`; anything else **fails** the gate rather than opening
it.

Precedence, pinned by tests:

- `auto_if` only ever *adds* an automatic approval. It is not a veto: an `auto_if` that is false
  does not stop `--yes` or `--approve-class` from approving the gate.
- `auto_if` can never *escalate* one. Under a class that may not be approved automatically the
  expression is not even evaluated, so no expression — however it is written, and whatever it
  would evaluate to — can approve a gate the class holds shut.

Continue a paused run with `rayspec resume <run>` (or `rayspec run <wf> --resume <run>`): after
the workflow-hash check (a changed workflow is exit 2 whatever the flags, see above), on a
terminal the gate asks again; with `--yes` it approves; with `--no-interactive` (or no TTY) it
prints the approve/reject hint and exits 3 again. `rayspec approve <run> [comment]` /
`rayspec reject <run> [reason]` decide without a terminal: they record `pause.decision` and resume
in-process (exit code = how the run ends). Whichever way the gate is answered, `run.pause` is
cleared once the decision is recorded — a finished run never reports a pending gate. The summary
printed after a pause (`decide with: rayspec approve <run> [comment] · rayspec reject <run>
[reason] · rayspec resume <run>`) names these commands — except at a gate whose class requires a
terminal, where it names only `rayspec resume <run>`, because the other two would be refused. A
cancelled run (`rayspec cancel`, `stop:`, reject) is not resumable without `--force`.

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

`defaults.budget_usd` and `defaults.max_tokens` cap a **whole run** ([schema.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/schema.md#defaults)):

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
- The three caps are **one breaker** with one skip reason, so the reason text is what says which
  of them fired. It names **every** cap that is over, in a fixed order — `budget_usd` and
  `max_tokens` first (one sentence: they are one budget), `timeout_total` after them — and the
  warning names exactly the knobs you would have to raise. `rayspec explain <run> <step>` repeats
  the cap next to the step's `skip_reason: budget_exceeded`, which on its own would point at
  money for a run that ran out of time.
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

## Operational limits (policy, not workflow)

The caps above belong to the *workflow author*: "this task is not worth more than five dollars".
Exceeding one is a defect, so the run fails. The limits in this section belong to the *operator*
of the machine — "not more than twenty dollars a day without me looking" — and reaching one is
not a defect. It is the moment the machine was supposed to stop and ask. So these **pause** the
run (exit **3**) instead of failing it.

All of it is local: one user, one machine. The state is a JSON file under `$RAYSPEC_HOME` next to
that project's runs; there is no server, no shared ledger and nothing to roll up across projects
or people. The file holds dates, run ids, counts and dollar amounts — never an input, never an
environment variable, never a prompt.

These are keys of the same `policy.yaml` [the policy page](policy.md#the-keys) documents — read
through the same loader, layered by the same most-restrictive-wins rules, and counted as controls
by the same trigger. There is one document, not two.

### Spending envelopes and the failure breaker

```yaml
# policy file
budget:
  per_run: 2.00
  per_day: 20.00
  per_month: 200.00
max_consecutive_failures: 3
```

- Spend is committed to `<store root>/limits/spend.json` as the run goes, under an exclusive
  `flock` that covers the whole read-modify-write — two runs finishing in the same instant both
  land. A run commits its *absolute* total, never a delta, so a resume can never double-count.
- Each commit is accounted to the day and month it is **made** in: a run that crosses midnight,
  or one resumed on Thursday, pays for what it spends then into that day. Otherwise every other
  run started that day would get headroom nobody granted.
- When a ceiling is reached the run drains exactly like a capped run does — nothing new starts,
  running steps finish — and then **pauses**: `run.json` gains
  `pause: {reason: "budget", step: "<where it stopped>", message: "spending envelope reached
  (today $20.4 > policy budget.per_day $20.0)"}` and the process exits **3**. The message is
  refreshed from the run's final totals, so it names what was actually spent.
- A `join: always` step still **runs** while the run drains — that is what the join is for — but
  it may not **spend**: once a ceiling is reached no further agent turn is opened, whichever
  join a step declares. A cleanup shell step finishes; a cleanup prompt step is recorded
  `skipped`. `defaults.budget_usd` is the author's own cap and their `join: always` steps are
  exempt from it; `policy.budget` is the operator's cap *over* the author, and a ceiling a
  workflow can opt out of in four characters of YAML is not a ceiling.
- `max_consecutive_failures` is the same instrument counting failed runs instead of dollars. It
  is checked before the first step, so a workflow that has been failing all night stops calling
  the provider. A successful run resets the counter. Its pause carries
  `pause.reason: "failures"` — a different control from `budget`, and a different decision.
- **Continuing**: `rayspec resume <run>` re-evaluates the ceiling — raise it (or wait for the
  next day) and the run picks up where it stopped. `rayspec approve <run> "checked it"` says
  "run it anyway" and **waives the control that stopped this run — only that one**: approving a
  spend does not clear the failure streak, and closing the breaker does not waive a spend. A run
  approved past the breaker that then reaches `policy.budget` pauses again on the money, and the
  console says which control the approval covered. `rayspec reject <run>` changes nothing, so
  the run pauses again on the same ceiling.
- A waived run is still **counted**: "this run may cost more" is not "this run costs nothing",
  and what it spends is in the day and month totals the next run is measured against.
- **The prices the ceiling is measured in are part of the ceiling.** For a provider that reports
  no cost of its own (Codex), the `pricing:` table in `config.yaml` *is* the figure a ceiling is
  compared against — so a `pricing:` section too malformed to parse used to void the envelope
  silently. It no longer can: the section is dropped, and `run`, `resume`, `approve` and `reject`
  all print it beside the policy warnings before the first step
  (`pricing.<model>: … — the pricing table is not applied, so a step whose provider reports no
  cost of its own counts as $0, including against a policy budget: ceiling`). The run still
  starts — an unreadable table is not a reason to refuse one — but nobody is left believing a
  ceiling is in force when it is measuring nothing. This is the same rule as a ceiling that
  cannot be parsed: dropped and named, never dropped in silence.
- `resume`, `approve` and `reject` are subject to all of it, exactly like `run`: they start the
  same agents, so they take the same host run slot and are measured against the same envelope.
- A `--dry-run` spends nothing and is never counted. A `--stubs` run of a workflow whose agents
  are `provider: stub` is a real run as far as the ledger is concerned: if you have configured
  `pricing:` for the stub's model, that price is what gets committed. Leave the stub's models
  out of `pricing:` if you would rather it counted as nothing.
- The ledger is replaced whole on every write and never left half-written. Reading it never
  fails a command: a document that cannot be parsed at all, one that is empty (a zero-byte file
  is a truncation — rayspec never writes one) and one stamped with a format version newer than
  this rayspec understands are each replaced with a fresh document, and a single total that is
  not a number is dropped while everything readable is kept. The repaired document is written
  back on the next commit, so one bad byte cannot break every later `run`, `resume` and
  `approve`. Every one of those is reported as a `warning` event, on the console and in
  `rayspec show`, because an envelope that quietly went back to zero is worse than none.
- Writing it cannot fail a command either. A path that can be read but never *written* — a
  directory where `spend.json` belongs, a read-only mount, a full disk — costs the run its
  accounting, not the run: one `warning` event says the ledger could not be written and that
  this run's spend and outcome are not in the operator's totals, and the run finishes normally.

### Host run slots

```yaml
# policy file
max_concurrent_runs:
  claude: 2
  codex: 1
```

A scheduler that fires five workflows at 03:00 should not start five agents at 03:00.
`rayspec run` — and `rayspec resume` / `approve` / `reject`, which start the same agents — take
one slot per provider the workflow's `prompt:` steps resolve to, and hold it for the run. A limit
of `0` means that provider may not run on this host at all:

```console
$ rayspec run review_pr
error: all 2 claude run slots on this host are taken (run 20260821-030001-ab12 (pid 5511); run 20260821-030002-cd34 (pid 5512))
hint: wait with --wait-slot, or raise policy max_concurrent_runs

$ rayspec run review_pr --wait-slot 30m     # queue instead; `forever` waits indefinitely
```

Slots are `flock` files under `$RAYSPEC_HOME/limits/slots/<provider>/`, which is what makes them
crash-safe: the kernel drops the lock when the holding process dies, so a slot held by a run that
was killed, crashed or lost power is free again the instant that process is gone. Nothing has to
detect a stale holder and no lock file is ever deleted. The JSON inside a slot file (run id, pid,
start time) only exists so the message above can name who is holding it. A `--dry-run` takes no
slot.

## Pinning models (`rayspec lock`)

`model: sonnet` is a tier, `@fast` is an alias and an unset `model:` is the provider's default:
all three mean "whatever this resolves to today". A provider can change what that is between the
review of a change and the merge of it, and nothing in the run record would have said so.

```console
$ rayspec lock                       # writes .rayspec/rayspec.lock — commit it
$ rayspec lock --check               # exit 1 on drift (a CI job)
$ rayspec run review_pr --locked     # refuses to run a drifted workflow
```

`--locked` is on by default under `CI` (the environment variable; Jenkins and TeamCity do not set
it, so spell the flag out there) and names exactly what moved:

```
error: agent 'agents.reviewer' resolves to model 'claude-opus-4-9' but the lockfile pins 'claude-sonnet-4-6'
```

`run`, `plan`, `validate`, `resume`, `approve` and `reject` all take `--locked` / `--no-locked`;
the details are in [cli.md](cli.md#rayspec-lock). The resume half matters: a poll-then-`approve`
CI job is the commonest unattended shape there is, and the workflow-hash guard does not see a
*tier* that was re-pointed in `config.yaml`. With `--repo`, the lockfile checked is the one in the
checkout the workflow came from. The CI default only enforces a lockfile that exists — a project
that never ran `rayspec lock` is not broken by setting a variable — while an explicit `--locked`
refuses a missing one. Not refusing is not the same as not mentioning it: under the CI default
with no lockfile the run prints one `warning:` line on stderr saying nothing is pinned, so a CI
log where the lockfile was checked and a CI log where there was no lockfile do not look alike. Agents are keyed the way `run.json`'s `toolchain.models` keys them, so a
stored run and the lockfile talk about the same agents.

## Publishing the run branch

A run that pauses at three in the morning has done real work in a worktree on one machine.
`RAYSPEC_PUSH_BRANCH` makes rayspec push that work somewhere you can see it:

```bash
RAYSPEC_PUSH_BRANCH=1 rayspec run nightly --no-interactive       # push to origin
RAYSPEC_PUSH_BRANCH=upstream rayspec run nightly                 # or to another remote
```

The hook fires when the run **pauses** and when it **ends** (whatever the final status), and it
publishes `rayspec/<workflow>-<shortid>` — the branch the run's worktree is on.

**It publishes commits, and rayspec makes none.** A push moves what is committed; work an agent
left in the worktree is not on the branch and does not leave the machine. Workflows that want
their work published commit it themselves (`shell: git add -A && git commit -m …`). If the
worktree is still dirty at push time you get a warning saying how many changes stayed behind —
`pushed rayspec/x to origin, but 3 uncommitted change(s) in the worktree were not published`.

Rules it keeps, deliberately:

- **Opt-in.** Nothing is pushed unless the variable is set. `1`/`true`/`yes`/`on` mean `origin`;
  any other value names the remote.
- **Only an isolated run.** An in-place run (`--no-worktree`, a non-git directory) is on *your*
  branch — pushing that would be a surprise, so it never happens. A `--dry-run` publishes nothing.
- **It never forces.** If somebody else moved the remote branch on, git rejects the push and
  rayspec leaves it alone.
- **It never asks you anything.** The push runs with `BatchMode=yes` and no askpass helper, so a
  locked ssh key or a missing credential fails immediately and becomes the warning below,
  instead of stalling a finishing run for a minute or opening a dialog. It writes no upstream
  configuration either: a throwaway branch leaves nothing behind in your repository's config.
- **It fails soft.** No remote, no such branch, a rejected push, a timeout (60s), no `git` at all:
  every one of them is a `warning` event on the finished run (`rayspec show` lists it under
  `warnings:`). The run's status and exit code are exactly what they would have been without the
  hook — a push is not part of the run's outcome, and a broken remote must never turn a succeeded
  run into a failed one.

A successful push is silent: `git ls-remote <remote> 'refs/heads/rayspec/*'` is the confirmation.

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
  ([schema.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/schema.md#secret-inputs)): the value never enters `run.json`, `context.json`,
  events, prompts or outputs and reaches `shell:`/`python:` steps as `RAYSPEC_INPUT_<NAME>` only
  (no template may name it elsewhere — a load-time error); or hand them to steps through the
  environment (`~/.rayspec/.env`, your shell). Ordinary inputs and all outputs are persisted (in
  `run.json`, `events.jsonl` `run.finished`, every later step's `context.json`, and on the console
  / in `show`) — and so is whatever a script *prints*, secret or not. Agent steps cannot receive
  secret inputs in v1.
- **`.env` trust.** A checkout's `.rayspec/.env` is controlled by whoever pushed the repository
  and can redirect credentials (`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `*_PROXY`) or
  reconfigure git (`GIT_CONFIG_*`, `GIT_SSH_COMMAND`). Inspection commands (`doctor`, `validate`,
  `plan`, `workflows`, `agents`, `runs`, `costs`, `show`, `logs`, …) never load it; only `run`,
  `resume`, `approve` and `reject` apply it — into the process environment that reaches step,
  provider and git subprocesses — and they say so on stderr, naming the variables (`env: loaded N
  variables from .rayspec/.env (project): NAME, NAME`).
  `~/.rayspec/.env` (yours) is loaded by **every** project command and is not announced, so read
  it if you have not looked lately: `$RAYSPEC_HOME` is exported into every workflow step, which
  makes that file writable by anything you run. `rayspec doctor` lists both (`home .env` and
  `project .env` rows), with the variable counts, so you can check before the first run.
  Neither file can supply an identity — see [Who ran it](#who-ran-it) — but both can still supply
  configuration, which is what they are for, and that is a boundary you are trusting the way you
  trust `~/.bashrc`.
- **`cancel` pid check.** A `running` record keeps the engine's pid; after a crash or reboot the
  pid may belong to an unrelated process. `rayspec cancel` writes its cooperative cancel flag (see
  [Cancelling a run](#cancelling-a-run)) only for a process whose start
  time equals the recorded `pid_started_at` (exact; older records without the field skip this)
  *and* whose command line contains a rayspec execution command (`rayspec run|resume|approve|
  reject`) and names this run (id or workflow, as whole words) — anything else is refused with
  exit 2; `rayspec cancel <run> --mark` finalizes such a stale record as cancelled without
  writing anything for the process.
- **Untrusted text on the terminal.** Agent output, step output and input values are printed by
  the console, `show`, `logs` and the approval panel with control characters and terminal escape
  sequences removed (no title changes, screen clears, colour spoofing or hyperlinks from a
  model's answer; Rich markup in them is shown literally). `rayspec logs --raw` is the unescaped
  escape hatch for debugging.

## Roadmap

Automatic worktree recreation on resume (today: `git worktree add <path> <branch>` by hand when
the directory is gone).
