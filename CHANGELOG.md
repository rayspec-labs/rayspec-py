# Changelog

All notable changes to rayspec are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

### Added
- **`rayspec test`** — run a project's declarative workflow cases offline against the stub provider:
  no credentials, no network, no tokens. Cases come from `.rayspec/tests/<workflow>/<case>.yaml` or a
  `checks.yaml` beside an example. Failures print in the four-line format (expectation, actual, fix,
  `file:line`). `--case`, `-k`, `--junit`, `--json`, `--exec-shell`; exit 0/1/2.
- **`rayspec runs stubs <run>`** records a real run as a stub script, and **`run --dry-run
  --stubs-from <run>`** replays it offline — a live 4.6s agent step replays in 4ms at zero cost. It
  refuses to record a run that had secret inputs.
- **Stub `expect:` blocks** — assert what the agent was actually asked (`prompt_contains`,
  `prompt_regex`, `not_contains`, `agent`, `access`, `model`, `output_schema`, `session`). A mismatch
  fails the step with the rendered prompt, turning "the graph executed" into "the agent got the right
  thing".
- **`rayspec runs diff <a> <b>`** — status/duration/cost/token deltas, changed fingerprints and
  per-step output diffs between two runs; `--exit-code` makes it a CI gate.
- **`rayspec explain <run> <step>`** — why a step ran, skipped or failed: the join row, the evaluated
  `when:`, retry history, the resolved agent after merge, and the exact prompt that was sent (now
  persisted as `steps/<path>/prompt.txt`).
- **`rayspec plan --render`** — see the rendered prompt or script *before* spending a token; secret
  inputs show as `<secret>`.
- **`rayspec eval <run> '<expr>'`** — a Jinja expression REPL over a stored run, with the same
  undefined-reference hints the engine raises.
- **Published JSON Schemas** for the workflow YAML and for `run.json` / `events.jsonl` /
  `stream.jsonl`, plus a `# yaml-language-server:` modeline in every scaffold and example — editor
  autocompletion on the YAML. `rayspec schema [workflow|run|events|stream]`.
- **`RunRecord.toolchain`** — rayspec, Python, platform, each provider's SDK/CLI and the resolved
  model per agent, shown by `rayspec show`.
- **Secrets, end to end**: `config.secrets` behind a `SecretProvider` seam (env/file/cmd), and one
  `Redactor` at every store writer and every sink. Verified live — a run with an input secret and a
  config secret, both echoed by a shell step, leaves zero occurrences of either anywhere under
  `RAYSPEC_HOME` or in stdout. Redaction is exact-match and best-effort: a value a step transformed
  or truncated can still survive.
- **`defaults.on_step_failure: continue`** — a failed step no longer stops independent branches.
  The failed step's downstream cone skips (`upstream_failed`, then `upstream_skipped` for steps
  below it); branches that do not depend on it keep being scheduled. It is **not** `allow_failure`:
  the run still ends `failed` with exit code 1, so a triage workflow can report every check's verdict
  without pretending it passed. `--fail-fast` beats it — the flag may only ever tighten. The policy
  is **run-level and global**, so it also applies inside `each:`/`loop:`/`include:` bodies. Note it
  is a *different knob* from the pre-existing `each.on_failure: continue`, which is per-item;
  `docs/schema.md` disambiguates the two under `each:`.
- **License: Apache-2.0.** `LICENSE` (verbatim Apache License 2.0), `NOTICE` (copyright, the Archon
  design attribution, and the third-party dependency list), and an
  `# SPDX-License-Identifier: Apache-2.0` header on every module under `src/rayspec/`. `pyproject`
  carries the PEP 639 `license = "Apache-2.0"` expression and `license-files`, so built wheels now
  report `License-Expression: Apache-2.0` and ship both files under `dist-info/licenses/`.
  Contributions are accepted under the Developer Certificate of Origin (`git commit -s`).
- Packaging: a `py.typed` marker, so downstream type checkers see the annotations the codebase
  already carries; `[project.urls]` (Homepage, Repository, Changelog, Issues) and the missing
  classifiers (`Development Status :: 5 - Production/Stable`, `Intended Audience`, `Operating System
  :: OS Independent`, `Typing :: Typed`), so the PyPI page is usable.
