# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R8: the middle-truncation primitives — keep head and tail on line boundaries, mark the
cut, and never empty a file even for one record larger than the whole cap."""

from __future__ import annotations

from pathlib import Path

from rayspec.store.file import CapSplit, split_for_cap, truncate_open_file, truncate_path


def marker(split: CapSplit) -> bytes:
    return b'{"kind":"truncated","dropped_bytes":%d}\n' % split.dropped_bytes


def test_head_and_tail_are_whole_lines() -> None:
    data = b"".join(b"line-%03d\n" % i for i in range(200))  # 200 * 9 = 1800 bytes
    split = split_for_cap(data, 600)
    assert split.head.endswith(b"\n") and split.head.startswith(b"line-000\n")
    assert split.tail.endswith(b"\n") and split.tail.startswith(b"line-")
    assert b"line-" in split.tail
    # every kept line is intact (no torn line at either seam)
    for line in (split.head + split.tail).split(b"\n"):
        if line:
            assert line.startswith(b"line-") and len(line) == 8, line
    assert split.dropped_bytes == split.tail_from - len(split.head)


def test_a_giant_single_line_keeps_a_tail_behind_the_marker() -> None:
    data = b"x" * 5000 + b"\n"  # one line far larger than the cap
    split = split_for_cap(data, 300)
    assert split.head == b""  # no newline in the head budget
    assert split.tail  # not emptied — a torn tail is kept
    assert len(split.tail) <= 300


def test_at_or_below_cap_is_untouched(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_bytes(b"small\n")
    assert truncate_path(p, 1000, marker=marker) is None
    assert p.read_bytes() == b"small\n"


def test_truncate_path_writes_head_marker_tail_within_cap(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_bytes(b"".join(b'{"n":%d}\n' % i for i in range(1000)))
    split = truncate_path(p, 2048, marker=marker)
    assert split is not None
    out = p.read_bytes()
    assert len(out) <= 2048
    assert out.startswith(b'{"n":0}\n')
    assert b'"kind":"truncated"' in out
    assert out.endswith(b"}\n")  # a whole final line
    # the marker sits between head and tail
    assert out.index(b"truncated") > len(split.head) - 1


def test_truncate_open_file_is_in_place(tmp_path: Path) -> None:
    p = tmp_path / "stdout.log"
    p.write_bytes(b"a" * 10 + b"\n")
    with open(p, "r+b") as fh:
        fh.seek(0, 2)
        fh.write(b"".join(b"row-%03d\n" % i for i in range(500)))
        split = truncate_open_file(fh, 1024, marker=marker)
        assert split is not None
    out = p.read_bytes()
    assert len(out) <= 1024
    assert b'"kind":"truncated"' in out and out.endswith(b"\n")
