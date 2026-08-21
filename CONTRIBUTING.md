# Contributing to rayspec

Thanks for being here. rayspec is a small project with one maintainer, so the most useful thing
this page can do is tell you where the lines are *before* you write the patch — especially the one
around the YAML schema, which is deliberately hard to move.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Found something
exploitable? Do not open an issue — [SECURITY.md](SECURITY.md) has the private channel.

## Before you write code

**Just send the pull request** for: a bug fix, a failing test that proves one, a documentation fix,
a new example, a clearer error message.

**Open an issue first** for: a new field in the workflow schema, a new CLI command or flag, a new
Jinja filter, a new event or record field, or anything that adds a runtime dependency.

That second list exists because rayspec's YAML is a *coordination* language, not a programming
language, and [docs/constitution.md](docs/constitution.md) is the admissibility test every schema
request is measured against. A field gets in only if all three hold: the **engine** needs to see
the value to schedule, gate, isolate, retry or account for a step; it is **data**, not a new
expression surface; and there is **no existing escape hatch** — a `shell:`/`python:` step, an
agent field, `provider_options:` or `defaults:`. Most requests are answered with "put it on the
agent", "put it in `provider_options:`" or "compute it in a step", and the case-law table at the
bottom of that page lists the ones already settled. This is a hard line, and a maintainer who only
mentions it after you have written 400 lines has wasted your evening — so please read it first, and
argue with it in an issue if you disagree. The bar for a *bug fix* is much lower: correctness with a
test wins.

The dependency list is short on purpose (`pyproject.toml`). "It's only one small package" is how
that stops being true.

## Setting up

```bash
git clone https://github.com/rayspec-labs/rayspec-py
cd rayspec-py
uv sync --all-groups
uv run rayspec --help
```

