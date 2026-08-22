---
name: rayspec-workflows
description: Author and edit rayspec agent workflows — the YAML DSL on the Claude Agent SDK / OpenAI Codex SDK, covering every step kind, field and templating rule, plus agents, prompts, includes, stubs and .rayspec/ project files. Use when asked to create or edit a rayspec workflow, agent or prompt. Load the companion rayspec-cli skill to validate, plan, dry-run, run or debug what you wrote from the CLI.
---

# rayspec workflows — authoring the YAML DSL

**Companion skill**: everything about *running* what you write — `rayspec validate`, `plan`,
`run`, `resume`, `logs`, `explain`, `test`, every flag, `--json` shape and exit code — is in the
**`rayspec-cli`** skill. Load it as soon as you leave the editor.

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
- Providers: `claude`, `codex`, `stub` (scripted; what `--dry-run` uses). Each declares
  capabilities; `rayspec validate` refuses a field the resolved provider lacks.
- Runs, run directories, worktrees and exit codes are the `rayspec-cli` skill's subject; you only
  need to know that a run happens elsewhere (a git worktree by default) and that authoring is
  finished when `rayspec validate` says `OK`.

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

Steps 3-7 are the `rayspec-cli` skill's subject: **now validate, plan and dry-run — load the
`rayspec-cli` skill** for the exact commands, flags, `--json` shapes and exit codes.

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
    provider_options: { claude: { max_thinking_tokens: 8000 } }   # raw pass-through per provider
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

The last row holds however the list ended: under fail-fast and after a `stop:` the running siblings are cancelled first and the `join: always` steps then run on their own, so `always` is finally-semantics everywhere. After a cancellation the skipped leftovers all read `stopped`; after a failure they keep `upstream_failed`/`upstream_skipped` and fall back to `run_failed`.

`defaults.on_step_failure` picks what a failed step does to its siblings:
**`drain`** (default) — running steps finish, nothing new starts except `join: always` steps.
**`fail_fast`** — running siblings are cancelled at once (`--fail-fast` does this too, and the
flag may only ever *tighten*: it overrides `drain` and `continue`, never the reverse).
**`continue`** — independent branches keep being scheduled; only the failed step's downstream
cone skips. All three still end the run `failed` (exit 1) — `continue` is not `allow_failure`,
and it is a different knob from `each.on_failure: continue`, which is per *item*. The policy is
lexically scoped: it applies inside `each:`/`loop:`/`include:` bodies too, except that an
`include:`d workflow which states its own `on_step_failure` governs its own body.

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

## Field index (every field the schema defines)

The cheat-sheet above shows the common ones in context; this index is the complete list, so a
field that is not here does not exist. Defaults in parentheses.

