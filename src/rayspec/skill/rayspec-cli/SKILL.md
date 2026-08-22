---
name: rayspec-cli
description: Run, inspect, resume, debug, test and govern rayspec agent workflows from the CLI (Claude Agent SDK + OpenAI Codex SDK) — every command, flag, --json shape and exit code, plus stubs, providers, capabilities, cost and the .rayspec/ project layout. Use when asked to validate, plan, run, resume, approve, audit or troubleshoot a rayspec workflow. Load the companion rayspec-workflows skill for the YAML DSL itself.
---

# rayspec CLI — running, inspecting and governing

**Companion skill**: every question about the *YAML* — which step kinds exist, what a field means,
how templating and scoping work — is answered by the **`rayspec-workflows`** skill. Load it before
editing a workflow file; do not guess a field from a flag.

## Mental model (runs, isolation, exit codes)

- A **run** = one execution with fixed inputs, an id `YYYYMMDD-HHMMSS-xxxx` and a run directory
  `~/.rayspec/projects/<slug>/runs/<run-id>/` (`run.json`, `events.jsonl`, one dir per step with
  `output.*`, `stream.jsonl`, `stdout.log`, `context.json`). `RAYSPEC_HOME` overrides `~/.rayspec`.
- **Worktree by default**: in a git repo every run gets `git worktree add` on branch
  `rayspec/<workflow>-<shortid>` under `~/.rayspec/projects/<slug>/worktrees/`; steps run there
  (`run.workdir`), workflows load from your checkout. `isolation: none` / `--no-worktree` runs in
  place; non-git dirs always run in place.
- Exit codes: `0` succeeded · `1` failed · `2` usage/validation error · `3` paused at a gate ·
  `4` cancelled (`stop:`/rejected gate) · `130` interrupted.
- Every `<run>` argument accepts a unique id **prefix**, and resolves across every project under
  `RAYSPEC_HOME` — the cwd does not scope it. Commands that read a project take `--root DIR`.
- Read-only unless stated: `plan`, `validate`, `runs`, `show`, `logs`, `explain`, `eval`, `audit`,
  `costs`, `workflows`, `agents`, `providers`, `plugins`, `trust list|check`, `worktrees list` and
  `lock --check` neither spend money nor need credentials. `run`, `resume`, `approve` and `reject`
  execute agents for real — **ask the human first** when a run may edit files, push, open a PR or
  spend money.

## CLI quick reference

Every command of the CLI except the four that create or describe authoring artifacts
(`init`, `new workflow`, `new agent`, `schema`) — those are in the `rayspec-workflows` skill.

