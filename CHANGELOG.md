# Changelog

All notable changes to rayspec are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow SemVer.

## [Unreleased]

## [1.0.0] — 2026-08-22

First release: a **CLI-only, file-based, provider-neutral engine for declarative agent workflows**
on the Claude Agent SDK and the OpenAI Codex SDK. YAML coordinates, code computes, agents judge
(`docs/constitution.md`).

Everything below ships in this one release; the two parts are a reading order, not a history.

## The authoring loop, governance and hardening

The work that turned an engine into something you can develop against, leave running and audit
afterwards.

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
- **`.rayspec/policy.yaml` — an operator's guardrails as a file.** Project and user layers plus
  `$RAYSPEC_POLICY`, combining most-restrictive-wins: allow-lists intersect, deny-lists unite, caps
  take the minimum, so a layer can only ever tighten a run. `providers.allow`, `models.deny`,
  `access.max`, `tools.deny`, `mcp.allow_servers`, `workspace.*`, `trust.*`, and the operational
  ceilings `budget:` / `max_consecutive_failures:` / `max_concurrent_runs:`. Violations are
  **load-time** errors naming the workflow field and the policy file and line that denies it — not a
  surprise halfway through a paid run — and the same check runs for `validate`, `plan`, `run`,
  `test`, `resume`, `approve` and `reject`. A policy file that cannot be read is never treated as an
  empty policy: a dangling symlink, a loop, a directory or an unreadable parent is an error naming
  the path. `validate`, `plan` and `run` print which layers are in force, or the paths they searched.
- **`provider_options` is an allow-list while any control is in force.** It is a raw pass-through
  applied over the options rayspec computed, so a workflow could otherwise put back the tools, the
  access level, the model or the MCP server a control just removed — and naming the dangerous keys
  cannot close that, because Claude's `extra_args` re-emits any CLI flag *after* the computed ones,
  where the last wins. Now every key of an agent's provider block must be one rayspec has written
  down the effect of; anything else is a load-time error naming the key, the control, the file and
  line that imposed it, and the keys that are permitted. Five permitted keys are checked by **value**
  rather than admitted outright. With no control in force the block is untouched.
- **"Any control" means any — from any source, in any schema.** The agent's own security-shaped
  fields; the workflow's `isolation:` and `defaults:` caps and a `secret: true` input; a step's
  `timeout:` over everything nested under it; every policy key; the `.rayspec/rayspec.lock` model
  lockfile; the machine owner's `providers:` block; and `run --worktree`. A restrictive *default* is
  a restriction: `isolation: worktree` is the default, so a workflow that does not say
  `isolation: none` is governed.
- **Approval classes.** `approve: {class: …}`, `--approve-class NAME`, and policy rules per class.
  `allow_yes: false` means the gate is **never** approved automatically — not by `--yes`,
  `--dry-run`, `--approve-class`, `auto_if`, or any combination; `require_tty: true` is stricter
  still. Rejecting a gate is never constrained by its class.
- `approve: {auto_if: <expression>}` — approves a gate when a condition holds. Checked at load time
  like `when:`, and it can only ever add an approval the gate's class already permits.
- **`rayspec plan --risk`** — a static report of what a run would be allowed to do: `access: full`
  agents, MCP servers started as a command or reached over the network, bodies that push, merge,
  force, delete, publish, escalate or install, steps working outside the workspace,
  `isolation: none`, and gates anything could waive. Worst first. It executes nothing.
- **`rayspec audit <run> [--commands]`** — a read-only ledger over one run: the actor, the steps,
  every command an agent ran, every tool it called, every file it changed, and every approval with
  the identity and the door behind it, in time order. `RAYSPEC_AUDIT_LOG=1` also writes it to
  `audit.jsonl` through the run store, so the redactor applies.
- **`RunRecord.actor` and `Decision.actor`** — who launched a run and who answered a gate, from
  `RAYSPEC_ACTOR` or the OS user, plus the CI system and any provider account the environment names.
  Every source is one the audited run **cannot write**: not git configuration in any scope, and not
  a `RAYSPEC_ACTOR` loaded from `$RAYSPEC_HOME/.env` or a checkout's `.rayspec/.env`. A `.env`-supplied
  actor is kept as `declared_id` — refused as the identity, recorded as a claim, so the refusal is
  visible rather than silent. An identity, never a credential.
- **`rayspec lock`** → `.rayspec/rayspec.lock`, pinning the literal model id and effort every agent
  resolves to; `--locked` refuses drift and is on by default under `CI`.
- **Cross-run spending envelopes and a failure breaker** (`policy.budget`,
  `policy.max_consecutive_failures`). Reaching a ceiling **pauses** the run (exit 3) rather than
  failing it, so the work is kept and `rayspec resume` continues once the ceiling allows. State is a
  flock-guarded file per user, per machine — nothing shared.
