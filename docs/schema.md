# Workflow schema reference

Every field of a workflow file, with its default. The executable spec is
`src/rayspec/schema/`; this page mirrors it. Unknown keys are errors everywhere (with a
did-you-mean hint and the step's id in the message, e.g. `steps[3] (id: review)`).

Conventions: **expression** fields take a bare Jinja expression (no `{{ }}`); **template**
fields are Jinja text. See [templating.md](templating.md) for the language itself.

## Top level

```yaml
rayspec: 1                # required; the only schema version
name: fix_issue           # required; identifier (see below)
description: ""           # optional; shown by `rayspec workflows`
inputs: {}                # name → input spec
defaults: {}              # run-wide defaults (below)
isolation: worktree       # worktree | none   (--worktree/--no-worktree overrides)
agents: {}                # name → agent definition
steps: []                 # required; the DAG
outputs: {}               # name → template (deep-rendered when the run succeeds)
```

### Identifiers and names

- Step ids, `as:` and `session:` targets are **identifiers**: `^[a-z][a-z0-9_]*$` and not one of
  the reserved template roots `inputs steps run project env iteration each loop self true false
  none null`. Step ids must be unique across the whole file, bodies included.
- Keys of `inputs:`, `agents:`, `outputs:` are **names**: same syntax, no reserved-word check.
- `item` is *not* reserved (it is the default `as:` of `each:`).

### `defaults`

| Field | Default | Meaning |
|---|---|---|
| `agent` | `null` | Agent used by `prompt:` steps that set no `agent:`. |
| `timeout` | `null` | Per-attempt timeout applied to every step that sets none (a [duration](#durations)). |
| `max_parallel` | `4` | Leaf steps (prompt/shell/python) running at once, run-wide. |
| `on_unsupported` | `error` | `error` or `warn`: what a provider-capability mismatch is at validation time (`--allow-unsupported` also downgrades). |
| `on_step_failure` | `drain` | `drain`, `fail_fast` or `continue`. `drain` lets already-running siblings finish and starts nothing new; `fail_fast` cancels running siblings as soon as a step fails; `continue` keeps scheduling independent branches (the failed step's downstream cone skips (`upstream_failed`, then `upstream_skipped` below it)), **including inside `each:`/`loop:`/`include:` bodies** — the policy is run-level and global. **All three still fail the run** — `continue` is not `allow_failure` (per-step, *tolerates* the failure) and not `each.on_failure: continue` (per-item; see the note under [`each:`](#each)). `--fail-fast` on the command line may only ever tighten: it beats both `drain` and `continue`, and never downgrades a workflow that asked for `fail_fast`. |
| `budget_usd` | `null` | Run-level **cost cap** (circuit breaker): a positive USD amount (`1.5`, `"1.50"`, `"$1.50"`). Measured over the whole run from per-step cost — provider-reported, or estimated from the pricing table (`~$`); steps without any known cost cannot trip it. See [runs-and-resume.md](runs-and-resume.md#run-level-budget-circuit-breaker). |
| `max_tokens` | `null` | Run-level **token cap**: a positive integer or `"500k"` / `"1.5M"` (input + output tokens of every step, always known). Same breaker semantics as `budget_usd`. Shown by `rayspec plan`. |
| `timeout_total` | `null` | Run-level **wall-clock cap**: a [duration](#durations) > 0 (`30m`, `2h`). Measured from the run's *original* start, so a resume keeps counting — `2h` is two hours of run, not two hours per attempt. Same breaker semantics as `budget_usd`; not to be confused with `timeout` above, which is per attempt of one step. |

When a cap is exceeded no new step starts (pending steps are skipped with `skip_reason:
budget_exceeded`; `join: always` steps and resume replays still run) — a step that is already
waiting for a `max_parallel` slot is asked again when it gets one, so a backlog does not outlive
the cap — running steps finish (drain), and the run ends `failed` with reason
`budget exceeded (tokens 12,000 > max_tokens 10,000)` — or
`time limit exceeded (elapsed 2h 4m > timeout_total 2h 0m)` for `timeout_total` (exit 1). The
three caps are one breaker: whichever trips first ends the run the same way. Raise the cap and resume
(`rayspec resume <run> --force` — the workflow hash changed): finished steps are replayed and
count towards the new cap, the rest runs. Caps apply to the root workflow only (an included
workflow's `defaults.budget_usd`/`max_tokens`/`timeout_total` are ignored); the per-agent
`budget_usd` is a different thing (one prompt step's native budget where the provider supports
it).

### `inputs`

```yaml
inputs:
  issue: { type: integer, required: true, description: "Issue number" }
  base:  { type: string, default: main }
  mode:  { type: string, enum: [fast, normal], default: normal }
  tags:  { type: array, items: { type: string }, default: [] }
```

| Field | Default | Meaning |
|---|---|---|
| `type` | `string` | `string` · `integer` · `number` · `boolean` · `array` · `object` |
| `required` | `false` | A required input cannot also have a `default` (load error). |
| `default` | — | Used when nothing else provides a value. An absent, non-required input without default is *undefined* in templates (`inputs.x is defined`). |
| `description` | `null` | Shown by `rayspec plan`. |
| `enum` | `null` | Allowed values (JSON Schema). |
| `items` | `null` | JSON Schema for array items (also drives CLI coercion of repeated `--input`). |
| `properties` | `null` | JSON Schema for object members. |
| `secret` | `false` | The value is **never persisted** and reaches `shell:`/`python:` steps **through the environment only** — see *Secret inputs* below. Cannot be combined with `default` (load error); `required: true` works as usual. |

All inputs compile into one JSON Schema object with `additionalProperties: false`; the
resolved values are validated against it. How values are supplied and coerced is in
[templating.md § Inputs](templating.md#inputs).

#### Secret inputs

```yaml
inputs:
  token: { type: string, secret: true, required: true }
steps:
  - id: push
    shell: gh auth login --with-token <<<"$RAYSPEC_INPUT_TOKEN" && gh pr create …
  - id: api
    shell: curl -H "Authorization: Bearer $GH_TOKEN" …
    env: { GH_TOKEN: "{{ inputs.token }}" }      # the ONE template that may name a secret
```

Ordinary input values are persisted in clear text for the life of a run (`run.json`,
`context.json`, `events.jsonl`, wherever a template renders them) and printed by `plan`/`show`.
An input declared `secret: true` is different:

- **Never persisted, never printed.** `run.json` stores the placeholder `"<secret>"` under
  `inputs` (and lists the names under `secret_inputs`), every step's `context.json` holds
  `"<secret>"`, `run.started`/`plan`/`show`/`--json` print `"<secret>"`, and `rayspec plan` /
  `rayspec validate` mark the input `(secret)`; even a value the input rejects (wrong type,
  not in `enum`) is reported as `<secret>`, never echoed. Leaf fingerprints are computed from the
  placeholder too, so a resumed step is replayed whatever value you supply.
- **Delivered through the environment only.** A `shell:`/`python:` step sees the real value as
  `RAYSPEC_INPUT_<NAME>` and through its own `env:` mapping (`env: {TOKEN: "{{ inputs.token }}"}`
  renders into that step's process environment, which is not persisted). Values are supplied
  like any input: `--input name=value`, `--inputs-file`, or `RAYSPEC_INPUT_<NAME>` in your shell.
- **Every other reference is a load-time error** naming the step and field:
  `steps.ask.prompt: inputs.token is declared secret: true — secret inputs can only reach
  shell/python steps via RAYSPEC_INPUT_TOKEN (or a shell/python env: mapping); …` — prompt
  bodies and prompt-step `env:`, agent `instructions`, the `shell:`/`python:` body itself
  (`{{ inputs.token }}` would be spilled into the script), `cwd:`, `when:`/`until:`/`each:`,
  `outputs:`, `approve:` messages, `stop.reason`, include `with:`. Using `inputs` as a whole
  (`inputs | tojson`, `inputs.get(...)`, `inputs.items()`, `inputs[expr]`) anywhere but a
  shell/python `env:` mapping is refused the same way as soon as one secret is declared (`inputs
  is used as a whole while inputs.token is declared secret: true — …`): name the other inputs
  individually. An included workflow cannot declare a secret input (its `with:` binding would be
  persisted); secrets belong to the root workflow and are exported to every shell/python step of
  the run, include bodies included (a body's own same-named input keeps its bound value).
- **Resume re-obtains them.** Because nothing is stored, `rayspec resume|approve|reject` and
  `run --resume` must get every secret that was given at launch again: `--input name=value`
  (accepted on resume for secret inputs only — other inputs stay fixed per run), then a
  [`secrets:` source](#secret-sources-secrets-in-configyaml) with that name, then
  `RAYSPEC_INPUT_<NAME>`, else exit 2 `missing secret input(s): token — pass --input token=… or
  set RAYSPEC_INPUT_TOKEN`. `rayspec show` on a resumable run prints a `secret inputs to
  re-supply:` line naming exactly what it still needs. See
  [runs-and-resume.md § Resume](runs-and-resume.md#resume).

> **Limits.** A secret reaches a step's *environment*; whatever the step then **prints** is its
> output. Every value rayspec knows is passed through a **redactor** on its way to
> every writer, so `echo "$RAYSPEC_INPUT_TOKEN"` now lands in `steps/<path>/output.txt`,
> `stdout.log`, `stream.jsonl` and the console as `[REDACTED:token]` — see
> [*Redaction*](#redaction-exact-match-best-effort) for what that does and does not buy you.
> Agent (`prompt:`) steps still cannot receive secrets: no template may name them, and a prompt
> step's `env:` is refused for secrets because one of the two supported CLIs writes the child's
> environment to a `0644` file outside the run store — the evidence is in
> [providers.md § Giving an agent a secret](providers.md#giving-an-agent-a-secret-tools-not-prompts),
> which also shows the pattern that *does* work (a tool holds the credential, the agent gets a
> capability). Values of the `env` root are never persisted either (`context.json` omits it),
> but anything a template renders from `{{ env.X }}` into a prompt or output *is* stored — the
> same rule as before.

#### Secret sources (`secrets:` in `config.yaml`)

An input is something a *run* is given. A **secret source** is something a *machine* has. The
`secrets:` block of `config.yaml` (user-level `~/.rayspec/config.yaml` or project-level
`.rayspec/config.yaml`, merged per key) says where each named secret comes from:

```yaml
# ~/.rayspec/config.yaml
secrets:
  GITHUB_TOKEN: { env: GH_TOKEN }                       # an environment variable
  DEPLOY_KEY:   { file: ~/.secrets/deploy_key }         # a file, refused unless 0600 or tighter
  API_KEY:      { cmd: "op read op://private/api/key" } # the stdout of a command
  OPTIONAL:     { env: MAYBE, required: false }         # absent ⇒ simply not exported
```

Exactly one of `env`, `file` or `cmd` per entry (`cmd` may be a string — split like a shell word
list, **not** run through a shell — or an argv list). What happens with them:

- **Resolved lazily, at run start** — and only the entries *this* workflow can read: a name is
  looked up when a `shell:`/`python:` step of the workflow mentions it (in its body, its `env:`
  or its `cwd:`), or when it supplies a `secret: true` input. A stale entry in
  `~/.rayspec/config.yaml` therefore does not break unrelated projects, and a `cmd:` helper does
  not run (or prompt for Touch ID) on every `rayspec run`. The exception to know: a script that
  reads the environment *dynamically* (`env | grep TOKEN`) never mentions the name, so name it
  in the script or export it in the shell that launches rayspec.
- **A source the run needs is exit 2** before anything is written, with a message that names
  `secrets.<NAME>` and the source and never the value (`secrets.DEPLOY_KEY: secret file /… is
  mode 0644; it must not be readable by group or others`, hint `chmod 600 …`). A failing `cmd:`
  reports the program and its exit code but never its output — set `RAYSPEC_DEBUG=1` to add the
  last stderr line when you are debugging the helper.
- **The name becomes an environment variable**, so it must look like one: letters, digits and
  `_`, not starting with a digit, never `RAYSPEC_*` and never `PATH`, `HOME`, `PWD`, `SHELL`,
  `IFS`, `LD_*` or `PYTHONPATH`. Anything else is refused when the config is read, where the
  message can still name the key — a `PATH` entry would otherwise surface as a step failing with
  `No such file or directory: 'bash'`.
- **Delivered as environment variables to `shell:`/`python:` steps only** — under their own name
  (`$GITHUB_TOKEN`), in every scope of the run, include bodies included. A step's own `env:`
  still wins. Prompt steps, templates, `context.json`, fingerprints and `run.json` never see them.
- **They can supply a `secret: true` input.** An entry whose name matches a secret input is used
  for that input, on the first run and on every resume entry. Precedence: `--input` /
  `--inputs-file` > `secrets:` > `RAYSPEC_INPUT_<NAME>` > `default`.
- **`rayspec doctor` lists them** (`GITHUB_TOKEN ← env GH_TOKEN`) and reports a source that does
  not resolve — never a value. That row is a *required* check: doctor exits non-zero on a source
  `rayspec run` would refuse to start on.
- **A value that is a bare JSON token needs quoting.** If a step prints `{"pin": 12345678}`
  under an `output_schema` and `12345678` is a secret, the redacted stdout the schema check sees
  is `{"pin": [REDACTED:PIN]}` — not a JSON document. The failure says so and names the secret;
  print the value quoted (`{"pin": "12345678"}`) and the stored document stays valid.

This is not a secret *store*: rayspec holds no vault and writes no credential file. It reads
what your machine already has.

#### Redaction (exact match, best effort)

Every value rayspec knows — the `secret: true` inputs that were given and every resolved
`secrets:` entry — is replaced with `[REDACTED:<name>]` on its way to **every writer**:
`run.json`, `steps/**/output.*`, `events.jsonl`, `stream.jsonl`, the executor's
`stdout.log`/`stderr.log`, the console tree and `--json`. A value split across two streamed
chunks is caught too: a stream holds back the tail that could still *grow* into a known value
(and nothing else, so a live log never lags behind a long-running step), and the buffer is
flushed when the step finishes — what `stream.jsonl` reassembles to is always exactly what the
step produced.

Be clear about what this is:

- it is **exact match**. It cannot catch a value an agent or a script transformed — base64, URL
  encoding, "the token starts with `ghp_` and ends with `3f`", a value reassembled from pieces;
- it is a **safety net under the load-time refusals**, not a replacement for them. The guarantee
  is still that a secret never reaches a prompt, an expression, an output or the store;
- values shorter than 4 characters are **not** redacted — replacing every `ab` in a transcript
  destroys the log without protecting anything. rayspec does not do it silently: the run prints
  `warning: <NAME> is shorter than 4 characters and is therefore not redacted` and `rayspec
  doctor` flags the row. Note the other end of the same trade-off: a 4-character secret like
  `test` also rewrites every ordinary word containing it;
- pattern **detectors** for well-known credential shapes are opt-in and default to off, because
  a false positive in a run log is worse than the gap:

```yaml
# ~/.rayspec/config.yaml
redact:
  detectors: [github, openai, aws, jwt, pem]   # or [all]; default: [] (off)
```

Detector matches are bounded so they can be caught across a chunk boundary too: a `pem` block up
to 8 KiB, a token up to 4 KiB. A longer one is still redacted when it arrives in one piece.

What is deliberately *not* here — rayspec storing credentials itself (a vault, a keychain-writing
`rayspec secrets set`), and redaction that survives transformation — is on the roadmap, not in
this release.

### `agents`

A (possibly partial) agent; unset fields are filled by merge and tier resolution.

```yaml
agents:
  implementer:
    provider: codex             # claude | codex | stub | <plugin id>; default: config.default_provider (claude)
    model: medium               # literal id | tier small|medium|large | "@alias"; default: the medium tier
    effort: high                # none|minimal|low|medium|high|xhigh|max|ultra (the provider's capability table decides which are accepted)
    access: workspace-write     # read-only | workspace-write | full
    instructions: "..."         # template; xor instructions_file
    instructions_file: prompts/implementer.md   # relative to the .rayspec/ dir of the file that set it
    instructions_mode: append   # append | replace
    max_turns: 60               # >= 1; capability max_turns
    budget_usd: 2.5             # > 0;  capability budget_usd
    tools: { allow: [], deny: [web] }   # see below
    thinking: true              # capability thinking
    on_denial: fail             # warn (default) | fail — what a refused tool call does
    mcp:                        # capability mcp_servers
      github: { transport: stdio, command: gh-mcp, args: [--stdio], env: {} }
      docs:   { transport: http, url: https://mcp.example/, headers: {} }
    provider_options:           # raw per-provider pass-through; replaces wholesale on override
      codex:  { config: { model_reasoning_summary: concise } }
      claude: { setting_sources: [project, user] }
```

| Field | Default |
|---|---|
| `provider` | `null` → `config.default_provider` (built-in default `claude`) |
| `model` | `null` → the provider's `medium` tier (from `config.tiers`, else the built-in tiers: Claude `haiku/sonnet/opus`, Codex `gpt-5.4` with `effort low / – / high`). A tier with no configured model warns and leaves the provider default. `@alias` comes from `config.aliases` and may also pin `provider` and `effort`. |
| `effort` | `null` (the tier or alias may supply one) |
| `access` | `workspace-write` |
| `instructions` / `instructions_file` | `null`; at most one of them |
| `instructions_mode` | `append` (`replace` = no provider system prompt at all) |
| `max_turns`, `budget_usd`, `thinking` | `null` |
| `on_denial` | `warn` — a tool call the provider's permission or sandbox layer refused is recorded on the step (`steps.<id>.denials`, `run.json`) and the step stands. `fail` fails the step instead. See [providers.md](providers.md#denied-tool-calls-on_denial) |
| `tools.allow`, `tools.deny` | `[]` |
| `mcp` | `{}`; `transport` defaults to `stdio` (needs `command`); `http`/`sse` need `url` |
| `provider_options` | `{}` |

**Tool vocabulary** (`tools.allow`/`tools.deny`): groups `read edit shell web agent mcp`,
`mcp:<server>` (all tools of a server), `mcp:<server>/<tool>`, or a provider-native name
prefixed with the provider id (`claude:WebFetch`). A raw name addressed to another provider is
ignored with a warning — `rayspec validate` and every `rayspec run` print it once per agent that
carries the entry; to avoid the noise keep `<provider>:` names on agents of that provider or
override `tools:` where the agent is used (`tools` replaces wholesale in `extends`).
`access: read-only` cannot `allow` `edit` or `shell`. How each provider honours the policy is
in [providers.md](providers.md#access-levels-and-tools).

**Resolution for a `prompt:` step**: step `agent:` (a name, `{extends: <name>, ...}` or an inline
mapping) > `defaults.agent` > `config.default_provider`. A bare provider id (`agent: claude`)
is that provider's default agent. Named lookup: workflow `agents:` > `.rayspec/agents/<name>.yaml`
> `~/.rayspec/agents/<name>.yaml`. `extends` is a shallow merge: only the override's explicitly
set keys apply; `tools` and `provider_options` replace wholesale. `rayspec plan` prints the
resolved agents.

## Steps

Exactly one kind key per step. Fields that are valid on every kind:

| Field | Default | Meaning |
|---|---|---|
| `id` | required | identifier, unique per file |
| `description` | `""` | free text |
| `needs` | `[]` | sibling ids (same `steps:` list only) |
| `when` | `null` | **expression**; must evaluate to exactly `true`/`false` (`false` → skipped with `when_false`; anything else or an error → the step **fails**) |
| `join` | `all` | `all` · `any` · `always` (see the [truth table](#join-truth-table)); a warning without `needs` |
| `timeout` | `null` | [duration](#durations) > 0; per attempt for leaves, whole-step for composites; falls back to `defaults.timeout` |
| `always_run` | `false` | ignore the resume cache (re-run on `--resume`) |
| `allow_failure` | `false` | a failure is recorded as `failed` + `tolerated: true` (`ok: false`); joins treat it as satisfied; the run status is unaffected |
| `artifacts` | `[]` | files this step promises to write, relative to its working directory (`cwd:` for `shell:`/`python:`, else the run's workdir). Checked after the step succeeds — a missing one, one that is not a regular file, or one outside the run's workspace, **fails the step** — then copied into the run directory and recorded with a sha256. Absolute paths, `~`, `..` and control characters are refused when the workflow loads, and so is `{{ … }}`: the entry is **not templated** — name a fixed file and put what varies per item in the step's `cwd:`, which *is* rendered. The same file declared twice is kept once. See [runs-and-resume.md](runs-and-resume.md#declared-artifacts). |

Leaf steps (`prompt`, `shell`, `python`) add:

| Field | Default | Meaning |
|---|---|---|
| `retry` | kind default | `{attempts, delay, on_error}` — `attempts` (1–10) is the **total** number of attempts (`1` = no retry), `delay` a duration (default `3s`) that doubles after each retry, `on_error` `transient` (default) or `all`. Default for `prompt:` is `attempts: 3, delay: 3s, on_error: transient`; `shell:`/`python:` have no retry. Per-attempt timeouts count as transient only with `on_error: all`. |
| `env` | `{}` | template values, str-coerced (`true`/`false`, numbers as text); on a `prompt:` step it needs the `env_injection` capability |
| `output_schema` | `null` | JSON Schema the output must satisfy; the step output becomes the parsed value |

### `prompt:` / `prompt_file:`

```yaml
- id: review
  needs: [check]
  agent: reviewer            # name | {extends: name, ...} | inline agent mapping | omitted
  prompt: |                  # template — xor prompt_file (relative to the .rayspec/ dir)
    Validation exit={{ steps.check.exit_code }}:
    {{ steps.check.output }}
  session: implement         # continue that step's session (same provider); self inside a loop
  output_schema: { type: object, properties: { ok: { type: boolean } }, required: [ok] }
```

The only kind that calls a provider. Output: the agent's text, or the validated JSON value with
`output_schema` (`enforced` providers return JSON natively; the engine validates and re-asks once
through the session before failing). Extra attributes: `session`, `model`, `usage`, `cost_usd`.
`session:` must name a transitive ancestor that is a prompt step on the **same** provider (or the
step's own id inside a loop body, which continues the previous iteration's session).

### `shell:`

```yaml
- id: test
  shell: pytest -q
  interpreter: bash          # bash (bash -euo pipefail -c) | sh (sh -eu -c)
  cwd: packages/api          # template; relative to run.workdir; default run.workdir
  env: { PYTEST_ADDOPTS: "-x" }
  allow_failure: true
```

Output: stdout with trailing newlines stripped; `exit_code`, `stderr`, `ok`. Non-zero exit fails
the step (tolerated with `allow_failure`). Every `{{ expr }}` in the body becomes an environment
variable reference — read the [shell rule](templating.md#shell-bodies) before writing scripts.
stdout/stderr are also kept in `steps/<path>/stdout.log` / `stderr.log`.

**Environment.** The child process inherits rayspec's environment plus `RAYSPEC_*`
([templating.md](templating.md#shell-bodies)), `RAYSPEC_CONTEXT`, `RAYSPEC_STEP_PATH`, the step's
`env:` and the `{{ }}` slots. Variables that only describe how rayspec itself was launched are
**scrubbed** first: `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `UV_PROJECT_ENVIRONMENT`, `PYTHONHOME`,
and any other variable whose value is a path inside rayspec's own virtualenv. A list value
(`os.pathsep`-separated, e.g. `PYTHONPATH=/rayspec/.venv/…/site-packages:/home/me/lib`) loses only
the entries inside that virtualenv (`/home/me/lib` stays; the variable disappears only when no
entry is left). `PATH` is left alone. So `uv`/`pip`/`python` inside a step see the
step's project, not rayspec's (`uv run` launched rayspec from `.venv` ⇒ no "VIRTUAL_ENV does not
match the project environment" warnings). A step that needs one of those variables sets it in its
own `env:` — explicit values always win.

### `python:`

```yaml
- id: summarize
  python: |
    import json
    data = {{ steps.fetch.output | fromjson }}
    print(json.dumps({"n": len(data)}))
  deps: [httpx]              # runs `uv run --no-project --with httpx python -`; else sys.executable
  cwd: .
  output_schema: { type: object }
```

Same output contract as `shell:`; `{{ expr }}` renders as a Python literal
([python bodies](templating.md#python-bodies)). The script is fed on stdin.

### `loop:`

```yaml
- id: build
  needs: [assess]
  loop:
    max_iterations: 3          # required, >= 1
    until: steps.review.output | has_signal('BUILD-CLEAN')   # expression over the body; optional
    on_exhausted: fail         # fail (default) | continue
    steps: [...]               # required, non-empty
```

Do-while: the body runs, then `until` is evaluated over that iteration's step views. Exhausting
`max_iterations` with `until` still false is `failed` (`error.type: exhausted`) unless
`on_exhausted: continue` (→ succeeded with `converged: false`). Without `until` the body runs
exactly `max_iterations` times and `converged` is `true`. A failed body step fails the loop.
Inside the body: `iteration.n` (1-based), `iteration.max`, `iteration.first`,
`iteration.prev.<body id>` (the previous iteration's step view; undefined on iteration 1).
Output: `{<body id>: output}` of the last executed iteration; attributes `iterations`, `converged`.

### `each:`

```yaml
- id: triage
  each: steps.list.output | fromjson      # expression → a list (mappings are rejected: use .values())
  as: issue                               # default item
  max_parallel: 3                         # extra limiter for this fan-out (default: none)
  on_failure: fail                        # fail (default) | continue
  steps: [...]                            # required, non-empty
```

Items run concurrently under their own limiter (the run-wide `max_parallel` still applies). An
empty list succeeds with `[]`. Inside the body: `<as>` (the item), `each.index` (0-based),
`each.total`. Output: a list aligned with the items of `{<body id>: output}`; `null` slots for
failed items under `continue`. Attribute `items`: `[{index, item, status, output, error}]`. With
`on_failure: fail` the step fails after every item finished.

> **Scoping caveat.** `defaults.on_step_failure` is resolved from the **root** workflow only. An
> `include:`d workflow that declares its own value is ignored in both directions — unlike
> `defaults.timeout`, which *is* lexically scoped to the body. Set the policy on the root
> workflow.

> **Two different `continue`s — they do not mean the same thing.** `each.on_failure: continue`
> (here) is about **items**: a failed item does not fail the `each` step. `defaults.on_step_failure:
> continue` (run level) is about **steps**: a failed step does not stop its independent siblings
> from being scheduled — including inside an `each`/`loop`/`include` body, since the run-level
> policy is global. They are independent and compose:
>
> | | `each.on_failure` | `defaults.on_step_failure` |
> |---|---|---|
> | scope | one `each:` step | the whole run, bodies included |
> | governs | does a failed **item** fail the `each` step? | does a failed **step** stop its independent siblings? |
> | `continue` means | tolerate the item, `null` in the output slot | keep scheduling; the run **still fails** |
>
> Setting only `defaults.on_step_failure: continue` leaves `each.on_failure` at its `fail`
> default — the `each` step still fails, and only its *siblings* keep going.

### `approve:`

```yaml
- id: confirm
  needs: [build]
  approve: "Open a PR for the fix?"                 # or {message: ..., on_reject: cancel|continue|fail}
```

A human gate. On a terminal (and without `--no-interactive`/`--yes`) it prompts with
`[a]pprove [r]eject [v]iew [d]iff [p]ause`; otherwise the run pauses (exit 3) until it is resumed.
`--yes` and `--dry-run` auto-approve. Reject: `cancel` (default) → step `rejected`, siblings
cancelled, run `cancelled` (exit 4); `continue` → succeeded with `approved: false`; `fail` →
failed. Output: the approver's comment (`''` if none); attribute `approved`. Details in
[runs-and-resume.md](runs-and-resume.md#approval-gates).

### `include:`

```yaml
- id: review
  include: review_block                   # workflow name (discovery) or a path relative to this file
  with: { target: "{{ inputs.target }}", strict: true }   # deep-rendered; validated against its inputs
```

Inlined at load time (cycles and depth > 8 are load errors). `with:` keys must be declared in the
included workflow's `inputs:`; required ones must be given; values are coerced and validated.
The body is a closed scope (its own steps + its `inputs`; `rayspec validate` rejects a reference
to the including workflow's steps, exactly as the engine does at run time); the included
workflow's `defaults.agent` and `defaults.timeout` apply inside it (`max_parallel` is run-wide). Output:
the included workflow's rendered `outputs:` map — `steps.review.output.<key>` is checked against
that map at load time.

### `stop:`

```yaml
- id: bail
  needs: [assess]
  when: steps.assess.output.verdict == 'skip'
  stop: { status: cancelled, reason: "Not worth fixing — {{ steps.assess.output.reason }}" }
```

Ends the run with `status` (`succeeded` · `failed` · `cancelled`, default `cancelled`) and the
rendered `reason`. Running siblings are cancelled (`interrupted`, skip reason `stopped`), pending
ones `skipped`. `succeeded` still renders the workflow `outputs:`. Exit code 0 / 1 / 4.

## Join truth table

`join` is evaluated only once **all** `needs` are terminal; a tolerated failure counts as
succeeded.

| needs outcome | `all` (default) | `any` | `always` |
|---|---|---|---|
| all succeeded | run | run | run |
| ≥ 1 skipped, rest succeeded, none failed | skip (`upstream_skipped`) | run | run |
| all skipped | skip (`upstream_skipped`) | skip | run |
| ≥ 1 failed (untolerated, incl. interrupted/rejected) | skip (`upstream_failed`) | skip | run |
| run draining (a sibling failed) or cancelled | skip (`run_failed`) | skip | run |

> **Exception — fail-fast.** Under `--fail-fast` (or `defaults.on_step_failure: fail_fast`) the run cancels immediately and *every* pending step is skipped `run_failed`, **including `join: always`** ones: once the task group is torn down there is nothing left to run them in. So `always` gives you finally-semantics under `drain` and `continue`, but not under fail-fast. Whether that is the behaviour we want is still an open question.

Then `when:` is evaluated. A failure anywhere in a sibling list puts that list into **drain**:
nothing new starts except `join: always` steps, running siblings finish; `--fail-fast` cancels
them instead.

## Status vocabulary

- Step status: `pending` · `running` · `succeeded` · `failed` · `skipped` · `interrupted` ·
  `paused` · `rejected`.
- Skip reasons (`steps.<id>.skip_reason`): `upstream_skipped` · `upstream_failed` · `run_failed`
  · `when_false` · `stopped` · `paused` · `failed` · `interrupted`.
- Run status: `running` · `succeeded` · `failed` · `cancelled` · `paused` · `interrupted`.
- Exit codes: `0` succeeded · `1` failed · `2` usage/validation error · `3` paused ·
  `4` cancelled · `130` interrupted.

## Durations

A number is seconds (`90`, `0.5`); a string combines `h`, `m`, `s`, `ms`: `"90s"`, `"10m"`,
`"1h30m"`, `"500ms"`. `timeout` must be > 0; `retry.delay` may be `0`.

## Expression vs template fields

| Expression fields (bare Jinja, `{{ }}` is a lint error) | Template fields |
|---|---|
| `when`, `loop.until`, `each` | `prompt`, `prompt_file` contents, `instructions`, `shell`, `python`, `approve.message`, `stop.reason`, `cwd`, `env` values, `with` values, `outputs` values |

A template that is exactly one `{{ expr }}` keeps the expression's type (so `with:`/`outputs:`/
`env:` can pass lists and objects through; `env` values are str-coerced afterwards).

## Jinja traps

- `when: "{{ x }}"` is an error: expression fields are bare expressions.
- `${{ x }}` in a `shell:` body is GitHub-Actions syntax and a lint error; write `{{ x }}`
  (it renders as `${RAYSPEC_V<n>}`).
- Literal braces in code bodies (`docker --format '{{.ID}}'`, `gh ... --json -q`, `kubectl -o
  go-template`, `helm`, `printf '{{'`) must be wrapped in `{% raw %} ... {% endraw %}`.
- Code bodies use `{{# ... #}}` as Jinja comment delimiters so bash `${#VAR}` survives.
- `{% macro %}`, `{% call %}`, `{% filter %}` and `{% set x %}…{% endset %}` blocks are rejected
  in shell/python bodies; use `{% set x = expr %}` and inline filters.
- Inside single quotes or a quoted heredoc (`<<'EOF'`) bash does not expand
  `${RAYSPEC_V1}` — use double quotes or an unquoted heredoc.
- YAML: a scalar starting with `{{` must be quoted (`prompt: "{{ steps.a.output }}"`), and the
  strict loader accepts only `true`/`false` spellings as booleans (`yes`, `no`, `on` stay
  strings), rejects duplicate keys, sexagesimal numbers and timestamps.

## Editor support

rayspec publishes a JSON Schema for the workflow document, generated from the same Pydantic
models the loader uses. Add the modeline as the **first line** of a workflow file and an editor
with [yaml-language-server](https://github.com/redhat-developer/yaml-language-server) (VS Code +
the YAML extension, Neovim, Helix, JetBrains) completes field names, shows the docs of a field
and flags typos while you type:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/rayspec-labs/rayspec-py/main/schemas/workflow.schema.json
rayspec: 1
name: example
steps:
  - id: files
    shell: git ls-files | head -n 50
```

`rayspec init` writes that line into the workflows it scaffolds, and every packaged example
carries it.

Offline or air-gapped? Write a local copy and point the modeline at it. The command prints a
ready-made modeline with the absolute `file://` URL of the copy it wrote — paste that line:

```console
$ rayspec schema workflow --out .rayspec/
wrote workflow.schema.json to .rayspec
editor modeline (local copy): # yaml-language-server: $schema=file:///abs/path/.rayspec/workflow.schema.json
```

A relative `$schema` resolves against the **workflow file**, and workflows live one level deeper
than the copy above, so from `.rayspec/workflows/<name>.yaml` the relative form is
`$schema=../workflow.schema.json` (`./workflow.schema.json` would point at a file that does not
exist). Write the copy next to the workflows — `rayspec schema workflow --out .rayspec/workflows/`
— if you prefer `./workflow.schema.json`.

Four schemas are published — `workflow`, `run` (`run.json`), `events` (`events.jsonl`) and
`stream` (`stream.jsonl`); see [cli.md → `rayspec schema`](cli.md#rayspec-schema). They are
checked in under [`schemas/`](../schemas) and regenerated by
`uv run python scripts/gen_schemas.py` (`--check` fails when they drift from the models).

The workflow schema is an **editor aid, not the validator**. It is deliberately relaxed where a
field accepts more spellings than its Python type describes (`timeout: 30m`, `budget_usd:
"$1.50"`, `approve: <message>`), and a JSON Schema cannot express the graph, references,
includes, agent resolution or provider capabilities. `rayspec validate` remains the authority —
it reports every problem of a document with its `file:line`.

## Strict YAML

Workflow, agent, config and input files are read with a strict SafeLoader: booleans are
only `true/false/True/False/TRUE/FALSE`; `0123` stays a string (`0o17` is octal); dates stay
strings; duplicate keys are errors.