| Command | Purpose | Key flags | Exit |
|---|---|---|---|
| `rayspec version` | print the rayspec version | — | 0 |
| `rayspec doctor` | environment: Python, home, config, git/uv, SDKs, CLIs, auth, pricing rows | `--probe`, `--provider ID`, `--json`, `--root` | 0 / 1 |
| `rayspec plugins` · `providers` | installed plugins (commands, providers, stores, sinks, approvals) · registered providers and their capability matrix | `--json`, `--output` | 0 / 2 |
| `rayspec completion <shell>` | print a shell-completion script to source (`bash\|zsh\|fish`) | `--values workflows\|runs`, `--root` | 0 / 2 |
| `rayspec workflows` · `agents` | discovered workflows / named agent files with their resolved provider+model | `--json`, `--root` | 0 / 2 |
| `rayspec validate [names…]` | schema, graph, references, templates, capabilities, policy | `--allow-unsupported`, `--locked`/`--no-locked`, `--json`, `--root` | 0 / 2 |
| `rayspec plan <wf>` | what a run would do: inputs, resolved agents, step order, capability report; `--render` prints the rendered prompt/script bodies, `--risk` what the run would be allowed to do | `--input k=v`, `--inputs-file`, `--render`, `--step`, `--stubs`, `--risk`, `--locked`, `--json` | 0 / 2 |
| `rayspec run <wf>` | run (or resume) a workflow | `--input`, `--inputs-file`, `--dry-run`, `--stubs f`, `--stubs-init f`, `--stubs-from`, `--exec-shell`, `--yes`, `--approve-class NAME`, `--no-interactive`, `--json`, `--quiet`, `--verbose`, `--fail-fast`, `--allow-unsupported`, `--worktree`/`--no-worktree`, `--base`, `--repo`, `--resume <id>`, `--force`, `--wait-slot`, `--locked` | 0 1 2 3 4 130 |
| `rayspec test [wf]` | run the project's workflow test cases offline (dry run, stub provider) | `--case`, `--select`, `--exec-shell`, `--junit`, `--json`, `--root` | 0 / 1 / 2 |
| `rayspec runs` · `runs diff` · `runs stubs` | list runs (newest first) · compare two runs of one workflow · write a stub script from a stored run | `--all`, `--limit N`, `--steps`, `--outputs`, `--exit-code`, `--across-projects`, `--redact`, `--force`, `--json`, `--root` | 0 / 1 / 2 |
| `rayspec show <run>` | header, workspace, step table, warnings, outputs, pause state | `--json`, `--root` | 0 / 2 |
| `rayspec logs <run>` | lifecycle events; `--step <path>` = that step's transcript | `--step`, `--stream`, `--follow`, `--verbose`, `--raw`, `--json` | 0 / 2 / 130 |
| `rayspec explain <run> <step>` | why one step ran, skipped or failed: the cap that fired, the join decision, `when:` with every operand's value, retries, the resolved agent, the rendered env and prompt | `--full`, `--json`, `--root` | 0 / 2 |
| `rayspec eval <run> <expr>` | evaluate a Jinja expression in a stored run's context (read-only) | `--step`, `--shell`, `--json`, `--root` | 0 / 2 |
| `rayspec audit <run>` | read-only ledger: commands, tools, files, warnings, approvals + who ran it | `--commands`, `--json`, `--root` | 0 / 2 |
| `rayspec costs` | sum a project's runs by workflow (tokens, cost, cost-source breakdown) | `--since 7d`, `--workflow NAME`, `--json` | 0 / 2 |
| `rayspec resume <run>` | resume a paused/failed/interrupted run (succeeded steps are reused) | `--force`, `--yes`, `--approve-class`, `--no-interactive`, `--input`, `--stubs`, `--fail-fast`, `--wait-slot`, `--json` | run's code / 2 |
| `rayspec approve <run> [comment]` · `reject <run> [reason]` | decide a paused gate and resume in process | `--force`, `--input`, `--stubs`, `--wait-slot`, `--quiet`, `--json` | run's code / 2 |
| `rayspec cancel <run>` | SIGINT a live run / mark a dead one cancelled | `--yes`, `--mark`, `--force`, `--json` | 0 / 1 / 2 |
| `rayspec lock [names…]` | pin every agent's literal model id and effort to `.rayspec/rayspec.lock` | `--check`, `--json`, `--root` | 0 / 1 / 2 |
| `rayspec trust add\|check\|list\|remove` | allow-list workflows by their resolved hash (`.rayspec/trusted.yaml`) | `--json`, `--root` | 0 / 1 / 2 |
| `rayspec worktrees list` · `clean` | rayspec worktrees of the project | `--older-than 7d`, `--merged`, `--merged-into`, `--repo`, `--force`, `--dry-run`, `--json` | 0 / 2 |
| `rayspec projects add\|list\|remove` | names for `--repo <name>` | `--base`, `--json` | 0 / 2 |
| `rayspec skill install\|show\|path` | the two rayspec skills (project or `--global`); a name installs just that one | `--global`, `--force`, `--root`, `--json` | 0 / 2 |

`--json` on `run`/`resume`/`approve`/`reject`: JSONL events on stdout, the summary object
(`run_id status exit_code reason outputs usage cost_usd cost_source run_dir workspace pause`) as
the **last** stdout line (`… --json | tail -1 | jq .exit_code`); Rich lines go to stderr. `--json`
does not imply `--no-interactive`. Every `<run>` accepts a unique id prefix. Commands that read a
project take `--root DIR`. `resume` refuses a run whose workflow file changed (`--force` re-runs
the steps whose fingerprint changed), a run with a live pid, and a succeeded or cancelled run.

## Stub file (`--stubs`, YAML; `--stubs-init` scaffolds it)