- Test suite: a golden run corpus over every runnable example case — it caught two `run.json` shape
  changes and a machine-specific `platform` string on its first day — and a fault-injecting
  `RunStore` with 16 crash points across every persistence method, from all of which resume
  converges.
- **Extension points** — a separate package can extend rayspec without forking it. Third-party CLI
  commands through the `rayspec.cli_plugins` entry point, using the same `register(app)` a builtin
  command module uses; builtin commands are never shadowed, and a plugin that fails to import is
  skipped with a warning rather than breaking the CLI. `rayspec.stores`, `rayspec.sinks` and
  `rayspec.approvals` follow the same precedence rules, with `rayspec.store.create_store` /
  `rayspec.events.create_sink` / `rayspec.registry.create_approval` for embedders. A third-party run
  store is wrapped so a run's secrets are redacted before it ever sees them.
- **`rayspec plugins [--output table|json]`** — which package provides which command, store, sink,
  approval or provider, and why one was skipped.
- Optional **`extensions:` block in `config.yaml`** (`sinks:`, `approval:`, `settings:`) selecting
  registered sinks and approval prompts by id; unset means unchanged behaviour.
- **`defaults.timeout_total`** — a wall-clock cap for a whole run, alongside `budget_usd` and
  `max_tokens`. Once exceeded no new step starts, including one already queued for a `max_parallel`
  slot; running steps finish and the run ends `failed` with `time limit exceeded (elapsed … >
  timeout_total …)`. The clock runs from the run's original start, so it keeps counting across
  resumes.
- **`artifacts:` on any step** — the files it promises to write, relative to its working directory.
  A missing file, or one that is not a regular file, fails the step; delivered files are copied into
  the run directory and recorded with a sha256, and `rayspec show` lists them. Absolute, `..` and
  templated paths are refused when the workflow loads.
- **`rayspec costs [--since WHEN] [--workflow NAME] [--output table|json]`** — sum a project's runs
  by workflow: run count, tokens, total cost and the cost-source breakdown. Runs with no recorded
  cost are counted and shown as `unknown`, and a total missing any of them is marked `≥` (a lower
  bound) rather than being quietly wrong. Runs still running or paused are counted and named as not
  final. `--since` takes a window (`7d`, `24h`, `90m`) or a date, inclusive at the cutoff. Read-only.
- **`rayspec init --from <example>`** scaffolds one of the packaged example projects — its
  `.rayspec/` tree, stub scripts and README — and prints the example's own scripted dry run as the
  first step. Applied whole or not at all: conflicting files are named and the command exits 2
  unless `--force`. An unknown name lists the catalogue with a did-you-mean. The examples now ship
  inside the wheel, so this works without a checkout.
- **`rayspec new workflow <name>`** and **`rayspec new agent <name>`** add one file to an existing
  project. The workflow validates and dry-runs as written; neither overwrites without `--force`.
- **`rayspec completion <bash|zsh|fish>`** prints a shell-completion script to source. It completes
  commands and options, plus workflow names after `run`/`plan`/`validate`/`test` and run ids after
  `show`/`logs`/`resume`/`approve`/`reject`/`cancel`/`eval`/`explain`/`runs diff`/`runs stubs`.
- **`--output table|json`** on every command that has `--json`. `--json` keeps working unchanged as
  the older spelling of `--output json`; passing both with different values is a usage error.
- **Community health files** — `SECURITY.md` (private reporting, the supported 1.x line, a 90-day
  coordinated-disclosure window, and the threat model: declared `shell:`/`python:` execution is by
  design, a `secret: true` leak is not), `CONTRIBUTING.md` (the quality gate verbatim, TDD, the
  frozen contract modules, DCO sign-off), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), and the
  `.github/` issue forms, pull-request template and `CODEOWNERS`.
- README badges (CI, licence, supported Python versions, code of conduct) and a Contributing
  section linking the new pages.
- **`docs/extending.md`** rewritten around the extension points, with a complete copy-pasteable
  example package that the test suite installs and runs.