You need Python ≥ 3.11 (CI runs 3.11 through 3.14), [uv](https://docs.astral.sh/uv/) and `git`.
Nothing else — dry runs and the whole test suite work without provider credentials.

## The gate

Run this before every commit — the same four checks CI runs, which it runs on 3.11, 3.12, 3.13
and 3.14 rather than on your one interpreter:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -m 'not live'
```

`uv run ruff format .` fixes formatting complaints. CI does one more thing on **every pull
request**, whatever you touched: it validates every example and the coverage matrix. Run that one
too if you went anywhere near `docs/`, `examples/` or `.rayspec/`, together with whichever of the
other three `--check` runs matches your change:

```bash
uv run python scripts/check_examples.py --matrix --verbose   # CI runs this on every pull request
uv run python scripts/gen_skill.py --check                   # docs/ pages the packaged skill mirrors
uv run python scripts/gen_schemas.py --check                 # src/rayspec/schema/ (JSON schemas)
uv run python scripts/gen_capability_matrix.py --check       # provider capabilities (docs/providers.md)
```

Each generator writes the artefact when you drop `--check`; commit what it produced.

## Tests

Write the failing test first, run it, watch it fail, then write the smallest thing that makes it
pass. A pull request without a test that would have caught the bug will be asked for one.

- Tests mirror the package: `tests/<area>/test_<module>.py`.
- Async tests are marked `pytest.mark.anyio` (the `anyio_backend` fixture lives in
  `tests/conftest.py`). Concurrency is **anyio only** — ruff bans the raw `asyncio` cancellation and
  task APIs, because both SDKs' shielded cleanup is only safe under anyio-originated cancellation.
- **No network, no real agent calls.** `rayspec.providers.stub` is the test double; the adapter
  tests fake SDK objects. A test that needs credentials to pass is a live test (below).
- There are two different `home` fixtures and they are not interchangeable: the shared one in
  `tests/conftest.py` exports `RAYSPEC_HOME`, while `tests/integration/conftest.py` deliberately
  defines one that does not, because those tests exercise how the CLI itself resolves the store.
  Read both docstrings before adding a fixture; do not consolidate them.
- `tests/docs/` pins documentation claims that drift (the CLI reference, the capability matrix,
  links, the files this page is part of). If one fails, the doc changed and the claim did not.

Live tests hit a real provider. They are skipped unless `RAYSPEC_LIVE=1` is set — a bare `pytest`
still collects them — and the gate's `-m 'not live'` deselects them outright:

```bash
RAYSPEC_LIVE=1 uv run pytest -m live        # needs a logged-in `claude` and/or `codex`
```

They cost money and tokens, CI never runs them, and they are the right tool for exactly one thing:
proving a claim about how an SDK actually behaves.

## Examples

Every directory under `examples/` is a self-contained project with a `checks.yaml` that says what
validating, planning and dry-running it must produce:

```bash
uv run python scripts/check_examples.py --only hello_review --verbose
cd examples/hello_review && uv run rayspec run hello_review -i target=src/ --dry-run --stubs stubs.yaml
```

Examples only use features that exist on `main`, and the coverage matrix in `examples/README.md` is
parsed by the tests — a new capability needs a row that names an example which really shows it.

## Contracts, and what "frozen" means

[CONTRACTS.md](CONTRACTS.md) is the working agreement between the modules: layout, dependency
direction, public surfaces and the semantics that were settled early and are not re-litigated. If
your change moves a contract, change `CONTRACTS.md` in the same pull request and say so in the
description.

These modules are **frozen**: `src/rayspec/schema/`, `providers/base.py`, `engine/paths.py`,
`store/model.py`, `store/base.py`, `events/model.py`, `events/base.py`. Frozen means *additive
changes only* — a new optional field, a new helper — never a rename or a change of meaning without
updating every consumer in the same pull request. Run records are read by older versions of the
tool than the one that wrote them.

Three consequences that save review time:

- **Adding a command** means adding `src/rayspec/cli/commands/<name>.py` with a `register(app)`
  function. `cli/app.py` discovers it; you never edit `app.py`.
- **Writing into a run directory** goes through `FileRunStore`, never through `open()`. The store
  wraps every writer in the redactor, so a writer that goes around it can leak a secret into
  `run.json` or a log.
- **Extending rayspec from your own package** — a provider, a sink, embedding the runner — is
  documented in [docs/extending.md](docs/extending.md). If a seam you need is missing, that is a
  good issue to open; forking is the outcome nobody wants.

## Documentation

A user-facing change updates `docs/` in the same pull request. The packaged Claude Code skill is
generated from those pages (`scripts/gen_skill.py`), so a doc edit can make the skill stale — the
gate's `--check` run tells you. Keep links relative; `tests/docs/test_links.py` resolves every one
of them, anchors included.

## Commits and pull requests

- **Conventional Commits**, imperative mood, one logical change per commit:
  `fix(engine): keep a tolerated failure out of the run status`.
- **Sign off every commit**: `git commit -s`. rayspec is Apache-2.0 and takes contributions under
  the [Developer Certificate of Origin](https://developercertificate.org/); the `Signed-off-by:`
  trailer that flag adds is how you certify you have the right to submit the work. Nothing in CI
  checks for it, so I will ask you to sign off before I merge — `git rebase --signoff` fixes a
  branch you already wrote.
- Fill in the pull-request template: what changed, why, contract changes, the changelog lines your
  change deserves, and the test plan you actually ran.
- `CHANGELOG.md` is folded in by the maintainer at release time — put your lines in the pull-request
  description rather than editing the file, so parallel branches do not fight over it.
- Reviews are direct and mostly about scope and contracts. It is about the change, never about you.

## Reporting things instead of fixing them

A good bug report is worth as much as a patch. The issue form asks for the output of
`rayspec doctor` and a run id (`rayspec runs` lists them) — those two turn most reports into a
five-minute diagnosis. `rayspec show <run>` and `rayspec logs <run>` are the rest of the story, and
neither of them prints a `secret: true` value.
