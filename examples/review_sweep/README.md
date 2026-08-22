# review_sweep — one run, every angle it could finish

Three independent reviews of the same change, in one run; each angle that answers gets its
findings written to a report file the run keeps (`artifacts:`). When one angle fails,
`defaults.on_step_failure: continue` keeps the other branches — **and the steps below them** —
running, so the run still produces every report it could. It still ends `failed`.

```
examples/review_sweep/
├── .rayspec/workflows/review_sweep.yaml   # the workflow
├── stubs.yaml                             # one angle fails (scripted, for --dry-run)
├── stubs_clean.yaml                       # every angle answers
├── checks.yaml                            # what scripts/check_examples.py asserts
└── README.md
```

| Feature | Where |
|---|---|
| `defaults.on_step_failure: continue` | top of the file |
| `artifacts:` — files a step promises to write, kept with the run | `api_report`, `docs_report`, `tests_report` |
| `join: always` (finally-semantics) | `digest` |
| `defaults.max_parallel`, `defaults.timeout`, `defaults.agent` | top of the file |
| `{% if %}` in a prompt over another step's `status` | `digest` |
| Jinja builtins over step statuses (`select('equalto', …)`) | `outputs:` |

## Why `on_step_failure: continue`

The three angles share one input and nothing else. One of them failing says nothing about the
other two, and stopping there would mean starting the whole run again to learn the rest. The
policy decides what happens to the work that has **not started** when a step fails — here, the
report step of every angle that was still reviewing:

| `defaults.on_step_failure` | when `api` fails | reports written |
|---|---|---|
| `fail_fast` | the angles still running are cancelled (`interrupted`) | 0 |
| `drain` (default) | the angles still running finish, nothing new starts | 0 |
| `continue` (here) | the other branches keep being scheduled, report steps and all | 2 |

(With scripted stubs the other two angles answer instantly, so under `fail_fast` there is
nothing left to cancel and the two policies look alike; against a real provider they do not.)

Three things it is **not**:

- it is not `allow_failure:`. That one *tolerates* a step's failure; this run still ends `failed`
  with exit code 1. `continue` buys information, not a green run;
- it is not `each.on_failure: continue`, which is about the *items* of one fan-out step (see
  [`triage_fanout`](https://github.com/rayspec-labs/rayspec-py/tree/main/examples/triage_fanout));
- it does not keep the failed branch going: `api_report` is skipped with `upstream_failed`,
  because it needed the answer that never came.

`--fail-fast` on the command line overrides it, and may only ever tighten: it beats `continue`
and `drain` and never loosens a workflow that asked for `fail_fast`.

## Try it without credentials

```sh
cd examples/review_sweep
rayspec validate                                          # schema, graph, references, capabilities
rayspec plan review_sweep                                 # step order, agents, run-level caps
rayspec run review_sweep --dry-run --stubs stubs.yaml      # one angle down: exit 1
rayspec run review_sweep --dry-run --stubs stubs_clean.yaml   # every angle answers: exit 0
rayspec test                                              # both scenarios, from checks.yaml
```

With `stubs.yaml` the run ends `failed` (exit 1) and the steps end like this:

| step | status |
|---|---|
| `collect` | succeeded |
| `api` | **failed** (`upstream model unavailable (503)`) |
| `api_report` | skipped (`upstream_failed`) — it needed the answer that never came |
| `docs`, `docs_report` | succeeded |
| `tests`, `tests_report` | succeeded |
| `digest` | succeeded (`join: always`) |

`rayspec show <run-id>` then lists the two reports the run did produce under `artifacts:`, with
their size and sha256.

## Run it for real

```sh
rayspec run review_sweep -i target=src/rayspec/loader
rayspec show <run-id>            # the step tree, and the artifacts table
```

Needs a logged-in `claude` CLI (or `ANTHROPIC_API_KEY`). `isolation: none` runs in the directory
itself, so the `reports/` directory the report steps write is created there; a declared artifact
is copied into the run directory as well, which is what keeps it after the checkout moves on.
A `--dry-run` writes nothing and checks no artifact — there is nothing to check, because no step
really ran.
