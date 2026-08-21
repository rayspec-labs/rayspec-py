# Providers

`prompt:` steps are the only steps that call a model. They do so through one neutral adapter
contract; `claude` (Claude Agent SDK), `codex` (OpenAI Codex SDK) and `stub` (scripted, used by
`--dry-run`) are built in, and more can be added through entry points
([extending.md](extending.md)).

## The neutral adapter

```
AgentRequest  ──▶  provider.run(req, emit)  ──▶  AgentResult
  step_path, prompt, cwd, access, instructions(+mode),      status success|error|interrupted|timeout|max_turns|budget
  model, effort, tools, env, max_turns, budget_usd,         text, structured, session_ref, usage, cost_usd,
  thinking, output_schema, resume_session, mcp_servers,     cost_source provider|table|none, model, error, raw
  timeout_s, provider_options (this provider's block)
            │
            └── emit(AgentEvent) stream: session · text_delta · text · reasoning · tool_call · tool_result ·
                command_start/output/end · file_change · plan · usage · warning · error · raw
                (recorded to steps/<path>/stream.jsonl and forwarded to the console / --json)
```

Each provider registration carries a static **capability table** that the validator maps YAML
fields onto; validation therefore never loads an SDK. Structured output is owned by the engine:
with `enforced` providers the schema is passed through and the returned value is validated with
jsonschema (one re-ask through the session on an invalid value, then the step fails); `best_effort`
would append a JSON instruction and extract JSON from the text (no built-in provider needs it).

## Capability matrix

Generated from the registry by `scripts/gen_capability_matrix.py` (`rayspec providers` prints the
same table; `tests/docs` fails when this block is stale).

<!-- capability-matrix:start -->
| capability | `claude` | `codex` | `stub` | meaning |
|---|---|---|---|---|
| `structured_output` | enforced | enforced | enforced | `output_schema` on prompt steps (`enforced`: the SDK returns JSON; `best_effort`: the engine asks for JSON and extracts it; `none`: unsupported) |
| `session_resume` | ✔ | ✔ | ✔ | `session: <step>` continues an earlier step's session |
| `session_fork` | ✔ | ✔ | ✔ | forking a session (reserved for the engine; no YAML field yet) |
| `instructions_modes` | append, replace | append, replace | append, replace | `instructions_mode: append` / `replace` |
| `access_levels` | read-only, workspace-write, full | read-only, workspace-write, full | read-only, workspace-write, full | `access:` levels the provider can enforce |
| `tool_groups` | read, edit, shell, web, agent, mcp | web | read, edit, shell, web, agent, mcp | neutral groups accepted in `tools.allow` / `tools.deny` |
| `raw_tool_names` | ✔ | ✘ | ✔ | provider-native names as `<provider>:<Name>` in `tools` |
| `max_turns` | ✔ | ✘ | ✔ | `max_turns` on the agent |
| `budget_usd` | ✔ | ✘ | ✔ | `budget_usd` on the agent |
| `cost_reporting` | ✔ | ✘ | ✔ | the provider reports USD cost itself (else the pricing table is used) |
| `effort_levels` | low, medium, high, xhigh, max | none, minimal, low, medium, high, xhigh, max, ultra | none, minimal, low, medium, high, xhigh, max | `effort:` values accepted |
| `effort_aliases` | minimal→low | — | — | effort values rewritten with a warning |
| `thinking` | ✔ | ✘ | ✔ | `thinking: true` / `false` on the agent |
| `denial_reporting` | ✔ | ✘ | ✔ | the adapter reports the tool calls a turn was REFUSED, so `on_denial: fail` can grade them |
| `mcp_servers` | ✔ | ✔ | ✔ | `mcp:` servers on the agent |
| `env_injection` | ✔ | ✔ | ✔ | `env:` on prompt steps |
| `images` | ✘ | ✘ | ✔ | image inputs (not used by any YAML field in v1) |
| `extra` | — | — | — | provider-specific extras (none declared) |
<!-- capability-matrix:end -->

Field → capability mapping enforced by `rayspec validate`: `output_schema` → `structured_output ≠
none`; `session` → `session_resume`; `instructions_mode` ∈ `instructions_modes`; `access` ∈
`access_levels`; `tools` groups ∈ `tool_groups` (raw `<provider>:<Name>` needs `raw_tool_names`);
`max_turns`; `budget_usd`; `effort` ∈ `effort_levels` (an alias is rewritten with a warning);
`thinking`; `mcp` → `mcp_servers`; prompt-step `env` → `env_injection`. `cost_reporting` is
informational (`rayspec plan` prints the cost source). A violation renders as:

```
unsupported: agents.implementer.max_turns = 60
  provider 'codex' does not support `max_turns` (capability max_turns=False)
  fix: remove it, use a provider that supports it (claude, stub), or set defaults.on_unsupported: warn / --allow-unsupported
  at .rayspec/workflows/fix_issue.yaml:77
```

## Access levels and tools

| `access` | meaning |
|---|---|
| `read-only` | the agent may read the workspace and (if allowed) the web; no edits, no shell. `tools.allow` may not contain `edit`/`shell`. |
| `workspace-write` (default) | edits and commands confined to the working directory |
| `full` | no sandbox / permission prompts (use with care) |

`tools.allow` / `tools.deny` use the neutral vocabulary `read edit shell web agent mcp`,
`mcp:<server>[/<tool>]` and `<provider>:<Name>`; a raw name addressed to another provider is
ignored **with a warning** — `rayspec validate` and every `rayspec run` print it once per agent
that carries the entry (`agents.triage.tools.deny: 'claude:WebSearch' targets provider 'claude';
ignored for 'codex'`). One agent file can still serve several providers, but to keep runs quiet
either define one agent per provider (only `claude:` names on agents that resolve to Claude) or
override `tools:` where the agent is used (`agent: {extends: triage, tools: {deny: [web]}}` —
`tools` replaces wholesale).

## Denied tool calls (`on_denial`)

An agent that is refused a tool call did not do what it was asked, and a run that only mentions
that in a transcript nobody reads has failed silently. rayspec records refusals on the step:

* the step record gains `denials: [{tool, reason, call_id}]` (`run.json`, `rayspec show`);
* a `warning` event names them — `2 tool call(s) denied: Bash, Write`;
* templates can read them: `{{ steps.review.denials | length }}`, `{% if steps.review.denials %}`.

Only *what* was refused is recorded — the tool's name, the provider's wording (capped at a few
hundred characters) and the call id. The arguments are step content (a command line, a file
body) and never enter a record.

**The two adapters do not report the same thing**, so the field is a declared capability
(`denial_reporting`) and `rayspec validate` refuses an agent that asks for what its provider
cannot do:

| Adapter | What it reports | `on_denial: fail` |
|---|---|---|
| `claude` | `result.permission_denials`: every call the permission layer refused, on a turn that otherwise **succeeded** | supported — the successful turn becomes a failed step |
| `codex` | a refused command is a `sandboxError` that **fails the turn**, so the step fails on its own; the denial is recorded next to the error so the record names what the sandbox blocked | **refused at validation** — there is nothing left for it to grade |

```yaml
agents:
  reviewer:
    provider: claude
    access: read-only
    on_denial: fail      # default: warn
```

| `on_denial` | Effect |
|---|---|
| `warn` (default) | the denials are recorded and warned about; the step's own status is whatever the turn produced. Valid on every provider |
| `fail` | a turn that reports denials fails the step (`error.type: denied`, message naming the tools), even when the provider called the turn a success. Needs `denial_reporting` |

`fail` is what an unattended run wants on Claude: an agent that quietly did three quarters of the
job because the permission layer blocked the rest is worse than one that stops. Retries apply as
usual (`error.type: denied` is not transient, so the kind defaults do not retry it), and
`allow_failure: true` still tolerates the step. `on_denial` is deliberately **not** part of a
step's fingerprint: it changes how rayspec grades a turn, not what the turn is, so flipping it
does not re-run finished steps on a resume.

A denial is not the same thing as a *failed* turn: on Claude, with `on_denial: warn`, a step whose
agent was refused one tool call and finished anyway still succeeds, and its record says what it
could not do.

## Claude (`claude`)

Runs `claude_agent_sdk.query()` — one `claude` CLI subprocess per prompt-step attempt, under the
step's timeout. Auth is inherited from the machine: a `claude` login, or `ANTHROPIC_API_KEY` /
`CLAUDE_CODE_OAUTH_TOKEN` in the environment (`.rayspec/.env` files are loaded first).

