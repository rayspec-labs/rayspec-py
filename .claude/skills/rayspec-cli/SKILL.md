---
name: rayspec-cli
description: Operate rayspec from the command line — run, inspect, resume, approve, debug, test, audit and govern agent workflows (Claude Agent SDK + OpenAI Codex SDK). Every command, flag, --json shape and exit code, plus dry runs, the stub, test-case and policy files, providers, cost, and the run records a .rayspec/ project leaves behind. Also how to turn a plain request that names no workflow — "fix issue 42", "review PR 118 from a security angle", "is this PR broken?" — into the right rayspec run line — discover with rayspec workflows --json, fill the inputs, ask when one is missing, dry-run before a first real run. Use when asked to validate, plan, run, resume, cancel, cost or troubleshoot a workflow, to get a review, fix, check or survey done with rayspec, to write a test case or a policy for one, or to install these skills. This skill does not explain the YAML — writing or editing it is the companion rayspec-workflows skill.
---

# rayspec CLI — running, inspecting and governing

**Companion skill.** Every question about the *YAML* — which step kinds exist, what a field means,
how templating, scoping and `include:` work — is answered by **`rayspec-workflows`**. Load it
before editing a workflow file; never infer a field from a flag. This skill is the other half:
what to type, what comes back, and what it costs.

