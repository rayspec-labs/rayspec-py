"""Test doubles: a regex-based TemplateChecker and a capability table."""

from __future__ import annotations

import re

from rayspec.errors import RayspecError
from rayspec.providers.base import AccessLevel, ProviderCapabilities

_REF_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)((?:\.[a-z_][a-z0-9_]*)*)")
_BLOCK_RE = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)


class FakeCompileError(RayspecError):
    pass


class FakeChecker:
    """Good-enough stand-in for the templating scope's compile/reference services."""

    def __init__(self) -> None:
        self.compiled: list[str] = []

    def compile_template(self, text: str, *, where: str) -> None:
        self.compiled.append(where)
        if text.count("{{") != text.count("}}"):
            raise FakeCompileError("unbalanced braces in template")
        if "{{ BAD" in text:
            raise FakeCompileError("unexpected token 'BAD'")

    def compile_expr(self, text: str, *, where: str) -> None:
        self.compiled.append(where)
        if "BAD" in text:
            raise FakeCompileError("unexpected token 'BAD'")

    def references(self, text: str) -> set[tuple[str, str, tuple[str, ...]]]:
        pieces: list[str]
        if "{{" in text or "{%" in text:
            pieces = [a or b for a, b in _BLOCK_RE.findall(text)]
        else:
            pieces = [text]
        refs: set[tuple[str, str, tuple[str, ...]]] = set()
        for piece in pieces:
            for root, name, rest in _REF_RE.findall(piece):
                attrs = tuple(p for p in rest.split(".") if p)
                refs.add((root, name, attrs))
        return refs


def caps(
    *,
    max_turns: bool = True,
    budget_usd: bool = True,
    structured: str = "enforced",
    session_resume: bool = True,
    tool_groups: frozenset[str] = frozenset({"read", "edit", "shell", "web", "agent", "mcp"}),
    raw_tool_names: bool = True,
    effort_levels: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"}),
    effort_aliases: dict[str, str] | None = None,
    thinking: bool = True,
    mcp_servers: bool = True,
    env_injection: bool = True,
    access_levels: frozenset[AccessLevel] = frozenset(AccessLevel),
    instructions_modes: frozenset[str] = frozenset({"append", "replace"}),
) -> ProviderCapabilities:
    return ProviderCapabilities(
        structured_output=structured,  # type: ignore[arg-type]
        session_resume=session_resume,
        session_fork=True,
        instructions_modes=instructions_modes,  # type: ignore[arg-type]
        access_levels=access_levels,
        tool_groups=tool_groups,
        raw_tool_names=raw_tool_names,
        max_turns=max_turns,
        budget_usd=budget_usd,
        cost_reporting=True,
        effort_levels=effort_levels,
        effort_aliases=effort_aliases or {},
        thinking=thinking,
        mcp_servers=mcp_servers,
        env_injection=env_injection,
    )


CLAUDE = caps()
CODEX = caps(
    max_turns=False,
    budget_usd=False,
    tool_groups=frozenset({"web"}),
    raw_tool_names=False,
    effort_levels=frozenset({"none", "minimal", "low", "medium", "high", "xhigh"}),
    effort_aliases={"max": "xhigh"},
    thinking=False,
)
TABLE = {"claude": CLAUDE, "codex": CODEX, "stub": caps()}


def capabilities_for(provider: str) -> ProviderCapabilities | None:
    return TABLE.get(provider)