- **Host-level run slots** — `policy.max_concurrent_runs` per provider as `flock` files, with
  `--wait-slot`. A slot held by a process that died is free immediately.
- **Denied tool calls are recorded and can fail the step** — `StepRecord.denials`,
  `steps.<id>.denials`, and the agent field `on_denial: warn|fail`.
- **Trusted-workflow allowlist by resolved hash**, a **worktree change guard** (protected paths and
  a diff budget checked after every prompt step), an **agent command policy**, and **`network: off`**
  as a neutral agent field.
- **Push the run branch on pause or finish** — opt-in, and failing soft: a push failure is a warning
  on a finished run, never a change to its status.
- **A release pipeline.** A `v*` tag builds the sdist and the wheel, refuses a tag that disagrees
  with the packaged version, checks the metadata, installs the wheel somewhere clean and runs it,
  and publishes to PyPI through **Trusted Publishing** — an OIDC exchange in a protected
  environment, with no token stored anywhere. Both artefacts are signed with **Sigstore**, a
  **CycloneDX bill of materials** is generated from the locked runtime environment, and all of it
  is attached to the GitHub release. `workflow_dispatch` runs the same build as a rehearsal,
  stopping short of publishing. Release notes come from this file: a tag whose version has no
  section here exits 2 and stops the release, so a build nobody described is never announced.
- **A reusable GitHub Actions workflow** any repository with a rayspec project can call. It
  installs rayspec from PyPI, dry-runs one workflow — no provider credential, no token, no cost,
  every agent replaced by the scripted stub — and reports the step table and outputs as a
  pull-request comment edited in place on every push, and into the job summary. A workflow that
  does not load is reported with the loader's errors, so a red check always says why. A pull
  request from a fork gets a read-only token, so the comment is skipped with a warning rather than
  failing the check.
- **A documentation site** (MkDocs + Material) built from the same `docs/*.md` GitHub renders, with
  the README as its front page and this changelog as a page. Every internal link resolves in both
  renderings, and `docs/ci.md` explains rayspec in CI: what a dry run checks, the reusable
  workflow, `--locked` under `CI`, and how rayspec itself is released.
- **Documentation snippets are tests.** Every fenced YAML block in `README.md` and `docs/*.md`
  carries a marker: `rayspec:validate` loads and validates it, `rayspec:run` also drives it through
  a dry run, and `rayspec:skip <why>` records in one line why a snippet is only illustrative. A
  block with neither fails the suite, so a documented workflow that stopped working can no longer
  sit there — one already had.
- **SDK drift cassettes** — committed Claude and Codex transcripts in the SDKs' own wire shapes,
  replayed through their parsers and the real adapters, so a provider changing its message shapes
  fails a test instead of a production run.
- **Generative tests** over the templating round trip and the scheduler, with a seeded driver
  rather than a new dependency: every case comes from a printed seed and the first failure is
  shrunk to a minimal counter-example. The templating properties found two real defects, both
  shipped as strict expected-failures so the suite turns red the day they are fixed. The scheduler
  properties check the join table, drain versus fail-fast, `stop:` bubbling and the wind-down at
  every nesting depth, and hold on every generated case.
- **A mutation-testing harness** for the join table, the scheduler's teardown path, the redactor,
  the approval gate and the two policy enforcement modules. It edits one expression at a time in a
  temporary copy, never the working tree, and reports the survivors — the lines the tests do not
  really check.
- **`rayspec resume --fail-fast`**, and `run.json` records the failure policy a run started with,
  so a resume continues with the blast radius the first half had rather than draining siblings the
  first half would have cancelled. Like the flag, the recorded policy only ever tightens — a run
  waiting at an approval gate records the tightening on its way out, so the blast radius can be
  narrowed before the gate is decided.

### Changed
- **The coding-agent skill became two skills.** Authoring a workflow and operating a run are
  different jobs with different vocabularies, so `rayspec-workflows` (the YAML DSL: every step
  kind and field, templating, scoping, agents, prompts, stubs) and `rayspec-cli` (the command
  line: every command, flag, `--json` shape and exit code, plus the stub file, providers, cost and
  policy) replace the single `rayspec` skill. Each names the other and says when to load it. The
  old name is retired, not aliased — 1.0.0 was never published, so there is nothing to keep
  working.
- `rayspec init` writes both skills (`--no-skill` still opts out of both).
  `rayspec skill install|show|path` now take an optional skill name: no name acts on both, a name
  on that one, an unknown name is exit 2 with a did-you-mean.
- **`rayspec skill show --json` changed shape**: `{"skills": [{name, packaged, project, global},
  …]}` instead of the single skill's `{packaged, project, global}` object.
