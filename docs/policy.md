# Policy: guardrails as a file

A workflow you run by hand is governed by your judgement at the moment you type the command. A
workflow on a schedule is governed by whatever was written down. This page is about writing it
down: a `policy.yaml` that says which providers, models, access levels, tools and MCP servers are
allowed, a change guard that bounds how much of the repository a run may rewrite, and a trust list
that says which workflows may run at all.

Three properties hold throughout:

* **Load time, not run time.** Every policy key is checked while the workflow is being loaded, so
  a violation is an error with a file and a line before a single token is spent. `rayspec run`,
  `rayspec plan`, `rayspec validate` and `rayspec test` all refuse the same workflows.
* **The deciding layer is always named.** "denied by policy" with nothing else is useless to the
  person who has to fix it, so every message quotes the policy file and line that denies, and what
  to do about it.
* **Everything is local.** Policy is read from files on the machine the process runs on. There is
  no key that fetches policy from a server, names an organisation or joins a shared registry.

## The three layers

| Layer | File | Who it belongs to |
|---|---|---|
| `RAYSPEC_POLICY` | the path in that environment variable | the operator running this process |
| project | `<project>/.rayspec/policy.yaml` | the checkout, reviewed like code |
| user | `$RAYSPEC_HOME/policy.yaml` (default `~/.rayspec/policy.yaml`) | you, on this machine |

All three are read; a missing file is simply an absent layer. `RAYSPEC_POLICY` is the exception —
it was named explicitly, so pointing it at a file that does not exist is an error rather than a
silently skipped guardrail. The same file reached through two layers is loaded once.

### Most-restrictive-wins

Layers do not override each other, because no layer can *loosen* another. They combine:

| Key | How layers combine |
|---|---|
| `providers.allow` | intersection — a provider must be allowed by every layer that has an opinion |
| `models.deny` | union |
| `access.max` | the lowest level any layer set |
| `tools.deny` | union |
| `mcp.allow_servers` | intersection |
| `workspace.protected_paths` | union |
| `workspace.max_changed_files` / `max_changed_lines` | the smallest value any layer set |
| `trust.require` | true when any layer sets it |

The consequence worth stating plainly: adding a policy file can only ever make a run *less*
capable. A user file cannot re-admit what the project file excluded, and a project file cannot
re-admit what `RAYSPEC_POLICY` excluded. When several layers forbid the same thing, the error
names all of them — because editing one of them would not be enough.

## The keys

```yaml
# .rayspec/policy.yaml
providers:
  allow: [claude, codex]          # nothing else may be resolved
models:
  deny: ["*opus*", gpt-5.6-pro]   # exact ids or globs
access:
  max: workspace-write            # read-only < workspace-write < full
tools:
  deny: [web]                     # neutral tool entries; also mcp:<server>
mcp:
  allow_servers: [github]         # every other MCP server is refused
workspace:
  protected_paths: [".github/**", "infra/**"]
  max_changed_files: 40
  max_changed_lines: 2000
trust:
  require: true                   # only workflows in .rayspec/trusted.yaml may run
```

An unknown key is an error naming the file and the line, with a "did you mean" for near misses. A
policy file that cannot be read or parsed is an error too — it is never treated as an empty
policy, because a guardrail that silently disappears is worse than none.

### What a violation looks like

```
$ rayspec validate nightly
errors
  - agents.reviewer.model: model 'claude-opus-4-1' is denied by policy: models.deny '*opus*'
    (~/.rayspec/policy.yaml:3); choose another model or drop that entry
    (at .rayspec/agents/reviewer.yaml:3)
```

Two locations: the field in your workflow that is wrong, and the policy line that says so.

### `tools.deny` is enforced, not only checked

`tools.deny` does two things. An agent that *explicitly allows* a denied entry is an error — a
declared contradiction should be fixed by a person, not silently overruled. Every other agent has
the denied entries folded into its effective `tools.deny`, so the provider adapter really is told
to keep them away.

