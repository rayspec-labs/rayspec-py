<!-- Thanks for the patch. Keep it short — these sections are what the review looks for. -->

## What changed

<!-- One paragraph, or one bullet per part if the change has parts. -->

## Why

<!-- The problem, not the diff. Link the issue if there is one. -->

## Contract changes

<!--
Anything in CONTRACTS.md, the frozen modules (`schema/`, `providers/base.py`, `engine/paths.py`,
`store/{model,base}.py`, `events/{model,base}.py`), a new record or event field, a changed CLI flag,
exit code or `--json` shape. Frozen modules take additive changes only, and CONTRACTS.md is updated
in this same pull request. Write "none" if nothing moved.
-->

none

## Changelog

<!--
The lines this change deserves in CHANGELOG.md, in Keep a Changelog style. Leave them here rather
than editing the file — the maintainer folds them in at release time so parallel branches do not
collide.
-->

-

## Test plan

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q -m 'not live'`
- [ ] New or changed behaviour has a test that fails without this patch
- [ ] `uv run python scripts/check_examples.py --matrix --verbose` (touched `docs/`, `examples/` or `.rayspec/`)
- [ ] `uv run python scripts/gen_skill.py --check`, `gen_schemas.py --check`, `gen_capability_matrix.py --check` (as applicable)
- [ ] Docs updated in this pull request (user-facing change)
- [ ] Every commit is signed off (`git commit -s`)

## Follow-ups

<!-- Anything deliberately left for later, or "none". -->

none