- **Both pages were written for their own job, not split down the middle.**
  `rayspec-workflows` carries the mental model, the authoring loop, a cheat-sheet using every step
  kind once, a complete field inventory (a test derives the expected set from the schema models,
  so a new field cannot be forgotten), the templating rules that bite, secrets, a best-practices
  section and three complete worked workflows that validate clean and dry-run to completion.
  `rayspec-cli` carries the run/directory/lock model, every command with its key flags and exit
  codes, the `--json` shape of every command that takes it, a safety class for every command it
  documents, the operating loops (check before you spend, offline tests, record and replay,
  debugging), governance and trust, and the stub file format.
- Seven docs pages that shipped with neither skill are now references of `rayspec-cli`
  (`testing.md`, `policy.md`, `runs-and-resume.md`, `isolation.md`, `ci.md` join `cli.md` and
  `providers.md`), and the ten commands the old skill never mentioned — `test`, `explain`, `eval`,
  `schema`, `trust`, `lock`, `new`, `plugins`, `completion`, `version` — are documented.
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
- **`resume`, `approve` and `reject` read the project the RUN belongs to** for everything
  project-scoped — its workflow, the models its agents resolve to, its lockfile, `secrets:`,
  `providers:`, `pricing:`, `extensions:` and the policy — instead of the project the command was
  typed in.
- **Nesting can only tighten `defaults.on_step_failure`** (`continue` < `drain` < `fail_fast`). An
  included block cannot relax a policy the including workflow stated. `--fail-fast` still tightens
  every scope at once and may only ever tighten.
- The run-level circuit breaker **names every cap that is over**, not just the first: a run that blew
  its cost cap and then ran out of time reported only the money, so the wrong knob got raised.
- **One implementation of each shared helper.** Durations, token counts, costs, the `usage`
  mapping, "this step failed" and the project root each had two to five copies; they have one now.
  Two renderings converge as a result: a token count just below a million reads `1.0M tok` rather
  than `1000.0k tok`, and a run listing reads a step that has a cost but names no source as
  `provider`, which is what the engine already recorded for it.
- **One JSON rule and one table style** across every command: compact when piped, indented on a
  terminal, and a redirected listing that is stable and diffable.
- CI pins every action to a commit sha, starts from read-only permissions, installs from the
  lockfile, and lints the workflow files.

### Fixed
- **The two skills disagreed about what a dry run produces.** The operating skill said a skipped
  `shell:`/`python:` step is recorded `succeeded` with *empty output*; a step that declares an
  `output_schema` actually gets the minimal instance of that schema (`{type: boolean}` → `false`),
  which is what makes a downstream `when:` fire where a reader expected `""`. Both pages now state
  the same rule, and the authoring page no longer prints the accepted *formats* of
  `defaults.budget_usd` / `max_tokens` in the column that means *default* — neither has one.
- **The skill could go stale without anything failing.** The only check over its CLI table
  asserted that everything the table *named* existed, with a `len(rows) >= 14` floor; ten commands
  were added and none was listed while the suite stayed green. The classification is now total and
  derived from the code: every leaf command and invokable group of the builtin CLI is in exactly
  one skill's table, every `docs/*.md` page is in exactly one skill's references or in a named
  online-only list with a reason, and every field of every schema model appears in the authoring
  skill. Each deliberate omission is a named, justified deny-list entry, so adding a command, a
  page or a field forces a decision instead of passing silently.
- A `shell:` value over the 64 KiB spill threshold now behaves exactly like a smaller one. It used
  to be spliced into the script as `$(cat '<path>')`, and that cost it every trailing newline (a
  captured build log is both large and newline-terminated, so it was the likeliest value to hit
  it), made `echo '{{ x }}'` print a command substitution instead of the documented literal
  `${RAYSPEC_V1}`, and put the run's temporary directory into the step's own output. The body now
  keeps the plain `${RAYSPEC_V<n>}` reference either side of the threshold, and a preamble line
  prepended to the script assigns the slot from the spill file with its trailing bytes intact.
  One difference across the threshold remains, deliberately: a spilled slot is a shell variable
  and is not exported, so a child process the body starts finds a small slot in its own
  environment but not a spilled one — exporting it would put a value larger than the threshold
  back into the environment block that spilling exists to keep it out of (`docs/templating.md`).
  Because the preamble is part of the rendered script, the step fingerprint of such a step
  changes: a run started before this version and resumed after it re-runs every `shell:` step
  whose value crossed the threshold, and reports it as a changed workflow. Runs started on this
  version resume as before.
- Unresolved merge-conflict markers were committed in the CLI reference and copied by the skill
  generator into both packaged copies, so the wheel shipped them. A guard now scans every tracked
  text file.
- `rayspec doctor` printed a `cmd:` secret source verbatim, so a credential passed on the command
  line (`curl -H "Authorization: Bearer …"`) appeared in output the bug-report template asks people
  to paste publicly. Only the program is named now; the arguments are counted.