Where the resolved provider cannot express a denial — the Codex adapter only understands
`deny: [web]` — nothing is folded in and `rayspec validate` warns that the restriction is
advisory on that provider. See [Honest enforcement](#honest-enforcement) below.

## The worktree change guard

`workspace:` bounds how much of the repository a run may rewrite. The guard measures the whole
worktree against the run's `base_sha`:

* **`protected_paths`** — globs that must not change. `*` crosses directory separators, a pattern
  ending in `/` covers the directory, a leading `**/` is optional, and a pattern without a
  separator also matches a bare file name in any directory.
* **`max_changed_files`** / **`max_changed_lines`** — size caps. Untracked files count as
  additions, because "quietly wrote 400 new files" is exactly what a diff against `HEAD` misses;
  a binary file counts as a changed file and no lines, the way git counts it.

When a limit is exceeded the report names every limit that broke, with the numbers behind it and a
short diff summary:

```
change guard: 2 limit(s) exceeded since 4bba429fd0a1
  - protected_path: '.github/**' matched .github/workflows/ci.yml
  - max_changed_files: 63 files changed, limit 40
  changed: .github/workflows/ci.yml (+4/-1), src/app.py (+12/-3), … (+61 more)
```

The measurement lives in `rayspec.workspace` (`check_change_guard`, `diff_since`, `match_path`)
and works on anything that is a git worktree with a base commit. Be clear about what ships here:
in this build the guard is a library and the limits are a policy key — the engine does not yet run
it after each `prompt:` step, so nothing fails a step on it today. Read the section above as a
description of the measurement, not as a promise that a run is currently stopped by it.

## Trusted workflows

`.rayspec/trusted.yaml` lists the workflows this checkout may run, by hash:

```yaml
workflows:
  - workflow: .rayspec/workflows/nightly.yaml
    hash: sha256:7d1a…
    added: 2026-08-21T09:12:00Z
```

```
$ rayspec trust add nightly
trusted nightly (.rayspec/workflows/nightly.yaml) 7d1a1f0c9b22
$ rayspec trust list
$ rayspec trust check          # exit 1 when anything drifted — the gate for a scheduled job
```

**What the hash covers decides whether the gate is real.** `ResolvedWorkflow.hash` is taken over
every file that contributed to the resolved workflow: the document itself, every `include:`d body,
every agent file and every `prompt_file`/`instructions_file`. Editing an included body or an
agent's instructions revokes trust exactly the way editing the workflow does. A gate that hashed
only the entry document would be theatre — the interesting code is usually somewhere else.

With `trust: {require: true}` in any policy layer, every command that loads a workflow refuses one
that is not listed at its current hash:

```
errors
  - trust: .rayspec/workflows/nightly.yaml hash has changed since it was added to
    .rayspec/trusted.yaml, and policy requires a trusted workflow (.rayspec/policy.yaml:2);
    review the workflow, then run: rayspec trust add nightly
```

Without that key the trust list is still useful on its own: `rayspec trust check` in front of
`rayspec run` in a cron entry is the same gate, spelled in shell.

The file belongs in the repository next to `policy.yaml`. It carries a path and a digest and
nothing else — never workflow content, never an input, never a secret.

## Per-agent controls: `network:` and `commands:`

Two things you want to say about an agent before leaving it alone overnight live on the agent, not
in the policy file, because they are part of what the workflow *is*:

```yaml
# .rayspec/agents/reviewer.yaml
provider: claude
access: read-only
network: off
commands:
  deny:
    - '^\s*rm\s+-rf\s+/'
    - '^\s*curl\b.*\|\s*sh'
```

`network: on|off` maps onto the one mechanism both shipped providers actually have: with `off`,
the provider's web tools are denied (the same machinery as `tools.deny: [web]`). Setting
`network: off` while also allowing `web` is a contradiction and an error.

`commands: {deny: [regex], allow: [regex]}` are Python regular expressions matched against the
command line an agent is about to run; `deny` is checked first, and a non-empty `allow` means
"nothing else". The patterns are compiled at load time, so a broken one is a file-and-line error
rather than a control that quietly matches nothing.

### Honest enforcement

This is the part that matters more than the feature list.

| Control | Claude adapter | Codex adapter |
|---|---|---|
| `providers.allow`, `models.deny`, `access.max`, `mcp.allow_servers` | enforced at load time (the run never starts) | same |
| `tools.deny` | folded into the agent's tool policy | only `web` — anything else is advisory, with a warning |
| `network: off` | denies the provider's web tools | denies web search |
| `commands:` | **advisory** — warned about on every validate | **advisory** — warned about on every validate |

* **`network: off` is not a firewall.** It denies the provider's own web tools. A shell command
  the agent runs — `curl`, a package install, a test that opens a socket — still reaches the
  network unless the provider's sandbox stops it (`access: read-only` and the Codex sandbox modes
  are separate mechanisms, documented in [providers.md](providers.md)). rayspec does not run a
  network sandbox of its own.
* **`commands:` is advisory on every provider that ships today.** Enforcing it needs the provider
  to hand rayspec its tool calls *before* they run. A provider that can do that declares
  `command_policy` in its capabilities and the warning disappears; neither shipped adapter does
  yet. `rayspec validate` therefore warns on every agent that sets the block:

  ```
  warnings
    - agents.reviewer.commands: commands: cannot be enforced on provider 'claude' — it does not
      hand rayspec its tool calls before they run, so the block is advisory there
  ```

  The block is still worth writing: it records the intent where a reviewer can see it, and it is
  the field an adapter will read when one can enforce it. It is not a control you may rely on
  today, and rayspec says so every time rather than letting you assume otherwise.

## What policy deliberately does not do

* No policy is fetched over the network, and no key names an organisation or a shared registry.
  Three files on this machine, that is all.
* Policy cannot grant a permission. Every key removes something; that is what makes layering safe.
* Policy does not change what a step *does*. It decides whether the workflow may run at all and
  which knobs the agents are allowed to reach.

## Where the code lives

`rayspec.policy` owns the document (`policy/model.py`), the layering and provenance
(`policy/layers.py`), the checks (`policy/enforce.py`) and the trust list (`policy/trust.py`).
`rayspec.loader.validate` calls it from exactly one place. The change guard is
`rayspec.workspace.guard`. See `CONTRACTS.md` for the public surface.
