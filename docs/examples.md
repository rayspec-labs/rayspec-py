# Examples

Runnable example projects live under `examples/` in the repository (each one is a `.rayspec/`
tree with its own README, a `stubs.yaml` and a `checks.yaml`, so it runs with `--dry-run --stubs`
and no login; `scripts/check_examples.py` drives every `checks.yaml`). The rayspec repository also
dogfoods itself through its own `.rayspec/workflows/`. The table below is the coverage matrix
(`examples/README.md` holds the full one) — every capability is exercised by at least one example.
The examples are projects to copy from; the workflows *bundled with rayspec* (`rayspec workflows`
lists them, `rayspec workflows eject <name>` copies one into `.rayspec/workflows/`) are the ones
that run by name in any project without copying anything.

| Example | Shows | Try |
|---|---|---|
| `hello_review` | a single `prompt:` step, `inputs` (string + enum), `outputs:`, a tier model, `rayspec plan`/`validate`, `--dry-run --stubs` | `rayspec run hello_review --dry-run --stubs stubs.yaml` |
| `fix_issue` | `shell:` → structured output (`output_schema`) → `when:` + `stop:` → `loop:` self-heal (`until` with `has_signal`, `iteration.prev`, `session:` continuation, `allow_failure`, `on_exhausted`) → `approve:` → a PR `shell:` step; worktree isolation by default | `rayspec plan fix_issue --input issue=123` |
| `triage_fanout` | `each:` fan-out with `max_parallel`, `on_failure: continue`, `items`, `join: any`/`always`, per-item structured output, a `python:` step with `deps` | `rayspec run triage_fanout --dry-run --stubs stubs.yaml` |
| `review_block` + `pr_review` | `include:` with `with:`/`outputs:`, named agents in `.rayspec/agents/`, `instructions_file`, a `tools` policy, `access: read-only`, Claude **and** Codex agents side by side, `provider_options`, a pricing table (`~$`), an `@alias` | `rayspec plan pr_review` |
| `review_sweep` | `defaults.on_step_failure: continue` — three independent review branches, one of them fails, the others still finish (the run still fails); declared `artifacts:` kept with the run; `join: always` | `rayspec run review_sweep --dry-run --stubs stubs.yaml` |
| `unsupported_demo` | `max_turns`/`tools` on a Codex agent → the capability error, and `--allow-unsupported` | `rayspec validate unsupported_demo` (exit 2) vs `--allow-unsupported` |
| `release_check` | `isolation: none`, `retry`/`timeout`, a `join: always` cleanup step, `env:`, `RAYSPEC_CONTEXT` + `jq`, `--repo` | `rayspec run release_check -i tag=v0.4.0 --dry-run --stubs stubs.yaml` |
| `notify_webhook` | a `secret: true` input: the webhook URL reaches the `shell:` step as `RAYSPEC_INPUT_WEBHOOK_URL` only, is never persisted (`<secret>` in `run.json`/`plan`/`show`) and is supplied again on resume | `rayspec run notify_webhook -i message="hi" --dry-run --stubs stubs.yaml` |
| `secret_via_tool` | a `secrets:` block in `config.yaml` (`env`/`file`/`cmd` source): the credential reaches a `shell:` **tool** that exposes a capability, and the agent consumes the tool's result — never the key | `rayspec run secret_via_tool --dry-run --stubs stubs.yaml` |

## Bundled workflows

These run by name in any project — `rayspec run <name>` with no `.rayspec/` at all — and
`rayspec workflows eject <name>` copies one into `.rayspec/workflows/` when you want to change it.
Every one of them is self-contained (inline agents, no prompt files) and covered offline by
`tests/workflows/checks.yaml` in the repository.

| Workflow | Does | Inputs |
|---|---|---|
| `fix_issue` | triage a GitHub issue, fix it in a self-healing loop, gate a PR (class `chore`) | `issue` (required), `base`, `mode`, `test_command` |
| `pr_review` | check out a PR, run `review_block` on it, optionally post the verdict | `pr` (required), `depth`, `post` |
| `review_block` | the includable Claude + Codex review with a judge; `include: review_block` | `target` (required), `depth` |
| `release_check` | tests, release notes, a human gate (class `release`), tag and notify | `tag` (required), `min_coverage`, `push` |
| `resolve_conflicts` | merge a base branch, classify every conflicted file with a read-only analyst, resolve in a loop until no marker is left and `test_command` passes, commit; a `risk: high` verdict pauses at a gate of class `risky` **before** any file is touched; a clean merge stops with `cancelled` (exit 4), giving up leaves the merge in progress for a human (exit 1, nothing committed) | `base`, `test_command`, `max_attempts` |

`resolve_conflicts` always runs in a worktree, so neither the merge nor the agent's edits touch
your checkout; the merge commit lands on the run's `rayspec/resolve_conflicts-<id>` branch and
pushing or opening a PR is the caller's job. Hold its gate shut in your policy — a class a
workflow names is only as strict as the operator's policy makes it ([policy.md](policy.md#approval-classes)):

<!-- rayspec:skip a policy document, not a workflow -->
```yaml
# .rayspec/policy.yaml
approvals:
  classes:
    risky: { allow_yes: false }   # never approved by --yes, --dry-run or --approve-class; a human still can
```

## Minimal workflow to copy

<!-- rayspec:run -->
```yaml
# .rayspec/workflows/review.yaml
rayspec: 1
name: review
description: Review the working tree and summarise findings.
inputs:
  target: { type: string, default: "." }
agents:
  reviewer: { provider: claude, model: small, access: read-only,
              instructions: You are a meticulous code reviewer. Be concrete. }
steps:
  - id: files
    shell: git ls-files "{{ inputs.target }}" | head -50
  - id: review
    needs: [files]
    agent: reviewer
    prompt: |
      Review these files and list findings:
      {{ steps.files.output }}
    output_schema:
      type: object
      properties: { verdict: { enum: [approve, request_changes] }, summary: { type: string } }
      required: [verdict, summary]
outputs:
  verdict: "{{ steps.review.output.verdict }}"
  summary: "{{ steps.review.output.summary }}"
```

```
rayspec validate
rayspec plan review
rayspec run review --dry-run --stubs-init stubs.yaml   # scaffold, then edit stubs.yaml
rayspec run review --dry-run --stubs stubs.yaml         # no provider login needed
rayspec run review --input target=src                   # the real thing (worktree by default)
```

## Patterns

- **Self-healing loop**: `loop:` with `until: steps.review.output | has_signal('BUILD-CLEAN')`;
  the implementer keeps its `session:`; a `shell: pytest -q` step with `allow_failure: true` is
  the authoritative verdict the reviewer sees (`{{ steps.check.exit_code }}`).
- **Branch and stop**: a structured `assess` step, then `when: steps.assess.output.verdict ==
  'skip'` → `stop: {status: cancelled, reason: ...}` and the happy path on the other branch.
- **Fan-out with partial failure**: `each:` over `steps.list.output | fromjson` with
  `on_failure: continue`; the downstream step reads `steps.triage.items` and skips the `null`
  slots.
- **Reusable block**: put a review block in `.rayspec/workflows/review_block.yaml` with its own
  `inputs:`/`outputs:` and `include:` it with `with:`; the including workflow only sees
  `steps.<include id>.output.<key>`.
- **Finally step**: `join: always` on a cleanup `shell:` step so it runs even when a sibling failed
  or the run is draining.