### Changed
- **Upgrade note — `defaults.on_step_failure: fail_fast` now takes effect.** In 1.0.0 the field was
  accepted but **inert** (`docs/schema.md` said so outright), so a workflow could carry
  `fail_fast` and still get drain behaviour. It is now honoured: such a workflow will start
  cancelling running siblings (`interrupted`) and skipping pending steps (`run_failed`), including
  `join: always` ones. Set `drain` explicitly, or remove the value, to keep 1.0.0 behaviour.
  Workflows that never set the field are unaffected — `drain` is still the default.
- `steps.<id>.ok` on a **skipped** step is now undefined-with-hint instead of a bare `False`, so
  `when: steps.x.ok` fails loudly rather than reading as a real answer.
- `rayspec validate` reports **every** schema problem of a document, each with its own `file:line`,
  instead of stopping at the first.
- `scripts/check_examples.py` delegates to `rayspec.testing`; `--matrix` drops from ~40s to under 7s.
- **Codex usage baseline settled.** A live run against `codex-cli` 0.147.0 confirmed that a fresh
  provider instance resuming a thread *without* a baseline reports usage exactly equal to the delta
  of the server totals — the app-server does not replay a carry-over update. The engine therefore
  does not pass `usage_baseline` back on resume; it stays an escape hatch. Re-run
  `RAYSPEC_LIVE=1 pytest -m live tests/providers/test_codex_live.py` after a Codex SDK bump.
- Docs: `README` Status reflects the released 1.0.0 and lists the `skill` command group; the
  packaged `SKILL.md` drops its pre-tag "v1.0.0 final" hedges; `docs/schema.md` documents
  `on_step_failure` as the working field it now is.

### Fixed
- **`defaults.on_step_failure` now works.** The field has been in the schema since 1.0.0 but the
  engine only ever read the `--fail-fast` CLI flag, so the YAML value was silently ignored. The
  scheduler now reads `RunContext.fail_fast` = the flag **or** `defaults.on_step_failure:
  fail_fast`. `--fail-fast` can only *enable* fail-fast, never downgrade a workflow that asked for
  it, and `drain` remains both the default and the 1.0.0 behaviour — no existing workflow changes
  meaning.
- **A `stop:` step can no longer report a failed run as succeeded.** `Runner._finalize` decided the
  run status from the control signal (`ctx.stopped`) *before* scanning for untolerated failures.
  Under `drain` that was safe — a `stop:` on another branch was unreachable after a failure — but
  `on_step_failure: continue` re-opens the ready-set, so `stop: {status: succeeded}` turned a run
  containing a failed step into `succeeded`, exit 0, **with `outputs:` published**. The failure scan
  now runs first and outranks `ctx.stopped`; a paused run names the prior failure in its reason. The
  step that *raised* the stop is excluded, so `on_reject: cancel` still cancels (exit 4) rather than
  failing. Only **top-level** records count, so `each.on_failure: continue` still tolerates a failed
  item. Found while reviewing the change that introduced it.
- **A human veto always drains.** Rejecting a gate with `on_reject: fail` now halts new work even
  under `on_step_failure: continue`. `continue` is for triaging machine failures, not for
  overriding an operator's "no".

## [1.0.0] — 2026-08-20

First release: a **CLI-only, file-based, provider-neutral engine for declarative agent workflows**
on the Claude Agent SDK and the OpenAI Codex SDK. YAML coordinates, code computes, agents judge
(`docs/constitution.md`).

### Added — workflow language
- YAML workflow files (`.rayspec/workflows/<name>.yaml`, `rayspec: 1`) with exactly one kind key per
  step: `prompt`/`prompt_file`, `shell`, `python`, `loop`, `each`, `approve`, `include`, `stop`;
  common fields `needs`, `join` (`all|any|always`), `when`, `timeout`, `retry`, `allow_failure`,
  `always_run`, `env`, `description`.
- Typed `inputs` (JSON Schema: string/integer/number/boolean/array/object, enum, default, required)
  with CLI coercion, `--inputs-file`, `RAYSPEC_INPUT_*` env and did-you-mean for unknown names.
- Reusable `agents` (workflow-local, `.rayspec/agents/`, `~/.rayspec/agents/`): provider, model
  tiers (`small|medium|large`) and `@aliases`, `effort`, `access` (`read-only|workspace-write|full`),
  neutral `tools` vocabulary with provider-prefixed raw names, `instructions`/`instructions_file`
  (+ `instructions_mode`), `max_turns`, `budget_usd`, `thinking`, `mcp`, `provider_options`
  escape hatch; `extends`-style overrides.
