---
name: rayspec-workflows
description: Author and edit rayspec agent workflows — the YAML DSL on the Claude Agent SDK / OpenAI Codex SDK, covering every step kind, field and templating rule, plus the agents, prompts, includes and secret inputs you write by hand under .rayspec/, and scaffolding a project with rayspec init. Use when asked to create or edit a rayspec workflow, agent or prompt. This skill runs nothing itself — load the companion rayspec-cli skill to validate, plan, dry-run, run or debug what you wrote, and for the stub, test-case and policy files.
---

# rayspec workflows — authoring the YAML DSL

**Companion skill**: everything about *running* what you write — `rayspec validate`, `plan`,
`run`, `resume`, `logs`, `explain`, `test`, every flag, `--json` shape and exit code — is in the
**`rayspec-cli`** skill. Load it as soon as you leave the editor.

## Mental model (read this first)

- A **workflow** is one YAML file: `.rayspec/workflows/<name>.yaml` (project) or
  `~/.rayspec/workflows/` (user; the project wins). `.yml` works too. The name you pass to
  `rayspec run <name>` is the **file stem**, not the `name:` field — keep them equal.
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
   (`file:line` on errors). Fix until `OK`. It also reports which operator `policy:` is in force.
4. `rayspec plan <wf> [--input k=v]` — resolved inputs, agents/models, step order, capability
   report. Check the agent/model/access column is what you intended.
5. Dry-run with scripted agents, no login needed: `rayspec run <wf> --dry-run --stubs-init
   stubs.yaml` writes one entry per prompt step, keyed as the engine names records
   (`build[*]/review` for loop bodies); edit the answers so branches and loops behave
   (`sequence:` for loops, `output:` for `output_schema` steps), then `--stubs stubs.yaml`.
   Shell/python steps are skipped (`''` output, or the *minimal instance* of their
   `output_schema`) unless `--exec-shell`; `--dry-run` also auto-approves every gate whose
   approval class permits an automatic approval (with no operator policy in force, all of them).
6. Only then a real run — **ask the human first** when agents may edit files, push, open PRs or
   spend money: `rayspec run <wf> --input k=v` (`--no-worktree` only if in-place is wanted,
   `--yes` only if the human said gates may auto-approve).
7. Inspect: `rayspec show|logs|audit <run>`, `rayspec eval <run> "<expr>"` to try an expression
   against a finished run. Paused (exit 3): `rayspec approve|reject <run>` then `resume`.
   Failed: fix, then `rayspec resume <run>` replays succeeded steps (`--force` when you edited
   the workflow — its hash changed).

Steps 3-7 are the `rayspec-cli` skill's subject: **now validate, plan and dry-run — load the
`rayspec-cli` skill** for the exact commands, flags, `--json` shapes and exit codes.

## YAML cheat-sheet (every step kind, annotated)

