"""``rayspec.loader.bundled``: the workflow library shipped inside the package.

Where it lives, how a bundled path is labelled (install-independent, so hashes and trust keys
survive a reinstall), and the eject header a copy carries.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from rayspec.loader.bundled import (
    BUNDLED_LABEL_PREFIX,
    EjectHeader,
    bundled_digest,
    bundled_dir,
    bundled_label,
    is_bundled,
    parse_eject_header,
    render_ejected,
)
from rayspec.loader.yaml import load_yaml

V1_SET = [
    "fix_issue",
    "pr_review",
    "release_check",
    "resolve_conflicts",
    "review_block",
    "review_panel",
]


def test_bundled_dir_holds_the_v1_set() -> None:
    files = sorted(bundled_dir().glob("*.yaml"), key=lambda p: p.name)
    assert [p.stem for p in files] == V1_SET
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# yaml-language-server:"), path
        data = load_yaml(text, source=str(path))
        assert isinstance(data, dict) and data["name"] == path.stem, path


def test_bundled_label_and_is_bundled(tmp_path: Path) -> None:
    inside = bundled_dir() / "pr_review.yaml"
    assert is_bundled(inside)
    assert (
        bundled_label(inside)
        == f"{BUNDLED_LABEL_PREFIX}pr_review.yaml"
        == "<bundled>/pr_review.yaml"
    )
    outside = tmp_path / "pr_review.yaml"
    assert not is_bundled(outside)
    assert bundled_label(outside) is None


def test_a_path_through_a_symlink_to_the_bundled_dir_is_bundled(tmp_path: Path) -> None:
    link = tmp_path / "link"
    os.symlink(bundled_dir(), link, target_is_directory=True)
    assert is_bundled(link / "pr_review.yaml")
    assert bundled_label(link / "pr_review.yaml") == "<bundled>/pr_review.yaml"


DIGEST = "ab" * 32


def test_render_ejected_keeps_the_modeline_first() -> None:
    text = "# yaml-language-server: $schema=x\nrayspec: 1\nname: pr_review\n"
    out = render_ejected("pr_review", text, version="1.2.3", digest=DIGEST)
    lines = out.splitlines()
    assert lines[0] == "# yaml-language-server: $schema=x"
    assert lines[1] == f"# rayspec-eject: version=1.2.3 workflow=pr_review sha256={DIGEST}"
    assert lines[2].startswith("# ")
    assert out.endswith("rayspec: 1\nname: pr_review\n")


def test_render_ejected_without_modeline_goes_on_top() -> None:
    text = "rayspec: 1\nname: pr_review\n"
    out = render_ejected("pr_review", text, version="1.2.3", digest=DIGEST)
    assert (
        out.splitlines()[0] == f"# rayspec-eject: version=1.2.3 workflow=pr_review sha256={DIGEST}"
    )
    assert out.endswith(text)


def test_parse_eject_header_round_trips() -> None:
    text = "# yaml-language-server: $schema=x\nrayspec: 1\n"
    out = render_ejected("pr_review", text, version="1.2.3", digest=DIGEST)
    assert parse_eject_header(out) == EjectHeader(
        version="1.2.3", workflow="pr_review", sha256=DIGEST
    )
    assert parse_eject_header("rayspec: 1\n") is None
    assert parse_eject_header("# rayspec-eject: version=1 workflow=x sha256=nothex\n") is None


def test_bundled_digest_is_sha256_of_bytes(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_bytes(b"rayspec: 1\n")
    assert bundled_digest(path) == hashlib.sha256(b"rayspec: 1\n").hexdigest()