- `outputs` rendered from the run; `include` exposes the included workflow's outputs; `stop` ends a
  run early with a status and reason.
- Jinja2 templating in a sandboxed environment with strict-but-chainable undefined, three rendering
  modes (text / shell env-ref `${RAYSPEC_V<n>}` with spill files / python repr), `{{# #}}` comment
  delimiters in code bodies, filters `fromjson`, `regex_search`, `has_signal`, lexical scoping of
  loop/each/include bodies, `iteration.*` and `each.*` context, `RAYSPEC_CONTEXT` JSON dump for
  shell/python steps.
- Run-level `defaults`: `agent`, `timeout`, `max_parallel`, `on_unsupported`, `on_step_failure`,
  `budget_usd` / `max_tokens` circuit breaker.

### Added — engine
- anyio-only runtime: ready-set scheduler, implicit DAG parallelism, `each` fan-out with
  `max_parallel`/`on_failure`, do-while `loop` with `until`/`max_iterations`/`on_exhausted`,
  load-time `include`, drain vs `--fail-fast`, leaf-only concurrency permits, SIGINT → clean
  interruption (exit 130) with child-process cleanup.
- Structured output (`output_schema`): enforced or best-effort with validation and re-ask; session
  continuation (`session: <step>`) and forks.
- Retries (`attempts` = total attempts; transient classification), per-attempt timeouts,
  `allow_failure` (tolerated failures), run-level budget/token caps.
- Approval gates: TTY prompt (`[a]pprove / [r]eject / [v]iew / [d]iff / [p]ause`) or pause with a
  per-attempt token → exit 3 → `rayspec approve|reject`; `on_reject: cancel|continue|fail`.
- File-based run store under `~/.rayspec/projects/<slug>/runs/<run-id>/` (`run.json`, `events.jsonl`,
  per-step outputs and `stream.jsonl`), write-ahead outputs, atomic saves; resume = re-execute with a
  reuse cache, workflow-hash guard (`--force`), refusal of finished runs; run-level usage/cost with
  `cost_source ∈ {provider, table, partial, none}`.
- Worktree isolation by default (`rayspec/<workflow>-<id>` branches under
  `~/.rayspec/projects/<slug>/worktrees/`), `--no-worktree`/`isolation: none`, `--base`, `--repo
  <path|name|url>` with bare clones and a project registry, per-workdir `flock` path lock held by
  the engine.
- Launcher env hygiene for shell/python steps (VIRTUAL_ENV & co. scrubbed).

### Added — providers
- Neutral provider seam: `Provider` protocol, `ProviderCapabilities`, `AgentRequest/Event/Result`,
  registry with built-ins and `rayspec.providers` entry points, stub provider for tests and dry
  runs (scripted answers, globs over step paths, failure injection), pricing table for providers
  without cost reporting.
- Validation maps every agent/step field to a capability: unsupported features fail `validate` with
  a fix hint or degrade to warnings with `defaults.on_unsupported: warn` / `--allow-unsupported`.
- **Claude Agent SDK adapter**: Claude Code preset system prompt + append/replace instructions,
  access levels → tools/permission modes, structured output, resume/fork, cost & usage, rate-limit
  warnings, healthcheck/probe, login detection.
- **OpenAI Codex SDK adapter**: pooled app-server clients, sandbox per access level, `deny_all`
  approvals, shielded-consumer cancellation (no leaked worker threads), usage from total deltas,
  strict-schema normalisation, `max`/`ultra` efforts for the gpt-5.6 family.

### Added — CLI (Typer + Rich)
- `run`, `validate`, `plan`, `workflows`, `agents`, `providers`, `runs`, `show`, `logs`
  (`--step`, `--stream`, `--follow`, `--raw`), `resume`, `approve`, `reject`, `cancel` (`--mark`),
  `projects add|list|remove`, `worktrees list|clean`, `init` (`--kind code|content`), `doctor`
  (`--probe`, `--provider`), `version`/`--version`.
