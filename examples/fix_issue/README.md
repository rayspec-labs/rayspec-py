# fix_issue — the self-healing loop

shell (`gh issue view`) → structured triage (`output_schema`) → `when:` branch + `stop:` →
`loop:` that implements, tests and reviews until `has_signal('BUILD-CLEAN')` → `approve:` gate →
shell step that pushes and opens the PR. The run happens in a git worktree on branch
`rayspec/fix_issue-<shortid>` (the default isolation for git projects), so your checkout stays clean.

What to look at in `.rayspec/workflows/fix_issue.yaml`:

| Feature | Where |
|---|---|
| `defaults.agent`, `defaults.timeout`, `defaults.max_parallel` | top of the file |
| Claude **and** Codex agents (`triage` read-only, `implementer` workspace-write) | `agents:` |
| `instructions_file` (Jinja-templated, relative to `.rayspec/`) | `.rayspec/prompts/implementer.md` |
| `output_schema` → `steps.assess.output.verdict` | `assess` |
| `when:` + `stop: {status: cancelled}` | `bail` |
| `loop:` with `max_iterations`, `until`, `on_exhausted: fail` | `build` |
| `session: implement` — the implementer continues its own session across iterations | `build/implement` |
| `iteration.first / n / max / prev.<id>` | `build/implement` prompt |
| `allow_failure: true`, `steps.check.ok`, `steps.check.exit_code` | `build/check`, `build/review` |
| `approve:` (TTY prompt; `--yes`; non-TTY pauses with exit 3) | `confirm` |
| `{{ }}` inside `shell:` → `${RAYSPEC_V<n>}` env references | `pr` |
| `outputs:` incl. `steps.build.iterations` | bottom |

## Try it without credentials

```sh
cd examples/fix_issue
rayspec plan fix_issue -i issue=42
rayspec run fix_issue -i issue=42 --dry-run --stubs-init my_stubs.yaml    # scaffold one entry per prompt step
rayspec run fix_issue -i issue=42 --dry-run --stubs stubs.yaml            # happy path, exit 0
rayspec run fix_issue -i issue=42 --dry-run --stubs stubs.yaml --verbose  # also prints step starts
rayspec run fix_issue -i issue=7  --dry-run --stubs stubs_skip.yaml       # stop → cancelled, exit 4
rayspec run fix_issue -i issue=9  --dry-run --stubs stubs_exhausted.yaml  # loop exhausted, exit 1
```

Happy path: the first review rejects, the second says `BUILD-CLEAN`:

```
✓ assess succeeded · 1.5k tok
○ bail skipped — when_false
✓ build[1]/implement succeeded · 1.5k tok
✓ build[1]/check succeeded
✓ build[1]/review succeeded · 1.5k tok
✓ build[2]/implement succeeded · 1.5k tok
✓ build[2]/check succeeded
✓ build[2]/review succeeded · 1.5k tok
✓ build succeeded
● decision: approved
✓ confirm succeeded
✓ pr succeeded
■ run … succeeded · 7.5k tok
outputs: verdict=fix iterations=2 pr_url=   (shell steps are skipped in a dry run → '')
```

`stubs.yaml` shows the stub file features: `defaults` usage, a glob key (`build[*]/review`) with a
`sequence` that advances per loop iteration, and replayed `events` (tool call/result).

## Run it for real

```sh
rayspec run fix_issue -i issue=42                      # worktree + branch rayspec/fix_issue-xxxx
rayspec run fix_issue -i issue=42 --worktree --base develop   # explicit; branch the worktree off develop
rayspec run fix_issue -i issue=42 --no-worktree        # in place
rayspec run fix_issue -i issue=42 --yes --json         # auto-approve, JSONL events
rayspec run fix_issue -i issue=42 --no-interactive     # pauses at `confirm` (exit 3) …
rayspec resume <run-id>                                # … re-asks at the gate on a TTY
rayspec approve <run-id> "ship it"                     # … or decide without a terminal
rayspec reject <run-id> "not yet"                      #     (on_reject: cancel ⇒ exit 4)
rayspec run fix_issue --resume <run-id> --force        # resume although the workflow file changed
rayspec runs; rayspec show <run-id>; rayspec logs <run-id> --step build[1]/implement
rayspec cancel <run-id>                                # SIGINT a live run / cancel a paused one
rayspec worktrees list; rayspec worktrees clean --merged
```

Needs `gh` (authenticated), `claude` login and `codex` login (or `OPENAI_API_KEY`). Add
`pricing` to `.rayspec/config.yaml` to see `~$` estimates for the Codex steps (see
`examples/pr_review`). If the run is interrupted (Ctrl-C) it can be resumed with
`rayspec resume <run-id>` (or `rayspec run fix_issue --resume <run-id>`); finished steps are
replayed from the cache.
