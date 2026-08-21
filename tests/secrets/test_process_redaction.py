"""The executor's own writer — ``steps/<path>/stdout.log`` / ``stderr.log``.

The log files are written by the shared subprocess runner, not by the store, so they are the
one persisted surface the store's redactor cannot reach; the pump applies the same
:class:`~rayspec.redact.StreamRedactor` (chunk boundaries included) before anything is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from rayspec.engine.executors._process import _pump
from rayspec.events.model import StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor

SECRET = "ghp_SECRETTOKEN_ABCDEF"


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _Store:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor


class _Ctx:
    def __init__(self, redactor: Redactor) -> None:
        self.store = _Store(redactor)
        self.emitted: list[StreamRecord] = []

    async def emit_stream(self, path: str, record: StreamRecord) -> None:
        self.emitted.append(record)


async def _pumped(tmp_path: Path, chunks: list[bytes], redactor: Redactor) -> tuple[str, str, Any]:
    ctx = _Ctx(redactor)
    log = tmp_path / "stdout.log"
    collected: list[str] = []
    # _pump only uses ctx.store.redactor and ctx.emit_stream — a fake keeps the test unit-sized
    await _pump(_Stream(list(chunks)), "stdout", log, collected, cast(Any, ctx), "s", 1)
    return log.read_text(), "".join(collected), ctx


@pytest.mark.anyio
async def test_the_log_file_is_redacted(tmp_path: Path) -> None:
    text, collected, ctx = await _pumped(
        tmp_path, [f"line {SECRET}\n".encode()], Redactor.build({"token": SECRET})
    )
    assert SECRET not in text and "[REDACTED:token]" in text
    assert SECRET not in collected
    assert all(SECRET not in (r.text or "") for r in ctx.emitted)


@pytest.mark.anyio
async def test_a_secret_split_across_two_process_chunks_is_redacted(tmp_path: Path) -> None:
    chunks = [f"pre {SECRET[:6]}".encode(), f"{SECRET[6:]} post\n".encode()]
    text, collected, _c = await _pumped(tmp_path, chunks, Redactor.build({"token": SECRET}))
    assert SECRET not in text and "[REDACTED:token]" in text
    assert collected == "pre [REDACTED:token] post\n"


@pytest.mark.anyio
async def test_without_secrets_the_output_is_byte_for_byte(tmp_path: Path) -> None:
    text, collected, _ctx = await _pumped(tmp_path, [b"a", b"bc\nd"], NULL_REDACTOR)
    assert text == "abc\nd"
    assert collected == "abc\nd"
