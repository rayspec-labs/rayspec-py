# Policy: guardrails as a file

A workflow you run by hand is governed by your judgement at the moment you type the command. A
workflow on a schedule is governed by whatever was written down. This page is about writing it
down: a `policy.yaml` that says which providers, models, access levels, tools and MCP servers are
allowed, a change guard that bounds how much of the repository a run may rewrite, and a trust list
that says which workflows may run at all.

Three properties hold throughout:

* **Load time, not run time.** Every policy key is checked while the workflow is being loaded, so
  a violation is an error with a file and a line before a single token is spent. `rayspec run`,
  `rayspec plan`, `rayspec validate` and `rayspec test` all refuse the same workflows — and so do
  `rayspec resume`, `rayspec approve` and `rayspec reject`, which re-load the workflow to continue
  a paused run. The second half of a run is subject to the policy in force *now*; pausing at a gate
  is not a way past a guardrail.
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

**The project layer is found under `--root`, not next to the workflow file.** `<project>` is the
project root of the invocation — the directory `--root` names, or the one discovered from the
working directory — so `rayspec validate some/other/checkout/.rayspec/workflows/w.yaml --root .`
validates that document against *this* project's policy and never reads the one sitting beside it.
That is deliberate (the policy belongs to the checkout you are running from, not to whatever file
you point at), and it is why every command says which layers it actually read.

### Which layers are in force

`rayspec validate`, `rayspec plan` and `rayspec run` each print one line naming the policy files
they read, before anything else they have to say:

```
$ rayspec validate nightly
nightly (.rayspec/workflows/nightly.yaml): OK
  policy: .rayspec/policy.yaml, ~/.rayspec/policy.yaml
```

When no layer is in force, the line says so and names the paths that were searched — unshortened,
so the root the discovery ran against is visible:

```
  policy: none in force (searched /srv/ci/build/.rayspec/policy.yaml, ~/.rayspec/policy.yaml)
```

"Silently absent" is the worst failure mode a guardrail has: a policy file that is one directory
too high, or a `--root` pointing somewhere else, otherwise looks exactly like a policy that is
being obeyed. `--json` carries the same information as `policy: {layers, searched}`.

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
policy, because a guardrail that silently disappears is worse than none. That covers the shapes a
`policy.yaml` ends up in after a bad checkout or a moved home directory: a dangling symlink, a
symlink loop, a directory or an unreadable parent are each an error naming the path and what it
is. A path that is genuinely absent is the only silent case, because that is the ordinary one.

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

### `provider_options` is an allow-list while a control is in force

A control is only real if the party it constrains cannot remove it. An agent's `provider_options:`
block is handed to the adapter *over* the options rayspec computed, so without care a few lines of
YAML **inside the very workflow a policy governs** hand the denied tools, the access level, a
denied model or an excluded MCP server straight back.

**A key rayspec cannot reason about is refused, rather than passed through.** That is the whole
rule, and the default is the point. Listing the dangerous keys cannot work: Claude's `extra_args`
re-emits *any* CLI flag, appended after the ones rayspec computed, where the last one wins — so
`extra_args: {"permission-mode": bypassPermissions}` is `--permission-mode dontAsk … --permission-mode
bypassPermissions`. `settings` carries a whole permissions document. `hooks`, `sandbox`, `plugins`,
`add_dirs`, `can_use_tool` each have their own route, and the SDKs grow fields between releases.

So while **any** control governs an agent, every key of its own provider's block has to be one
rayspec has written down the effect of. Anything else is a load-time error that names the key, the
control, the file and line that imposed it, and the keys that *are* permitted:

```
steps.review.agent.provider_options: provider_options.claude.extra_args is refused while
access.max (.rayspec/policy.yaml:2) is in force: the claude adapter applies provider_options over
the options rayspec computed, and rayspec cannot say whether this key widens what that control
narrowed — under a control only the keys it has reasoned about pass (env, load_timeout_ms,
max_buffer_size, max_thinking_tokens, mcp_servers, user). Remove the key, or drop the control it
could undo
```

#### What counts as a control

"Any control" is meant literally, and it is the half that is easy to get wrong: a trigger that
lists two controls is defeated by writing a third — or by writing the same one somewhere the
trigger was not looking. A control is anything that constrains the run, wherever it is spelled.

**Fields the agent sets on itself.** These come with no policy file at all — the common case, and
the case where an unprotected control does the most damage.

| Field | What it withholds |
| --- | --- |
| `access` | the sandbox level; anything below `full` withholds power the provider would otherwise grant |
| `tools` | which tools may run — `deny` and a non-empty `allow` alike |
| `network: off` | the provider's web tools, by folding `web` into `tools.deny` |
| `commands` | which shell commands may run |
| `mcp` | the servers the run may reach (declaring any makes the set strict) |
| `max_turns`, `budget_usd` | hard ceilings on turns and money |
| `on_denial: fail` | makes a refused tool call stop the step — the teeth of every denial |

**Fields the workflow sets over every agent it runs.** The same ceilings, one level up — and the
only spelling some of them have: `budget_usd` on the agent is a capability not every provider
declares, so a run-level cap is where an operator puts one.

