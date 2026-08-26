<!-- Generated from docs/concepts.md by scripts/gen_skill.py — do not edit here. -->
<!-- Canonical source: https://github.com/rayspec-labs/rayspec-py/blob/main/docs/concepts.md -->
<!-- Sibling references in this directory: concepts.md · schema.md · templating.md · examples.md -->

# Concepts

rayspec runs *declarative workflows for coding agents*. This page is the mental model; the
field-by-field reference is [schema.md](schema.md), the template language is
[templating.md](templating.md), and the commands are in [cli.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/cli.md).

> **YAML coordinates. Code computes. Agents judge.**

A workflow file decides *what runs, in what order, under which gates, with which agent*.
Computation belongs in `shell:`/`python:` steps, judgement in `prompt:` steps, and the engine
governs a run without understanding the content of prompts or scripts
([constitution.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/constitution.md) is the tie-breaker for every "can we add a field" question).

## Workflow → steps → run

| Word | Meaning |
|---|---|
| **workflow** | A YAML file in `.rayspec/workflows/<name>.yaml` (project), `~/.rayspec/workflows/` (user), or one of the workflows bundled with rayspec (`rayspec workflows` lists all three; `rayspec workflows eject <name>` copies a bundled one into the project). A name resolves project → user → bundled, so a file of yours shadows the bundled one. Its `name` is the file stem you pass to `rayspec run <name>`. |
| **step** | One entry of `steps:`. Exactly one *kind key* decides what it does: `prompt:`/`prompt_file:` (an agent), `shell:`, `python:`, `loop:`, `each:`, `approve:`, `include:`, `stop:`. |
| **agent** | A reusable bundle of provider + model + access + tools + instructions (`agents:` in the workflow, `.rayspec/agents/<name>.yaml`, or `~/.rayspec/agents/<name>.yaml`). Only `prompt:` steps use agents. |
| **run** | One execution of a workflow. It has an id (`YYYYMMDD-HHMMSS-xxxx`), fixed inputs, a working directory and a run directory under `~/.rayspec/projects/<slug>/runs/<run-id>/` that holds every checkpoint ([runs-and-resume.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/runs-and-resume.md)). |
| **provider** | The SDK behind an agent: `claude` (Claude Agent SDK), `codex` (OpenAI Codex SDK) or `stub` (scripted, used by `--dry-run`). Each declares *capabilities*; a workflow that uses a feature its provider lacks fails `rayspec validate` ([providers.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/providers.md)). |

## The DAG and its bodies

The top-level `steps:` list is a DAG. A step declares its upstream siblings with
`needs: [ids]`; everything whose needs are satisfied runs concurrently (bounded by
`defaults.max_parallel`, default 4). No `needs` means "ready at start".

```yaml
steps:
  - id: fetch
    shell: gh issue view "$RAYSPEC_INPUT_ISSUE" --json title,body
  - id: assess
    needs: [fetch]
    prompt: "Is this issue worth fixing? {{ steps.fetch.output }}"
    output_schema: { type: object, properties: { verdict: { enum: [fix, skip] } }, required: [verdict] }
  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    stop: { status: cancelled, reason: not worth it }
```

Three step kinds carry a **body** — a nested `steps:` list that is itself a DAG:

| Kind | Body runs… | Output of the composite step |
|---|---|---|
| `loop:` | repeatedly (do-while), `max_iterations` times at most, stopping early when `until:` is true | `{<body id>: output}` of the last iteration, plus `iterations` / `converged` |
| `each:` | once per item of a list, concurrently (`max_parallel`) | a list aligned with the items, each element `{<body id>: output}` (`null` for failed items under `on_failure: continue`); `items` holds per-item detail |
| `include:` | once; the body is another workflow file inlined at load time, fed through `with:` | the included workflow's rendered `outputs:` map |

A body step is addressed in records by its **path**: `build[2]/implement` (loop iteration 2,
1-based), `triage[0]/classify` (each item 0, 0-based), `review/lint` (include). Paths appear in
`run.json`, `events.jsonl`, the console and the run directory — never in templates.

## Lexical scopes

Templates reference other steps with `steps.<id>`. What is visible is decided *lexically*:

- a step sees its transitive ancestors in the same list (`needs` closure) and the ancestors of
  every enclosing composite;
- a loop/each body sees the outside (innermost scope first), and its own `iteration.*` /
  `each.*` / `<as>` variables;
- the outside never sees inside a body — use `steps.build.output.review` (the composite's
  output), never `steps.review`;
- an `include:` body is a closed scope: it sees only its own steps and the `inputs` bound by
  `with:` (plus `run`, `project`, `env`) — never the including workflow's steps, `iteration` or
  `each`.

`rayspec validate` checks every reference at load time — include bodies included — and names the
fix (e.g. "steps.review is inside loop 'build'; use steps.build.output.review").