| Where | Fields |
|---|---|
| top level | `rayspec` (must be `1`) · `name` · `description` · `inputs` · `defaults` · `isolation` (`worktree`) · `agents` · `steps` · `outputs` |
| `defaults:` | `agent` · `timeout` · `max_parallel` (`4`) · `on_unsupported` (`error`) · `on_step_failure` (`drain`) · `budget_usd` · `max_tokens` (`"500k"`) · `timeout_total` (whole-run wall clock, measured from the run's *original* start — a resume keeps counting) |
| every step | `id` · `description` · `needs` · `when` · `join` (`all`) · `timeout` · `always_run` (`false`) · `allow_failure` (`false`) · `artifacts` |
| `artifacts:` | files the step must leave behind, relative to its working directory; absolute paths, `~`, `..`, trailing `/` and `{{`/`{%` are rejected, and a declared file that is missing after a *successful* step fails it (not checked on reused records or in `--dry-run`) |
| leaf steps only (`prompt`/`shell`/`python`) | `retry` · `env` · `output_schema` |
| `retry:` | `attempts` (required, 1-10, the TOTAL count) · `delay` (`3s`, doubles each retry) · `on_error` (`transient`, or `all`) |
| kind keys (exactly one per step) | `prompt` · `prompt_file` · `shell` · `python` · `loop` · `each` · `approve` · `include` · `stop` — `prompt_file:` alone is a complete prompt step (`prompt:` and `prompt_file:` are mutually exclusive) |
| `prompt:` step | `agent` · `session` |
| `shell:` step | `interpreter` (`bash`, or `sh`) · `cwd` |
| `python:` step | `deps` · `cwd` |
| `loop:` | `steps` (required) · `max_iterations` (required) · `until` · `on_exhausted` (`fail`) |
| `each:` | `as` (`item`) · `steps` (required) · `max_parallel` · `on_failure` (`fail`) |
| `approve:` | `message` (required; a bare string is shorthand for it) · `on_reject` (`cancel`) · `class` (the approval class the operator's policy governs — see the `rayspec-cli` skill) · `auto_if` (bare expression that approves the gate without asking; it can only add to what the class already permits) |
| `include:` | `with` |
| `stop:` | `status` (`cancelled`) · `reason` |
| `inputs.<name>:` | `type` (`string`) · `required` · `default` · `description` · `enum` · `items` · `properties` · `secret` |
| agents | `provider` · `model` · `effort` · `access` (`workspace-write`) · `instructions` · `instructions_file` · `instructions_mode` (`append`) · `max_turns` · `budget_usd` · `tools` · `network` · `commands` · `thinking` · `on_denial` (`warn`) · `mcp` · `provider_options` · `extends` (only in a step's `agent: {extends: name, …}` form) |
| `tools:` | `allow` · `deny` — entries are groups (`read` `edit` `shell` `web` `agent` `mcp`), `mcp:<server>[/<tool>]`, or `<provider>:<RawName>` |
| `network:` | `on` / `off` — whether the agent may reach the network through its provider's own tools; `off` is folded into `tools.deny: [web]` |
| `commands:` | `allow` · `deny` — Python regexes compiled at load time, `deny` first, a non-empty `allow` means "nothing else". Advisory (a validate warning) unless the provider declares the `command_policy` capability |
| `on_denial:` | `warn` (default) or `fail` — what a refused tool call does to the step; `fail` needs the provider's `denial_reporting` capability |
| `mcp.<server>:` | `transport` (`stdio`, or `http`/`sse`) · `command` · `args` · `env` · `url` · `headers` — `stdio` requires `command`, `http`/`sse` require `url` |

## CLI quick reference

Only the commands that **create or describe the authoring artifacts themselves** live here; every
command that executes, inspects or governs a run is in the `rayspec-cli` skill.

| Command | Purpose | Key flags | Exit |
|---|---|---|---|
| `rayspec init` | scaffold `.rayspec/` (+ both agent skills into `.claude/skills/`) | `--kind code\|content`, `--from EXAMPLE`, `--force`, `--no-skill`, `--root` | 0 / 2 |
| `rayspec new workflow <name>` · `new agent <name>` | add one workflow or agent file to a project that already exists (it never creates one) | `--agent NAME`, `--description`, `--force`, `--root` | 0 / 2 |
| `rayspec schema [kind]` | print the published JSON Schemas (`workflow`, `run`, `events`, `stream`) | `--out DIR` | 0 / 2 |

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
- Include files are discovered by name (`include: review_block`) or by a path relative to the
  including file; their `with:` keys must be declared inputs of the block.

## References (read on demand — same directory)

- `references/concepts.md` — mental model: DAG + bodies, step paths (`build[2]/implement`),
  lexical scopes, outputs, runs/resume, isolation (read when scoping or debugging references).
- `references/schema.md` — every field with its default, join table, statuses, durations, Jinja
  traps, strict YAML (read before using a field not shown above).
- `references/templating.md` — context roots, step views, shell/python body rules, filters,
  env of shell steps, input coercion, error messages.
- `references/examples.md` — the example projects (what each shows) and the patterns: self-heal
  loop, branch-and-stop, fan-out with partial failure, reusable block, finally step.
- Everything about running what you wrote — `cli.md`, `providers.md`, `testing.md`, `policy.md`,
  `runs-and-resume.md`, `isolation.md`, `ci.md` — is in the **`rayspec-cli`** skill. Load it
  instead of guessing a flag.
- Online only: `extending.md` (plugins and the provider seam), `constitution.md` (why the DSL
  refuses fields), `agent-skill.md` (these two skills) at
  https://github.com/rayspec-labs/rayspec-py/blob/main/docs/.