```yaml
# .rayspec/workflows/fix_issue.yaml — every step kind once, in one file
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
  timeout_total: 4h                     # whole run, from its ORIGINAL start (a resume keeps counting)
  max_parallel: 4
  on_step_failure: drain                # drain (default) | fail_fast | continue — see below
  # budget_usd: 5 / max_tokens: "500k"  # run-level circuit breaker (root workflow only)
isolation: worktree                     # worktree (default) | none
agents:                                 # full agent shape below; provider default = config.default_provider
  triage:      { provider: claude, model: small, access: read-only, tools: { deny: [web] } }
  implementer: { provider: codex, model: medium, effort: high, access: workspace-write,
                 instructions_file: prompts/implementer.md }   # relative to this file's .rayspec/
  reviewer:    { provider: claude, model: large, access: read-only, network: "off", max_turns: 40,
                 provider_options: { claude: { max_thinking_tokens: 8000 } } }
steps:
  - id: fetch                           # ids: ^[a-z][a-z0-9_]*$, unique in the whole file
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE" --json title,body   # inputs also as env vars
    interpreter: bash                   # bash (default) | sh
  - id: assess
    needs: [fetch]
    agent: triage
    prompt: "{{ steps.fetch.output }}\n\nIs this a real, well-scoped bug worth fixing now?"
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
      max_iterations: 3                 # required, a plain int (not templated); exhausting it = failed
      until: steps.review.output | has_signal('BUILD-CLEAN')   # checked over the body after each pass
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
            Tests exit {{ steps.check.exit_code }} (tolerated: {{ steps.check.tolerated }}):
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
        shell: echo "checking {{ label }} ({{ each.index }}/{{ each.total }})"
  - id: summary
    needs: [build, lint_all]
    join: always                        # run even when a sibling failed (finally step)
    artifacts: [reports/summary.json]   # promised file; missing after a SUCCESSFUL step = step fails
    python: |
      import json, os, pathlib
      mode = {{ inputs.mode }}          # renders a Python literal: 'fast'
      ctx = json.load(open(os.environ["RAYSPEC_CONTEXT"]))     # inputs/steps/run/project as JSON
      out = {"mode": mode, "iterations": ctx["steps"]["build"]["iterations"]}
      pathlib.Path("reports").mkdir(exist_ok=True)
      pathlib.Path("reports/summary.json").write_text(json.dumps(out))
      print(json.dumps(out))
  - id: second_opinion
    needs: [build]
    include: review_block               # another workflow file, inlined at load time
    with: { target: "." }               # validated against the included workflow's inputs
  - id: confirm
    needs: [build, second_opinion]
    approve:
      message: "Open a PR for issue {{ inputs.issue }}?"
      class: release                    # the OPERATOR's policy decides how strictly this is held
      on_reject: cancel                 # cancel (default) | continue | fail
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

**Join truth table** (evaluated once every `needs` is terminal; tolerated failure = succeeded):

| needs outcome | `all` (default) | `any` | `always` |
|---|---|---|---|
| all succeeded | run | run | run |
| some skipped, rest succeeded | skip (`upstream_skipped`) | run | run |
| all skipped | skip | skip | run |
| any failed (untolerated) | skip (`upstream_failed`) | skip | run |
| run draining / cancelled | skip (`run_failed`) | skip | run |

The last row holds however the list ended: under fail-fast and after a `stop:` the running siblings are cancelled first and the `join: always` steps then run on their own, so `always` is finally-semantics everywhere. After a cancellation the skipped leftovers read `stopped`; after a failure they keep `upstream_failed`/`upstream_skipped` and fall back to `run_failed`.

`defaults.on_step_failure` picks what a failed step does to its siblings. **`drain`** (default):
running steps finish, nothing new starts except `join: always` steps. **`fail_fast`**: running
siblings are cancelled at once (`--fail-fast` does this too, and the flag may only ever *tighten*
— it overrides `drain` and `continue`, never the reverse). **`continue`**: independent branches
keep being scheduled; only the failed step's downstream cone skips. All three still end the run
`failed` (exit 1) — `continue` is not `allow_failure`, and it is a different knob from
`each.on_failure: continue`, which is per *item*. The setting is lexically scoped: `loop:`/`each:`
bodies always inherit it, and an `include:`d workflow that *states* one governs its own body — but
a stated policy may only ever TIGHTEN what an enclosing workflow stated, so a vendored block
cannot write `continue` to undo the caller's `fail_fast`.

**Inputs**: types `string` (default) `integer` `number` `boolean` `array` `object`; `required`
(cannot have a `default`), `default`, `enum`, `items`, `properties`, `description`, `secret`.
Supplied by `--input name=value` > `--inputs-file f.yaml|json` > env `RAYSPEC_INPUT_<NAME>` >
`default`; text is coerced by type, arrays via JSON or repeated `--input`. Inputs are fixed per
run (`--resume` refuses `--input` except for secrets).

**Agents** are looked up workflow `agents:` > `.rayspec/agents/<name>.yaml` > `~/.rayspec/agents/`.
A step's `agent:` is a name, `{extends: name, model: large, …}` (shallow merge; `tools` and
`provider_options` replace wholesale), an inline mapping, or a bare provider id; unset →
`defaults.agent` → the default provider's `medium` tier. `model:` is a literal id, a tier
`small|medium|large` (resolved per provider by `config.tiers` — Claude `haiku/sonnet/opus`, Codex
`gpt-5.4` at effort low/–/high) or an `@alias` from `config.aliases`, which may also pin the
provider and effort. The full shape:

```yaml
agents:
  sandboxed:
    provider: claude
    model: medium
    effort: high                        # none|minimal|low|medium|high|xhigh|max|ultra (per provider)
    access: read-only                   # read-only cannot allow the `edit` or `shell` groups
    instructions: Answer with one line.  # or instructions_file: prompts/x.md (mutually exclusive)
    instructions_mode: append           # append (default) onto the provider's prompt | replace
    max_turns: 20
    budget_usd: 2.5
    thinking: true
    network: "off"                      # "on" | "off" — QUOTE it (see the YAML trap below)
    on_denial: warn                     # warn (default) | fail — `fail` needs denial_reporting
    tools: { allow: [read], deny: [web, "mcp:files/write"] }
    commands:                           # Python regexes, compiled at load time; deny wins
      allow: ['^git (status|log|diff)\b']
      deny: ['\brm\b']
    mcp:
      files:                            # stdio transport requires `command:`
        transport: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]
        env: { LOG_LEVEL: "warn" }
      remote:                           # http and sse require `url:`
        transport: http
        url: https://mcp.example.com/v1
        headers: { Authorization: "Bearer <token>" }   # NOT templated — see below
    provider_options: { claude: { max_thinking_tokens: 8000 } }