| Field | What it withholds |
| --- | --- |
| `isolation` | where the run may write. `worktree` (the **default**) puts it on a copy on its own branch instead of the checkout you are sitting in; only `isolation: none` withholds nothing |
| `defaults.budget_usd`, `defaults.max_tokens` | what the whole run may spend and how many tokens it may use |
| `defaults.timeout_total`, `defaults.timeout` | the run's wall clock, and any one step's |
| `inputs.<name>.secret` | a secret input is never persisted and may be named only in a few places — a restriction on where a value may go |
| a step's own `timeout:` | how long that step, and anything nested in it, may run |

A restrictive **default** is still a restriction: `isolation: worktree` and `access:
workspace-write` are both things a run has to give up on purpose, so the carve-out below has to
be asked for rather than fallen into.

**Every key any policy layer sets**, whatever it is — see [The keys](#the-keys).

**Controls imposed from outside the workflow file:**

| Artefact | Why it constrains the run |
| --- | --- |
| `.rayspec/rayspec.lock` | the model lockfile pins what every agent resolves to, and `--locked` (on by default under CI) refuses a run that resolves to anything else |
| `config.yaml` `providers:` | the machine owner's adapter settings; `provider_options` is applied *over* them (Codex `config`), so a value the owner set there could otherwise be replaced from inside the workflow |

**And the command line.** `rayspec run --worktree` on a workflow that says `isolation: none` is an
operator adding a restriction, so it is written onto the document before the workflow is checked:
what the check reads is `isolation: worktree`, exactly as if the file had said so. It is the only
flag that adds one; the rest choose what is printed, where it goes, which run is addressed — or
*loosen* something (`--yes`, `--allow-unsupported`, `--no-worktree`), and a widening is never a
reason to shut an escape hatch.

An agent that **no** control applies to is untouched: `isolation: none`, `access: full`, no tool
list, no cap, no `mcp:`, no `commands:`, no secret input, no policy file, no lockfile and no
machine settings. It has nothing to bypass, so the escape hatch is still an escape hatch.

None of those lists is where the rule lives, and no list of them could be: a classification is
only as total as the set of schemas it is pointed at, and pointing it at the agent alone is
exactly how the caps one level up stayed invisible. So the universe is read rather than written
down — every model reachable from the workflow document and from the policy document, the resolved
agent a provider receives, every project file rayspec's own source names, and every option of
every CLI command. Each field is classified in `rayspec.policy.controls` as a control (with its
kinds and the one line that earned it the place), as carried by a control on its parent, or as
restricting nothing (with the one line saying why), and `tests/policy/test_control_universe.py`
fails when any of that stops being total — in either direction, so a stale entry fails as loudly
as a missing one, and a nested model added tomorrow fails until someone classifies it.

The keys that pass, and why:

| Provider | Key | Why it is safe |
| --- | --- | --- |
| claude | `env` | extra environment variables, merged **under** rayspec's own *and* the machine owner's `providers.claude.env` — a workflow can add a variable, never displace one of theirs. Checked by name: a variable in the vendor's own namespace (`ANTHROPIC_*`, `CLAUDE_*`) configures the CLI rather than the agent's work, and is refused under a control (below) |
| claude | `mcp_servers` | extra MCP servers, merged under the agent's `mcp:` block; checked server by server (below) |
| claude | `max_thinking_tokens` | how many tokens a turn may think for. It moves what a turn costs, never what a cost is measured against — the thinking tokens are reported as usage like any other |
| claude | `max_buffer_size`, `load_timeout_ms` | transport knobs: how much stdout is buffered, how long to wait for the CLI to come up (the step's own deadline is enforced by the engine around the whole call) |
| claude | `user` | an opaque end-user id forwarded to the API |
| codex | `config.mcp_servers` | as above, checked server by server |
| codex | `config.model_reasoning_summary` | how much of the model's reasoning is summarised into the stream: transcript verbosity |
| codex | `approval_mode` | `deny_all` (the default) refuses every sandbox escalation; see below |
| codex | `ephemeral` | do not persist the thread — it withholds state, it grants nothing |
| codex | `usage_baseline` | usage counters **subtracted** from a resumed thread's totals — the number every spend ceiling is then measured against. Checked by value: under a spend ceiling only a baseline that subtracts nothing passes (below) |

Each of the four unguarded keys says out loud why it needs no guard (`INERT_BECAUSE` in
`rayspec.policy.enforce`), and each reason is paired with the test that holds it to the code: a
key set to an extreme value has to leave every option the adapter computes byte-identical. An
allow-listed key with no guard is inert under every control, which is a second unsafe default
hiding inside a safe design — `usage_baseline` sat there as "accounting only" while setting the
number every ceiling is compared against — so "no guard" cannot be reached by leaving a field out.

**A control that blocks the permitted case is its own defect** — it teaches people to switch the
control off. So the two merged keys are checked by *value*, not refused wholesale: under
`mcp.allow_servers: [github]` an agent may still add `github` through
`provider_options.claude.mcp_servers`, and only a server the policy excludes is named:

```yaml
provider_options:
  claude:
    mcp_servers:
      github: {type: stdio, command: github-mcp-server}   # allowed — github is on the list
      evil: {type: stdio, command: /bin/sh}               # refused, by name
```

Codex's `approval_mode` is guarded the same way. `deny_all` passes anywhere; `auto_review` answers
the agent's sandbox escalation requests *for* it, so it is refused under any control that withholds
sandbox power or network access — `access.max` in a policy file, the agent's own `access:`,
`network: off`, a `tools.deny` that names `web`. A guard matches on the KIND of control, not on
its spelling: `access.max` and `access: read-only` withhold the same thing, and a guard that knew
only the first is how a bypass gets written.

**Everything else the adapter already refused, it still refuses.** Every field an adapter derives
from an agent's neutral fields — `tools`, `allowed_tools`, `disallowed_tools`, `permission_mode`,
`model`, `system_prompt`, `setting_sources`, `strict_mcp_config`, the limits, and on Codex the
`config` keys `model`, `sandbox_mode`, `approval_policy`, `web_search`, `tools.web_search` — is
adapter-owned and ignored with a warning even without a policy. The rule there is mechanical: if
`build_options` sets it, `provider_options` cannot. Change it through the neutral field, which is
the field policy and code review both look at.

**The check reads the block the adapter will act on.** Both adapters and this check narrow
`provider_options` with the same function, because a check that walks one shape while an adapter
accepts two leaves the shape it does not walk unguarded — `provider_options.codex.codex.config`
is a real spelling the adapter honours, and it is checked as the same block.

A provider from a [plugin](extending.md) has no allow-list yet, so under a control its
`provider_options` block is refused whole. That is the same fail-closed default applied to a
provider rayspec knows nothing about. The way through is `config.yaml` under `providers.<id>`,
which belongs to the machine owner rather than to the workflow — and which is *why* it is safe:
that is a setting a workflow cannot edit.

## The worktree change guard

**Read this first: nothing runs the guard in this build.** It ships as a library
(`rayspec.workspace.guard`) and `workspace:` is parsed, merged and reported, but no executor calls
it, so no step fails on it today. A policy file that sets a `workspace:` key gets a warning from
`rayspec validate` saying exactly that. The rest of this section describes the measurement, not a
promise that a run is currently stopped by it.

`workspace:` bounds how much of the repository a run may rewrite. The guard measures the whole
worktree against the run's `base_sha`:

* **`protected_paths`** — globs that must not change. `*` crosses directory separators, a pattern
  ending in `/` covers the directory, a leading `**/` is optional, and a pattern without a
  separator also matches a bare file name in any directory.
* **`max_changed_files`** / **`max_changed_lines`** — size caps. Untracked files count as
  additions, because "quietly wrote 400 new files" is exactly what a diff against `HEAD` misses;
  a binary file counts as a changed file and no lines, the way git counts it. Files `.gitignore`
  hides count too — `.env`, a gitignored `secrets/`, a build directory are the paths most worth
  protecting — which `diff_since(..., include_ignored=False)` turns off for a repository whose
  ignored tree makes the walk pointless.
* **Renames count on both sides.** Moving a file out of a protected directory is a change to the
  protected path as well as to the new one, so `git mv .github/ci.yml ci.yml` trips
  `protected_paths: ['.github/**']`. The line counts go to the destination, so a rename is not
  measured twice.

When a limit is exceeded the report names every limit that broke, with the numbers behind it and a
short diff summary:

```
change guard: 2 limit(s) exceeded since 4bba429fd0a1
  - protected_path: '.github/**' matched .github/workflows/ci.yml
  - max_changed_files: 63 files changed, limit 40
  changed: .github/workflows/ci.yml (+4/-1), src/app.py (+12/-3), … (+61 more)
```

The measurement lives in `rayspec.workspace` (`check_change_guard`, `diff_since`, `match_path`)
and works on anything that is a git worktree with a base commit. Wiring it into the prompt executor
is one call; until that exists, `workspace:` is a recorded intention and `rayspec validate` says so
on every run that sets it.

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

## Per-agent controls

Two of the agent's own controls have a shape of their own and are documented here; the full list
of the fields that count as a control is
[above](#what-counts-as-a-control).

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
| `workspace:` (the change guard) | **not enforced in this build** — library + policy key, warned about on every validate | **not enforced in this build** — library + policy key, warned about on every validate |
| `provider_options:` | fields the adapter computes are ignored with a warning; `env`/`mcp_servers` merge under them; under any control the block is an ALLOW-list (`env` — vendor variables refused —, `mcp_servers`, `max_thinking_tokens`, `max_buffer_size`, `load_timeout_ms`, `user`) | same, for the `config` keys the adapter computes; allow-list is `config.mcp_servers`, `config.model_reasoning_summary`, `approval_mode`, `ephemeral`, `usage_baseline` (zero counters only under a spend ceiling) |

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
(`policy/layers.py`), what counts as a control (`policy/controls.py`), the checks
(`policy/enforce.py`) and the trust list (`policy/trust.py`). `rayspec.loader.validate` calls it
from exactly one place. The change guard is `rayspec.workspace.guard`. See `CONTRACTS.md` for the
public surface.