- `rayspec show` accepts `--output` like every other command that has `--json`.
- `SECURITY.md` described a fallback reporting route that no issue form made possible; there is now
  a contact-only form, and a test keeps the page and the form in agreement.
- The README described `timeout_total` and `artifacts:` as roadmap while both ship, and omitted
  seven commands; the list is now checked against the CLI. `docs/schema.md` dated the redactor to a
  version that does not exist.
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
- **`join: always` now runs when a sibling list is torn down.** Under `--fail-fast` and after a
  `stop:`, cancelling the task group blanket-skipped every pending step — the cleanup step included —
  so a cancelled run never reached its cleanup. It only ever misbehaved with two or more steps in
  flight, which is why no example-based test caught it.
- **A cleanup step that pauses no longer wedges the run.** A `join: always` gate reached while a
  `stop:` was tearing the list down ended the run `failed — engine error`, and every resume hit the
  same crash.
- **`defaults.on_step_failure` in an `include:`d workflow is no longer ignored.** The policy is
  lexically scoped: an included workflow that states one governs its own body; one that says nothing
  inherits the including run's.
- **A `secret: true` value that is not a plain string no longer breaks the run it was protecting.**
  `run.json` was redacted as serialised *text*, so a bare JSON token rewrote `"budget": 4242` into an
  unquoted marker and the checkpoint stopped parsing. Records, events and stream records are redacted
  on their parsed values now.
- **The streaming buffer no longer releases half a value**, and **a secret in a key position is
  redacted**.
- **Redaction no longer depends on the caller wiring it.** A run installs a redactor covering its own
  secret inputs before it writes a byte, so an embedded run cannot persist a secret by omission; a
  store that will not accept one makes the workflow refuse to start rather than write anything.
- **`rayspec explain` names the cap that actually fired** for a step skipped by the wall-clock cap,
  instead of reporting the budget.
- **`rayspec schema` prints the same bytes as the file it publishes.** It re-serialised the parsed
  document, and the serialiser escapes non-ASCII by default, so every em dash in a description
  printed as `\u2014` while the checked-in file held the character. The documented promise was
  simply false.
- **The golden corpus no longer depends on which concurrent step finishes first.** Steps that run
  in parallel interleave, so the committed file pinned whichever coroutine the event loop happened
  to resume — and disagreed with itself about two runs in five. Each step's events are grouped and
  ordered by path now; within a step the order is untouched, so a changed sequence still fails.
  The same fix went into a README comparison that failed the same way under load.
- **An example ships whole**, `checks.yaml` included, so a scaffolded project can run the cases its
  own README describes.

## The engine, the language and the CLI

The workflow language, the scheduler, the two provider adapters and the command line.

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
- **Two Claude Code skills** shipped as package data — `rayspec-workflows` for authoring the YAML
  and `rayspec-cli` for operating the engine. Each is a hand-written `SKILL.md` plus a `references/`
  set generated from `docs/` by `scripts/gen_skill.py` and mirrored to `.claude/skills/<name>/`, and
  each names the other and says when to load it. `rayspec skill install [NAME] [--global] [--force]`,
  `rayspec skill show [NAME]`, `rayspec skill path [NAME]` — no name acts on both; `rayspec init`
  writes both (`--no-skill` opts out); `docs/agent-skill.md`.
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
- Secret inputs reach only `shell:`/`python:` steps, through the environment; an agent step cannot
  receive one, and asking for it is a load-time error (see `docs/schema.md`). A value a step prints
  IS redacted on its way to every writer — the store, the event stream, the console — but redaction
  is exact-match, so a step that transforms a secret before printing it (encoding it, splitting it,
  hashing it) defeats it. Give a step a capability rather than a credential where you can;
  `examples/secret_via_tool` is the idiom.
- Cost for Codex models is shown as tokens only unless a `pricing:` entry is configured.
- POSIX shell semantics: a value is passed as `${RAYSPEC_V<n>}` and never spliced into a script, so
  single quotes stop the shell expanding it — the one thing most likely to surprise you.
- This release is verified by the local gate (`ruff`, `ruff format`, `pyright`,
  `pytest -m "not live"`, `scripts/check_examples.py`) and by CI on Python 3.11, 3.12, 3.13 and
  3.14, plus the example matrix and a lint of the workflow files. The documentation site builds and
  publishes from the same pipeline. The release workflow has been rehearsed end to end on a runner
  through its `workflow_dispatch` path, which builds both artefacts, checks the metadata, installs
  the wheel into a clean environment, and writes the notes and the bill of materials.
  **Two jobs have still never run: the PyPI upload and the release signing.** They fire only on a
  version tag, so this release is the first time they execute.

[Unreleased]: https://github.com/rayspec-labs/rayspec-py/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/rayspec-labs/rayspec-py/releases/tag/v1.0.0
