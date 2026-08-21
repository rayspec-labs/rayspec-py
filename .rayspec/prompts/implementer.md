You are implementing a change in **rayspec** (the repo at {{ run.workdir }}, project
{{ project.slug }}). rayspec is a Python CLI for declarative agent workflows on the Claude Agent SDK
and the OpenAI Codex SDK. Work with `uv` only (`uv run …`).

Non-negotiables:
- Read `CONTRACTS.md` (especially "Pinned semantics") and `docs/constitution.md` before touching
  code. Frozen contract modules (`src/rayspec/schema`, `providers/base.py`, `engine/paths.py`,
  `store/model.py`, `store/base.py`, `events/model.py`, `events/base.py`) accept ADDITIVE changes
  only; list any such change in your final answer and mirror it in `CONTRACTS.md`.
- TDD: write a failing test under `tests/<scope>/`, watch it fail, implement the minimum, refactor.
- Concurrency through the anyio API only (ruff bans the raw asyncio APIs).
- No new third-party dependencies. No network in tests. No real SDK calls.
- The gate must be green before every commit:
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
  (fix formatting with `uv run ruff format .`).
- Docstrings on every public function/class; a short module docstring stating the boundary.
- Small logical commits. Never push to main, never force-push.

Finish by summarising: files touched, tests added, contract changes (or "none"), open questions.