**Before you spend anything, read [Safety](#safety) below.** `rayspec run` executes real agents
with real credentials against a real checkout.

## Mental model (a run, its directory, its lock)

- A **run** is one execution with frozen inputs: id `YYYYMMDD-HHMMSS-xxxx`, directory
  `$RAYSPEC_HOME/projects/<slug>/runs/<run-id>/`. `RAYSPEC_HOME` defaults to `~/.rayspec`; your
  checkout stays clean.

  ```
  run.json            the whole record, rewritten atomically after every step
  events.jsonl        lifecycle events, one JSON object per line
  audit.jsonl         only with RAYSPEC_AUDIT_LOG=1 (`rayspec audit` works without it)
  steps/<path>/       output.txt|output.json · prompt.txt · stream.jsonl · stdout.log · context.json
  artifacts/<step>/   files a step declared under artifacts:
  ```

- **Step paths are record paths**: `facts`, `build[2]/review` (loop/`each` bodies are indexed),
  `block/step` (include bodies). Use them for `--step`, `explain`, stub keys and `expect:`.
- **Worktree by default.** In a git checkout each run gets `git worktree add` on branch
  `rayspec/<workflow>-<shortid>` under `$RAYSPEC_HOME/projects/<slug>/worktrees/`; steps run
  there (`run.workdir`), while workflows load from your checkout. `isolation: none` or
  `--no-worktree` runs in place; a non-git directory always runs in place. **`--dry-run` forces
  isolation `none`** and creates no worktree — **unless `--exec-shell`**, which restores worktree
  isolation because your `shell:`/`python:` steps then really run, and leaves the worktree and
  branch behind afterwards (`rayspec worktrees clean`).
- **One run per working directory.** A live run holds a lock on its workdir; a second run or a
  resume in the same directory is exit 2 (`… is already locked by run <id> (pid <n>)`). A paused
  run holds no lock. Fresh `rayspec run`s never collide — each gets its own worktree.
- **Project discovery**: walk up from the cwd to the first `.rayspec/`, then `.git`, else the cwd.
  `--root DIR` names it explicitly and must be an existing directory (exit 2 otherwise).
  `~/.rayspec/config.yaml` and `<project>/.rayspec/config.yaml` merge, project wins per key.
- **`.env`**: `~/.rayspec/.env` is loaded by the project and run commands. The *project*
  `<project>/.rayspec/.env` is a credential surface whoever pushed the checkout controls, so
  **only `run`, `resume`, `approve`, `reject` apply it** — they print
  `env: loaded N variables from .rayspec/.env (project): NAME, …` on stderr. `RAYSPEC_ACTOR` in
  either file is refused with a warning (a workflow step could write that file).
- **Every `<run>` argument takes a unique id prefix** and is found in *any* project under
  `RAYSPEC_HOME` — the cwd does not scope it. An ambiguous prefix is exit 2 and lists candidates.

## CLI quick reference

The four commands that *create or describe authoring artifacts* — `rayspec init`,
`new workflow`, `new agent`, `schema` — live in the **`rayspec-workflows`** skill. Everything
that executes, inspects or governs a run is here. `rayspec --version` / `-V` and `rayspec <cmd>
--help` always work; a click usage error (unknown command, bad flag, bad enum) is exit 2. **Key
flags is every flag**, not a selection — `--help` aside, a flag missing from a row does not exist.

| Command | Purpose | Key flags | Exit |
|---|---|---|---|
| `rayspec quickstart` | first run on a fresh machine: check the environment, offer a login and `git init`, scaffold a project, and prove it with a free dry run. Never overwrites, never asks without a terminal, never spawns a login without one | `--provider claude\|codex\|both\|none`, `--yes`, `--no-interactive`, `--kind code\|content`, `--no-init`, `--no-run`, `--no-skill`, `--json`, `--output`, `--root` | 0 / 1 / 2 / 130 |
| `rayspec version` | print the rayspec version (same as the root `-V`) | — | 0 |
| `rayspec doctor` | environment: python, home, config, project, git/uv, SDKs, bundled CLIs, auth, pricing rows | `--probe` (one real turn per provider), `--provider ID`, `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec providers` · `plugins` | registered providers + the declared capability matrix · installed plugins (commands, providers, stores, sinks, approvals) and the registered ids | `--json`, `--output` | 0 / 2 |
| `rayspec workflows` · `agents` | discovered workflows — project, user and the bundled library, `source: overridden` when a file of yours shadows a bundled one (an unparseable one is still listed, with a parse-error note, exit 0) · named agent files with the provider/model/effort they resolve to | `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec workflows eject <name>` | copy a bundled workflow to `.rayspec/workflows/<name>.yaml` (under a version+digest header) so this project can edit it; the copy then takes precedence | `--force`, `--root` | 0 / 2 |
| `rayspec completion <shell>` | print a shell-completion script to source; shell is `bash\|zsh\|fish` | `--values workflows\|runs`, `--root` | 0 / 2 |
| `rayspec validate [names…]` | schema, graph, references, templates, provider capabilities, policy, trust | `--allow-unsupported`, `--locked`/`--no-locked`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec plan <wf>` | three views, one at a time: the plan (inputs, resolved agents, step order, capability report), `--render` (the rendered prompt/script bodies), `--risk` (what the run would be *allowed* to do) | `--input k=v`, `--inputs-file`, `--render`, `--step PATH`, `--stubs FILE`, `--risk`, `--allow-unsupported`, `--locked`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec run <wf>` | run a workflow — **spends money and needs credentials unless `--dry-run`** | `--input k=v`, `--inputs-file`, `--dry-run`, `--stubs FILE`, `--stubs-from RUN`, `--stubs-init FILE`, `--exec-shell`, `--yes`, `--approve-class NAME`, `--no-interactive`, `--fail-fast`, `--allow-unsupported`, `--worktree`/`--no-worktree`, `--base REF`, `--repo NAME`, `--resume ID`, `--force`, `--wait-slot 30m`, `--locked`, `--detach`, `--json`, `--output`, `--quiet`, `--verbose`, `--root` | 0 1 2 3 4 130 |
| `rayspec test [wf]` | run the project's declarative test cases: every case is a dry run against the stub provider | `--case ID`, `--select`/`-k`, `--exec-shell`, `--junit FILE`, `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec runs` | list runs, newest first (of this project; `--all` for every project) | `--all`, `--limit N`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec runs stubs <run>` | write a stub script *from a stored run* — YAML to stdout, or to a file with `-o` | `--output PATH` (`-o`), `--force`, `--redact` (always refused), `--root` | 0 / 2 |
| `rayspec runs diff <a> <b>` | compare two runs of one workflow: status, timing, cost, steps, outputs | `--exit-code`, `--outputs`, `--steps`, `--across-projects`, `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec show <run>` | the whole record: header, workspace, toolchain, step table, artifacts, warnings, outputs, pause block | `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec logs <run>` | the event log; `--step` prints that step's transcript instead | `--step PATH`, `--follow`/`-f`, `--exit-code`, `--stream`, `--verbose`, `--raw`, `--json`, `--output`, `--root` | 0 / 2 / 130 (`--follow --exit-code`: 0 / 1 / 3 / 4) |
| `rayspec explain <run> <step>` | **the debugging command**: status/skip reason, the join decision per `needs`, `when:` with every operand's value, retries, the resolved agent, the rendered env and prompt/script | `--full`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec eval <run> <expr>` | evaluate a Jinja expression in a stored run's context (nothing runs, nothing is written) | `--step PATH`, `--shell`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec audit <run>` | what the run did: commands, tool calls, files, warnings, approvals, and who launched it | `--commands`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec costs` | sum this project's runs by workflow: tokens, cost and where each cost came from | `--since 7d`, `--workflow NAME`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec resume <run>` | resume a paused/failed/interrupted run; succeeded steps are replayed, not re-executed | `--force`, `--yes`, `--approve-class NAME`, `--no-interactive`, `--input k=v`, `--stubs FILE`, `--fail-fast`, `--wait-slot 30m`, `--locked`, `--json`, `--output`, `--quiet`, `--verbose`, `--root` | run's code / 2 |
| `rayspec approve <run> [comment]` · `reject <run> [reason]` | decide the pending gate and continue the run **in this process** (real agents, real money) | `--force`, `--input k=v`, `--stubs FILE`, `--wait-slot 30m`, `--locked`, `--quiet`, `--json`, `--output`, `--root` | run's code / 2 |
| `rayspec cancel <run>` | cooperatively cancel a live run (`--now` to SIGINT it), or mark a paused/stale one cancelled | `--yes`, `--now`, `--mark`, `--force`, `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec lock [names…]` | pin every agent's literal model id and effort to `.rayspec/rayspec.lock` | `--check`, `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec trust list` · `check [names…]` | what `.rayspec/trusted.yaml` holds and whether each entry still matches its hash · exit 0 only when every named workflow is trusted **now** | `--json`, `--output`, `--root` | 0 / 1 / 2 |
| `rayspec trust add <wf…>` · `remove <wf…>` | record / drop a workflow's resolved hash (the hash covers includes, agent files and prompt files) | `--root` | 0 / 2 |
| `rayspec worktrees list` | rayspec worktrees of the project: branch, age, dirty/merged/locked | `--repo NAME`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec worktrees clean` | **destructive**: `git worktree remove` + `git branch -D`. Safe by default — only merged, clean, unlocked ones go; the rest are listed as skipped with a reason | `--older-than 7d`, `--merged`, `--merged-into REF`, `--force`, `--dry-run`, `--repo NAME`, `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec projects list` | the project names usable as `--repo <name>` | `--json`, `--output` | 0 / 2 |
| `rayspec projects add <name> <src>` · `remove <name>` | register (or update) a name for a checkout path or git URL · unregister it. Both write `~/.rayspec/config.yaml`; `remove` deletes no files | `--base REF` | 0 / 2 |
| `rayspec skill install [name]` | write both packaged skills into `<project>/.claude/skills/`; a name installs just that one, `--global` targets `~/.claude/skills/` | `--global`, `--force`, `--root` | 0 / 2 |
| `rayspec skill show [name]` | packaged version, digest and path, plus the state of each installed copy (`missing\|current\|stale` — `stale` means edited or extra files, not an old version) | `--json`, `--output`, `--root` | 0 / 2 |
| `rayspec skill path [name]` | print the packaged skill directories (each holds `SKILL.md` and `references/`) | — | 0 |

## Exit codes and the `--json` contract

`0` succeeded · `1` failed · `2` usage/validation error · `3` paused · `4` cancelled ·
`130` interrupted. **No rayspec command ends in a traceback**: anything unhandled becomes
`error: <message>` (plus an optional `hint:`) on stderr with exit 2.

| Code | From a run | What to do |
|---|---|---|
| `0` | succeeded | read `outputs` from the summary object, or `rayspec show <run>` |
| `1` | a step failed | `rayspec show <run>` → find the failed path → `rayspec explain <run> <path>` → `rayspec logs <run> --step <path>`; fix, then `rayspec resume <run>` |
| `2` | nothing ran | read the `error:`/`hint:` lines and fix the command or the file. Never retry unchanged |
| `3` | paused | `pause.reason` decides: `approval` → ask the human, then `approve`/`reject`; `budget`/`failures` → an operator ceiling (see [Governance](#governance-and-trust)) |
| `4` | cancelled | a `stop:` step or a rejected gate — a decision, not a defect. `resume` refuses it without `--force` |
| `130` | interrupted | resumes cleanly: `rayspec resume <run>` |

`1` means something different per command and never "the run failed": `doctor` = a required
environment check failed · `lock --check` = lockfile drift (**no lockfile at all counts as
drift**) · `trust check` = something drifted or is untrusted · `test` = at least one case failed ·
`runs diff --exit-code` = something changed · `cancel` = you declined the confirmation.

`--json` is identical to `--output json`; passing both with different values is exit 2. Every
command that takes it is in exactly one of these three shapes.

| Shape | Commands | Read it with |
|---|---|---|
| **JSONL stream** | `run` · `resume` · `approve` · `reject` | `… --json \| tail -1 \| jq .exit_code` — the **last stdout line is the summary object** `{run_id, status, exit_code, reason, outputs, usage, cost_usd, cost_source, run_dir, workspace, pause}`; the lines before it are `run.started`, `step.started`, `step.finished`, `loop.iteration`, `warning`, `run.decision`, `run.finished` and one `{"type":"stream", "step_path":…, "record":{…}}` per transcript record. **One exception**: `resume`'s approval short-circuit (below) writes no stream and no summary at all — read its exit code |
| **stored JSONL, verbatim** | `logs` | events exactly as they sit in `events.jsonl`; with `--stream`, step records under the same `{"type":"stream",…}` wrapper |
| **one JSON document** | array — `runs` · `validate` · `workflows` · `agents` · `providers` · `trust list` · `trust check` · `worktrees list` · `projects list`; object — `plan` · `show` · `explain` · `eval` · `audit` · `costs` · `test` · `doctor` · `lock` · `runs diff` · `cancel` · `worktrees clean` · `skill show` · `plugins` · `quickstart` | `… --json \| jq` — indented on a terminal, compact when redirected, keys in the payload's own order |

- Under `--json`, **every stdout line parses as JSON** — warnings arrive as `warning` records
  in the stream, not as prose. The `policy:` line, the `env: loaded …` line and any
  `error:`/`hint:` go to stderr. (In text mode the human rendering is on stdout instead.)
- Input/validation failures still print JSON first, then exit 2 — `run` prints
  `{"error":"input errors","errors":[…]}`; `plan` prints the whole plan document and *then* exits 2.
- **`--json` does not imply `--no-interactive`.** On a TTY a gate still prompts. In a script pass
  `--no-interactive` (pause at gates, exit 3) or `--yes` explicitly.
- No `--json` at all on `version`, `completion`, `schema`, `skill path`, `trust add`/`remove`,
  `projects add`/`remove`, `skill install`, `workflows eject`, and `runs stubs` — whose `-o/--output PATH` predates
  the flag and means "write the script to this file".

## Selecting a workflow from a request

Most requests name a job, not a workflow — "fix issue 42", "is PR 118 broken?". You are the
router (rayspec has none), and you route from what is installed, never from memory:

1. **Discover**: `rayspec workflows --json` (read-only). That array is the whole menu. A
   `project` or `overridden` row beats a `bundled` one of the same name; `description` says what
   a workflow is for *and not for*; `inputs` says what it needs (`required`, `default`, `enum`,
   `type`). A workflow added yesterday is in the list; a list you remember is stale.
2. **Discriminate** on what the request asks for, not on the words it uses: *measure* (run the
   tests on both sides, classify the delta) vs *judge* (read the diff, give an opinion); a
   *change* (edit, commit, open a PR or an issue) vs a *report* (a document, nothing written);
   one review vs a *panel* of named angles (`lenses`). "From a security and performance angle"
   is a panel, "was it already broken" is a measurement, "how is this code layered" is a report.
3. **Fill the inputs** from the request and from facts you can check for free —
   `git branch --show-current`, `gh pr status`, `gh pr view <n>` — never from a guess. Numbers
   as given (`--input pr=118`), arrays as JSON (`--input lenses='["security","performance"]'`),
   `enum` values verbatim; leave a defaulted input alone unless the request overrides it.
4. **Ask instead of guessing** when a `required` input has no source in the request or the
   checkout, when two workflows fit, or when the repository, PR or issue is ambiguous. Show the
   command with the gap marked (`--input issue=<n>`); a `create_issue` filed in the wrong
   repository is a public mistake.
5. **Then `rayspec plan <wf> --input … --json`** — free, and it settles who presses enter:
   - every `agents[].access` is `read-only` and neither the description nor any input
     description names a side effect (post, push, create, commit) →
     **state the command, then run it** (it still spends money — say so);
   - anything else → **print the command and wait** for the human. A `post: true` comment, a
     PR, an issue, a commit are all "anything else";
   - `isolation` is `worktree`, an agent is `workspace-write` or `full`, and `rayspec runs --json`
     lists no run with that `workflow` in this project → **propose `--dry-run` first**: free,
     offline, and it shows the shape of the run before it costs anything.

| Request | Command |
|---|---|
| review PR 118 from a security and performance angle | `rayspec run review_panel --input pr=118 --input lenses='["security","performance"]'` — read-only unless `post`: run it |
| is PR 118 broken, or was it already broken? | `rayspec run validate_pr --input pr=118` — measures in a worktree, read-only judge: run it |
| fix issue 42 | `rayspec run fix_issue --input issue=42` — writes and opens a PR: propose; first time here, `--dry-run` first |
| how is this codebase layered? | `rayspec run architect --input focus=layering` — a report, nothing written: run it |
| implement docs/prd-09.md as a pull request | `rayspec run prd_to_pr --input prd=docs/prd-09.md --input typecheck_command='mypy src'` — writes tests and code, pushes, opens a PR: propose; first time here, `--dry-run` first |
| fix the failing issue | which issue? ask, showing `rayspec run fix_issue --input issue=<n>` |

## Operating loops

**1 · Check before you spend.** Every step is free; do them in order and stop at the first failure.

```bash
rayspec validate                                     # schema, graph, refs, capabilities, policy
rayspec plan fix_issue -i issue=42                   # inputs, resolved agents+models, step order
rayspec plan fix_issue -i issue=42 --render          # the prompts as the agent will see them
rayspec plan fix_issue -i issue=42 --risk            # what the run would be ALLOWED to do
rayspec run fix_issue -i issue=42 --dry-run --stubs-init stubs.yaml   # scaffold, then edit it
rayspec run fix_issue -i issue=42 --dry-run --stubs stubs.yaml        # the graph, end to end
rayspec run fix_issue -i issue=42                    # only now: real agents, real money
```

`--render` accepts `--step PATH` for one step and `--stubs FILE` to supply upstream outputs
instead of placeholders. Upstream values it has no stub for are *text* placeholders, so a step
that reads a **field** of another step's output (`{{ steps.block.output.summary }}`) renders as an
inline `error:` line while the run itself is fine — and `--stubs` does not cure it for a `loop:`,
`each:` or `include:` step, which no stub script can answer. That error is a limit of the preview,
not a failure: the exit code stays 0, and a `--dry-run` is what proves such a step. `--risk` is
advisory and never changes the exit code; `--render` and `--risk` are different views and are
refused together.

**2 · The offline test loop** (`rayspec test`) — no credentials, no network, no money; it *does*
write run records under `RAYSPEC_HOME` (deleted for a passing case, kept for a failing one, whose
id and directory are printed). Cases live in `.rayspec/tests/<workflow>/<case>.yaml` (the
directory names the workflow, the file stem names the case) or in a root `checks.yaml`.

```yaml
# .rayspec/tests/fix_issue/converges.yaml   — run it: rayspec test fix_issue -k converges
inputs: { issue: "42" }
stubs: stubs/loop.yaml          # relative to THIS file; a stubs/ subdir is never read as a case
expect:                         # everything you leave out is not checked
  status: succeeded
  exit_code: 0
  outputs: { iterations: 2 }    # a SUBSET of the workflow outputs
  steps:
    "build[2]/review": { status: succeeded, output_regex: "BUILD-CLEAN" }
    publish: succeeded
```

`--junit FILE` for CI, `--json` for one object with every case. A case may **not** widen the shell
rule: `exec_shell: true` in a case is exit 2 unless the operator also passes `--exec-shell`.

**3 · Record and replay.** Turn a real run into a stub script, then re-drive the graph for free:

```bash
rayspec runs stubs 20260823-0031 -o stubs/recorded.yaml   # or omit -o to read it on stdout
rayspec run fix_issue -i issue=42 --dry-run --stubs stubs/recorded.yaml
rayspec run fix_issue -i issue=42 --dry-run --stubs-from 20260823-0031   # same, no file
```

Iterations that differed become a `sequence:` under the glob key; `each` items keep indexed keys.
A run launched with `secret: true` inputs is **refused** (exit 2, naming them) — its prompts may
quote the secret. `--redact` is refused permanently, not unimplemented.

**4 · Debug a run** — work outside-in; each command is read-only.

```bash
rayspec show    20260823-0031                  # which step, which status, which pause
rayspec explain 20260823-0031 'build[1]/implement'   # WHY that step ran/skipped/failed
rayspec logs    20260823-0031 --step 'build[1]/implement'   # the transcript: tools, results, answer
rayspec eval    20260823-0031 'steps.facts.output' --step 'build[2]/review'   # what a template saw
rayspec audit   20260823-0031 --commands       # every command + every shell/python ROW inside an execution
rayspec runs diff <good> <bad> --outputs       # what changed between two runs of one workflow
rayspec logs    20260823-0031 --follow         # tail a live run (Ctrl-C = 130)
```

`explain` is the one to reach for first: it re-renders the prompt and the `when:` operands from
the stored context, so it distinguishes "the template was wrong" from "the agent was wrong". A
step with no record is still explained, with a warning that the sections were re-evaluated.
`eval` warns when an expression reads `env.*`, because that reads *this* process's environment.

One carve-out on `eval`: it rebuilds the context from `run.json`, and an `each:` step's
`.items` (the per-item `[{index, item, status, output, error}]` list) is **not persisted**, so
`steps.<each>.items` is refused with *"is not set for this each step"* even though the run
rendered it correctly. That message is about the store, not about your workflow. Read the list
from the transcript of the step that consumed it (`rayspec logs <run> --step <consumer>`), or
read the per-item step records themselves — `rayspec show <run>` lists `fan[0]/emit`,
`fan[1]/emit`, … with their status and output. `.output`, `.status` and `.ok` of the same `each`
step evaluate normally.

**5 · An interrupted run.** Look at the status first (`rayspec runs -n 5`).

| Status | Meaning | Continue with |
|---|---|---|
| `paused` | an approval gate, or an operator ceiling | `approve` / `reject`, or `resume` |
| `failed` | a step failed | fix, then `resume` (unchanged steps are replayed) |
| `interrupted` | Ctrl-C, SIGTERM or `cancel --yes` | `resume` — nothing extra needed |
| `running` but the process is gone | a crash | `resume` — the dead pid is detected. `cancel --mark` if you want it closed instead |
| `cancelled` / `succeeded` | a decision already made | `resume --force`, and only deliberately |

Rules that bite: `resume` **refuses a workflow whose hash changed**
(`error: workflow 'x' changed since run <id> (hash a → b)`; `--force` re-runs the steps whose
fingerprint changed and reuses the rest); a run whose process is still alive is refused because
its workdir is still locked; every `secret: true` input must be supplied again (`--input
name=value` or `RAYSPEC_INPUT_<NAME>`) while all other inputs are fixed per run; a `--stubs` path
recorded at launch is reused automatically.
And the short-circuit to know: `resume` on a run paused at an **approval** gate, non-interactive
and without `--yes`/`--approve-class`, prints the decide hint and exits 3 **without restarting the
engine** — and under `--json` that path writes **nothing at all** to stdout (the hint is on
stderr), so on `resume` read the process exit code, never `… | tail -1 | jq`. An operational pause
is not short-circuited — `resume` re-evaluates the ceiling.

## Safety

**Ask the human before any run that writes** — edits files (in a worktree or not), commits,
pushes, opens a PR or an issue, posts a comment, calls a webhook — and, for such a workflow, before
its first real run in a project that has never run it (propose `--dry-run` first; the rule is in
[Selecting a workflow](#selecting-a-workflow-from-a-request)). A workflow whose every agent is
`read-only` and whose description names no side effect is the one thing you may run unasked — a
survey, a review, a measurement — after stating the command, because it still spends money. To
find out which kind you have, read the workflow and then run `rayspec plan <wf> --json` (the
`isolation`, and the `access` level, provider and model of every agent) and `rayspec plan <wf>
--risk` (what the run would be *allowed* to do, agent by agent and step by step). A **dry run
proves the graph, not the blast radius** — it replaces every agent with the stub and skips every
`shell:`/`python:` step, so a clean dry run says nothing about whether a real run writes, pushes
or spends.

Every command of this skill's table is in exactly one of these three classes.

| Class | Commands | What it costs you |
|---|---|---|
| **read-only** | `version` · `doctor` · `providers` · `plugins` · `workflows` · `agents` · `completion` · `validate` · `plan` · `runs` · `runs diff` · `show` · `logs` · `explain` · `eval` · `audit` · `costs` · `trust list` · `trust check` · `worktrees list` · `projects list` · `skill show` · `skill path` | nothing: no credentials, no network, no writes. Safe unattended. The one exception is `doctor --probe`, which runs a real one-turn healthcheck per provider and therefore needs a login and costs a little |
| **writes locally** | `quickstart` · `lock` · `trust add` · `trust remove` · `projects add` · `projects remove` · `skill install` · `worktrees clean` · `runs stubs` · `test` · `cancel` · `workflows eject` | files and records, never money. `workflows eject` writes one file under `.rayspec/workflows/` and refuses an existing one without `--force`; `worktrees clean` is destructive (`git worktree remove` + `git branch -D`); `runs stubs` writes only where `-o` points; `test` creates run records under `RAYSPEC_HOME` (kept only for a failing case); `quickstart` scaffolds files that do not exist yet, may run git init after you say so, may hand the terminal to a provider's own login command, and ends in a dry run — no credentials are read or written, and nothing is overwritten; `cancel` rewrites a run record and, on a **live** run, signals the process — both `--yes` and `--json` waive the confirmation that guards that |
| **executes agents** | `run` · `resume` · `approve` · `reject` | money, credentials and your checkout. `approve`/`reject` are not bookkeeping — they resume the run **in this process**, and a gate with `on_reject: continue` keeps spending after a rejection. Only `run --dry-run` is exempt |

- **`--dry-run` is free**: every provider is replaced by the stub, gates are auto-approved
  (except a class the policy protects — see below), isolation is forced to `none` (`--exec-shell`
  restores the worktree — last clause of this bullet), no host slot is taken, no login is needed.
  What it does **not** prove: `shell:` and `python:` steps are *skipped* — they are recorded
  `succeeded` with a **stand-in output**: `""`, or the *minimal instance* of their `output_schema`
  when they declare one (`{type: boolean}` → `false`, not the value the script would have
  produced). A downstream `when:` therefore reads a placeholder, so a branch gated on a script
  always takes the same side, and `runs diff` against a real run flags exactly those steps as
  changed. Add `--exec-shell` to really run them — they then execute for real, in a fresh git
  worktree on branch `rayspec/<workflow>-<shortid>`, **not** in your checkout, so it is no longer
  free of side effects and leaves a worktree behind (`rayspec worktrees clean`) — or run the
  command yourself first.
- **`--yes` waives the interactive prompt at `approve:` gates, and nothing else.** It cannot waive
  an approval class the operator marked `allow_yes: false` (that gate pauses anyway, with a
  warning naming the class), a `--locked` lockfile mismatch, a policy denial, a trust requirement,
  a budget ceiling, or the workflow's own `stop:`. `--approve-class NAME` is the narrower tool:
  it pre-approves gates of that class only, and every other gate still pauses.
- **Never pass a secret as a plain `--input`.** A non-secret value is persisted verbatim in
  `run.json`. Declare the input `secret: true` (then `run.json` stores `"<secret>"` and
  `secret_inputs` names it) and pass it as `RAYSPEC_INPUT_<NAME>`, or better, give the agent a
  tool rather than the credential.
- Give an unattended experiment its **own `RAYSPEC_HOME`** — every run record, worktree and lock
  lives under it, so a scratch home is a clean slate you can delete.
- `cancel` on a **live** run asks for confirmation first; without a terminal that is exit 2, so an
  unattended caller must pass `--yes` — **or `--json`, which waives it too**, because a machine
  caller cannot answer a prompt. Do not reach for `--json` here out of habit: on this one command
  an output-format flag arms the kill.

## Governance and trust

Guardrails are files on this machine, checked **at load time** — a violation is an error with a
file and a line before a token is spent, and `run`, `plan`, `validate`, `test`, `resume`,
`approve` and `reject` all refuse the same workflow. Three layers combine most-restrictive-wins:
`$RAYSPEC_POLICY`, `<project>/.rayspec/policy.yaml`, `$RAYSPEC_HOME/policy.yaml`. Every command
that reads them prints which layers are in force, or `policy: none in force (searched …)` — read
that line, because a policy one directory too high looks exactly like a policy being obeyed. A
missing file is simply an absent layer, except `$RAYSPEC_POLICY`, which was named explicitly and
is an error when it does not exist.

```yaml
# .rayspec/policy.yaml  — restrictive keys only; see references/policy.md for all of them
providers: { allow: [claude, codex] }
models:    { deny: ["*opus*"] }
access:    { max: workspace-write }
tools:     { deny: [web] }
workspace: { protected_paths: [".github/**"], max_changed_files: 40 }
trust:     { require: true }            # only workflows in .rayspec/trusted.yaml may run
approvals:
  classes:
    publish: { allow_yes: false, require_tty: true }
budget:    { per_run: 2.00, per_day: 20.00 }
max_consecutive_failures: 3
max_concurrent_runs: { claude: 2 }      # host run slots
```

**When you hit one, do not route around it.** Report the rule and the line to the human.

- *An approval class.* `--yes`, `--approve-class` and even `--dry-run` cannot approve a class
  with `allow_yes: false` — the gate pauses with a warning naming the class and the flag that did
  not apply. With `require_tty: true` a recorded `rayspec approve` is refused too and the run
  stays paused (exit 3); someone must answer at a terminal. A **rejection** is never blocked, so
  `rayspec reject <run>` always works.
- *A ceiling.* `budget.per_run|per_day|per_month` drains the run and then **pauses** it (exit 3,
  `pause.reason: budget`); `max_consecutive_failures` counts failed *runs* and is checked before
  the first step, so a workflow that has been failing all night stops calling the provider at all
  (`pause.reason: failures`). Both pause rather than fail, because an operator's envelope is not
  a defect in the workflow. `rayspec resume` re-checks the ceiling and pauses again; `rayspec
  approve <run>` **waives the one control that stopped this run**, and its spend is still counted.
  Waiving is a spending decision — ask first.
- *A host run slot* (`max_concurrent_runs`). Exit 2, naming the run holding it. `--wait-slot 30m`
  (or `forever`) queues instead of failing.
- *Trust.* `rayspec trust add <wf>` records the workflow's resolved hash — which covers every
  `include:`d body, agent file and prompt file, so editing any of them revokes trust.
  `rayspec trust check` is the gate to put in front of a scheduled `rayspec run`: exit 0 only when
  everything is trusted at its current hash, exit 1 for drift, for untrusted, and for a workflow
  that no longer loads (reported as untrusted, never as a crash). On its own the trust list blocks
  nothing — `trust.require: true` in a policy is what makes it binding.
- *Pinned models.* `rayspec lock` writes `.rayspec/rayspec.lock`; `--locked` refuses to proceed
  when an agent resolves differently (default: **on under CI**, off otherwise). `rayspec validate
  --locked` reports drift as an error row instead of refusing.
- `rayspec audit <run>` answers "what did this run actually do" from `run.json`, `events.jsonl`
  and the step streams — commands, tool calls, file changes, warnings, approvals, and the actor.
  `--commands` is "only what was executed", drawn **per row**: a `step.started` record opens an
  execution and the step's next outcome record (or the next attempt) closes it, so a step a
  resumed run skipped or replayed is out even when another attempt ran it, and a `--dry-run`
  rehearsal is empty.
  `RAYSPEC_AUDIT_LOG=1` additionally writes `audit.jsonl` into the run directory; it is
  append-only in behaviour but **not tamper-evident**, and local to one machine.

## Providers, capabilities, cost

`rayspec providers` prints the live matrix; read it before promising a workflow a feature.

- **claude**: every tool group (`read edit shell web agent mcp`), raw `claude:<Name>` tool names,
  `max_turns`, `budget_usd`, `thinking`, and it reports its own cost.
- **codex**: tools only as `deny: [web]`; **no** `max_turns`, `budget_usd`, `thinking`, raw tool
  names or cost reporting. Asking for one is a `rayspec validate` error naming the capability;
  `--allow-unsupported` (or `defaults.on_unsupported: warn`) downgrades it to a warning — after
  which `max_turns`/`budget_usd`/`thinking` are silently ignored by the adapter, while an
  unsupported `tools` entry still fails the step at run time.
- **stub**: accepts everything, so it can stand in for either after real validation.
- Structured output (`output_schema`) is enforced natively on all three; keep schemas to
  `type`/`properties`/`required`/`enum` — Codex's strict mode rejects `format`, `pattern`,
  `minimum` and friends.
- **Auth**: `claude` login or `ANTHROPIC_API_KEY`; `codex login` or `OPENAI_API_KEY`. Keys belong
  in `~/.rayspec/.env` (or the shell). `rayspec doctor` shows SDK, bundled CLI and login state per
  provider; `--probe` proves it with one real turn. `--dry-run` needs no login at all.
- **Cost markers** are literal and mean different things: a bare `$0.12` is provider-reported ·
  `~$0.12` is estimated from a `pricing:` table in `config.yaml` · `≥$0.12` is a **lower bound**
  (some step reported tokens nobody could price). A `-` in the cost column means no cost is known
  at all — normal for a dry run, and for Codex without a pricing entry. `rayspec costs --since 7d` aggregates per
  workflow and says how many runs are unpriced, partial or still in flight.
- Two ceilings, two different endings. The **workflow's own** `defaults.budget_usd` /
  `max_tokens` **fails** the run (exit 1, `budget exceeded (cost ~$0.013 > budget_usd $0.010)`):
  no new step starts, running steps finish. The **operator's** `policy.budget.*` **pauses** it
  (exit 3) instead, because an operator's envelope is not a defect in the workflow. Neither ever
  truncates a step silently.

## Stub file (`--stubs`, YAML; `--stubs-init` scaffolds one)

```yaml
defaults: { latency_ms: 0, usage: { input: 1200, output: 300 } }
steps:                                   # key = a record path or a glob over one
  assess: { output: { verdict: fix, reason: "repro present" } }      # a dict -> structured output
  "build[*]/implement":
    text: "Implemented; committed."
    events: [ { tool_call: { name: Bash, call_id: c1, data: { command: "pytest -q" } } },
              { tool_result: { call_id: c1, text: "3 passed" } } ]
    expect: { prompt_regex: "Fix|Address this review", not_contains: "{{", access: workspace-write }
  "build[*]/review": { sequence: ["Fix the flaky test", "BUILD-CLEAN"] }   # nth call; last repeats
  pr: { fail: { kind: api, message: "simulated 529", transient: true, times: 1 }, text: "ok" }
match:                                   # tried after steps: first prompt regex that matches
  - { prompt_regex: "Is this real", output: { verdict: skip, reason: "dup" } }
```

Resolution per call: exact path → first matching glob in declaration order → `match[]` → the
default (`"[stub] " + prompt[:80]`, or a minimal instance of the step's `output_schema`).
`sequence` advances per matched entry, and a glob sees every loop iteration — that is how a loop
converges in a dry run. `events` are streamed **before** the answer, so `rayspec logs --step`
reads like a real transcript. `expect:` asserts what the agent was *asked*
(`prompt_regex`, `prompt_contains`, `not_contains`, `model`, `access`, `output_schema`, `session`)
and a mismatch fails the step with `stub_expectation`; an `expect:` under a key that matches no
prompt step is refused before the run starts, so a renamed step cannot silently disarm it. Two
traps: `model:` is the **resolved** model id of the workflow's own agent, which `--dry-run` does
not replace (`stub-*` matches only an agent declared `provider: stub`; a `provider: codex` agent
still reports `gpt-…`, so assert `access:` instead when you want a stable check); and a needle
under a **glob** key is asserted on every call it matches, so it has to hold for iteration 2 of a
loop as much as for iteration 1.

`--stubs` **without** `--dry-run` is allowed only when every prompt agent is `provider: stub` —
then it is a *real* run (shell executes, worktree and locks as usual) with scripted answers;
otherwise exit 2. The absolute path is recorded in `run.json` (`stubs_path`), so
`resume`/`approve`/`reject` script the same agents again without repeating the flag.

## Pitfalls and conventions

- A dry run reports `shell:`/`python:` steps as `succeeded` without running them — output `""`,
  or the minimal instance of their `output_schema`. `runs diff` between a real run and a dry run
  will show exactly those steps as changed. Use `--exec-shell` when the shell output matters.
- `--root` must exist (exit 2 otherwise) — a mistyped path is never created. `rayspec init` and
  `new *` take `--root` as the directory *itself*, not a walk-up start.
- `runs` and `costs` outside a rayspec project are **exit 0** with a stderr notice, and mint no
  project directory. Under `--json` each keeps its own shape: `runs` prints `[]`, `costs` prints a
  zeroed document (`{"project":null,…,"runs":0,"workflows":[]}`) — never an array.
  `worktrees list|clean` are the opposite: outside a git repository they are exit 2 with a
  plain-text error **even under `--json`** — so a caller that only parses stdout sees nothing.
  `new workflow` / `new agent` in a directory without
  `.rayspec/` is exit 2 — they grow a project, they never create one.
- A listing flag placed **before** a `runs` or `workflows` subcommand is exit 2 (`--limit belongs to
  the rayspec runs listing`); only `--root` may go there. A project file that shares a name with a
  bundled workflow shadows it silently — `rayspec workflows` shows `overridden`.
- `run --resume <id>` only resumes a run of *this* workflow in *this* project. For anything else
  use `rayspec resume <id>`, which finds the run in any project and re-scopes itself to that
  project's workflow, config, lockfile, policy and pricing. `--repo` cannot be combined with
  `--resume`.
- `logs --raw` prints stored text unescaped, control characters included — debugging only;
  everything else is sanitised.
- `rayspec completion` is silent by contract: `--values` prints nothing and exits 0 on any
  failure, so never diagnose a project with it.
- A plugin may add commands but can never shadow one rayspec already provides: the registration
  is dropped (`a plugin can not shadow an existing command`) and a plugin problem becomes one
  stderr line pointing at `rayspec plugins`.
- Hints in error messages quote real, runnable commands — follow them before improvising.

## References (read on demand — same directory)

- `references/cli.md` — every command, argument, flag, `--json` shape and exit code in full. Read
  it before using a flag this page does not show, or when a `--json` field is unfamiliar.
- `references/runs-and-resume.md` — the run directory, `run.json`, the event stream, the reuse
  cache, resume rules, approval gates and classes, retries, budgets and host slots. Read it when a
  resume behaves unexpectedly or you need a field of `run.json`.
- `references/providers.md` — the neutral adapter, the full capability matrix, Claude/Codex option
  mapping, access levels and tools, the stub script in full, tiers/aliases, pricing and auth.
- `references/testing.md` — `rayspec test`: the case format, discovery, `--junit` in CI.
- `references/policy.md` — `policy.yaml`, its layers and every key, approval classes, the trust
  list, the worktree change guard, and what is only advisory. Read it whenever a run is refused.
- `references/isolation.md` — worktrees, the run branch, `--repo`, registered projects, locks,
  slugs. Read it when a workspace or a lock is in the way.
- `references/ci.md` — rayspec in CI: the dry-run check, `--locked` under CI, `trust check` as a
  gate, releases.
- The YAML itself — `concepts.md`, `schema.md`, `templating.md`, `examples.md` — ships with the
  **`rayspec-workflows`** skill. Load it before writing or editing a workflow, an agent or a
  prompt file; this skill will not tell you what a field means.
- Online only, in neither skill: `extending.md` (writing plugins and the provider seam),
  `constitution.md` (why the DSL refuses fields), `agent-skill.md` (these two skills),
  `README.md` (the docs index) — at
  https://github.com/rayspec-labs/rayspec-py/blob/main/docs/.
