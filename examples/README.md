# rayspec examples

Each directory is a self-contained project: its own `.rayspec/` tree (workflows, agents, prompts,
`config.yaml` when needed), a `stubs.yaml` so it runs without credentials via
`rayspec run <wf> --dry-run --stubs stubs.yaml`, a `checks.yaml` that states what the dry run must
produce, and a README (what it shows, how to run it for real, expected output).

| Example | Showcases |
|---|---|
| [`hello_review`](hello_review/) | single `prompt:`, string + enum inputs, `outputs:`, tier model, `validate`/`plan`, `--dry-run --stubs` |
| [`fix_issue`](fix_issue/) | shell → structured output → `when:` + `stop:` → `loop:` self-heal (`until`/`has_signal`, `iteration.prev`, `session:`, `allow_failure`, `on_exhausted`) → `approve:` → PR step; worktree default |
| [`review_sweep`](review_sweep/) | `defaults.on_step_failure: continue` (independent branches finish when one fails), declared `artifacts:`, `join: always` |
| [`triage_fanout`](triage_fanout/) | `each:` fan-out (`max_parallel`, `on_failure: continue`, `items`), `join: any`/`always`, per-item structured output, `retry:` on a prompt, `python:` with `deps` + `env:` |
| [`pr_review`](pr_review/) (`review_block` + `pr_review`) | `include:` with `with:`/`outputs:`, named agents in `.rayspec/agents/`, `instructions_file`, `tools` policy, `access: read-only`, Claude **and** Codex agents, `provider_options`, pricing (`~$`), `@alias` |
| [`unsupported_demo`](unsupported_demo/) | the capability error (`max_turns`/`tools` on a Codex agent), `--allow-unsupported` and `defaults.on_unsupported: warn` (`unsupported_warn`) |
| [`release_check`](release_check/) | `isolation: none`, `retry`/`timeout`, `join: always` cleanup, `env`, `RAYSPEC_CONTEXT` + `jq`, `--repo` usage |
| [`notify_webhook`](notify_webhook/) | a `secret: true` input (webhook URL) delivered to a `shell:` step as `RAYSPEC_INPUT_WEBHOOK_URL` only — never persisted, `<secret>` in `plan`/`show`, re-supplied on resume |
| [`secret_via_tool`](secret_via_tool/) | a `secrets:` block in `config.yaml` (`env`/`file`/`cmd` source) feeding a `shell:` **tool** that exposes a capability — the agent sees the result, never the credential |
| `dogfood` (this repo's [`.rayspec/`](../.rayspec/)) | `review_pr`, `fix_issue`, `implement_feature_tdd`, `docs_sync`, `release_check` — rayspec develops rayspec |

## Running every example (what CI does)

```sh
uv run python scripts/check_examples.py --matrix     # validate + plan + dry-run every check, verify this matrix
uv run python scripts/check_examples.py --only fix_issue --verbose
uv run pytest tests/examples -q                      # the same checks as pytest cases
```

`checks.yaml` format (one list under `checks:`): `workflow`, `inputs` (passed with
`--inputs-file`), `stubs` (relative path), `env: {NAME: value|null}` (pin or unset process env for
the run), `allow_unsupported`, `validate: ok|error`, `run: false` (validate + plan only) and
`expect: {status, exit_code, outputs: {subset}, steps: {path: status}, reason_contains}`. The
dogfood checks live in `.rayspec/dryrun/checks.yaml`.

## Stub files (`--stubs`)

```yaml
defaults: { latency_ms: 0, usage: { input: 1200, output: 300 } }
steps:                                   # key = step path or glob (build[*]/review, *judge)
  assess: { output: { verdict: fix, reason: "…" } }          # dict → structured output
  "build[*]/implement": { text: "done", events: [ {tool_call: {name: Bash, data: {command: ls}}} ] }
  "build[*]/review": { sequence: ["fix the test", "BUILD-CLEAN"] }   # n-th call of this entry; last repeats
  flaky: { fail: { kind: api, message: "529", transient: true, times: 1 }, text: "recovered" }
match:                                   # consulted after steps; first prompt regex that hits wins
  - { prompt_regex: "Classify it", output: { severity: low, area: docs } }
```

Resolution: exact path → first matching glob → `match[]` → default (`"[stub] " + prompt[:80]`, or a
minimal instance of the step's `output_schema`). `rayspec run <wf> --dry-run --stubs-init stubs.yaml`
scaffolds a file with one entry per prompt step.

## Coverage matrix

Every capability of the workflow language (`docs/schema.md`) and the CLI appears at least once.
`tests/examples/test_examples.py` (and `scripts/check_examples.py --matrix`) parse these tables:
each row must name at least one existing example, the list of required rows in the test is the
checklist this matrix must satisfy, and every backticked token of a row (`--flag`, `RAYSPEC_*`,
`rayspec <cmd>`, `key:` …) must literally occur in the named examples' trees or READMEs — each
named example must back at least one token of its row (comment-only YAML lines do not count).

### Step kinds

| Capability | Examples | Notes |
|---|---|---|
| `prompt:` | `hello_review`, `fix_issue`, `pr_review`, `dogfood` | the only kind that calls a provider |
| `prompt_file:` | `pr_review` | `review_block` → `prompts/review_prompt.md` |
| `shell:` | `fix_issue`, `release_check`, `pr_review`, `dogfood` | `bash -euo pipefail` by default |
| `python:` | `triage_fanout` | `summarize` |
| `loop:` | `fix_issue`, `dogfood` | `build`, `tdd` |
| `each:` | `triage_fanout` | `triage` |
| `approve:` | `fix_issue`, `release_check`, `dogfood` | string and `{message, on_reject}` forms |
| `include:` | `pr_review` | `review` → `review_block` |
| `stop:` | `fix_issue`, `dogfood` | `cancelled` (`bail`) and `succeeded` (`docs_sync` `done`) |

### Common step fields

| Capability | Examples | Notes |
|---|---|---|
| `id`, `description` | `fix_issue`, `triage_fanout`, `dogfood` | `fetch`, `changed`, `contracts` |
| `needs:` | `fix_issue`, `triage_fanout`, `release_check` | implicit DAG; siblings only |
| `when:` | `fix_issue`, `triage_fanout`, `release_check`, `pr_review` | bare Jinja expression → bool |
| `join: all` (default) | `fix_issue`, `release_check` | every step without `join:` |
| `join: any` | `triage_fanout` | `digest` |
| `join: always` | `triage_fanout`, `release_check`, `dogfood` | `cleanup` |
| `timeout:` | `release_check`, `pr_review`, `dogfood` | `tests`, `lint`, `gate` |
| `retry:` | `release_check`, `triage_fanout`, `dogfood` | `{attempts, delay, on_error}`; `classify` retries a stubbed transient `429` |
| `always_run:` | `release_check`, `dogfood` | `cleanup` |
| `allow_failure:` | `fix_issue`, `release_check`, `dogfood` | `check`, `last_tag`, `gate` |
| `artifacts:` | `review_sweep` | the three report steps; a promise checked on a real run, then copied into the run dir with a sha256 |
| `env:` (shell / python / prompt) | `release_check`, `triage_fanout` | `tests` (shell), `summarize` (python), `notes` (prompt) |
| `output_schema:` (prompt, shell) | `fix_issue`, `release_check`, `triage_fanout` | `assess`, `meta`, `classify` |
| `interpreter:` | `release_check` | `last_tag` uses `sh` |
| `cwd:` | `dogfood` | `docs_sync` `links` (relative to the workdir) |
| `deps:` | `triage_fanout` | `summarize` installs `tabulate` |
| `session:` | `fix_issue`, `dogfood` | self inside a loop (`implement`, `red`, `green`) |
| `loop.max_iterations` / `until:` / `on_exhausted` | `fix_issue`, `dogfood` | `build`, `tdd` |
| `each` / `as:` / `max_parallel` / `on_failure` | `triage_fanout` | `triage` |
| `approve.on_reject` | `release_check`, `dogfood` | `continue` (`gate`, `gate_human`) |
| `approve.class` | `release_check`, `fix_issue` | the operator's rules for a kind of gate, named by the workflow and defined outside it |
| `approve.auto_if` | `fix_issue` | a clean first round approves the PR gate; anything else asks |
| `include.with:` | `pr_review` | validated against the block's `inputs:` |
| `stop.status` / `stop.reason` | `fix_issue`, `dogfood` | templated reason |
| `outputs:` (workflow and include) | `hello_review`, `pr_review`, `triage_fanout` | deep-rendered, typed when a single `{{ }}` |
| `defaults.agent` | `fix_issue`, `dogfood` | `implementer` |
| `defaults.timeout` | `fix_issue`, `release_check`, `dogfood` | per-step default |
| `defaults.max_parallel` | `fix_issue`, `triage_fanout` | run-wide leaf cap |
| `defaults.on_unsupported` | `unsupported_demo` | `unsupported_warn.yaml` (`warn`), checked without `--allow-unsupported` |
| `defaults.on_step_failure` | `release_check`, `review_sweep` | `drain` (default, `release_check`) · `continue` (`review_sweep`: the branches beside a failed one still finish, and the run still fails) · `fail_fast` (`--fail-fast` overrides, and may only tighten) |
| `isolation: none` | `hello_review`, `release_check`, `pr_review` | run in place |
| `isolation: worktree` (default) | `fix_issue`, `dogfood` | branch `rayspec/<wf>-<shortid>` |

### Templating and expressions

| Capability | Examples | Notes |
|---|---|---|
| `inputs.*` | `hello_review`, `fix_issue`, `triage_fanout` | every example |
| `steps.<id>.output` (text / dict / list) | `fix_issue`, `triage_fanout`, `pr_review` | |
| `steps.<id>.ok` / `steps.<id>.exit_code` / `.status` | `fix_issue`, `triage_fanout`, `release_check` | `review` prompt, `until`, outputs |
| `steps.<id>.items` (each detail) | `triage_fanout` | `classified`/`failed` outputs |
| `steps.<id>.iterations` / `.approved` | `fix_issue`, `release_check` | outputs |
| `run.*` (`run.workdir`, `run.branch`) | `fix_issue`, `release_check`, `dogfood` | prompts, `instructions_file` |
| `project.*` (`project.name`, `project.slug`) | `fix_issue`, `pr_review`, `dogfood` | prompts |
| `iteration.n` / `.max` / `.first` | `fix_issue`, `dogfood` | loop bodies |
| `iteration.prev.<id>` | `fix_issue`, `dogfood` | previous iteration's records |
| `each.index` / `each.total` / `<as>` | `triage_fanout` | `classify` |
| `env.<VAR>` / `is defined` | `release_check` | `when: env.SLACK_WEBHOOK is defined` (`checks.yaml` pins it per scenario) |
| `fromjson` | `pr_review` | Codex JSON text → dict |
| `regex_search` | `hello_review` | `verdict` output |
| `has_signal` | `fix_issue`, `dogfood` | `until:` |
| `default(` / Jinja builtins (`selectattr`, `join`, `truncate`) | `hello_review`, `triage_fanout`, `dogfood` | `default('x', true)` also replaces `None` (`run.branch` on a detached HEAD) |
| `{% if %}` / `{% for %}` in prompts | `fix_issue`, `pr_review`, `dogfood` | |
| `{% raw %}` for Go-template braces | `release_check` | `last_tag` |
| `{{ }}` in `shell:` → `${RAYSPEC_V<n>}` | `fix_issue`, `release_check` | `pr`, `publish` |
| `RAYSPEC_INPUT_<NAME>` | `fix_issue`, `pr_review`, `notify_webhook`, `dogfood` | `fetch`, `checkout`, `notify` (the only way a secret input reaches a step) |
| `RAYSPEC_CONTEXT` (+ `jq`) | `release_check` | `tests` |
| `RAYSPEC_ARTIFACTS_DIR` / `RAYSPEC_WORKDIR` / `RAYSPEC_RUN_ID` | `triage_fanout`, `release_check` | scratch dir `triage[*]/view` → `cleanup`; coverage report kept under `artifacts/` |

### Inputs

| Capability | Examples | Notes |
|---|---|---|
| `type: string` (+ `enum`, `default`, `description`) | `hello_review`, `fix_issue` | |
| `type: integer` | `fix_issue`, `pr_review` | `issue`, `pr` |
| `type: number` | `release_check` | `min_coverage` |
| `type: boolean` | `pr_review`, `release_check` | `post`, `push` |
| `type: array` (+ `items`; repeated `--input` or JSON) | `triage_fanout`, `dogfood` | `issues`, `docs` |
| `type: object` | `triage_fanout` | `severity_by_label` |
| `required: true` | `hello_review`, `fix_issue` | |
| `default` / `enum` | `hello_review`, `fix_issue` | `focus`, `mode` |
| `secret: true` | `notify_webhook` | `webhook_url`: env-only delivery (`RAYSPEC_INPUT_WEBHOOK_URL`), `<secret>` in `run.json`/`plan`/`show`, re-supplied on `rayspec resume` |
| `secrets:` in `config.yaml` | `secret_via_tool` | `GITHUB_TOKEN` from an `env:` source, delivered to the `shell:` tool only; `rayspec doctor` lists the source, never the value |

### Agents

| Capability | Examples | Notes |
|---|---|---|
| `provider: claude` | `hello_review`, `fix_issue`, `pr_review` | |
| `provider: codex` | `fix_issue`, `pr_review`, `unsupported_demo`, `dogfood` | |
| `model: <tier>` (`small`/`medium`/`large`) | `hello_review`, `fix_issue`, `dogfood` | |
| `model: <literal>` | `pr_review` | `gpt-5.4` via tiers/aliases in `config.yaml` |
| `model: "@<alias>"` | `pr_review` | `@fast`, `@deep` |
| `effort` | `fix_issue`, `pr_review`, `dogfood` | |
| `access: read-only` | `hello_review`, `pr_review` | |
| `access: workspace-write` (default) | `fix_issue`, `unsupported_demo` | |
| `access: full` | `dogfood` | `implementer.yaml` (uv writes outside the worktree) |
| `instructions` | `hello_review`, `fix_issue` | |
| `instructions_file` | `fix_issue`, `pr_review`, `dogfood` | Jinja-templated |
| `instructions_mode` (`append` / `replace`) | `pr_review` | |
| `max_turns` | `unsupported_demo`, `dogfood` | Claude only |
| `budget_usd` | `pr_review`, `dogfood` | Claude only |
| `tools.allow` | `pr_review` | `[read, mcp:github]` |
| `tools.deny` | `fix_issue`, `pr_review`, `unsupported_demo` | `[web]` works on Codex too |
| `thinking` | `pr_review`, `dogfood` | |
| `mcp` | `pr_review` | GitHub MCP over stdio |
| `provider_options` | `pr_review`, `dogfood` | `claude.setting_sources`, `codex.config` |
| `agent: {extends: …}` | `pr_review` | `judge` |
| inline agent mapping | `triage_fanout` | `classify` |
| named agents in `.rayspec/agents/` | `pr_review`, `dogfood` | |
| workflow `agents:` | `hello_review`, `fix_issue`, `release_check` | |
| `session:` target on the same provider | `fix_issue`, `dogfood` | |

### Config (`.rayspec/config.yaml`)

| Capability | Examples | Notes |
|---|---|---|
| `config.yaml tiers` | `pr_review` | |
| `config.yaml aliases` | `pr_review` | |
| `config.yaml pricing` (`~$`) | `pr_review` | |
| `config.yaml providers` | `pr_review` | settings per provider factory |
| `config.yaml default_provider` | `pr_review` | |

### CLI

| Capability | Examples | Notes |
|---|---|---|
| `rayspec validate [names] [--allow-unsupported] [--root]` | `hello_review`, `unsupported_demo` | |
| `rayspec plan <wf>` | `hello_review`, `pr_review` | |
| `rayspec run <wf>` | `hello_review`, `fix_issue` | |
| `rayspec workflows` / `rayspec agents` | `pr_review` | |
| `rayspec providers` | `unsupported_demo` | the capability matrix |
| `rayspec projects add\|list\|remove` | `release_check` | `--repo <name>` |
| `rayspec worktrees list\|clean` | `fix_issue` | |
| `--input` / `-i` (repeatable; arrays) | `hello_review`, `triage_fanout` | |
| `--inputs-file` | `hello_review`, `triage_fanout` | every `checks.yaml` uses it |
| `--dry-run` / `--stubs` / `--stubs-init` / `--exec-shell` | `hello_review`, `fix_issue`, `release_check` | |
| `--yes` / `--no-interactive` | `fix_issue`, `release_check` | approval gates |
| `--json` / `--quiet` / `--verbose` | `hello_review`, `fix_issue`, `triage_fanout` | |
| `--allow-unsupported` | `unsupported_demo` | |
| `--fail-fast` | `release_check` | cancels running siblings on failure |
| `--resume <run-id>` / `--force` | `fix_issue` | replay from the cache |
| `--worktree` / `--no-worktree` / `--base` | `fix_issue` | |
| `--repo <url\|path\|name>` | `release_check` | |
| `--root` | `hello_review` | used by `scripts/check_examples.py` |
| `rayspec approve\|reject\|resume <run>` | `release_check`, `fix_issue` | decide a paused gate without a TTY / re-ask on one; `rayspec run <wf> --resume <id>` still works; a `--stubs` file given at launch is reused, secret inputs are supplied again (`notify_webhook`) |

### Stub file features

| Capability | Examples | Notes |
|---|---|---|
| `stubs: output` (dict → structured) | `fix_issue`, `triage_fanout` | |
| `stubs: text` / `usage` / `defaults` | `hello_review`, `fix_issue` | |
| `stubs: sequence` (per entry, advances per loop iteration) | `fix_issue`, `dogfood` | |
| `stubs: fail` (`kind`, `transient`, `times`) | `triage_fanout` | `triage[1]` fails every call, `triage[3]` once (`times: 1`) |
| `stubs: events` (replayed tool calls) | `fix_issue` | |
| `stubs: match` (prompt regex) | `triage_fanout` | `stubs_quiet.yaml` |
| glob keys (`build[*]/review`, `*judge`) | `fix_issue`, `pr_review` | |

## Not covered by an example

- `rayspec init` and `rayspec doctor` — both ship (see the README quickstart and
  [docs/cli.md](../docs/cli.md)); they scaffold/inspect a project rather than run a workflow, so no
  example needs them.
- The Rich live step tree is what `rayspec run` shows on a terminal; the expected-output blocks
  in the example READMEs are the one-line-per-step console you get when stdout is not a TTY
  (pipes, CI, `scripts/check_examples.py`).
