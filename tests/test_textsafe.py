"""``rayspec.textsafe`` — neutralising untrusted text before it reaches a terminal."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text

from rayspec.textsafe import safe_markup, safe_text

ESC = "\x1b"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # OSC title change (BEL terminated) is dropped whole
        (f"before{ESC}]0;PWNED-TITLE\x07after", "beforeafter"),
        # OSC terminated by ST (ESC \)
        (f"x{ESC}]8;;https://evil{ESC}\\link{ESC}]8;;{ESC}\\y", "xlinky"),
        # unterminated OSC swallows the rest of the text: neutralised, tail kept
        (f"a{ESC}]0;title-without-end", "a0;title-without-end"),
        # CSI colour and clear-screen
        (f"{ESC}[31mRED{ESC}[0m {ESC}[2J after", "RED  after"),
        # CSI with intermediate bytes, DCS/APC/PM/SOS string sequences
        (f"{ESC}[?25l{ESC}P payload {ESC}\\ok", "ok"),
        (f"x{ESC}_apc{ESC}\\y{ESC}^pm\x07z", "xyz"),
        # SS3 / two-byte escapes
        (f"{ESC}OAtext{ESC}7{ESC}8", "text"),
        # C1 controls (8-bit CSI/OSC) and a bare C1
        ("a\x9b31mb\x9d0;t\x07c\x85d", "abcd"),
        # C0 controls other than \n/\t and DEL are dropped
        ("a\x00b\x07c\x08d\x7fe\rf", "abcdef"),
        # plain text with Rich markup stays literal text
        ("[bold red]MARKUP[/] plain", "[bold red]MARKUP[/] plain"),
        ("", ""),
    ],
)
def test_safe_text_strips_escapes(raw: str, expected: str) -> None:
    assert safe_text(raw) == expected


def test_safe_text_keeps_or_drops_newlines() -> None:
    raw = "line1\nline2\ttabbed\r\n"
    assert safe_text(raw) == "line1\nline2\ttabbed\n"
    assert safe_text(raw, keep_newlines=False) == "line1 line2 tabbed "


def test_safe_text_keeps_unicode() -> None:
    assert safe_text("✓ héllo — 日本語 🚀") == "✓ héllo — 日本語 🚀"


def test_safe_text_is_idempotent_and_handles_non_str() -> None:
    once = safe_text(f"{ESC}[2J{ESC}]0;t\x07x")
    assert safe_text(once) == once == "x"
    assert safe_text(None) == ""  # type: ignore[arg-type]
    assert safe_text(42) == "42"  # type: ignore[arg-type]


def test_safe_markup_escapes_rich_markup_after_stripping() -> None:
    out = safe_markup(f"{ESC}[31m[bold red]MARKUP[/]")
    assert ESC not in out
    console = Console(record=True, width=80, force_terminal=False, color_system=None)
    console.print(out)
    assert console.export_text().strip() == "[bold red]MARKUP[/]"
    # and the plain helper is what ``Text(...)`` cells should receive
    console2 = Console(record=True, width=80, force_terminal=False, color_system=None)
    console2.print(Text(safe_text(f"{ESC}[31m[bold red]MARKUP[/]")))
    assert console2.export_text().strip() == "[bold red]MARKUP[/]"


def test_pinned_signature_uses_s() -> None:
    # the seam is ``safe_text(s: str, *, keep_newlines: bool = True) -> str`` (CONTRACTS.md);
    # both copies of the module must accept the keyword form
    assert safe_text(s=f"a{ESC}[2Jb") == "ab"
    assert safe_markup(s="[bold]x[/]") == "\\[bold]x\\[/]"
    assert safe_text(s="a\nb", keep_newlines=False) == "a b"
