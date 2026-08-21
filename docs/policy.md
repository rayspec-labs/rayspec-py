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

### `provider_options` cannot remove a control

A control is only real if the party it constrains cannot remove it. An agent's `provider_options:`
block is handed to the adapter as-is, so without care four lines of YAML *inside the very workflow
a policy governs* would hand the denied tools, the access level, a denied model or an excluded MCP
server straight back. Two mechanisms stop that, and both are needed.

**The adapter never lets a raw option replace a value rayspec computed.** Every field the adapter
derives from an agent's neutral fields — `tools`, `allowed_tools`, `disallowed_tools`,
`permission_mode`, `model`, `system_prompt`, `setting_sources`, `strict_mcp_config`, the limits —
is adapter-owned: naming it under `provider_options` is ignored, with a warning on the run. The
Codex adapter does the same for the `config` keys it computes (`model`, `sandbox_mode`,
`approval_policy`, `web_search`, `tools.web_search`). The rule is mechanical, not a list of the
dangerous ones: if `build_options` sets it, `provider_options` cannot. Change it through the
neutral field, which is the field policy and code review both look at.

Two keys are deliberately *merged* rather than replaced, because adding to what rayspec computed
is a real need: `env` and `mcp_servers` (`config.mcp_servers` on Codex). rayspec's own entries win
on a name collision.

**Policy checks the merged keys, and the workflow's own controls too.** While any control is in
force, naming a `provider_options` key that would undo it is a load-time error quoting both the
option and the line that imposed the control:

```yaml
provider_options:
  claude:
    allowed_tools: [Bash, WebSearch]     # refused while a layer sets tools.deny
    permission_mode: bypassPermissions   # refused while a layer sets tools.deny or access.max
    model: claude-opus-4-1               # refused while a layer sets models.deny
    mcp_servers: {evil: {...}}           # refused while a layer sets mcp.allow_servers
    disallowed_tools: []                 # refused on an agent with network: off
```

The last line is the one worth dwelling on: `network: off` is a *workflow* control, not a policy
key, so it is checked with no policy file at all — which is the common case, and the case where an
unprotected control does the most damage. The table the check is derived from
(`POLICY_CONTROLLED_OPTIONS` in `policy/enforce.py`) has one entry per control and covers both
kinds; a control added without an entry there is an escape hatch waiting to be found.

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
| `workspace:` (the change guard) | **not enforced in this build** — library + policy key, warned about on every validate | **not enforced in this build** — library + policy key, warned about on every validate |
| `provider_options:` | fields the adapter computes are ignored with a warning; `env`/`mcp_servers` merge under them; policy refuses keys that would undo a control | same, for the `config` keys the adapter computes |

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
