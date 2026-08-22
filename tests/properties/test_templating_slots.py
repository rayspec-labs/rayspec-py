# SPDX-License-Identifier: Apache-2.0
"""Templating round-trip properties: a ``{{ }}`` becomes a slot and the value arrives verbatim.

This is the project's most-documented promise (``docs/templating.md``) and, before this suite,
its least generated one: the examples cover the values somebody thought of. Here the values are
generated — quotes, newlines, backslashes, unicode (precomposed and decomposed, astral,
zero-width), ANSI escapes, and the substitution rayspec itself performs, so a value that *is*
``${RAYSPEC_V1}`` gets tried on every run.

The promise has three halves, and each is a property below:

1. **structure** — every ``{{ }}`` renders to exactly one ``${RAYSPEC_V<n>}``, numbered from 1
   in source order, and the value is nowhere in the script;
2. **verbatim** — the bytes bash sees are the bytes the value had;
3. **inertness** — nothing in a value is ever re-expanded by the shell or re-rendered by Jinja.

Two of these do NOT hold above the 64 KiB spill threshold. Those cases are marked
``xfail(strict=True)`` with the promise stated exactly as the documentation makes it, and are
paired with a test that pins what happens today — never by narrowing the property until the
counter-example disappears.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from rayspec.templating import TemplateEngine, TemplateRenderError

from .generate import (
    Discard,
    forall,
    json_value,
    raises,
    shrink_json,
    shrink_seq,
    shrink_text,
    text,
)

#: The bash every shell property runs, resolved ONCE. ``subprocess`` resolves argv[0] through
#: ``os.get_exec_path(env)`` — the env the call passes, not the real ``PATH`` — so a skip marker
#: keyed off ``shutil.which`` and a call keyed off ``"bash"`` can disagree: on a host whose bash
#: is outside the minimal ``PATH`` below, the marker would not fire and the properties would die
#: with ``FileNotFoundError`` instead of skipping.
BASH: str | None = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash required")

#: Values that are the whole point of the property: they look like the machinery itself, or like
#: an injection. Drawn deliberately so they appear in every run, not only when the RNG is kind.
LANDMINES: tuple[str, ...] = (
    "${RAYSPEC_V1}",
    "${RAYSPEC_V2} ${RAYSPEC_V1}",
    "$(cat /etc/passwd)",
    "'; id ;'",
    '"; id ;"',
    "`id`",
    "\\",
    "\\\\",
    "\n",
    "a\nb\n",
    "  padded  ",
    "{{ inputs.v0 }}",
    "{% raw %}{% endraw %}",
    "-n",
    "--",
    "",
)


def shell_value(rng: random.Random) -> str:
    """A generated string, or (one time in four) one of the :data:`LANDMINES`."""
    return rng.choice(LANDMINES) if rng.random() < 0.25 else text(rng)


def shell_values(rng: random.Random) -> list[str]:
    """One to four values — several slots per script is where the numbering can go wrong."""
    return [shell_value(rng) for _ in range(rng.randint(1, 4))]


def ctx_of(values: Sequence[str]) -> dict[str, Any]:
    """A template context binding ``inputs.v0``, ``inputs.v1`` … to ``values``."""
    return {"inputs": {f"v{i}": value for i, value in enumerate(values)}}


def body_of(values: Sequence[str], *, quote: str = '"') -> str:
    """A script that writes each value to ``$OUT/<i>``, interpolated inside ``quote``."""
    return "\n".join(
        f'printf %s {quote}{{{{ inputs.v{i} }}}}{quote} > "$OUT/{i}"' for i in range(len(values))
    )


def run_bash(script: str, env: dict[str, str], out: Path, count: int) -> list[bytes]:
    """Run ``script``; return ``$OUT/0`` … ``$OUT/<count-1>`` as bytes.

    Only the slot env plus ``PATH``/``OUT`` reaches the script. ``count`` is what the CALLER
    expects, not what the script happened to write: inferring it from the directory listing
    turns "output 3 was never written" into a length mismatch, and hands any caller that reuses
    a directory a silently wrong answer.

    Bytes, never text: ``text=True`` would translate ``\\r`` on the way back and hide exactly
    the kind of mangling these properties exist to catch.
    """
    assert BASH is not None, "guarded by needs_bash"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [BASH, "-euo", "pipefail", "-c", script],
        env={**env, "PATH": "/usr/bin:/bin", "OUT": str(out)},
        check=True,
        capture_output=True,
    )
    written = sorted(entry.name for entry in out.iterdir())
    assert written == [str(i) for i in range(count)], (
        f"the script wrote {written}, expected {count} output file(s)"
    )
    return [(out / str(i)).read_bytes() for i in range(count)]


@pytest.fixture
def engine() -> TemplateEngine:
    """The default engine — real 64 KiB spill threshold; the spill suites build their own."""
    return TemplateEngine()


# --------------------------------------------------------------------------------------------------
# 1. structure
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """One generated piece of a shell body: the template text and what it must render to.

    ``parts`` is the rendered text split at the slots, so ``len(parts) == len(values) + 1`` and
    the chunk renders to ``parts[0] + slot + parts[1] + …``. Split rather than formatted: the
    rendered script is full of ``${…}`` and a format string would need every brace escaped.
    """

    kind: str
    source: str
    parts: tuple[str, ...]
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        assert len(self.parts) == len(self.values) + 1, self


@dataclass(frozen=True)
class BodyCase:
    """A whole generated body: its chunks, the values bound to ``inputs``, and the expectation.

    The expectation is derived from the number of EVALUATIONS, never from the number of source
    occurrences — that difference is the entire content of a ``{% for %}`` and a
    ``{% if false %}``, and it is what the flat one-expression-per-line body never asked about.
    """

    chunks: tuple[Chunk, ...]
    values: tuple[str, ...]
    many: tuple[str, ...]

    @property
    def source(self) -> str:
        """The body as written."""
        return "".join(chunk.source for chunk in self.chunks)

    @property
    def context(self) -> dict[str, Any]:
        """``inputs.v0`` … plus ``inputs.many``, the list the ``{% for %}`` chunks iterate."""
        context = ctx_of(self.values)
        context["inputs"]["many"] = list(self.many)
        return context

    @property
    def slot_values(self) -> tuple[str, ...]:
        """The value of ``RAYSPEC_V1``, ``V2`` … in slot order."""
        return tuple(value for chunk in self.chunks for value in chunk.values)

    @property
    def expected(self) -> str:
        """The script the body must render to, with the slots numbered from 1."""
        out: list[str] = []
        slot = 0
        for chunk in self.chunks:
            out.append(chunk.parts[0])
            for index in range(len(chunk.values)):
                slot += 1
                out.append(f"${{RAYSPEC_V{slot}}}")
                out.append(chunk.parts[index + 1])
        return "".join(out)

    def __str__(self) -> str:
        return repr(self.source)


def _one_expression(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """``printf %s "{{ inputs.vN }}"`` in a random quoting context — one slot."""
    index = rng.randrange(len(values))
    quote = rng.choice(['"', "'", ""])
    return Chunk(
        kind="one",
        source=f"printf %s {quote}{{{{ inputs.v{index} }}}}{quote}\n",
        parts=(f"printf %s {quote}", f"{quote}\n"),
        values=(values[index],),
    )


def _repeated_expression(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """The same expression twice: two evaluations, so two slots — never one reused."""
    index = rng.randrange(len(values))
    expression = f"{{{{ inputs.v{index} }}}}"
    return Chunk(
        kind="repeated",
        source=f'printf %s "{expression}{expression}"\n',
        parts=('printf %s "', "", '"\n'),
        values=(values[index], values[index]),
    )


def _heredoc(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """A quoted or unquoted heredoc — the other context the documentation talks about."""
    index = rng.randrange(len(values))
    quote = rng.choice(["'", ""])
    return Chunk(
        kind="heredoc",
        source=f"cat <<{quote}EOF{quote}\n{{{{ inputs.v{index} }}}}\nEOF\n",
        parts=(f"cat <<{quote}EOF{quote}\n", "\nEOF\n"),
        values=(values[index],),
    )


def _loop(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """``{% for %}`` over ``inputs.many``: one slot per ITERATION, none for an empty list."""
    count = len(many)
    inner = ('printf %s "', *['"\nprintf %s "'] * max(0, count - 1), '"\n')
    return Chunk(
        kind=f"loop{min(count, 2)}",
        source='{% for item in inputs.many %}\nprintf %s "{{ item }}"\n{% endfor %}\n',
        parts=inner if count else ("",),
        values=tuple(many),
    )


def _conditional(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """``{% if %}``: the untaken branch consumes no slot number and leaves no env entry."""
    index = rng.randrange(len(values))
    taken = rng.random() < 0.5
    source = (
        f"{{% if {'true' if taken else 'false'} %}}\n"
        f'printf %s "{{{{ inputs.v{index} }}}}"\n'
        "{% endif %}\n"
    )
    if taken:
        return Chunk(
            kind="if_true", source=source, parts=('printf %s "', '"\n'), values=(values[index],)
        )
    return Chunk(kind="if_false", source=source, parts=("",), values=())


def _literal(rng: random.Random, values: Sequence[str], many: Sequence[str]) -> Chunk:
    """A line with no expression at all: it must not disturb the numbering around it."""
    return Chunk(kind="literal", source="echo plain\n", parts=("echo plain\n",), values=())


#: The chunk builders a generated body is assembled from.
CHUNKS = (
    _one_expression,
    _one_expression,
    _repeated_expression,
    _heredoc,
    _loop,
    _conditional,
    _literal,
)


def body_case(rng: random.Random) -> BodyCase:
    """A body of one to five chunks over one to four values and a list of zero to three."""
    values = shell_values(rng)
    many = tuple(shell_value(rng) for _ in range(rng.randint(0, 3)))
    chunks = tuple(rng.choice(CHUNKS)(rng, values, many) for _ in range(rng.randint(1, 5)))
    return BodyCase(chunks=chunks, values=tuple(values), many=many)


def shrink_body(case: BodyCase) -> Iterator[BodyCase]:
    """Fewer chunks, then a shorter list for the ``{% for %}`` chunks to iterate."""
    for smaller in shrink_seq(case.chunks):
        yield replace(case, chunks=tuple(smaller))
    if case.many:
        yield replace(case, many=(), chunks=tuple(_relist(c, ()) for c in case.chunks))


def _relist(chunk: Chunk, many: tuple[str, ...]) -> Chunk:
    """Rebuild a ``{% for %}`` chunk for a different list; other chunks are returned unchanged."""
    return _loop(random.Random(0), (), many) if "{% for" in chunk.source else chunk


def test_every_evaluated_expression_becomes_one_numbered_slot(
    engine: TemplateEngine, tmp_path: Path
) -> None:
    """``{{ }}`` → ``${RAYSPEC_V<n>}``, numbered from 1 per EVALUATION, value only in the env.

    Asserting the *whole* script (not a substring) is what makes this a non-splicing property:
    a value that leaked into the script would change the text no matter what it contained. The
    body is generated too, not only the values — repeated expressions, ``{% for %}`` over zero
    to three items, a taken and an untaken ``{% if %}``, quoted, unquoted and heredoc contexts —
    because "exactly one slot per ``{{ }}``" is a claim about bodies, and a flat body of
    distinct expressions is the one shape where source order and evaluation order agree.
    """

    seen: Counter[str] = Counter()

    def prop(case: BodyCase) -> None:
        rendered = engine.render_shell(case.source, case.context, spill_dir=tmp_path)
        assert rendered.script == case.expected
        expected_env = {f"RAYSPEC_V{i + 1}": value for i, value in enumerate(case.slot_values)}
        assert rendered.env == expected_env
        assert rendered.spills == []
        seen.update(chunk.kind for chunk in case.chunks)

    forall("shell-body-structure", body_case, prop, shrink=shrink_body, show=str)
    for kind in ("one", "repeated", "heredoc", "loop0", "loop2", "if_true", "if_false", "literal"):
        assert seen[kind] > 0, f"the generator drew no {kind} chunk: {seen}"


def test_a_slot_is_never_both_an_env_value_and_a_spill(tmp_path: Path) -> None:
    """Each expression consumes exactly one index: ``len(env) + len(spills) == n``.

    Generated at a tiny threshold so both branches are taken in one script — the numbering is
    shared between them and an off-by-one there would silently hand a step the wrong value.
    """
    engine = TemplateEngine(spill_threshold=8)

    def prop(values: list[str]) -> None:
        rendered = engine.render_shell(body_of(values), ctx_of(values), spill_dir=tmp_path)
        assert len(rendered.env) + len(rendered.spills) == len(values)
        indices = sorted(int(name.removeprefix("RAYSPEC_V")) for name in rendered.env)
        assert all(1 <= n <= len(values) for n in indices)
        assert len(set(indices)) == len(indices)

    forall("shell-slot-indices", shell_values, prop, shrink=shrink_seq)


# --------------------------------------------------------------------------------------------------
# 2. verbatim
# --------------------------------------------------------------------------------------------------


@needs_bash
def test_shell_values_arrive_verbatim(engine: TemplateEngine, tmp_path: Path) -> None:
    """The bytes bash writes are the bytes the value had — quotes, newlines, unicode included."""
    counter = iter(range(10_000))

    def prop(values: list[str]) -> None:
        rendered = engine.render_shell(body_of(values), ctx_of(values), spill_dir=tmp_path)
        out = tmp_path / f"out{next(counter)}"
        got = run_bash(rendered.script, rendered.env, out, len(values))
        assert got == [v.encode("utf-8") for v in values]

    forall("shell-verbatim", shell_values, prop, cases=40, shrink=shrink_seq)


@needs_bash
def test_a_value_that_looks_like_a_slot_is_not_expanded_again(
    engine: TemplateEngine, tmp_path: Path
) -> None:
    """``RAYSPEC_V1='${RAYSPEC_V2}'`` prints ``${RAYSPEC_V2}``: bash never re-expands a value.

    The one substitution rayspec performs must not compose with itself — otherwise a step's
    output that mentions a slot would be silently replaced by another step's value.
    """
    counter = iter(range(10_000))

    def prop(values: list[str]) -> None:
        decoys = [f"${{RAYSPEC_V{i + 1}}}" for i in range(len(values))]
        both = [*decoys, *values]
        rendered = engine.render_shell(body_of(both), ctx_of(both), spill_dir=tmp_path)
        got = run_bash(rendered.script, rendered.env, tmp_path / f"decoy{next(counter)}", len(both))
        assert got == [v.encode("utf-8") for v in both]

    forall("shell-slot-decoy", shell_values, prop, cases=25, shrink=shrink_seq)


def test_a_value_is_never_rendered_as_a_template(engine: TemplateEngine, tmp_path: Path) -> None:
    """A value containing ``{{ ... }}`` is data: the engine renders the body, never the value."""

    def prop(value: str) -> None:
        payload = "{{ inputs.v0 }}" + value + "{% raw %}"
        rendered = engine.render_shell(
            'printf %s "{{ inputs.v0 }}"', {"inputs": {"v0": payload}}, spill_dir=tmp_path
        )
        assert rendered.script == 'printf %s "${RAYSPEC_V1}"'
        assert rendered.env == {"RAYSPEC_V1": payload}

    forall("shell-template-in-value", text, prop, shrink=shrink_text)


# --------------------------------------------------------------------------------------------------
# 3. inertness
# --------------------------------------------------------------------------------------------------


@needs_bash
def test_a_single_quoted_slot_stays_literal(engine: TemplateEngine, tmp_path: Path) -> None:
    """``echo '{{ x }}'`` prints ``${RAYSPEC_V1}`` — the documented single-quote consequence."""
    counter = iter(range(10_000))

    def prop(values: list[str]) -> None:
        rendered = engine.render_shell(
            body_of(values, quote="'"), ctx_of(values), spill_dir=tmp_path
        )
        got = run_bash(rendered.script, rendered.env, tmp_path / f"sq{next(counter)}", len(values))
        assert got == [f"${{RAYSPEC_V{i + 1}}}".encode() for i in range(len(values))]

    forall("shell-single-quote", shell_values, prop, cases=25, shrink=shrink_seq)


@needs_bash
def test_a_value_never_runs_a_command(engine: TemplateEngine, tmp_path: Path) -> None:
    """Command substitution, backticks and ``;`` inside a value stay inert under ``set -e``.

    The canary file must not exist afterwards: if any generated value were spliced into the
    script rather than passed through the environment, one of these would create it.
    """
    counter = iter(range(10_000))
    canary = tmp_path / "pwned"

    def prop(values: list[str]) -> None:
        payloads = [f"$(touch {canary}){v}`touch {canary}`" for v in values]
        rendered = engine.render_shell(body_of(payloads), ctx_of(payloads), spill_dir=tmp_path)
        got = run_bash(
            rendered.script, rendered.env, tmp_path / f"inert{next(counter)}", len(payloads)
        )
        assert got == [p.encode("utf-8") for p in payloads]
        assert not canary.exists(), "a value was spliced into the script"

    forall("shell-inert", shell_values, prop, cases=25, shrink=shrink_seq)


# --------------------------------------------------------------------------------------------------
# python bodies
# --------------------------------------------------------------------------------------------------


def test_python_literals_round_trip(engine: TemplateEngine, tmp_path: Path) -> None:
    """``v = {{ x }}`` binds a value equal to ``x`` for every JSON-like ``x``."""

    def prop(value: Any) -> None:
        rendered = engine.render_python("v = {{ inputs.v0 }}", ctx_of([value]), spill_dir=tmp_path)
        namespace: dict[str, Any] = {}
        exec(rendered.script, namespace)
        assert namespace["v"] == value
        assert rendered.env == {}

    forall("python-literal", json_value, prop, shrink=shrink_json)


def test_python_literals_are_inert(engine: TemplateEngine, tmp_path: Path) -> None:
    """A string that closes the literal and appends code is still just a string."""

    def prop(value: str) -> None:
        payload = f"'); import os; os.system('touch {tmp_path / 'pwned'}'); ('{value}"
        rendered = engine.render_python(
            "v = {{ inputs.v0 }}", ctx_of([payload]), spill_dir=tmp_path
        )
        namespace: dict[str, Any] = {}
        exec(rendered.script, namespace)
        assert namespace["v"] == payload
        assert not (tmp_path / "pwned").exists()

    forall("python-inert", text, prop, shrink=shrink_text)


def test_a_spilled_python_literal_round_trips(tmp_path: Path) -> None:
    """Above the threshold the literal becomes a ``json.loads`` call — same value, exactly."""
    engine = TemplateEngine(spill_threshold=8)

    def prop(value: Any) -> None:
        rendered = engine.render_python("v = {{ inputs.v0 }}", ctx_of([value]), spill_dir=tmp_path)
        namespace: dict[str, Any] = {}
        exec(rendered.script, namespace)
        assert namespace["v"] == value

    forall("python-spill", json_value, prop, shrink=shrink_json)


@needs_bash
def test_python_bodies_survive_a_real_interpreter(engine: TemplateEngine, tmp_path: Path) -> None:
    """A handful of generated values through a real ``python -c``, compared as bytes."""

    def prop(value: str) -> None:
        rendered = engine.render_python(
            "import sys\nsys.stdout.buffer.write({{ inputs.v0 }}.encode())",
            ctx_of([value]),
            spill_dir=tmp_path,
        )
        proc = subprocess.run(
            [sys.executable, "-c", rendered.script], capture_output=True, check=True
        )
        assert proc.stdout == value.encode("utf-8")

    forall("python-subprocess", text, prop, cases=15, shrink=shrink_text)


# --------------------------------------------------------------------------------------------------
# text fields
# --------------------------------------------------------------------------------------------------


def test_a_text_field_that_is_one_expression_is_the_value(engine: TemplateEngine) -> None:
    """``render_str('{{ x }}')`` is ``x`` for a string — no escaping, no coercion, no trimming."""

    def prop(value: str) -> None:
        assert engine.render_str("{{ inputs.v0 }}", ctx_of([value])) == value

    forall("text-round-trip", text, prop, shrink=shrink_text)


def test_deep_rendering_keeps_values_and_types(engine: TemplateEngine) -> None:
    """``outputs:``/``with:``/``env:`` deep rendering: a lone ``{{ expr }}`` keeps its type."""

    def prop(value: Any) -> None:
        shape = {"a": "{{ inputs.v0 }}", "b": ["{{ inputs.v0 }}", 3], "c": {"d": "x"}}
        rendered = engine.render_value(shape, ctx_of([value]))
        assert rendered == {"a": value, "b": [value, 3], "c": {"d": "x"}}

    forall("deep-render", json_value, prop, shrink=shrink_json)


def test_a_non_json_value_is_refused_rather_than_repred(
    engine: TemplateEngine, tmp_path: Path
) -> None:
    """``python:`` never emits something that is not a literal — the failure is loud."""

    def prop(value: Any) -> None:
        with raises(TemplateRenderError):
            engine.render_python(
                "v = {{ inputs.v0 }}", {"inputs": {"v0": {"k": value}}}, spill_dir=tmp_path
            )

    forall(
        "python-refuses",
        lambda rng: rng.choice([object(), float("nan"), float("inf"), {1: "int key"}, set()]),
        prop,
        cases=20,
    )


# --------------------------------------------------------------------------------------------------
# FINDINGS: two promises the spill path does not keep
# --------------------------------------------------------------------------------------------------


def spilled_text(rng: random.Random) -> str:
    """A value that always exceeds the tiny threshold used by the spill properties."""
    return text(rng) + "abcdefghij"


@needs_bash
@pytest.mark.xfail(
    strict=True,
    reason=(
        "a spilled value renders as $(cat '<path>'), and command substitution strips trailing "
        "newlines: above the spill threshold the value no longer arrives verbatim. "
        "Delete this marker when the round trip is made verbatim — a strict xfail turns red "
        "as XPASS on the day it starts passing, which is the signal to do so"
    ),
)
def test_a_spilled_shell_value_arrives_verbatim(tmp_path: Path) -> None:
    """The verbatim promise, stated for values over the spill threshold.

    Every generated case ends in a newline, so the property is falsified deterministically —
    this test is the promise, not a flaky search for it.
    """
    engine = TemplateEngine(spill_threshold=8)
    counter = iter(range(10_000))

    def prop(value: str) -> None:
        values = [value]
        rendered = engine.render_shell(body_of(values), ctx_of(values), spill_dir=tmp_path)
        if not rendered.spills:
            raise Discard("the property is about values over the threshold")
        got = run_bash(rendered.script, rendered.env, tmp_path / f"spill{next(counter)}", 1)
        assert got == [values[0].encode("utf-8")]

    forall(
        "shell-spill-verbatim",
        lambda rng: spilled_text(rng) + "\n",
        prop,
        cases=5,
        shrink=shrink_text,
    )


@needs_bash
def test_today_a_spilled_shell_value_loses_its_trailing_newlines(tmp_path: Path) -> None:
    """Pin the defect above, minimally, so a fix is noticed from both directions."""
    engine = TemplateEngine(spill_threshold=8)
    value = "abcdefghij\n"
    rendered = engine.render_shell(body_of([value]), ctx_of([value]), spill_dir=tmp_path)
    assert rendered.spills and rendered.env == {}
    assert rendered.spills[0].read_bytes() == value.encode(), "the file itself is verbatim"
    assert run_bash(rendered.script, rendered.env, tmp_path / "pin", 1) == [b"abcdefghij"]


@needs_bash
@pytest.mark.xfail(
    strict=True,
    reason=(
        "docs/templating.md: \"echo '{{ x }}' prints the literal ${RAYSPEC_V1}\" — above the "
        "spill threshold it prints $(cat <absolute path>) instead, leaking the run's tmp path. "
        "Delete this marker when the behaviour or the documented promise is corrected"
    ),
)
def test_a_single_quoted_spilled_slot_stays_literal(tmp_path: Path) -> None:
    """The single-quote promise, stated for values over the spill threshold."""
    engine = TemplateEngine(spill_threshold=8)
    counter = iter(range(10_000))

    def prop(value: str) -> None:
        values = [value]
        rendered = engine.render_shell(
            body_of(values, quote="'"), ctx_of(values), spill_dir=tmp_path
        )
        if not rendered.spills:
            raise Discard("the property is about values over the threshold")
        got = run_bash(rendered.script, rendered.env, tmp_path / f"sqspill{next(counter)}", 1)
        assert got == [b"${RAYSPEC_V1}"]

    forall("shell-spill-single-quote", spilled_text, prop, cases=5, shrink=shrink_text)


@needs_bash
def test_today_a_single_quoted_spilled_slot_prints_the_spill_path(tmp_path: Path) -> None:
    """Pin the defect above: the script's own scratch path reaches the step's output."""
    engine = TemplateEngine(spill_threshold=8)
    values = ["abcdefghij"]
    rendered = engine.render_shell(body_of(values, quote="'"), ctx_of(values), spill_dir=tmp_path)
    assert rendered.spills
    got = run_bash(rendered.script, rendered.env, tmp_path / "sqpin", 1)
    assert got == [f"$(cat {rendered.spills[0]})".encode()]


def test_the_spill_file_itself_is_always_verbatim(tmp_path: Path) -> None:
    """Whatever the shell then does with it, the file rayspec writes holds the exact bytes.

    Compared as BYTES on purpose. ``read_text`` decodes with universal newlines and would hide
    exactly the mistake this asserts against: the spill file is opened in text mode with the
    default ``newline=None``, so on a platform whose ``os.linesep`` is not ``"\n"`` every
    newline in a spilled value is rewritten on the way out. (The store's own durable writer
    passes ``newline=""`` for this reason.)
    """
    engine = TemplateEngine(spill_threshold=8)

    def prop(value: str) -> None:
        values = [value + "abcdefghij"]
        rendered = engine.render_shell(body_of(values), ctx_of(values), spill_dir=tmp_path)
        assert rendered.spills
        assert rendered.spills[0].read_bytes() == values[0].encode("utf-8")

    forall("shell-spill-file", text, prop, shrink=shrink_text)


def test_a_spilled_python_value_is_json_not_repr(tmp_path: Path) -> None:
    """The spilled python payload is the JSON of the value — no lossy repr on the way out."""
    engine = TemplateEngine(spill_threshold=8)

    def prop(value: Any) -> None:
        rendered = engine.render_python("v = {{ inputs.v0 }}", ctx_of([value]), spill_dir=tmp_path)
        if not rendered.spills:
            return
        assert json.loads(rendered.spills[0].read_bytes().decode("utf-8")) == value

    forall("python-spill-file", json_value, prop, shrink=shrink_json)


@needs_bash
def test_a_quoted_heredoc_keeps_the_slot_literal(engine: TemplateEngine, tmp_path: Path) -> None:
    """``<<'EOF'`` is the other documented non-expanding context; an unquoted one expands.

    Both halves matter: a promise that a slot stays literal is only useful next to the promise
    that the same body, one quote different, delivers the value.
    """
    counter = iter(range(10_000))

    def prop(value: str) -> None:
        quoted = "cat > \"$OUT/0\" <<'EOF'\n{{ inputs.v0 }}\nEOF\n"
        plain = 'cat > "$OUT/0" <<EOF\n{{ inputs.v0 }}\nEOF\n'
        context = ctx_of([value])
        literal = engine.render_shell(quoted, context, spill_dir=tmp_path)
        expanded = engine.render_shell(plain, context, spill_dir=tmp_path)
        got_literal = run_bash(literal.script, literal.env, tmp_path / f"hd{next(counter)}", 1)
        got_expanded = run_bash(expanded.script, expanded.env, tmp_path / f"hd{next(counter)}", 1)
        assert got_literal == [b"${RAYSPEC_V1}\n"]
        assert got_expanded == [value.encode("utf-8") + b"\n"]

    forall(
        "shell-heredoc",
        lambda rng: text(rng).replace("\n", " ").replace("\r", " "),
        prop,
        cases=20,
        shrink=shrink_text,
    )


def test_a_nul_byte_is_carried_into_the_slot_and_fails_at_the_step(
    engine: TemplateEngine, tmp_path: Path
) -> None:
    """A value holding NUL cannot round-trip at all — a process environment has no room for it.

    So the only question is whether it fails in a way an operator can act on, and today it does
    not: the value is accepted into the slot and the step later dies with ``embedded null
    byte``, naming neither the value nor a fix. Pinned here (see the finding) so the message can
    be improved deliberately rather than by accident.
    """
    rendered = engine.render_shell(
        'printf %s "{{ inputs.v0 }}"', ctx_of(["a\x00b"]), spill_dir=tmp_path
    )
    assert rendered.env == {"RAYSPEC_V1": "a\x00b"}
    assert BASH is not None, "guarded by needs_bash"
    with pytest.raises(ValueError, match="null byte"):
        subprocess.run(
            [BASH, "-c", rendered.script],
            env={**rendered.env, "PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
        )


@needs_bash
def test_the_bash_helper_names_an_output_the_script_never_wrote(tmp_path: Path) -> None:
    """A missing output must read as a missing FILE, not as a list one entry too short."""
    with pytest.raises(AssertionError, match=r"wrote \['0'\], expected 2"):
        run_bash('printf %s a > "$OUT/0"', {}, tmp_path / "short", 2)