## Outputs

Every succeeded step has an output, stored as a file in the run directory:

- `prompt:` → the agent's text, or a validated JSON object when `output_schema` is set;
- `shell:`/`python:` → stdout (trailing newlines stripped), parsed and validated as JSON when
  `output_schema` is set — there is **no** automatic JSON detection;
- `approve:` → the approver's comment (`''` when none);
- composites → JSON as described above.

References are strict: a missing field, a skipped or failed producer, a `null` value, or
`.field` on plain text all **fail the consuming step** with a message naming the fix
(`| default(...)`, `| fromjson`, `steps.x.status == 'succeeded'`, …). Nothing silently becomes
`''`.

The workflow's own `outputs:` map is rendered when the run succeeds (it is also what an
`include:` step exposes as its output) and printed at the end of `rayspec run`.

## Runs, checkpoints, resume

Files are the checkpoint. `run.json` is rewritten atomically after every step; every
succeeded step's output is written *before* its record (write-ahead), so a crash never leaves
a record that points at a missing file. `rayspec run <wf> --resume <run-id>` re-executes from
the top with a reuse cache: succeeded (or tolerated) steps whose output file exists are
replayed, everything else re-runs. Inputs are fixed per run. See
[runs-and-resume.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/runs-and-resume.md).

Approval gates (`approve:`) ask on a terminal; without one the run **pauses** (exit code 3)
and is continued with `--resume`.

> **Secrets.** The run directory is a plain-text record: inputs, outputs, rendered prompts,
> agent transcripts (`stream.jsonl`) and every step's `context.json` are stored in clear text
> under `RAYSPEC_HOME` and are shown by `rayspec show`/`logs`. rayspec gives you three things,
> in this order of preference:
>
> 1. **`secret: true` inputs** ([schema.md](schema.md#secret-inputs)) — the value is never
>    persisted, reaches only `shell:`/`python:` steps as `RAYSPEC_INPUT_<NAME>` or through that
>    step's `env:`, and is refused anywhere else *at load time*. That refusal, not any amount of
>    scrubbing, is the actual guarantee.
> 2. **`secrets:` sources in `config.yaml`**
>    ([schema.md](schema.md#secret-sources-secrets-in-configyaml)) — `{NAME: {env|file|cmd}}`,
>    resolved at run start (lazily: only the entries this workflow actually reads), handed only to
>    `shell:`/`python:` steps as `$NAME`, and re-fetched automatically when a paused run is
>    resumed — `--input` still wins, so a source that is briefly unavailable never strands a run. `~/.rayspec/.env` still works for anything an
>    adapter or an MCP server has to inherit.
> 3. **Redaction** ([schema.md](schema.md#redaction-exact-match-best-effort)) — every value
>    rayspec knows is replaced with `[REDACTED:<name>]` at every writer, so a script that echoes
>    its own token no longer persists it. It is exact match and best effort: it cannot catch a
>    value an agent transformed. Treat it as the net under the first two, never as permission to
>    hand an agent a credential.
>
> For agents, hand out a **capability, not a credential**: a `shell:` step or an MCP server holds
> the secret and exposes one narrow tool ([examples/secret_via_tool](https://github.com/rayspec-labs/rayspec-py/blob/main/examples/secret_via_tool)).
> Prompt bodies — and a prompt step's `env:` — stay refused. What rayspec still does *not* do — hold
> credentials itself, or redact a value an agent transformed — is on the roadmap, not in this
> release.

## Isolation

By default every run in a git repository gets its own **worktree** on a branch
`rayspec/<workflow>-<shortid>`, created from the current branch (`--base` overrides) under
`~/.rayspec/projects/<slug>/worktrees/`. Steps run there (`run.workdir`); the worktree is kept
after the run and the branch name is printed. `isolation: none` in the workflow or
`--no-worktree` runs in place; non-git directories always run in place. `--repo <url|path|name>`
runs against another project. See [isolation.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/isolation.md).

## Providers and capabilities

`prompt:` steps are the only ones that call a provider, through a neutral adapter
(`AgentRequest` → streamed events → `AgentResult`). Each provider declares what it can honour
(structured output, sessions, tool groups, `max_turns`, …); `rayspec validate` maps YAML fields
onto those flags and refuses (or, with `--allow-unsupported` / `defaults.on_unsupported: warn`,
warns about) mismatches. `rayspec providers` prints the matrix. See [providers.md](https://github.com/rayspec-labs/rayspec-py/blob/main/docs/providers.md).

## Exit codes

`0` succeeded · `1` failed · `2` usage or validation error · `3` paused (awaiting approval)
· `4` cancelled (`stop:` or a rejected gate) · `130` interrupted (Ctrl-C).
