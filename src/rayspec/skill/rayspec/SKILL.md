---
name: rayspec
description: Author, validate, dry-run and run rayspec agent workflows (a YAML DSL on the Claude Agent SDK / OpenAI Codex SDK). Use when asked to create or edit a rayspec workflow, .rayspec/ project files, agents, stubs, or to run, inspect, resume or debug rayspec from the CLI.
---

# rayspec — declarative agent workflows

## Mental model (read this first)

- A **workflow** is one YAML file: `.rayspec/workflows/<name>.yaml` (project) or
  `~/.rayspec/workflows/` (user). `name` = file stem = what you pass to `rayspec run <name>`.
- **YAML coordinates. Code computes. Agents judge.** The file says what runs, in what order,
  under which gates, with which agent. Computation belongs in `shell:`/`python:` steps
  (their verdicts are authoritative); judgement in `prompt:` steps. Do not put logic into
  templates that a script could do.
- `steps:` is a DAG: `needs: [ids]` names upstream siblings, everything ready runs concurrently
  (`defaults.max_parallel`, default 4). `loop:`/`each:`/`include:` carry a nested `steps:` body.
- Every succeeded step has an **output** (agent text or validated JSON with `output_schema`;
  stdout for shell/python; the approver's comment; JSON for composites). References are strict:
  a missing field, a failed/skipped producer or `null` fails the consuming step loudly.
- A **run** = one execution with fixed inputs, an id `YYYYMMDD-HHMMSS-xxxx` and a run directory
  `~/.rayspec/projects/<slug>/runs/<run-id>/` (`run.json`, `events.jsonl`, one dir per step with
  `output.*`, `stream.jsonl`, `stdout.log`, `context.json`). `RAYSPEC_HOME` overrides `~/.rayspec`.
- **Worktree by default**: in a git repo every run gets `git worktree add` on branch
  `rayspec/<workflow>-<shortid>` under `~/.rayspec/projects/<slug>/worktrees/`; steps run there
  (`run.workdir`), workflows load from your checkout. `isolation: none` / `--no-worktree` runs in
  place; non-git dirs always run in place.
- Providers: `claude`, `codex`, `stub` (scripted; what `--dry-run` uses). Each declares
  capabilities; `rayspec validate` refuses a field the resolved provider lacks.
- Exit codes: `0` succeeded · `1` failed · `2` usage/validation error · `3` paused at a gate ·
  `4` cancelled (`stop:`/rejected gate) · `130` interrupted.

## The authoring loop (follow it in this order)

1. Look before writing: `ls .rayspec/` (workflows, agents, prompts, stubs, config.yaml),
   `rayspec workflows`, `rayspec agents`, `rayspec providers`, `rayspec doctor`. No `.rayspec/`?
   `rayspec init` scaffolds one (`--kind content` for non-code projects).
2. Write the YAML (cheat-sheet below). Prompts longer than a few lines go to
   `.rayspec/prompts/<name>.md` + `prompt_file:`; reusable agents to `.rayspec/agents/<name>.yaml`.
3. `rayspec validate` — schema, graph, references, templates, provider capabilities
   (`file:line` on errors). Fix until `OK`.
4. `rayspec plan <wf> [--input k=v]` — resolved inputs, agents/models, step order, capability
   report. Check the agent/model/access column is what you intended.
5. Dry-run with scripted agents, no login needed:
   `rayspec run <wf> --dry-run --stubs-init stubs.yaml` (writes one entry per prompt step,
   keyed like the engine names records: `build[*]/review` for loop bodies), edit the answers so
   branches/loops behave (`sequence:` for loops, `output:` for `output_schema` steps), then
   `rayspec run <wf> --dry-run --stubs stubs.yaml`. Shell/python steps are skipped in a dry run
   (`''` output, or a minimal `output_schema` instance); `--exec-shell` runs them for real.
6. Only then a real run — **ask the human first** when agents may edit files, push, open PRs or
   spend money: `rayspec run <wf> --input k=v` (add `--no-worktree` only if in-place is wanted,
   `--yes` only if the human said gates may auto-approve, `--json` for machine-readable output).
7. Inspect: `rayspec runs`, `rayspec show <run>`, `rayspec logs <run>` (events),
   `rayspec logs <run> --step <path>` (one step's transcript / stdout / stderr; the failure hint
   prints the path), `rayspec audit <run>` (what it actually did: commands, tools, files,
   approvals and who ran it). Paused (exit 3): `rayspec approve <run> [comment]` / `reject <run> [reason]`
   / `resume <run>`. Failed/interrupted: fix, then `rayspec resume <run>` replays succeeded steps
   (`--force` when you edited the workflow — its hash changed). Run ids accept a unique prefix.

## YAML cheat-sheet (every step kind, annotated)

```yaml
# .rayspec/workflows/fix_issue.yaml
rayspec: 1                              # required; the only schema version
name: fix_issue                         # required identifier; discovery uses the file stem — keep them equal
description: Triage an issue, fix it in a loop, fan out checks, gate a PR.
inputs:                                 # name -> spec; keys are names (^[a-z][a-z0-9_]*$)
  issue:  { type: integer, required: true, description: "Issue number" }
  mode:   { type: string, enum: [fast, thorough], default: fast }
  labels: { type: array, items: { type: string }, default: [] }
  base:   { type: string, default: main }
defaults:
  timeout: 20m                          # per attempt for leaves; "90s", "1h30m", 90 (seconds)
  max_parallel: 4
  # budget_usd: 5 / max_tokens: "500k"  # run-level circuit breaker (root workflow only)
  on_step_failure: drain                # drain (default) | fail_fast | continue — see below
isolation: worktree                     # worktree (default) | none
agents:
  triage:                               # provider default: config.default_provider (claude)
    provider: claude
    model: small                        # literal id | tier small|medium|large | "@alias"
    access: read-only                   # read-only | workspace-write (default) | full
    instructions: Be terse and concrete. Never edit files.
    tools: { deny: [web] }              # groups: read edit shell web agent mcp; mcp:<server>[/<tool>]
  implementer:
    provider: codex
    model: medium
    effort: high                        # provider decides which values exist (see providers.md)
    access: workspace-write
    instructions_file: prompts/implementer.md   # relative to the .rayspec/ dir of this file
  reviewer:
    provider: claude
    model: large
    access: read-only
    max_turns: 40                       # claude only (capability max_turns); codex would fail validate
    provider_options: { claude: { setting_sources: [project] } }   # raw pass-through per provider
steps:
  - id: fetch                           # ids: ^[a-z][a-z0-9_]*$, unique in the whole file
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE" --json title,body   # inputs also as env vars
  - id: assess
    needs: [fetch]
    agent: triage
    prompt: |
      {{ steps.fetch.output }}
      Is this a real, well-scoped bug worth fixing now?
    output_schema:                      # the output becomes the validated JSON value
      type: object
      properties: { verdict: { enum: [fix, skip] }, reason: { type: string } }
      required: [verdict, reason]
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'       # bare expression, must be exactly true/false
    stop: { status: cancelled, reason: "not worth fixing: {{ steps.assess.output.reason }}" }
  - id: build
    needs: [assess]
    when: steps.assess.output.verdict == 'fix'
    loop:
      max_iterations: 3                 # required; exhausting it = failed unless on_exhausted: continue
      until: steps.review.output | has_signal('BUILD-CLEAN')   # evaluated over the body after each pass
      steps:
        - id: implement
          agent: implementer
          session: implement            # inside a loop: continue this step's own previous session
          prompt: |
            {% if iteration.first %}Fix: {{ steps.fetch.output }}
            {% else %}Address this review: {{ iteration.prev.review.output }}{% endif %}
        - id: check
          needs: [implement]
          shell: pytest -q
          allow_failure: true           # failed + tolerated; the reviewer sees the verdict
        - id: review
          needs: [check]
          agent: reviewer
          prompt: |
            Tests exit code {{ steps.check.exit_code }}:
            {{ steps.check.output }}
            Review the diff. Reply with a whole line BUILD-CLEAN when nothing is left to fix.
  - id: lint_all
    needs: [build]
    each: inputs.labels                 # expression -> a list (use | fromjson for JSON text)
    as: label                           # default: item
    max_parallel: 2
    on_failure: continue                # failed items become null slots in the output list
    steps:
      - id: lint
        shell: echo "checking label {{ label }} ({{ each.index }}/{{ each.total }})"
  - id: summary
    needs: [build, lint_all]
    join: always                        # run even when a sibling failed (finally step)
    python: |
      import json, os
      mode = {{ inputs.mode }}          # renders a Python literal: 'fast'
      ctx = json.load(open(os.environ["RAYSPEC_CONTEXT"]))
      print(json.dumps({"mode": mode, "iterations": ctx["steps"]["build"]["iterations"]}))
    output_schema: { type: object }
  - id: second_opinion
    needs: [build]
    include: review_block               # another workflow file, inlined at load time
    with: { target: "." }               # validated against the included workflow's inputs
  - id: confirm
    needs: [build, second_opinion]
    approve: "Open a PR for issue {{ inputs.issue }}?"    # or {message:, on_reject: cancel|continue|fail}
  - id: pr
    needs: [confirm]
    shell: |
      git push -u origin HEAD
      gh pr create --base "{{ inputs.base }}" --title "fix: #{{ inputs.issue }}" \
        --body "{{ steps.second_opinion.output.summary }}"
outputs:                                # rendered when the run succeeds; printed at the end
  verdict: "{{ steps.assess.output.verdict }}"
  iterations: "{{ steps.build.iterations }}"
  review: "{{ steps.second_opinion.output.summary }}"
```

```yaml
# .rayspec/workflows/review_block.yaml — an includable block: own inputs/outputs, closed scope
rayspec: 1
name: review_block
inputs:
  target: { type: string, default: "." }
steps:
  - id: review
    agent: { provider: claude, model: small, access: read-only }
    prompt: "Review {{ inputs.target }} and summarise the risks in three lines."
outputs:
  summary: "{{ steps.review.output }}"
```

Common fields on every step: `id`, `description`, `needs`, `when` (expression), `join`
(`all` | `any` | `always`), `timeout`, `always_run` (ignore the resume cache), `allow_failure`.
Leaf steps (`prompt`, `shell`, `python`) add `retry: {attempts, delay, on_error}` (`attempts` is
the TOTAL count, `1` = no retry; prompt default `attempts: 3, delay: 3s, on_error: transient`;
shell/python default none), `env:` (templated, str-coerced) and `output_schema`.

**Join truth table** (evaluated once every `needs` is terminal; tolerated failure = succeeded):

| needs outcome | `all` (default) | `any` | `always` |
|---|---|---|---|
| all succeeded | run | run | run |
| some skipped, rest succeeded | skip (`upstream_skipped`) | run | run |
| all skipped | skip | skip | run |
| any failed (untolerated) | skip (`upstream_failed`) | skip | run |
| run draining / cancelled | skip (`run_failed`) | skip | run |

Exception: under fail-fast every pending step is skipped `run_failed`, `join: always` included — the task group is already torn down. `always` is finally-semantics under `drain`/`continue` only.

`defaults.on_step_failure` picks what a failed step does to its siblings:
**`drain`** (default) — running steps finish, nothing new starts except `join: always` steps.
**`fail_fast`** — running siblings are cancelled at once (`--fail-fast` does this too, and the
flag may only ever *tighten*: it overrides `drain` and `continue`, never the reverse).
**`continue`** — independent branches keep being scheduled; only the failed step's downstream
cone skips. All three still end the run `failed` (exit 1) — `continue` is not `allow_failure`,
and it is a different knob from `each.on_failure: continue`, which is per *item*. The policy is
run-level and global, so it also applies inside `each:`/`loop:`/`include:` bodies.

**Inputs**: types `string` (default) `integer` `number` `boolean` `array` `object`; `required`
(cannot have a `default`), `default`, `enum`, `items`, `properties`, `description`. Supplied by
`--input name=value` > `--inputs-file f.yaml|json` > env `RAYSPEC_INPUT_<NAME>` > `default`;
text is coerced by type, arrays via JSON or repeated `--input`. Inputs are fixed per run
(`--resume` refuses `--input`). Plain inputs are persisted in clear text in the run dir and
printed by `plan`/`show` — **never pass a secret as a plain input**. For secrets use
`inputs.<name>.secret: true` (since 1.0.0):

```yaml
inputs:
  token: { type: string, secret: true }          # secret + default is a load-time error
steps:
  - id: publish
    shell: |                                      # $RAYSPEC_INPUT_TOKEN is the ONLY way in
      curl -H "Authorization: Bearer $RAYSPEC_INPUT_TOKEN" https://example/api
```

A secret value is delivered **only** as `RAYSPEC_INPUT_<NAME>` in the environment of `shell:`/
`python:` steps (and via their `env:` mappings); `{{ inputs.token }}` anywhere else — prompts,
prompt-step `env`, `when`/`until`/`each`, `outputs`, `approve`, `stop.reason`, include `with:` —
is a load-time validation error. It is never persisted (`run.json`, `context.json`, `plan`/`show`
print `<secret>`), so every resume entry (`resume`/`approve`/`reject`/`run --resume`) must
re-supply it via `--input name=value` (allowed on resume only for secret inputs) or
`RAYSPEC_INPUT_<NAME>` in the environment, else exit 2 listing the missing secret inputs.
`rayspec plan`/`validate` mark such inputs `(secret)`.

**Agents** (workflow `agents:` > `.rayspec/agents/<name>.yaml` > `~/.rayspec/agents/`): fields
`provider`, `model` (tier `small|medium|large` resolved per provider via `config.tiers` — Claude
`haiku/sonnet/opus`, Codex `gpt-5.4` with effort low/–/high — or `@alias` from `config.aliases`,
which may pin provider+effort), `effort`, `access`, `instructions`/`instructions_file` (+
`instructions_mode: append|replace`), `max_turns`, `budget_usd`, `tools: {allow, deny}`,
`thinking`, `mcp:`, `provider_options: {claude: {...}, codex: {...}}`. A step's `agent:` is a
name, `{extends: name, model: large, ...}` (shallow merge; `tools`/`provider_options` replace
wholesale), an inline mapping, or a bare provider id; unset → `defaults.agent` → the default
provider's `medium` tier. `access: read-only` cannot `allow` `edit`/`shell`.

## Templating rules that bite

- `{{ }}` is for **template fields** (`prompt`, `instructions`, `shell`, `python`, `approve`
  message, `stop.reason`, `cwd`, `env`/`with`/`outputs` values). `when`, `until`, `each` are
  **bare expressions** — `when: "{{ x }}"` is a lint error; they must evaluate to exactly
  `true`/`false` (`each`: a list; `steps.a.output | length > 0`, not a string).
- Context roots: `inputs`, `steps.<id>`, `run` (`id workflow workdir artifacts_dir state_dir
  branch base_branch started_at`), `project` (`root name slug`), `env.<VAR>`, `iteration`
  (`n max first prev.<body id>`), `each` (`index total`), `<as>`. Everything else is undefined.
- **Shell env-ref rule**: in a `shell:` body every `{{ expr }}` renders to `${RAYSPEC_V<n>}` with
  the value exported — never spliced into the script. So quote it: `"{{ x }}"`; single quotes and
  `<<'EOF'` heredocs do NOT expand it; lists/objects arrive as JSON (pipe to `jq`). `${{ x }}`
  (GitHub syntax) is a lint error. Inputs are also `$RAYSPEC_INPUT_<NAME>`; the whole context is
  the JSON file `$RAYSPEC_CONTEXT` (`jq -r '.steps.review.output.summary' "$RAYSPEC_CONTEXT"`).
- Python bodies: `{{ expr }}` renders a **Python literal** (`data = {{ steps.x.output }}`) — do
  not wrap it in quotes; values must be JSON-like. Runs as `sys.executable -` (stdin), or
  `uv run --no-project --with <dep> python -` with `deps: [...]`.
- Code bodies (shell/python) use `{{# ... #}}` as Jinja comment delimiters (bash `${#VAR}` is
  safe); literal `{{` (Go templates, `docker --format '{{.ID}}'`, `printf '{{'`) needs
  `{% raw %}...{% endraw %}`; `{% macro %}`/`{% call %}`/`{% filter %}`/`{% set x %}…{% endset %}`
  blocks are compile errors there (use `{% set x = expr %}` and inline filters).
- Strict undefined: a missing attribute, `null`, a skipped/failed producer, `.field` on text
  output ("no output_schema — try `| fromjson`") all fail the step with a hint. Guard with
  `| default(...)`, `is defined`, or `steps.x.status == 'succeeded'`. `steps.x.ok` is `false`
  for failed AND skipped steps (a plain boolean) while `steps.x.output` of a skipped step fails.
- Filters: every Jinja builtin plus `fromjson` (parse JSON text), `regex_search(pattern, group)`
  (undefined on no match) and `has_signal('NAME')` (a whole line equal to `NAME`, ignoring
  `*`/`_`/backticks, or `<signal>NAME</signal>`; also a test: `is has_signal('X')`).
- Step views: `steps.<id>.output`, `.status` (`succeeded|failed|skipped|...`), `.ok`,
  `.exit_code`, `.stderr` (shell/python), `.approved` (approve), `.iterations`/`.converged` (loop),
  `.items` (each: `[{index, item, status, output, error}]`), `.session`, `.model`, `.usage`,
  `.cost_usd`, `.duration_s`, `.error`, `.skip_reason`. Loop output = `{<body id>: output}` of the
  last iteration; each output = a list aligned with the items; include output = its `outputs:`.
- **Lexical scope**: a step sees its ancestors (`needs` closure) and the ancestors of every
  enclosing composite; a loop/each body sees the outside; the outside never sees a body — use
  `steps.build.output.review`, not `steps.review`. `iteration.prev.<id>` is the previous
  iteration's view (undefined on iteration 1: `iteration.prev.review.output | default('')`).
  An `include:` body is closed: only its own steps + `inputs` bound by `with:` (+ `run`,
  `project`, `env`).
- Text rendering: one `{{ expr }}` alone keeps its type (that is how `outputs`/`with`/`env` pass
  lists/objects through); `None`/undefined/callables in text are errors (`{{ x.keys }}` without
  `()` is rejected). Booleans render `true`/`false`; mappings as pretty JSON.
- YAML: quote a scalar that starts with `{{`; `:` inside a plain scalar breaks YAML — use a block
  scalar (`prompt: |`) or quotes; booleans are only `true/false` (`yes`/`no`/`on` stay strings);
  duplicate keys are errors.

## CLI quick reference

| Command | Purpose | Key flags | Exit |
|---|---|---|---|
| `rayspec init` | scaffold `.rayspec/` (+ this skill into `.claude/skills/rayspec/`) | `--kind code\|content`, `--force`, `--no-skill`, `--root` | 0 / 2 |
| `rayspec doctor` | Python, home, config, git/uv, SDKs, CLIs, auth, pricing rows | `--probe`, `--provider ID`, `--json` | 0 / 1 |
| `rayspec workflows` · `agents` · `providers` | list discovered workflows / agent files / the capability matrix | `--json` (`--root` for `workflows`/`agents`) | 0 / 2 |
| `rayspec validate [names…]` | schema, graph, references, templates, capabilities | `--allow-unsupported`, `--json` | 0 / 2 |
| `rayspec plan <wf>` | inputs, resolved agents, step order, capability + cost report | `--input k=v`, `--inputs-file`, `--json` | 0 / 2 |
| `rayspec run <wf>` | run (or resume) a workflow | `--input`, `--inputs-file`, `--dry-run`, `--stubs f`, `--stubs-init f`, `--exec-shell`, `--yes`, `--no-interactive`, `--json`, `--quiet`, `--verbose`, `--fail-fast`, `--allow-unsupported`, `--worktree/--no-worktree`, `--base`, `--repo`, `--resume <id>`, `--force` | 0 1 2 3 4 130 |
| `rayspec runs` | list runs (newest first) | `--all`, `--limit N`, `--json` | 0 |
| `rayspec costs` | sum a project's runs by workflow (tokens, cost, cost-source breakdown) | `--since 7d`, `--workflow NAME`, `--json` | 0 / 2 |
| `rayspec show <run>` | header, workspace, step table, warnings, outputs, pause state | `--json` | 0 / 2 |
| `rayspec logs <run>` | lifecycle events; `--step <path>` = that step's transcript | `--step`, `--stream`, `--follow`, `--verbose`, `--json` | 0 / 2 |
| `rayspec audit <run>` | read-only ledger: commands, tools, files, warnings, approvals + who ran it | `--commands`, `--json` | 0 / 2 |
| `rayspec resume <run>` | re-run from the top with the reuse cache | `--force`, `--yes`, `--no-interactive`, `--json` | run's code / 2 |
| `rayspec approve <run> [comment]` · `reject <run> [reason]` | decide a paused gate and resume | `--force`, `--json` | run's code / 2 |
| `rayspec cancel <run>` | SIGINT a live run / mark a dead one cancelled | `--yes`, `--mark`, `--force`, `--json` | 0 / 1 / 2 |
| `rayspec worktrees list` · `clean` | rayspec worktrees of the project | `--older-than 7d`, `--merged`, `--force`, `--dry-run` | 0 / 2 |
| `rayspec projects add\|list\|remove` | names for `--repo <name>` | `--base` | 0 / 2 |
| `rayspec skill install\|show\|path` | this skill (project or `--global`) | `--global`, `--force` | 0 / 2 |

`--json` on `run`/`resume`/`approve`/`reject`: JSONL events on stdout, the summary object
(`run_id status exit_code reason outputs usage cost_usd cost_source run_dir workspace pause`) as
the **last** stdout line (`… --json | tail -1 | jq .exit_code`); Rich lines go to stderr. `--json`
does not imply `--no-interactive`. Every `<run>` accepts a unique id prefix. Commands that read a
project take `--root DIR`. `resume` refuses a run whose workflow file changed (`--force` re-runs
the steps whose fingerprint changed), a run with a live pid, and a succeeded or cancelled run.

**Stub file** (`--stubs`, YAML; `--stubs-init` scaffolds it):

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

- Ids/`as:`/`session:` match `^[a-z][a-z0-9_]*$` and are not a context root (`inputs steps run
  project env iteration each loop self true false none null`); unique across the whole file.
- Unknown keys are errors everywhere (with did-you-mean). Exactly one kind key per step.
- `retry.attempts` = total attempts; timeouts count as transient only with `on_error: all`;
  `timeout:` on `approve:`/`stop:` is an error. `allow_failure` is valid on any step.
- `session:` must name a transitive ancestor prompt step on the SAME provider (or the step's own
  id inside a loop body). `each:` rejects mappings (`.values()`), tuples are fine.
- Secrets never go into prompts, `outputs:`, inputs without `secret: true`, or anything a
  template renders — transcripts and outputs are stored in clear text.
- Keep agents `access: read-only` unless editing is the point; give the editing agent
  `workspace-write` (confined to the worktree); `full` only when the human asked for it. Prefer
  `shell:`/`python:` for deterministic checks (tests, lint, `gh` queries) and let the agent read
  their output — "code computes, agents judge".
- `stop:` defaults to `status: cancelled` (exit 4); `stop: {status: succeeded}` still renders
  `outputs:`. Exhausting a loop is `failed` unless `on_exhausted: continue`.
- `--dry-run` creates no worktree and skips shell/python (unless `--exec-shell`); it cannot
  prove a shell step works — run the command yourself before a real run if in doubt.
- Include files are discovered by name (`include: review_block`) or by a path relative to the
  including file; their `with:` keys must be declared inputs of the block.
- A run in progress holds a per-workdir lock. A `running` record whose process died (crash)
  resumes normally (`rayspec resume <run>` detects the dead pid); `rayspec cancel <run> --mark`
  marks it cancelled instead (a cancelled run resumes only with `--force`).

## References (read on demand — same directory)

- `references/concepts.md` — mental model: DAG + bodies, step paths (`build[2]/implement`),
  lexical scopes, outputs, runs/resume, isolation (read when scoping or debugging references).
- `references/schema.md` — every field with its default, join table, statuses, durations, Jinja
  traps, strict YAML (read before using a field not shown above).
- `references/templating.md` — context roots, step views, shell/python body rules, filters,
  env of shell steps, input coercion, error messages.
- `references/cli.md` — every command, flag, `--json` shape and exit code (read for `runs`/
  `show`/`logs`/`resume`/`cancel` details, `init`, `doctor`).
- `references/providers.md` — the neutral adapter, the capability matrix, Claude/Codex option
  mapping, access levels and tools, the stub file format, tiers/aliases, pricing, auth.
- `references/examples.md` — the example projects (what each shows) and the patterns: self-heal
  loop, branch-and-stop, fan-out with partial failure, reusable block, finally step.
- Online only: `runs-and-resume.md` (run dir layout, resume rules, approval flow),
  `isolation.md` (worktrees, `--repo`, locks), `extending.md`, `constitution.md` at
  https://github.com/rayspec-labs/rayspec-py/blob/main/docs/.