- Rich Live step tree on a TTY, `--quiet`, `--verbose`, `--json` JSONL events with the summary
  object as the last stdout line, exit codes 0/1/2/3/4/130; dry runs with `--dry-run`, `--stubs`,
  `--stubs-init`, `--exec-shell`.
- Hardening from the acceptance pass: clean errors for malformed config (no tracebacks), project
  `.rayspec/.env` applied only by execution commands, run store created 0700/0600, terminal-escape
  sanitising of all untrusted text (`rayspec.textsafe`), `cancel` verifies the target is a rayspec
  process, provider warnings surfaced on the console and in `show`.

### Added — secrets, stubs and the packaged skill
- `inputs.<name>.secret: true`: secret inputs are never persisted (`run.json`/`context.json`/events/
  `plan`/`show` print `<secret>`), reach `shell:`/`python:` steps only as `RAYSPEC_INPUT_<NAME>` or
  through their `env:` mapping, are refused at load time everywhere else, and are supplied again
  (`--input`/`RAYSPEC_INPUT_<NAME>`) on `resume`/`approve`/`reject`/`run --resume`; `plan`/`validate`
  mark them `(secret)`; example `notify_webhook`.
- `rayspec resume|approve|reject --stubs PATH` and automatic reuse of the `--stubs` file recorded at
  launch (`run.json` `stubs_path`); a `--dry-run` record resumes as a dry run.
- **Claude Code skill `rayspec`** shipped as package data (`SKILL.md` + `references/` generated from
  `docs/` by `scripts/gen_skill.py`, mirrored to `.claude/skills/rayspec/`); `rayspec skill install
  [--global] [--force]`, `rayspec skill show`, `rayspec skill path`; `rayspec init` writes the project
  skill (`--no-skill` opts out); `docs/agent-skill.md`.
- `run.json` records `pid_started_at`; `rayspec cancel` verifies the pid by it (on top of the
  command-line check) before sending SIGINT.
- Dogfood `release_check`: a `history` shell step prepares the commit log and merged pull-request
  titles for the notes agent (Codex gpt-5.6-sol, `effort: xhigh`), which returns the complete notes
  in its `notes` field; `examples/release_check` mirrors the step.

### Added — docs and examples
- `docs/`: concepts, schema reference, templating, providers (generated capability matrix), CLI,
  runs & resume, isolation, extending, examples, constitution.
- `examples/`: `hello_review`, `fix_issue`, `triage_fanout`, `review_block` + `pr_review`,
  `unsupported_demo`, `release_check` — every capability covered at least once
  (`examples/README.md` matrix), all runnable with `--dry-run --stubs` and checked by
  `scripts/check_examples.py`.
- Dogfood workflows in `.rayspec/workflows/` (`review_pr`, `fix_issue`, `implement_feature_tdd`,
  `docs_sync`, `release_check`).

### Fixed
- `worktrees/`, the parent of `source.git/` and `~/.rayspec/config.yaml` are created private
  (0700/0600) regardless of the umask; git checkouts keep git's modes.
- `validate`/`plan`/`run` check references inside `when`/`until`/`each` expressions against the real
  template engine (they were parsed as text and never checked).

### Quality gate for this release
- **Acceptance pass**: the release was exercised from five angles — first-time setup, workflow
  authoring, a security review, a blind install on a fresh Ubuntu 24.04 workstation, and live
  Claude/Codex runs. Every issue that pass turned up was fixed before the tag, leaving nothing
  open.
- Live-verified: Claude and Codex runs with the CLIs' own logins (structured review, worktree edit
  loop with approval, SIGINT → no orphan processes → resume); `pip-audit` clean on `uv.lock`.

### Known issues / limitations
- POSIX-first (macOS, Linux); Windows is best-effort and untested.
- Secret inputs (v1) reach only `shell:`/`python:` steps via the environment — agent steps cannot
  receive secrets; a shell step that echoes a secret persists it in its output (see `docs/schema.md`).
- Cost for Codex models is shown as tokens only unless a `pricing:` entry is configured.
- This release was verified with the local gate (`ruff`, `ruff format`, `pyright`,
  `pytest -m "not live"`, `scripts/check_examples.py`); CI has not yet produced a green run.

[1.0.0]: https://github.com/rayspec-labs/rayspec-py/releases/tag/v1.0.0
