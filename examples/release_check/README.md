# release_check — in-place runs, retry/timeout, finally-steps, env and `RAYSPEC_CONTEXT`

A pre-release gate that runs **in place** (`isolation: none`) against the checkout you intend to
tag — no worktree, so the tag lands on your branch.

| Feature | Where |
|---|---|
| `isolation: none`, `defaults.timeout`, `defaults.on_step_failure: drain` | top |
| `interpreter: sh`, `{% raw %}` around Go-template braces, `allow_failure` | `last_tag` |
| `retry: {attempts: 2, delay: 5s, on_error: all}` (timeouts count as retryable), step `timeout` | `tests` |
| `env:` on a shell step (values are templates, str-coerced) | `tests`, `meta` |
| `RAYSPEC_CONTEXT` + `jq` — the whole template context as JSON for scripts | `tests` |
| `RAYSPEC_ARTIFACTS_DIR` + `RAYSPEC_RUN_ID` (keep the coverage report as a run artifact), `RAYSPEC_WORKDIR` | `tests`, `cleanup` |
| `output_schema` on a **shell** step (stdout must be JSON) | `meta` |
| code computes, agents judge: a shell step prepares `git log` + `gh pr list` for a read-only agent, which returns the complete notes in a structured field | `history`, `notes` |
| `env:` on a **prompt** step (capability `env_injection`) | `notes` |
| `run.workdir`, `project.name` in prompts | `notes` |
| `approve: {message, on_reject: continue}` + `steps.gate.approved` | `gate`, `publish` |
| `when: env.SLACK_WEBHOOK is defined` | `notify` |
| `join: always` + `always_run: true` cleanup | `cleanup` |
| `type: number` / `type: boolean` inputs | `inputs:` |

## Try it without credentials

```sh
cd examples/release_check
rayspec run release_check -i tag=v0.4.0 --dry-run --stubs stubs.yaml
rayspec run release_check -i tag=v0.4.0 -i push=true --dry-run --stubs stubs.yaml
SLACK_WEBHOOK=https://hooks.example/x rayspec run release_check -i tag=v0.4.0 -i push=true --dry-run --stubs stubs.yaml  # `notify` runs
```

Expected output of the first run (shell steps are skipped in a dry run, `meta` gets the minimal
instance of its `output_schema` — `{previous: '', commits: 0}` —, gates auto-approve, `publish`
is `when_false` because `push=false`, `cleanup` runs thanks to `join: always`):

```
▶ run 20260820-195822-mnry started (release_check)
✓ last_tag succeeded 7ms
✓ tests succeeded 7ms
✓ meta succeeded 2ms
✓ history succeeded 0ms
✓ notes succeeded 5ms · 146 tok
● decision: approved
✓ gate succeeded 1ms
○ publish skipped 0ms — when_false
○ notify skipped 0ms — upstream_skipped
✓ cleanup succeeded 0ms
■ run 20260820-195822-mnry succeeded · 146 tok
outputs
name      ┃ value
━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
title     │ v0.4.0 — worktrees by default
notes     │ ### Added
          │ - `rayspec worktrees clean`
          │ ### Fixed
          │ - resume after Ctrl-C
published │ false
approved  │ true
  tokens: 146 · run dir: ~/.rayspec/projects/<slug>/runs/20260820-195822-mnry
```

Durations vary from run to run, and the token counts of `notes` vary with the checkout path (the
stub counts the rendered prompt, which embeds `run.workdir`).

Add `--exec-shell` to really run the shell steps while the agent stays stubbed. The walkthrough
needs `git` and `jq` and nothing else: `last_tag` needs `gh` and a GitHub remote (without them it
fails *tolerated*, `allow_failure`), `meta` counts the commits since the previous release — or
every commit when there is none, so a fresh one-commit repo works —, `history` prints the commit
log for the notes (its `gh pr list` falls back to a one-line notice without `gh`/a remote) and
`tests` skips with a notice when the checkout has no `tests/` directory (otherwise it runs
`pytest --cov` under `uv`). In a fresh copy of this directory (`git init && git add -A && git
commit -m init`, no remote):

```
▶ run 20260820-195832-elfo started (release_check)
✓ tests succeeded 34ms
✗ last_tag failed (tolerated) 254ms — exit code 1: no git remotes found
✓ meta succeeded 31ms
✓ history succeeded 91ms
✓ notes succeeded 6ms · 168 tok
● decision: approved
✓ gate succeeded 2ms
○ publish skipped 0ms — when_false
○ notify skipped 0ms — upstream_skipped
✓ cleanup succeeded 13ms
■ run 20260820-195832-elfo succeeded · 168 tok
```

`rayspec logs <run-id> --step meta` then shows `{"previous": "", "commits": 1}` and `--step
history` the one-commit log followed by `(gh unavailable — no PR list)`. The agent never runs
`git` itself: `notes_writer` is `access: read-only` (no shell), so the workflow computes the
material in `history` and the agent's only job is to judge and write — its JSON `notes` field
holds the finished notes, not a description of what it did.
`--stubs-init my_stubs.yaml` writes a stub scaffold with one entry per prompt step. `checks.yaml`
asserts all three scenarios; its `env:` key pins `SLACK_WEBHOOK` per check so the result does not
depend on your shell environment.

## Run it for real

```sh
rayspec run release_check -i tag=v0.4.0                         # prompts at the gate
rayspec run release_check -i tag=v0.4.0 --fail-fast             # a failing step cancels running siblings
rayspec run release_check -i tag=v0.4.0 -i push=true --yes      # tag + push without asking
rayspec run release_check -i tag=v0.4.0 --no-interactive        # exit 3 (paused at the gate)
rayspec resume <run-id>                                         # re-asks on a TTY
rayspec approve <run-id> "ship it"                              # decide without a TTY …
rayspec reject <run-id> "hold the release"                      # … (on_reject: cancel ⇒ exit 4)
rayspec show <run-id>; rayspec logs <run-id> --follow           # inspect / tail a run
```

### `--repo`: run it against another checkout or a registered project

```sh
rayspec run release_check --repo ~/src/other-app -i tag=v1.2.0          # that path is the project root
rayspec projects add app git@github.com:me/app.git --base main           # register once …
rayspec run release_check --repo app -i tag=v1.2.0                       # … run by name
rayspec projects list                                                    # what is registered
rayspec projects remove app                                              # clones/worktrees are kept
rayspec run release_check --repo git@github.com:me/app.git -i tag=v1.2.0 # bare clone under ~/.rayspec
```

Note: URL and registered-name sources always run in a fresh worktree (from `origin/<base>`), even
though the workflow says `isolation: none` — the notice is printed at start. The workflow is loaded
from the target checkout's `.rayspec/`, so copy this file there first.
