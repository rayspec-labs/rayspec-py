# triage_fanout — dynamic fan-out with `each:`

Classify a batch of issues in parallel, tolerate a failing item, summarise with a `python:` step
that installs its own dependency, escalate only when something is critical, and always clean up.

| Feature | Where |
|---|---|
| `each: inputs.issues` with `as: issue`, `max_parallel: 3`, `on_failure: continue` | `triage` |
| `each.index`, `each.total`, `{{ issue }}` in the body | `triage/classify` |
| `RAYSPEC_WORKDIR` / `RAYSPEC_RUN_ID` — a per-run scratch dir written by the fan-out, removed by `cleanup` | `triage/view`, `cleanup` |
| `retry: {attempts: 2, delay: 1s}` on a prompt step (a stubbed transient `429` is retried) | `triage/classify` |
| per-item `output_schema` (structured output inside a fan-out) | `triage/classify` |
| inline agent mapping (`agent: {provider, model, access}`) | `classify`, `alert`, `digest` |
| `python:` with `deps: [tabulate]` (`uv run --with tabulate`) and `env:` | `summarize` |
| `steps.triage.output` (list aligned with items, `None` for failed ones) | `summarize`, `alert` |
| `steps.triage.items` (`index,item,status,output,error`) | `outputs` |
| `join: any` (runs when `alert` was skipped) | `digest` |
| `join: always` (finally semantics) | `cleanup` |
| `type: array` (of `integer` items), `type: object`, `enum` inputs with defaults | `inputs:` |
| `defaults.max_parallel` (run-wide cap) vs the fan-out's own `max_parallel` | top + `triage` |

## Try it without credentials

```sh
cd examples/triage_fanout
rayspec run triage_fanout --dry-run --stubs stubs.yaml
rayspec run triage_fanout --dry-run --stubs stubs.yaml --verbose                        # + a → line per step start
rayspec run triage_fanout --dry-run --stubs stubs_quiet.yaml -i issues=7 -i issues=8   # repeated --input → array
```

`stubs.yaml` scripts item 1 to fail (`fail:` without `transient` → no retry), item 2 to be
critical and item 3 to hit a transient `429` once (`times: 1`) that the step's `retry:` absorbs;
`stubs_quiet.yaml` uses `match:` (prompt regex) instead of step paths. Expected output of the first
run (the `triage[N]/view` lines are the fan-out body's shell step, skipped in a dry run; the
`python:` step is skipped too, so `table` is empty; the `↻` retry line is printed by the one-line
console — on a terminal the live tree shows it under the step while it waits — and `--verbose`
adds a `→ <step> (kind)` line per step start):

```
▶ run 20260820-175752-vrdj started (triage_fanout)
✓ triage[0]/view succeeded 1ms
✓ triage[1]/view succeeded 1ms
✓ triage[2]/view succeeded 1ms
✓ triage[0]/classify succeeded 7ms · 460 tok
✗ triage[1]/classify failed 6ms — api: simulated provider outage
✓ triage[2]/classify succeeded 7ms · 460 tok
✓ triage[3]/view succeeded 2ms
↻ triage[3]/classify retry in 1s (attempt 2): api: 429 rate limited
✓ triage[3]/classify succeeded 1.0s · 460 tok
✓ triage succeeded 1.0s
✓ summarize succeeded 2ms
✓ alert succeeded 4ms · 460 tok
✓ digest succeeded 3ms · 460 tok
✓ cleanup succeeded 0ms
■ run 20260820-175752-vrdj succeeded · 2.3k tok
outputs
name       ┃ value
━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
table      │
digest     │ 4 issues triaged, 1 critical (auth), 1 classifier error, rest low/high.
classified │ 3
failed     │ 1
escalated  │ true
  tokens: 2300 · run dir: ~/.rayspec/projects/<slug>/runs/20260820-175752-vrdj
```

Token counts are per *successful* attempt (`460 tok` for `triage[3]/classify`, the retried `429`
attempt is not billed by the stub); durations vary from run to run.

In the quiet run `alert` is `skipped — when_false` and `digest` still runs thanks to `join: any`.

## Run it for real

```sh
rayspec run triage_fanout -i issues='[101,102,103]'       # JSON array also works
rayspec run triage_fanout --inputs-file batch.yaml --quiet
```

Needs `gh`, `uv` (for the `deps`) and a Claude login. Pass `--exec-shell` with `--dry-run` to run
the `python:`/`shell:` steps while still stubbing the agents.
