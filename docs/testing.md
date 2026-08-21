# Testing workflows

`rayspec test` runs a project's **declarative cases**: each one loads a workflow, validates it and
executes it as a dry run against the stub provider, then checks what came out. No network, no
worktree, no subprocess, and no credentials — the project's `.rayspec/.env` is not loaded for this
command. A full suite of the seven shipped examples runs in about two seconds. It is the check half of the edit → check loop: after changing a prompt, a `when:` or a
stub, `rayspec test` is what tells you whether the graph still does what you said it does.

```console
$ rayspec test
ok fix_issue:happy (0.09s)
ok fix_issue:skip (0.05s)
...
25 passed in 2.3s
```

See [cli.md](cli.md#rayspec-test) for every flag and exit code. This page is the file format and
the way of working.

## Where cases live

Two layouts are discovered, and they parse into exactly the same case:

| Layout | Suite name | Project root of the run |
|---|---|---|
| `.rayspec/tests/<workflow>/<case>.yaml` — one case per file | `tests/<workflow>` | the project |
| `.rayspec/tests/<case>.yaml` — loose case files | `tests` | the project |
| `examples/<name>/checks.yaml` — a `checks:` list | `<name>` | `examples/<name>/` |
| `.rayspec/dryrun/checks.yaml` — a `checks:` list | `dogfood` | the project |

Use the first for your own workflows: the directory names the workflow, the file stem names the
case, so `.rayspec/tests/fix_issue/duplicate_issue.yaml` needs no `workflow:` and no `id:`. The
suite form exists because each rayspec example is a self-contained project with its own
`.rayspec/`, and one file per example is easier to read than a directory of stubs — both are
first-class and both run under `rayspec test`.

## The case format

```yaml
# .rayspec/tests/fix_issue/duplicate.yaml
workflow: fix_issue            # optional here (the directory says so); required in a checks: list
inputs: { issue: 7, mode: fast }
stubs: ../../dryrun/fix_issue.stubs.yaml   # relative to THIS file
env: { SLACK_WEBHOOK: null }   # process env for this case (null unsets)
allow_unsupported: false       # treat capability mismatches as warnings
exec_shell: false              # declares this case wants real shell:/python: execution
                               # (refused unless you pass --exec-shell — see below)
validate: ok                   # ok (default) | error — the expected `rayspec validate` outcome
run: true                      # false = load + validate only
expect:
  status: cancelled            # the final run status
  exit_code: 4
  outputs: { verdict: skip }   # a SUBSET of the rendered workflow outputs
  reason_contains: "duplicate of #41"
  steps:                       # a SUBSET of step path -> expectation
    bail: succeeded            # shorthand for {status: succeeded}
    build: skipped
    review:
      status: succeeded
      skip_reason: "when: false"
      output_regex: "VERDICT: (ok|fix)"
      output_json: { verdict: ok }   # for a step with an output_schema
```

Every key is optional except `workflow:` (and that one only in a `checks:` list). Everything under
`expect:` that you leave out is not checked, so a case says exactly as much as you mean. An
`expect:` block that can never be evaluated — next to `run: false` or `validate: error` — is
refused at load time rather than silently ignored, because a case whose assertions are switched
off would report `ok` whatever it claims.

A case file is **committed to git**: never put a real secret in `inputs:` or `env:`. A dry run
never sends a value anywhere, so a throwaway value is enough even for a `secret: true` input.

A suite file wraps the same mappings in a list:

```yaml
checks:
  - id: happy                  # optional (default: <workflow>-<n>)
    workflow: fix_issue
    inputs: { issue: 42 }
    stubs: stubs.yaml
    expect: { status: succeeded, outputs: { verdict: fix } }
```

Unknown keys are refused the way the workflow loader refuses them — with the `file:line` of the
offending key and a did-you-mean — and every problem of a file is reported together:

```
error: .rayspec/tests/fix_issue/duplicate.yaml:8: unknown field 'statuss' for expect; did you mean 'status'?
```

### Step paths

`expect.steps` keys are **record** paths, the ones the engine writes into `run.json` — loop and
`each` bodies are indexed (`build[2]/review`), include bodies are nested (`block/step`). A path
that never finished is a failure that lists the paths that did, so a typo is obvious.

### Stubs

`stubs:` points at an ordinary [stub script](examples.md) — the same file
`rayspec run --dry-run --stubs` takes, and `rayspec run --stubs-init` scaffolds. Without it the
stub provider answers with its default `[stub] <prompt excerpt>`, which is enough for a case that
only asserts the shape of the graph.

A stub script may sit right next to the case file: discovery globs `.rayspec/tests/<workflow>/`
for cases but skips a document that is recognisably a stub script (its top-level keys are
`steps:` / `match:` / `defaults:` and nothing else). A `stubs/` subdirectory is never globbed at
all, so that is the safe place for a script whose shape is unusual. Anything else in that
directory — an empty file, a typo where a case key was meant — is read as a case, so a mistake is
reported with its `file:line` instead of quietly disappearing from the suite.

## Real shell steps

A dry run simulates `shell:`/`python:` steps; `rayspec test --exec-shell` runs them for real, in
place, in the project root. **Only the flag can do that.** `exec_shell: true` in a case file is a
declaration that the case needs it — without the flag the command refuses to run (exit 2, naming
the case's `file:line`) instead of executing it. That is deliberate: `rayspec test` is the command
a reviewer or a CI job points at a checkout they have not read, and a checked-in YAML file must
not be able to turn it into arbitrary code execution. `--exec-shell` also takes no workdir lock,
so do not run it beside a real `rayspec run` on the same checkout.

## Reading a failure

Failures are printed in the same four-line shape the rest of rayspec uses — the claim, the
context, the fix, the location:

```
expect.steps.build.status: step 'build' is 'failed', expected 'succeeded'
  status failed · attempts 3 · error: stub failure
  fix: update the expectation, or fix the workflow/stubs (rayspec logs 20260821-080147-sccz --step build)
  at .rayspec/tests/fix_issue/duplicate.yaml:14
```

The first line names the expectation by its path in the case file, so you know which line to edit;
the last names the file and line. Because a failing case keeps its run directory, the run id in
the fix line works: `rayspec show <id>` for the tree, `rayspec logs <id> --step <path>` for the
transcript. Passing cases delete their run, so a suite does not bury the project's real runs in
`rayspec runs`.

## In CI

```yaml
- run: rayspec test --junit test-results.xml
```

Exit `1` means a case failed, `2` means the suite could not be run at all (a malformed case file,
a filter matching nothing, a case that needs `--exec-shell`) — never confuse the two: `2` is your
fault, `1` is the workflow's. The JUnit file is written in both cases; a usage error becomes one
erroring `<testcase>` so the CI UI shows why nothing ran. `--json` gives the same information as
one object for a script.

Nothing in a case reaches the network, a real agent or a subprocess, and no credential is loaded
into the process, so a suite is safe to run on every pull request and costs nothing.

## From pytest

`rayspec.testing` ships in the wheel; the pieces `rayspec test` uses are importable:

```python
from rayspec.testing import discover_suites, run_case

@pytest.mark.parametrize(("suite", "case"), CASES, ids=...)
def test_case(suite, case, tmp_path):
    result = run_case(suite, case, home=tmp_path)
    assert result.ok, result.report()
```

`discover_suites(project_root)` returns the suites, `run_case(...)` a `CaseResult` with `.ok`,
`.failures` (each a `Failure` with `field`, `summary`, `detail`, `fix`, `location`) and
`.report()`. `run_case` never raises: a workflow that fails to load, a missing stubs file, a
broken template and even a bug in the harness itself all become failures with a location. It
takes `exec_shell=` from *you*, never from the case file, and it patches `os.environ` for the
duration of a case, so it is not thread-safe — drive cases sequentially or in separate processes.

## How rayspec tests itself

Three nets guard the shapes this page depends on, and they are worth copying:

- **`rayspec test`** over the examples and the repo's own workflows (`scripts/check_examples.py`
  runs the same cases and additionally verifies the coverage matrix of `examples/README.md`).
- **A golden run corpus** (`tests/golden/`): the `--json` event stream, `run.json` and the summary
  object of every case, captured from `rayspec run --dry-run --stubs` and masked (run ids,
  timestamps, durations, absolute paths, host, pids). A test replays each and diffs, so an
  accidental change to the JSONL or `run.json` shape — invisible to unit tests, breaking to
  scripts and sinks — turns into a red test. `RAYSPEC_UPDATE_GOLDEN=1 uv run pytest tests/golden`
  regenerates the corpus; the diff in the pull request is the record of what changed.
- **A fault-injecting run store** (`tests/engine/_faulty_store.py`): a `RunStore` wrapper that
  raises at the n-th `save` / `write_output` / `append_event` / `append_stream` — before it, after
  it, or (for the JSONL writers) halfway through the line, which is the only way to exercise the
  store's promise that readers tolerate a torn trailing line. A parametrised test crashes a run at
  every persistence point and asserts that a resume converges on the same final state as an
  uninterrupted run — the write-ahead order and the reuse predicate are promises only if they hold
  at every interleaving.