```yaml
defaults: { latency_ms: 0, usage: { input: 1200, output: 300 } }
steps:                                  # key = step path or glob (build[*]/review, block/step)
  assess: { output: { verdict: fix, reason: "repro present" } }     # dict -> structured output
  "build[*]/implement": { text: "Implemented; committed.",
                          events: [ {tool_call: {name: Bash, input: {cmd: "pytest -q"}}}, {tool_result: {text: "3 passed"}} ] }
  "build[*]/review": { sequence: ["Fix the flaky test", "BUILD-CLEAN"] }   # n-th call; last repeats
  pr: { fail: { kind: api, message: "simulated 529", transient: true, times: 1 }, text: "ok" }
match:                                  # after steps: first prompt regex that matches
  - { prompt_regex: "Is this real", output: { verdict: skip, reason: "dup" } }
```

Resolution: exact path → first matching glob (declaration order) → `match[]` → default
(`"[stub] " + prompt[:80]`, or a minimal `output_schema` instance). `sequence` advances per
matched entry (a glob sees every loop iteration) — that is how a loop converges in a dry run.
`--stubs` without `--dry-run` is allowed only when every prompt agent is `provider: stub`
(a real run with scripted answers; `resume`/`approve`/`reject` reuse the recorded stubs path,
`rayspec resume --stubs PATH` overrides).

## Providers, capabilities, cost

- Claude: all tool groups, `max_turns`, `budget_usd`, `thinking`, raw `claude:<Name>` tools,
  reports cost itself (`$0.12`). Codex: tools only `deny: [web]`; `max_turns`/`budget_usd`/
  `thinking`/other tools → validation error (`unsupported: agents.x.max_turns …`); no USD cost —
  add `pricing:` to `config.yaml` for estimates (`~$0.12`), else tokens only. Stub: everything.
- An unsupported field is a `rayspec validate` error; `--allow-unsupported` or
  `defaults.on_unsupported: warn` downgrades it to a warning (Codex then ignores
  `max_turns`/`budget_usd`/`thinking`; an unsupported `tools` entry still fails at run time).
- Structured output (`output_schema`) is native on both: keep schemas to `type`/`properties`/
  `required`/`enum` (Codex strict mode rejects `format`, `pattern`, `minimum`, …).
- `rayspec doctor` shows SDKs, bundled CLIs and auth (`claude` login / `ANTHROPIC_API_KEY`;
  `codex login` / `OPENAI_API_KEY`); `--probe` runs one real turn per provider. Keys live in
  `~/.rayspec/.env` or `.rayspec/.env` (the project file is applied only by `run`/`resume`/
  `approve`/`reject`). `--dry-run` needs no login.
- Costs: `rayspec run` footer, `runs`, `show` print `$` (provider-reported), `~$` (pricing table),
  `≥$` (partially priced); `defaults.budget_usd`/`max_tokens` stop a run that overshoots.

## Pitfalls and conventions

- `--dry-run` creates no worktree and skips shell/python (unless `--exec-shell`); it cannot
  prove a shell step works — run the command yourself before a real run if in doubt.
- A run in progress holds a per-workdir lock. A `running` record whose process died (crash)
  resumes normally (`rayspec resume <run>` detects the dead pid); `rayspec cancel <run> --mark`
  marks it cancelled instead (a cancelled run resumes only with `--force`).

## References (read on demand — same directory)

- `references/cli.md` — every command, flag, `--json` shape and exit code (read before using a
  flag not shown above).
- `references/providers.md` — the neutral adapter, the capability matrix, Claude/Codex option
  mapping, access levels and tools, the stub file format, tiers/aliases, pricing, auth.
- `references/runs-and-resume.md` — the run directory layout, `run.json`, events, the reuse
  cache, resume rules and the approval flow.
- `references/testing.md` — `rayspec test`, the case format, `--junit` in CI, the golden corpus.
- `references/policy.md` — `policy.yaml` and its layers, approval classes, trusted workflows,
  the worktree change guard, and what is only advisory.
- `references/isolation.md` — worktrees, `--repo`, registered projects, locks.
- `references/ci.md` — rayspec in CI: the dry-run check, `--locked` under CI, releases.
- The YAML itself — `concepts.md`, `schema.md`, `templating.md`, `examples.md` — is in the
  **`rayspec-workflows`** skill. Load it before editing a workflow.
- Online only: `extending.md` (plugins and the provider seam), `constitution.md` (why the DSL
  refuses fields), `agent-skill.md` (these two skills) at
  https://github.com/rayspec-labs/rayspec-py/blob/main/docs/.