| YAML | Claude Agent SDK option |
|---|---|
| `instructions_mode: append` | `system_prompt={"type": "preset", "preset": "claude_code", "append": <instructions>}` (Claude Code's prompt + yours) |
| `instructions_mode: replace` | `system_prompt=<instructions>` (vanilla Claude; CLAUDE.md still arrives via `setting_sources`) |
| `access: read-only` | `tools=["Read","Glob","Grep"]` (+ `WebFetch`/`WebSearch` iff `web` is allowed), `allowed_tools` the same (+ allowed `mcp__*`), `permission_mode="dontAsk"` |
| `access: workspace-write` | `permission_mode="acceptEdits"`, `allowed_tools=["Bash", <web if allowed>, <explicitly allowed names>, <mcp>]` |
| `access: full` | `permission_mode="bypassPermissions"` |
| `tools.deny` | `disallowed_tools` (all modes) |
| `tools` groups | `read` → Read, Glob, Grep · `edit` → Edit, Write, NotebookEdit · `shell` → Bash · `web` → WebFetch, WebSearch · `agent` → Agent · `mcp:<s>` → `mcp__<s>` · `mcp:<s>/<t>` → `mcp__<s>__<t>` · bare `mcp` → `mcp__<server>` for every configured server |
| `claude:<Name>` | passed through; legacy names are renamed with a warning (`Task`→`Agent`, `MultiEdit`→`Edit`, `BashOutput`→`TaskOutput`, `KillShell`→`TaskStop`); unknown names warn |
| `model`, `effort`, `max_turns`, `budget_usd` | `model`, `effort` (`minimal` → `low`), `max_turns`, `max_budget_usd` |
| `thinking: true / false` | `thinking={"type": "adaptive"}` / `{"type": "disabled"}` |
| `mcp:` | `mcp_servers` (+ `strict_mcp_config=True`) |
| `output_schema` | `output_format={"type": "json_schema", "schema": ...}`; the engine validates `structured_output` |
| `session:` | `resume=<session id>` |
| `env:` | merged into the subprocess environment (`CLAUDE_AGENT_SDK_CLIENT_APP=rayspec/<version>` is always set) |
| `provider_options.claude` | any other `ClaudeAgentOptions` field, verbatim. `env` and `mcp_servers` are merged *under* the computed values; **every field the adapter computes** — `tools allowed_tools disallowed_tools permission_mode model system_prompt setting_sources strict_mcp_config effort thinking max_turns max_budget_usd output_format resume fork_session cwd cli_path stderr include_partial_messages` — is adapter-owned and ignored with a warning (change it through the neutral field instead); unknown keys warn. **While any control governs the agent** the block becomes an allow-list — `env`, `mcp_servers`, `max_thinking_tokens`, `max_buffer_size`, `load_timeout_ms`, `user` — and every other key is a load-time error, because `extra_args` and `settings` reach past anything rayspec computed; see [policy.md](policy.md#provider_options-is-an-allow-list-while-a-control-is-in-force) |

Result mapping: `aborted_*` → interrupted, `error_max_turns` → max_turns, `error_max_budget_usd`
→ budget, `is_error` → error; HTTP 408/409/429/5xx/529 and synthetic rate-limit/server errors
are *transient* (retried by the default prompt retry policy), auth/billing/invalid-request are
not. Usage: `input = input + cache_read + cache_creation`, `cached_input`, `cache_write`,
`output`; cost is the SDK's `total_cost_usd` (`cost_source: provider`, shown as `$0.12`).

Settings (`config.yaml` → `providers.claude`):

| key | default | meaning |
|---|---|---|
| `setting_sources` | `["project"]` | which Claude settings to load (`user`, `project`, `local`; `null` = all) |
| `cli_path` | bundled → `PATH` → known locations | explicit `claude` binary |
| `env` | `{}` | extra subprocess environment (precedence: client app < `settings.env` < `provider_options.env` < run env < step `env:`) |

## Codex (`codex`)

Runs `openai_codex.AsyncCodex` threads (`thread_start` / `thread_resume`, one `turn` per attempt)
against the bundled `codex` runtime. Auth is inherited: `codex login` (ChatGPT) or
`OPENAI_API_KEY`.

| YAML | Codex SDK |
|---|---|
| `access` | `sandbox=read_only` / `workspace_write` / `full_access` on `thread_start` |
| `instructions_mode: append` / `replace` | `developer_instructions` / `base_instructions` |
| `tools: { deny: [web] }` | `config.web_search = "disabled"` — the **only** tool policy Codex accepts; every other group or raw name is an `UnsupportedFeatureError` at validation |
| `model`, `effort` | `model`, `effort` (`none` … `xhigh`, `max`, `ultra` passed through as-is; `max`/`ultra` are model-dependent — gpt-5.6 family — and surface as a turn error elsewhere) |
| `mcp:` | `config.mcp_servers` (`stdio`: command/args/env; `http`: url/http_headers; `sse` is rejected) |
| `output_schema` | `turn(output_schema=...)` after normalisation to OpenAI strict mode (`additionalProperties: false` + all properties required, recursively; keep schemas to types/enum/required — `format`, `pattern`, `minimum` … surface as a `badRequest` turn error) |
| `session:` | `thread_resume(<thread id>)` |
| `env:` | the app-server client is created per environment signature (pooled per run) |
| `max_turns`, `budget_usd`, `thinking`, raw tool names, tool groups other than `web` | **unsupported** (validation error). With `--allow-unsupported` the check becomes a warning: `max_turns`/`budget_usd`/`thinking` are then silently ignored by the adapter, while an unsupported `tools` entry still fails the step at run time (`ProviderError` naming the capability) |
| `provider_options.codex` | `approval_mode` (`deny_all` default, `auto_review`), `config` (extra Codex config merged into every thread; `model`, `sandbox_mode`, `approval_policy`, `web_search` and `tools.web_search` are computed by the adapter and ignored with a warning), `ephemeral`, `usage_baseline`. **While any control governs the agent** the block becomes an allow-list — `config.mcp_servers`, `approval_mode` (`deny_all` only under `access.max` / `network: off`), `ephemeral`, `usage_baseline` — and every other `config` key is a load-time error; see [policy.md](policy.md#provider_options-is-an-allow-list-while-a-control-is-in-force) |

Result mapping: `completed` → success; `interrupted` → timeout (when rayspec's deadline fired) else
interrupted; `failed` → error classified by the Codex error code (transient: `serverOverloaded`,
`internalServerError`, `httpConnectionFailed`, `responseStream*`; fatal: `unauthorized` → auth,
`usageLimitExceeded` → budget, `badRequest`, `contextWindowExceeded` → model, `cyberPolicy`,
`sandboxError` → sandbox). Usage comes from `thread/tokenUsage/updated` totals; Codex reports no
USD, so cost is estimated from the [pricing table](#pricing) (`cost_source: table`, shown as
`~$0.12`) or not at all.

Settings (`providers.codex`): `approval_mode`, `config`, `codex_bin` (override the bundled
runtime), `pricing` (a per-provider price table), `drain_s` (seconds to let an interrupted turn
finish before the client is recreated; default 10).

**Tip — package installs inside the `workspace-write` sandbox.** Codex confines writes to the
working directory (plus the temp dirs, `/tmp` and `$TMPDIR`); an agent that runs `uv sync`,
`pip install` or `npm install` is blocked from its cache under `$HOME` (`uv sync` reports
"blocked by sandbox access to ~/.cache/uv" and the step falls back to a later unsandboxed `shell:`
step). Either grant the cache directory as an extra writable root or move the cache into the
workdir:

```yaml
# agent file — extra writable roots (Codex config `sandbox_workspace_write.writable_roots`)
provider: codex
provider_options:
  codex:
    config:
      sandbox_workspace_write:
        writable_roots: ["~/.cache/uv"]   # Codex expands ~; absolute paths work too
```

`sandbox_workspace_write` widens the sandbox, so it is refused on an agent a control governs (a
`policy.yaml` key, or `network: off` on the agent itself) — it is a machine-owner decision, and
`providers.codex.config` in `config.yaml` is where a machine owner makes it. On a project with no
policy the agent-file form above is fine.

```yaml
# …or keep the cache inside the workspace: `env:` on the prompt step (works for every access level)
- id: implement
  agent: builder
  prompt: Install the dependencies and make the tests pass.
  env: { UV_CACHE_DIR: "{{ run.workdir }}/.uv-cache" }   # add .uv-cache to .gitignore
```

`config:` is forwarded as the app-server's per-thread config overrides (the same keys
as `codex -c key=value` / `~/.codex/config.toml`; nested mappings are TOML tables) — except for
the keys the adapter computes from the agent's own fields, which a workflow may not overwrite: a
`provider_options.codex.config` naming `model`, `sandbox_mode`, `approval_policy`, `web_search`
or `tools.web_search` is dropped with a warning, because otherwise a workflow could undo the
`model:`, `access:`, `tools:` and `network:` it was given (`config.mcp_servers` is *merged* under
the agent's own servers rather than replaced). `providers.codex.config` in `config.yaml` belongs
to the machine owner and is not filtered. So
`writable_roots` can also live under `providers.codex.config` in `config.yaml` for every Codex
agent of a project, and `network_access: true` under the same key allows package downloads when
the sandbox blocks the network. The key and the `~` expansion were checked offline with the
bundled CLI (`codex sandbox -c 'sandbox_mode="workspace-write"' -c
'sandbox_workspace_write.writable_roots=["~/.cache/uv"]' -- touch ~/.cache/uv/probe`, Codex
0.147); sandbox details vary between Codex versions — see the Codex config reference.

## Stub (`stub`)

Accepts every capability so it can stand in for any provider after real validation. `--dry-run`
maps every provider id to it; `--stubs file.yaml` scripts its answers. An agent may also use
`provider: stub` explicitly — then `rayspec run <wf> --stubs file.yaml` (no `--dry-run`) is a
**real** run (shell/python execute, worktree/locks as usual) whose stub agents answer from the
script; it is refused (exit 2) when any resolved agent of a prompt step would run on a real
provider (`pass --dry-run, or switch the agents to provider: stub`). The script's absolute path is
recorded in `run.json` (`stubs_path`), so `rayspec resume|approve|reject <run>` and `run --resume`
script the stub agents again without repeating the flag (a missing file is exit 2 with a `--stubs
<path>` hint; `--stubs PATH` on any of them overrides and replaces the recorded path; a `--dry-run`
record resumes as a dry run).

```yaml
defaults: { latency_ms: 0, usage: { input: 1200, output: 300 } }
steps:
  assess:               { output: { verdict: fix, reason: "repro steps present" } }   # dict → structured
  "build[*]/implement": { text: "Implemented; committed.",
                          events: [ {tool_call: {name: Bash, input: {cmd: "pytest -q"}}}, {tool_result: {text: "3 passed"}} ] }
  "build[*]/review":    { sequence: ["Fix the flaky test", "BUILD-CLEAN"] }           # nth call; last repeats
  pr:                   { fail: { kind: api, message: "simulated 529", transient: true, times: 1 }, text: "https://github.com/x/y/pull/1" }
  audit:
    text: "clean"
    expect:                          # assert what the agent was ASKED (see below)
      prompt_contains: ["parser.py", "def parse"]
      not_contains: "{{"
      model: claude-sonnet-4-5
      access: read-only
      session: resumed
match:
  - { prompt_regex: "Is this real", output: { verdict: skip, reason: "dup" } }
```

Resolution per call: exact step path → first matching glob (declaration order) → first
`match[].prompt_regex` → default (`"[stub] " + prompt[:80]`, or a minimal instance of the step's
`output_schema`). `sequence` advances per matched entry (a glob sees every loop iteration);
`fail.times` and `session_ref` (`stub:<path>:<n>`) count per step path; a `latency_ms` above
the step timeout yields `status: timeout`. `rayspec run <wf> --dry-run --stubs-init stubs.yaml`
scaffolds one entry per prompt step, keyed the way the engine names records at run time: steps
inside `loop:`/`each:` bodies get glob keys (`"build[*]/implement"`, `"fanout[*]/label"`, nested
`"outer[*]/inner[*]/deep"`), include bodies `"block/step"`, root steps their id — so the scaffold
is usable as written (`--dry-run --stubs stubs.yaml` drives every prompt step) and a `sequence`
on a loop-body entry (`"build[*]/review": {sequence: ["Fix the flaky test", "BUILD-CLEAN"]}`)
makes the loop converge on the iteration that hits the `until:` signal. The stub streams its
scripted `events` (tool calls/results, …) **before** the answer text, so `rayspec logs <run>
--step <path>` reads like an agent transcript: tool call → tool result → answer.

### `expect:` — assert what the agent was asked

A dry run proves the graph executed, not that the agent received the right thing: a prompt that
rendered empty, an agent that silently ran on the wrong model, a `session:` that started fresh.
An `expect:` block on an entry (or on a single `sequence` item, which replaces the entry's block
for that call) asserts the request the engine built:

| Key | Asserts |
|---|---|
| `prompt_regex` | the rendered prompt matches this regex (`re.search`) |
| `prompt_contains` | one string, or a list of strings, all of which must occur in the prompt |
| `not_contains` | one string or a list; none of them may occur (`"{{"` catches an unrendered template) |
| `access` | `read-only` \| `workspace-write` \| `full` |
| `model` | the resolved model id |
| `output_schema` | `true` = the step must send a schema, `false` = it must not |
| `session` | `resumed` (the step continued a session) \| `fresh` |

A mismatch fails the step with `error.kind: stub_expectation` — one bullet per mismatch, then the
rendered prompt (long prompts are cut around what went wrong):

```
✗ audit failed  stub_expectation: stub expectation failed at audit:
  - prompt_contains: 'def parse' is not in the prompt
  - session: expected a resumed session, got fresh
prompt as rendered (58 chars):
  | Audit parser.py and report anything suspicious.
```

Assertions run before `latency_ms`, before `fail:` and before the scripted answer, so a
mismatched request is never masked *by the script*. It is still an ordinary step failure, so the
**step's** own `allow_failure: true` (or `each.on_failure: continue`) tolerates it like any other
— if you want an assertion to stop the run, do not tolerate that step's failures.

A `steps:` key that carries an `expect:` block but names no prompt step of the workflow is
refused before the run starts (exit 2, listing the workflow's prompt steps): a renamed step would
otherwise turn every assertion written against the old key into a silent no-op, and the run would
stay green while asserting nothing. Keys without an `expect:` block stay tolerated — a script may
legitimately be shared by several workflows.

### Record & replay

`rayspec runs stubs <run> [-o PATH]` writes a stub script from a **stored run** — every prompt
step's answer, usage and failure, keyed as the engine names records — and `rayspec run <wf>
--dry-run --stubs-from <run>` replays one without writing a file at all. `--stubs-init` scaffolds
from the plan; this records reality. Loop iterations that differ become a `sequence:` under the
glob key, parallel `each` items keep their indexed keys (as does a loop body whose recording
contains a *transient* failure — a `sequence:` advances per call, so the engine's retry would eat
the next iteration's answer), and a run with `secret: true` inputs is refused. The replay records its donor in `run.json` (`stubs_path: "run:<run id>"`), so a replay
that pauses at an approval gate answers the rest from the same recording after
`resume`/`approve`/`reject` — never from the stub provider's built-in default. See
[cli.md](cli.md#rayspec-runs-stubs).

## Models, tiers and aliases

`model:` accepts a literal id, a tier (`small`/`medium`/`large`, resolved per provider through
`config.tiers`, with built-in defaults — Claude `haiku`/`sonnet`/`opus`, Codex `gpt-5.4` with
effort `low` / default / `high`) or an `@alias` from `config.aliases` (which may pin provider and
effort; an alias that pins a different provider than the agent's explicit `provider:` is a load
error). An unset `model` is the `medium` tier.

```yaml
# ~/.rayspec/config.yaml
default_provider: claude
tiers:
  claude: { small: haiku, medium: sonnet, large: opus }
  codex:  { small: { model: gpt-5.4, effort: low }, medium: gpt-5.4, large: { model: gpt-5.4, effort: high } }
aliases:
  "@mini": { provider: codex, model: gpt-5.4, effort: minimal }
providers:
  claude: { setting_sources: [project] }
  codex:  { approval_mode: deny_all }
```

## Pricing

`pricing:` in `config.yaml` maps a model id (exact, or an `fnmatch` glob — the longest matching
glob wins; `null` disables pricing for the match) to USD per million tokens. It is used whenever
a provider reports no cost (Codex always, Claude never):

```yaml
pricing:
  "gpt-5.4*": { input: 2.0, cached_input: 0.5, output: 8.0 }   # cache_write omitted → billed at the input rate
```

`cost = (uncached × input + cached × cached_input + cache_write × (cache_write or input) + output ×
output) / 1e6`. The console shows `$0.12` for provider-reported cost, `~$0.12` for a table
estimate and only the token count otherwise. At run level (`rayspec run` footer, `runs`, `show`,
`run.json` `cost_source`) a run that mixes priced and unpriced steps — say Claude (reports cost)
plus Codex without a `pricing:` entry — is `partial` and prints `≥$0.12`: the sum covers only the
priced steps (see [runs-and-resume.md](runs-and-resume.md#failures-retries-and-timeouts)). Until a Codex model is priced, `rayspec plan`'s
capability report and the `codex pricing` row of `rayspec doctor` say `tokens only — add
pricing.<model> for estimates`; a model you disabled with `null` is reported as `pricing disabled
(null) for <model>` instead (no nudge). The adapter's own table (`providers.codex.pricing`) is
consulted first, then the global `pricing:` section (a `null` in the adapter table still lets the
global section price the model; a malformed global section is reported but does not hide the
adapter table's prices).

## Auth and health

Both SDKs inherit the machine's logins (`claude` once via claude.ai, `codex login`) or API keys
from the environment; put keys in `~/.rayspec/.env` / `.rayspec/.env` (`KEY=VALUE`, never
overriding already-set variables). `--dry-run` needs no login at all.

Every provider implements `healthcheck(probe=False)` (SDK version, CLI path and version, auth
state; `probe=True` runs one "Reply with exactly OK" turn) and `rayspec doctor [--probe]`
([cli.md](cli.md#rayspec-doctor)) exposes it. Auth state per adapter:

- **claude** — `ok` via `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`, else `ok` when the
  `claude` CLI's own claude.ai login is found: `~/.claude/.credentials.json` (`CLAUDE_CONFIG_DIR`
  honoured; Linux and every platform) or, on macOS, the `Claude Code-credentials` keychain item
  (`security find-generic-password -s …`, exit status only — the secret is never requested, the
  lookup is bounded by 5 s and skipped when `security` is missing). Otherwise `unknown`
  (`login state unknown`; a login kept elsewhere is still used at run time — the probe is the
  proof). `missing` is never reported.
- **codex** — `ok` via `OPENAI_API_KEY` or a `codex login` account (`account()`); `missing` when
  neither exists (the probe then says `no codex login`). When `codex` is not on `PATH` the doctor
  hint names the bundled binary: `run \`<bundled path> login\``.

`rayspec doctor --probe` turns a non-`ok` auth row `ok` after a successful one-turn probe, and a
failed probe only fails the exit code for a provider that is configured or explicitly requested
(see [cli.md](cli.md#rayspec-doctor) for the rule). A missing CLI surfaces as a step failure with
an install hint (`ProviderNotInstalledError`) and as a failed `<id> CLI` doctor row.

## Giving an agent a secret (tools, not prompts)

An agent needs a credential often enough — a GitHub token, an API key, a deploy key. rayspec's
answer is not "inject it into the prompt" but **hand out a capability, not a credential**:

- a `shell:`/`python:` step holds the secret and exposes a *result* the agent can reason about
  (issue titles, a deploy status, a signed URL) — see
  [examples/secret_via_tool](../examples/secret_via_tool/);
- or an **MCP server** holds it and publishes one narrow tool. The server inherits the process
  environment the adapter was launched with, so put the credential in `~/.rayspec/.env` (loaded
  by `run`/`resume`/`approve`/`reject`) or in the server's own launcher — never in a template:

```yaml
agents:
  triager:
    provider: claude
    mcp:
      github: { transport: stdio, command: my-github-mcp }   # reads GITHUB_TOKEN itself
```

Either way the transcript under `RAYSPEC_HOME` holds the *capability result*, never the key.

### Why a `prompt:` step's `env:` is refused for secrets

`env:` on a `prompt:` step is a real schema field and both adapters do pass it to the child
process — so the obvious question is why a `secret: true` input may not be named there, when a
`shell:` step's `env:` may. It was measured, not assumed. A probe value was fed through a prompt
step's `env:` and run live against both adapters
(claude-agent-sdk 0.2.142 / bundled CLI 2.1.237, openai-codex 0.147.0, 2026-08-21):

| | value delivered to the child | value found under `$RAYSPEC_HOME` | value found elsewhere |
|---|---|---|---|
| Claude | yes | no | no |
| Codex | yes | no | **yes** — `~/.codex/shell_snapshots/<id>.<ts>.sh`, mode `0644`, a literal `export PROBE_TOKEN=<value>` line |

The Codex CLI snapshots the child's environment into a world-readable file that outlives the run,
sits outside the run store and no rayspec redactor can reach. One adapter is enough: the rule
stays. The reproduction is `tests/integration/test_secret_placement_live.py` (`RAYSPEC_LIVE=1`);
if a future Codex release stops snapshotting, that test fails and the decision can be revisited.

## Roadmap

- `best_effort` structured output is implemented in the engine but no built-in provider uses it.
- Image inputs (`images` capability is declared `✘` for Claude and Codex).
- Session forking (`session_fork`) has no YAML field yet.