```

Two rules about that block. **Only `instructions:` is templated** — the text of
`instructions_file:` becomes `instructions` and is rendered and reference-checked the same way,
but `mcp.<server>.env` / `.headers` / `.args` and every other agent field are passed through
verbatim, so `{{ … }}` there arrives as literal text. And **`commands:` is advisory** unless the
provider hands rayspec its tool calls before they run (capability `command_policy`); no builtin
provider does today, so `rayspec validate` prints `commands: cannot be enforced on provider
'claude' … the block is advisory there`. The real controls are `access:`, `tools:` and the
operator's policy.

## Field index (every field the schema defines)

The cheat-sheet shows the common ones in context; this index is the complete list, so a field
that is not here does not exist. Defaults in parentheses.

| Where | Fields |
|---|---|
| top level | `rayspec` (must be `1`) · `name` · `description` · `inputs` · `defaults` · `isolation` (`worktree`) · `agents` · `steps` · `outputs` |
| `defaults:` | `agent` · `timeout` · `max_parallel` (`4`) · `on_unsupported` (`error`) · `on_step_failure` (`drain`) · `budget_usd` (no cap; written `1.5`, `"$1.50"` or `"12 USD"`) · `max_tokens` (no cap; written `500000`, `"500k"` or `"1.5M"`) · `timeout_total` |
| every step | `id` · `description` · `needs` · `when` · `join` (`all`) · `timeout` · `always_run` (`false`) · `allow_failure` (`false`) · `artifacts` |
| `always_run:` | re-execute this step on a **resume** instead of replaying its cached record. It is *not* finally-semantics — a step whose upstream skipped is still skipped, `always_run: true` or not; the field that runs a step anyway is `join: always` |
| `artifacts:` | files the step must leave behind, **relative to its working directory**; absolute paths, `~`, `..`, trailing `/` and `{{`/`{%` are rejected at load time, `./b//r.md` normalises to `b/r.md`. Checked only **after the step succeeds** — a declared file that is missing fails the step; kept files are copied to `<run dir>/artifacts/<step>/…` and listed by `rayspec show`. Not checked on reused records or in `--dry-run` |
| leaf steps only (`prompt`/`shell`/`python`) | `retry` · `env` (values templated, bool/int/float coerced to text) · `output_schema` |
| `retry:` | `attempts` (required, 1-10, the TOTAL count) · `delay` (`3s`, doubles each retry) · `on_error` (`transient`, or `all`) — a prompt step with no `retry:` gets `attempts 3 / delay 3s / transient`; shell and python get none |
| kind keys (exactly one per step) | `prompt` · `prompt_file` · `shell` · `python` · `loop` · `each` · `approve` · `include` · `stop` — `prompt_file:` alone is a complete prompt step (`prompt:` and `prompt_file:` are mutually exclusive, and one of them is required) |
| `prompt:` step | `agent` · `session` |
| `shell:` step | `interpreter` (`bash`, or `sh`) · `cwd` (templated) |
| `python:` step | `deps` (runs under `uv run --no-project --with …` when set) · `cwd` (templated) |
| `loop:` | `steps` (required, non-empty) · `max_iterations` (required, ≥1) · `until` (bare expression over the body) · `on_exhausted` (`fail`, or `continue`) |
| `each:` | `each` (bare expression yielding a list) · `as` (`item`) · `steps` (required, non-empty) · `max_parallel` · `on_failure` (`fail`, or `continue`) |
| `approve:` | `message` (required, templated; a bare string is shorthand for it) · `on_reject` (`cancel`) · `class` · `auto_if` |
| `class:` | the approval class this gate belongs to. The workflow names it; the operator's `policy.yaml` (project, `~/.rayspec/`, or `$RAYSPEC_POLICY`) decides what may approve it, and `rayspec run --approve-class NAME` pre-authorises one class. A workflow can never loosen a rule — and a class no policy in force defines keeps the permissive default and says so |
| `auto_if:` | bare expression; when true the gate is approved without asking. It can only **add** an automatic approval to what the class already permits, so it can never escalate a gate |
| `include:` | `include` (name, or a path relative to the including file) · `with` (keys must be declared inputs of the block) |
| `stop:` | `status` (`cancelled`, or `succeeded`/`failed`) · `reason` (templated) |
| `inputs.<name>:` | `type` (`string`) · `required` · `default` · `description` · `enum` · `items` · `properties` · `secret` |
| agents | `provider` · `model` · `effort` · `access` (`workspace-write`) · `instructions` · `instructions_file` · `instructions_mode` (`append`) · `max_turns` · `budget_usd` · `tools` · `network` · `commands` · `thinking` · `on_denial` (`warn`) · `mcp` · `provider_options` · `extends` (only in a step's `agent: {extends: name, …}` form) |
| `tools:` | `allow` · `deny` — entries are groups (`read` `edit` `shell` `web` `agent` `mcp`), `mcp:<server>[/<tool>]`, or `<provider>:<RawName>` |
| `commands:` | `allow` · `deny` — Python regexes, `deny` checked first, a non-empty `allow` means "nothing else" |
| `mcp.<server>:` | `transport` (`stdio`, or `http`/`sse`) · `command` · `args` · `env` · `url` · `headers` |

`timeout:` is a load-time error on `approve:` and `stop:` steps ("it would be ignored"), and a
field written on the wrong kind is named precisely: `field 'interpreter' is not valid on python
steps (valid on: shell)`.

## Templating rules that bite

- `{{ }}` is for **template fields** (`prompt`, `instructions`, `shell`, `python`, `approve`
  message, `stop.reason`, `cwd`, `env`/`with`/`outputs` values). `when`, `until`, `each`,
  `auto_if` are **bare expressions** — `when: "{{ x }}"` is an error ("expression fields take a
  bare Jinja expression"); they must evaluate to exactly `true`/`false` (`each`: a list).
  Numeric fields (`max_iterations`, `max_parallel`, `max_turns`, `attempts`) are never templated.
  The expression field itself is a **string**, so parking a step with a bare YAML `when: false` is
  a load-time type error (*"Input should be a valid string"*) — write `when: "false"`, or an
  expression such as `when: 1 == 2`.
- Context roots: `inputs`, `steps.<id>`, `run` (`id workflow workdir artifacts_dir state_dir
  branch base_branch started_at`), `project` (`root name slug`), `env.<VAR>`, `iteration`
  (`n max first prev.<body id>`), `each` (`index total`), `<as>`. Everything else is undefined.
- **Step views** — the 20 attributes of `steps.<id>`: `.output`, `.status`
  (`succeeded|failed|skipped|…`), `.ok`, `.exit_code`, `.stderr`, `.duration_s`, `.cost_usd`,
  `.usage`, `.session`, `.model`, `.approved`, `.iterations`, `.converged`, `.items`, `.id`,
  `.kind`, `.skip_reason`, `.error`, `.tolerated`, `.denials`. An attribute that does not apply
  to the kind is undefined with a hint, not `None`. `.tolerated` is `true` for a failed step that
  `allow_failure` absorbed; `.denials` is the list of refused tool calls
  (`{tool, reason, call_id}`) on a prompt step and `[]` when nothing was refused. Loop output =
  `{<body id>: output}` of the last iteration; each output = one such mapping per item, in order
  (and `.items` is `[{index, item, status, output, error}]`); include output = its `outputs:`.
- **Shell env-ref rule**: in a `shell:` body every `{{ expr }}` renders to `${RAYSPEC_V<n>}` —
  never spliced into the script, so `$(rm -rf /)` inside a value stays inert text. Up to 64 KiB the
  value is in the step's environment; above that it is written to a file and read back into the
  same slot by a preamble line, so it is a plain shell variable and a child process the body starts
  does not inherit it. The slot reads the same either side of the threshold: quote it
  `"{{ x }}"`, and single quotes and `<<'EOF'` heredocs do NOT expand it (you get the literal
  `${RAYSPEC_V2}`) whatever the size. Lists/objects arrive as JSON (pipe to `jq`).
  `${{ x }}` (GitHub syntax) is an error.
- **Env of every shell/python step**: `RAYSPEC_INPUT_<NAME>` per input, `RAYSPEC_RUN_ID`,
  `RAYSPEC_WORKDIR`, `RAYSPEC_ARTIFACTS_DIR`, `RAYSPEC_STATE_DIR`, and `RAYSPEC_CONTEXT` — a JSON
  file holding `inputs`, `steps`, `run`, `project` (`env` is deliberately left out):
  `jq -r '.steps.review.output.summary' "$RAYSPEC_CONTEXT"`.
- Python bodies: `{{ expr }}` renders a **Python literal** (`data = {{ steps.x.output }}`) — do
  not quote it; values must be JSON-like. Runs as `sys.executable -` (stdin), or
  `uv run --no-project --with <dep> python -` when `deps:` is set.
- Code bodies (shell/python) use `{{# ... #}}` as Jinja comment delimiters (bash `${#VAR}` is
  safe); literal `{{` (Go templates, `docker --format '{{.ID}}'`, `printf '{{'`) needs
  `{% raw %}...{% endraw %}`; `{% macro %}`/`{% call %}`/`{% filter %}`/`{% set x %}…{% endset %}`
  blocks are compile errors there (use `{% set x = expr %}` and inline filters).
- Strict undefined: a missing attribute, `null`, a failed producer, `.field` on text output
  ("no output_schema — try `| fromjson`") all fail the step with a hint. On a **skipped**
  producer *both* `.output` and `.ok` fail loudly — a skipped step never answered, so `.ok` is
  undefined rather than `false`. Write `steps.x.status == 'succeeded'` for "ran and succeeded",
  or `steps.x.ok | default(false)`. (A *failed* step's `.ok` is a plain `false`.)
- Filters: every Jinja builtin plus `fromjson` (parse JSON text; already-structured input is an
  error), `regex_search(pattern, group=0)` (a chainable undefined on no match, never `''`) and
  `has_signal('NAME')` (a whole line equal to `NAME` after stripping whitespace and surrounding
  `*`, `_` or backticks, or `<signal>NAME</signal>` anywhere; structured input is an error). The
  only custom **test** is the same one: `steps.x.output is has_signal('DONE')`.
- **Lexical scope**: a step sees its ancestors (`needs` closure) and the ancestors of every
  enclosing composite; a loop/each body sees the outside; the outside never sees a body —
  `steps.implement` from outside gives *"steps.implement is inside loop 'build'; use
  steps.build.output.implement"*. `iteration.prev.<id>` is the previous iteration's view
  (undefined on iteration 1: `iteration.prev.review.output | default('')`). An `include:` body is
  closed: only its own steps + `inputs` bound by `with:` (+ `run`, `project`, `env`).
- Text rendering: one `{{ expr }}` alone keeps its type (that is how `outputs`/`with`/`env` pass
  lists and objects through); `None`, undefined and callables in text are errors (`{{ x.keys }}`
  without `()` is rejected). Booleans render `true`/`false`, mappings and lists as pretty JSON —
  use `| join(', ')` when you want a sentence.

## Secrets

Plain inputs are persisted in clear text in the run directory and printed by `plan`/`show` —
**never pass a credential as a plain input**. Declare `inputs.<name>.secret: true` instead:

```yaml
inputs:
  token: { type: string, secret: true }          # secret + default is a load-time error
steps:
  - id: publish
    shell: |                                      # $RAYSPEC_INPUT_TOKEN is the ONLY way in
      curl -H "Authorization: Bearer $RAYSPEC_INPUT_TOKEN" https://example/api
```

A secret value is delivered **only** as `RAYSPEC_INPUT_<NAME>` in the environment of
`shell:`/`python:` steps (and through their `env:` mappings). `{{ inputs.token }}` anywhere else
— prompts, a prompt step's `env`, `when`/`until`/`each`/`auto_if`, `outputs:`, `approve.message`,
`stop.reason`, an include's `with:` — is a **load-time validation error** naming the input. It is
**never persisted** (`run.json`, `context.json`, and `plan`/`show` all read `<secret>`), so every
resume entry (`resume`/`approve`/`reject`/`run --resume`) must re-supply it with
`--input name=value` (allowed on resume only for secret inputs) or `RAYSPEC_INPUT_<NAME>` in the
environment, else **exit 2 listing the missing secret inputs**. `rayspec plan`/`validate` mark
such inputs `(secret)`.

If an agent needs a credentialled capability, give it a `shell:` **tool** that uses the secret and
let the agent consume the tool's *result*: transcripts and outputs are stored in clear text.

## Best practices

Each of these exists because the alternative fails in a specific way.

- **Let code decide, let agents judge.** Anything with a right answer — tests, lint, `gh` queries,
  diff size — belongs in `shell:`/`python:`, because its exit code is a fact the engine can gate
  on, while an agent's "looks fine" is a sentence. Feed the script's output *into* the prompt
  (`Tests exit {{ steps.check.exit_code }}`) instead of asking the agent to run it.
- **Read-only by default.** `access: read-only` for every agent that reads, reviews, triages or
  summarises; `workspace-write` only for the one agent whose job is editing; `full` only when the
  human asked. A reviewer with write access can silently fix what it was asked to judge.
- **Prompts over ~10 lines go in `prompt_file:`** (`.rayspec/prompts/<x>.md`): the graph stays
  readable and prompt changes diff on their own. Prompt files are templates and are
  reference-checked by `validate` like any other.
- **`loop:` vs `each:` vs `include:`.** `loop:` when the *same* work repeats until a condition
  holds; `each:` when independent items are processed once; `include:` when a block is used by
  more than one workflow, or twice in one workflow with different inputs. Reach for `include:`
  the second time you copy a step pair.
- **Make a loop converge on a signal, not on a vibe.** Give the reviewer one exact line to emit
  and test it with `has_signal('SHIP-IT')` — it ignores markdown emphasis, so `**SHIP-IT**` still
  counts while "not SHIP-IT yet" does not. Set `on_exhausted` deliberately; the default `fail` is
  right when convergence is the point.
- **Let the checker fail.** A `check` step inside a self-healing loop needs `allow_failure: true`:
  without it the first red test skips the reviewer and fails the whole loop on iteration 1
  (`body: iteration 1: step 'check' failed`) — the agent never gets to see what broke.
- **Dry-run with stubs before you spend.** `--stubs-init` then `--stubs` costs nothing and proves
  the graph, the templates, the loop exit and every branch gated on an *agent's* answer. Script a
  *failure* into one entry too — the branch you never rehearse is the one that breaks live.
- **Declare `output_schema` properties, not just `type: object`.** A dry run answers a skipped
  shell/python step with the *minimal instance* of its schema, so a bare `{type: object}` hands
  the next step an empty `{}` and its `when:` fails; `properties` + `required` make the rehearsal
  take a **definite** branch instead of erroring. But always the *same* branch: the minimal
  instance is fixed (`boolean` → `false`, `array` → `[]`, `integer` → `0`), so a `when:` reading a
  shell or python output is answered identically every time. Stubs cannot help — they script
  agents, not scripts. To rehearse the other side, add `--exec-shell` so the real script answers.
- **Name a `class:` on every gate that matters**, so an operator's policy has a handle; and
  **declare `artifacts:`** for files you want after the run — it is both a promise (the step fails
  if the file is missing) and how a file written in the worktree gets copied into the run
  directory, hashed, and listed by `rayspec show`.
- **Conventions**: lowercase snake_case ids that read as verbs (`fetch`, `assess`, `build`); one
  workflow file per outcome; `description:` on every workflow and on any step whose purpose is not
  obvious from its id; `isolation: none` only for workflows that read.
- **No logic in a template.** No arithmetic pipelines, no nested `{% if %}` chains, no JSON
  assembled by hand — that is a `python:` step with an `output_schema`, which is testable,
  greppable and visible in `rayspec show`.

## Pitfalls and conventions

- Ids/`as:`/`session:` match `^[a-z][a-z0-9_]*$`, are unique across the whole file, and may not be
  a context root (`inputs steps run project env iteration each loop self true false none null`).
- Unknown keys are errors everywhere, with a suggestion (`unknown field 'allow_failures' for
  shell step; did you mean 'allow_failure'?`). A step with no kind key lists all nine.
- YAML: a `:` inside a plain scalar breaks the parse — `shell: echo '{"a": 1}'` fails with
  *"mapping values are not allowed here"*; use a block scalar (`shell: |`) or quote the whole
  value. Quote a scalar that starts with `{{`. Duplicate keys are errors.
- rayspec's loader takes booleans as `true`/`false` only, so `on`/`off`/`yes`/`no` stay strings
  (which is what makes `network: off` load) — but PyYAML and most other YAML 1.1 readers turn
  them into booleans, so an editor, a linter or a script reading the same file disagrees.
  **Quote them**: `network: "off"`.
- The expression-vs-template mistake, both directions: `{{ }}` in `when`/`until`/`each`/`auto_if`
  is an error, and a bare expression in a template field renders as literal text.
- `each:` rejects a mapping (*"must evaluate to a list, got dict — use .values()/.items()"*);
  tuples are fine, JSON text needs `| fromjson`.
- `session:` must name a transitive ancestor prompt step on the SAME provider (or, inside a loop
  body, the step's own id); otherwise *"'a' must be an ancestor of 'b' (add it to needs:)"*.
- `retry.attempts` is the TOTAL number of attempts (1-10); a timeout counts as retryable only
  with `on_error: all`. `allow_failure:` is valid on any step, `timeout:` is not.
- `access: read-only` cannot `allow` the `edit` or `shell` groups; `instructions` and
  `instructions_file` are mutually exclusive; `required` + `default` and `secret` + `default` are
  both load-time errors.
- `stop:` defaults to `status: cancelled` (exit 4); `stop: {status: succeeded}` still renders
  `outputs:`. Exhausting a loop is `failed` unless `on_exhausted: continue`.
- `include:` bodies nest at most 8 deep and may not form a cycle; `with:` keys must be declared
  inputs of the included workflow.

## Worked examples

Three complete workflows, each a different shape. All three validate clean and dry-run to
completion; `rayspec run <name> --dry-run --stubs-init stubs.yaml` writes the scaffold for the
prompt answers each one needs.

### 1. Self-healing loop (implement → check → review, until a signal)

```yaml
# .rayspec/workflows/selfheal.yaml — implement, test, review, repeat until the reviewer signs off
rayspec: 1
name: selfheal
description: Fix a task in a loop that ends on a signal line, not on a fixed number of passes.
inputs:
  task: { type: string, required: true, description: "What to implement" }
defaults:
  timeout: 15m                          # per attempt, per leaf step
  timeout_total: 2h                     # whole run, from its ORIGINAL start (a resume keeps counting)
agents:
  coder:                                # it edits; workspace-write confines it to the run's worktree
    { provider: claude, model: medium, access: workspace-write, tools: { deny: [web] },
      instructions: "Make the smallest change that passes the tests. Never edit a test." }
  critic:                               # a judge must not be able to "fix" what it judges
    { provider: claude, model: large, access: read-only, network: "off" }
steps:
  - id: baseline
    shell: pytest -q 2>&1 | tail -n 20
    allow_failure: true                 # a red baseline is the input to the task, not an error
  - id: build
    needs: [baseline]
    loop:
      max_iterations: 3
      until: steps.review.output | has_signal('SHIP-IT')
      on_exhausted: fail
      steps:
        - id: implement
          agent: coder
          session: implement            # keep this step's own transcript across iterations
          prompt: |
            {% if iteration.first %}
            Task: {{ inputs.task }}
            Baseline test output (exit {{ steps.baseline.exit_code }}):
            {{ steps.baseline.output }}
            {% else %}
            Attempt {{ iteration.n }} of {{ iteration.max }}. Address this review:
            {{ iteration.prev.review.output }}
            {% endif %}
            Edit the working tree. Do not commit.
        - id: check
          needs: [implement]
          shell: pytest -q 2>&1 | tail -n 40
          allow_failure: true           # the reviewer needs to SEE a failure, not inherit a skip
        - id: review
          needs: [check]
          agent: critic
          prompt: |
            Tests exited {{ steps.check.exit_code }} (tolerated: {{ steps.check.tolerated }}).
            {{ steps.check.output }}
            Review the working-tree diff. Reply with a line SHIP-IT when nothing is left to fix.
outputs:
  iterations: "{{ steps.build.iterations }}"
  converged: "{{ steps.build.converged }}"
```

Stub the loop so the second review converges, and the run ends `iterations 2 · converged true`:

```yaml
defaults: { latency_ms: 0 }
steps:
  "build[*]/implement": { text: "patched src/thing.py" }
  "build[*]/review":
    sequence:                           # nth call of this entry; the last item repeats
      - "Rename the helper; it shadows a builtin."
      - "SHIP-IT"
```

### 2. Fan-out with partial failure and a finally step

```yaml
# .rayspec/workflows/audit_sweep.yaml — fan out, tolerate the items that fail, always report
rayspec: 1
name: audit_sweep
description: Audit each package in parallel; one bad package must not lose the other verdicts.
inputs:
  packages: { type: array, items: { type: string }, default: [core, cli, web] }
  publish:  { type: boolean, default: false }
defaults:
  max_parallel: 4
  on_step_failure: continue             # independent branches keep running; the run still fails
agents:
  auditor: { provider: claude, model: small, access: read-only, network: "off" }
steps:
  - id: audit
    each: inputs.packages               # a bare expression that must yield a LIST
    as: pkg                             # the default name is `item`
    max_parallel: 2
    on_failure: continue                # a failed item becomes a null slot, the sweep goes on
    steps:
      - id: scan
        interpreter: bash
        shell: |
          set -euo pipefail
          echo "scanning {{ pkg }} ({{ each.index }}/{{ each.total }})"
      - id: judge
        needs: [scan]
        agent: auditor
        prompt: |
          Package {{ pkg }}:
          {{ steps.scan.output }}
          Answer with the JSON verdict.
        output_schema:
          type: object
          properties: { risk: { enum: [low, high] } }
          required: [risk]
  - id: report
    needs: [audit]
    join: always                        # finally: runs even if every item failed
    artifacts: [reports/audit.json]     # promised file, relative to the workdir; missing ⇒ step fails
    python: |
      import json, pathlib
      items = {{ steps.audit.items }}   # a Python literal, not a string — do not quote it
      ok = [i for i in items if i["status"] == "succeeded"]
      pathlib.Path("reports").mkdir(exist_ok=True)
      pathlib.Path("reports/audit.json").write_text(json.dumps(items, indent=2))
      print(json.dumps({"total": len(items), "ok": len(ok)}))
    output_schema:
      type: object
      properties: { total: { type: integer }, ok: { type: integer } }
      required: [total, ok]
  - id: gate
    needs: [report]
    when: inputs.publish
    approve:
      message: "{{ steps.report.output.ok }}/{{ steps.report.output.total }} packages audited — publish?"
      class: release                    # the OPERATOR's policy decides how strictly this is held
      auto_if: steps.report.output.ok == steps.report.output.total
      on_reject: cancel
  - id: publish
    needs: [gate]
    shell: echo "publishing"
outputs:
  audited: "{{ steps.report.output.total }}"
```

Script one item to fail — a `sequence:` whose second entry is
`{ fail: { kind: api, message: "model refused", transient: false } }` — and the sweep still
succeeds: `audit[1]/judge` fails, the `audit` step succeeds, `report` runs, and
`steps.audit.items` carries the null slot so the report can count it.

### 3. A reusable block with its own inputs and outputs

```yaml
# .rayspec/workflows/quality_block.yaml — a reusable block: own inputs, own outputs, closed scope
rayspec: 1
name: quality_block
description: Run one command and have an agent grade the result.
inputs:
  label:   { type: string, required: true }
  command: { type: string, required: true }
  strict:  { type: boolean, default: false }
steps:
  - id: run_check
    shell: "{{ inputs.command }}"
    allow_failure: true
    retry: { attempts: 2, delay: 2s, on_error: transient }
  - id: grade
    needs: [run_check]
    agent: { provider: claude, model: small, access: read-only }
    prompt: |
      Check "{{ inputs.label }}" exited {{ steps.run_check.exit_code }}.
      stdout:
      {{ steps.run_check.output }}
      stderr:
      {{ steps.run_check.stderr | default('(none)') }}
      Grade it{% if inputs.strict %} strictly{% endif %}.
    output_schema:
      type: object
      properties:
        status:  { enum: [pass, fail] }
        comment: { type: string }
      required: [status, comment]
outputs:                                # this is the block's whole interface to its caller
  label:  "{{ inputs.label }}"
  status: "{{ steps.grade.output.status }}"
  detail: "{{ steps.grade.output.comment }}"
```

```yaml
# .rayspec/workflows/pipeline.yaml — compose the block twice, then decide
rayspec: 1
name: pipeline
description: Two quality gates from one reusable block, then a verdict.
inputs:
  strict: { type: boolean, default: true }
steps:
  - id: lints
    include: quality_block              # by name, or by a path relative to the including file
    with: { label: lint, command: "ruff check .", strict: "{{ inputs.strict }}" }
  - id: types
    include: quality_block              # same block, second instance, its own scope
    with: { label: types, command: "mypy src", strict: "{{ inputs.strict }}" }
  - id: verdict
    needs: [lints, types]
    python: |
      import json
      results = [{{ steps.lints.output }}, {{ steps.types.output }}]   # each block's outputs:
      bad = [r["label"] for r in results if r["status"] != "pass"]
      print(json.dumps({"failed": bad, "blocked": bool(bad)}))
    output_schema:                      # spell the shape out: a dry run answers with the MINIMAL
      type: object                      # instance of this schema, so a bare `{type: object}`
      properties:                       # would hand `halt` an empty {} and fail its `when:`
        failed:  { type: array, items: { type: string } }
        blocked: { type: boolean }
      required: [failed, blocked]
  - id: halt
    needs: [verdict]
    when: steps.verdict.output.blocked
    stop:
      status: failed
      reason: "quality gates failed: {{ steps.verdict.output.failed | join(', ') }}"
outputs:
  clean: "{{ not steps.verdict.output.blocked }}"
```

The body steps are addressed as `lints/run_check`, `lints/grade`, `types/grade` — that is also
how the stub scaffold keys them, and how `rayspec logs <run> --step lints/grade` finds one.
`steps.lints.output` is the block's rendered `outputs:` mapping; the body's own step ids are
invisible from `pipeline`.

## CLI quick reference

Only the commands that **create or describe the authoring artifacts themselves** live here; every
command that executes, inspects or governs a run is in the `rayspec-cli` skill.

| Command | Purpose | Key flags | Exit |
|---|---|---|---|
| `rayspec init` | scaffold `.rayspec/` (+ both agent skills into `.claude/skills/`) | `--kind code\|content`, `--from EXAMPLE`, `--force`, `--no-skill`, `--root` | 0 / 2 |
| `rayspec new workflow <name>` | add one workflow file to a project that already exists (it never creates one) | `--agent NAME`, `--description`, `--force`, `--root` | 0 / 2 |
| `rayspec new agent <name>` | add one reusable `.rayspec/agents/<name>.yaml` | `--force`, `--root` | 0 / 2 |
| `rayspec schema [kind]` | print the published JSON Schemas (`workflow`, `run`, `events`, `stream`) — the machine-readable twin of the field index above | `--out DIR` | 0 / 2 |

## References (read on demand — same directory)

- `references/concepts.md` — mental model: DAG + bodies, step paths (`build[2]/implement`),
  lexical scopes, outputs, runs/resume, isolation (read when scoping or debugging references).
- `references/schema.md` — every field with its default, join table, statuses, durations, Jinja
  traps, strict YAML (read before using a field not shown above).
- `references/templating.md` — context roots, step views, shell/python body rules, filters,
  env of shell steps, input coercion, error messages.
- `references/examples.md` — the packaged example projects (what each shows) and the patterns:
  self-heal loop, branch-and-stop, fan-out with partial failure, reusable block, finally step.
- Everything about running what you wrote — `cli.md`, `providers.md`, `testing.md`, `policy.md`,
  `runs-and-resume.md`, `isolation.md`, `ci.md` — is in the **`rayspec-cli`** skill. Load it
  instead of guessing a flag or an exit code, and also to *write* the hand-written files this
  skill does not cover: stub scripts (`.rayspec/stubs/`), declarative test cases
  (`.rayspec/tests/<workflow>/<case>.yaml`, `checks.yaml`) and the operator files
  (`policy.yaml`, `trusted.yaml`, `rayspec.lock`).
- Online only, in neither skill: `extending.md` (plugins and the provider seam),
  `constitution.md` (why the DSL refuses fields), `agent-skill.md` (these two skills),
  `README.md` (the docs index) — at
  https://github.com/rayspec-labs/rayspec-py/blob/main/docs/.
