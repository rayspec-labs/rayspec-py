# SPDX-License-Identifier: Apache-2.0
"""Neutral tool vocabulary → provider-native tool names.

Boundary: pure translation, no SDK imports. The loader calls :func:`validate_tools` at load time
(capability check); adapters call :func:`translate_tools` when building an SDK request.

Vocabulary (``tools.allow`` / ``tools.deny``):

* groups ``read edit shell web agent mcp``
* ``mcp:<server>`` (every tool of a server) / ``mcp:<server>/<tool>``
* raw provider names ``<provider>:<Name>`` — honoured only by that provider and only when it
  declares ``raw_tool_names``; entries addressed to another provider are ignored silently so one
  agent definition can serve several providers.

Claude Code MCP names follow the documented permission grammar — ``mcp__<server>`` (every tool
of a server) and ``mcp__<server>__<tool>`` — and never a trailing ``*`` (Claude Code documents
no wildcard for MCP tool names, so ``mcp__github__*`` would be silently inert). The bare ``mcp``
group therefore has no single native name: :class:`ToolTranslation` carries ``allow_all_mcp`` /
``deny_all_mcp`` flags that the adapter expands to ``mcp__<server>`` for each ``req.mcp_servers``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, overload

from rayspec.providers.base import TOOL_GROUPS, AccessLevel, ProviderCapabilities, ToolPolicy
from rayspec.providers.capabilities import KNOWN_CLAUDE_TOOLS, RENAMED_CLAUDE_TOOLS
from rayspec.providers.registry import get_registration
from rayspec.schema import ToolsSpec

#: Claude Code built-in names behind each neutral group. ``mcp`` (every MCP tool) has no native
#: name (no wildcard in Claude Code's grammar); it is reported via ``ToolTranslation.*_all_mcp``.
CLAUDE_GROUP_TOOLS: Mapping[str, tuple[str, ...]] = {
    "read": ("Read", "Glob", "Grep"),
    "edit": ("Edit", "Write", "NotebookEdit"),
    "shell": ("Bash",),
    "web": ("WebFetch", "WebSearch"),
    "agent": ("Agent",),
    "mcp": (),
}

#: Groups that imply writing to / executing in the workspace (refused under ``read-only``).
WRITE_GROUPS: frozenset[str] = frozenset({"edit", "shell"})

#: Server / tool / provider-prefix / raw-name token: letters, digits, ``_ . -``.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

EntryKind = Literal["group", "mcp", "raw", "invalid"]


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One parsed ``tools.allow``/``tools.deny`` entry."""

    raw: str
    kind: EntryKind
    group: str | None = None
    server: str | None = None
    tool: str | None = None
    provider: str | None = None
    name: str | None = None

    @property
    def claude_mcp_name(self) -> str:
        """Claude Code permission name: ``mcp__<server>`` or ``mcp__<server>__<tool>``."""
        return f"mcp__{self.server}" if self.tool is None else f"mcp__{self.server}__{self.tool}"


def parse_tool_entry(entry: str) -> ToolEntry:
    """Classify an entry as group / mcp / raw provider name / invalid (never raises)."""
    if not isinstance(entry, str) or not entry:
        return ToolEntry(raw=str(entry), kind="invalid")
    if entry in TOOL_GROUPS:
        return ToolEntry(raw=entry, kind="group", group=entry)
    if entry.startswith("mcp:"):
        rest = entry[4:]
        server, sep, tool = rest.partition("/")
        if not server or not _TOKEN_RE.match(server):
            return ToolEntry(raw=entry, kind="invalid")
        if sep and (not tool or not _TOKEN_RE.match(tool)):
            return ToolEntry(raw=entry, kind="invalid")
        return ToolEntry(raw=entry, kind="mcp", server=server, tool=tool or None)
    provider, sep, name = entry.partition(":")
    if sep and provider and name and _TOKEN_RE.match(provider) and _TOKEN_RE.match(name):
        return ToolEntry(raw=entry, kind="raw", provider=provider, name=name)
    return ToolEntry(raw=entry, kind="invalid")


@dataclass(frozen=True, slots=True)
class ToolTranslation:
    """Result of :func:`translate_tools`. ``errors`` non-empty ⇒ the policy is unsupported.

    ``allow_all_mcp`` / ``deny_all_mcp`` are set when the bare ``mcp`` group appears in the
    respective list (and the provider supports it); adapters without a native "every MCP tool"
    name (Claude) expand them to ``mcp__<server>`` over ``AgentRequest.mcp_servers``.
    """

    allow_native: tuple[str, ...] = ()
    deny_native: tuple[str, ...] = ()
    config_overrides: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    allow_all_mcp: bool = False
    deny_all_mcp: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


class _Translator:
    """Accumulates native names / diagnostics while walking allow and deny lists."""

    def __init__(self, provider_id: str, capabilities: ProviderCapabilities) -> None:
        self.provider_id = provider_id
        self.caps = capabilities
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.config: dict[str, Any] = {}
        self.allow_all_mcp = False
        self.deny_all_mcp = False

    # -- diagnostics ------------------------------------------------------------------------

    def _unsupported_group(self, entry: ToolEntry, group: str) -> None:
        supported = ", ".join(sorted(self.caps.tool_groups)) or "none"
        self.errors.append(
            f"provider {self.provider_id!r} does not support tool group {group!r} "
            f"({entry.raw!r}; capability tool_groups={{{supported}}})"
        )

    # -- per entry --------------------------------------------------------------------------

    def translate(self, entry: ToolEntry, *, deny: bool) -> list[str]:
        if entry.kind == "invalid":
            self.errors.append(
                f"unknown tool entry {entry.raw!r}: expected a group "
                f"({', '.join(sorted(TOOL_GROUPS))}), mcp:<server>[/<tool>] or <provider>:<Name>"
            )
            return []
        if entry.kind == "raw":
            if entry.provider != self.provider_id:
                return []  # addressed to another provider
            if not self.caps.raw_tool_names:
                self.errors.append(
                    f"provider {self.provider_id!r} does not accept raw tool names "
                    f"({entry.raw!r}; capability raw_tool_names=False)"
                )
                return []
            return self._raw(entry)
        group = entry.group if entry.kind == "group" else "mcp"
        assert group is not None
        if group not in self.caps.tool_groups:
            self._unsupported_group(entry, group)
            return []
        if entry.kind == "group" and group == "mcp":
            if deny:
                self.deny_all_mcp = True
            else:
                self.allow_all_mcp = True
        return self._group(entry, group, deny=deny)

    def _raw(self, entry: ToolEntry) -> list[str]:
        name = entry.name or ""
        if self.provider_id != "claude":
            return [name]
        if name.startswith("mcp__"):
            return [name]
        renamed = RENAMED_CLAUDE_TOOLS.get(name)
        if renamed is not None:
            self.warnings.append(
                f"tool {entry.raw!r}: {name!r} was renamed to {renamed!r}; using {renamed!r}"
            )
            return [renamed]
        if name not in KNOWN_CLAUDE_TOOLS:
            self.warnings.append(
                f"tool {entry.raw!r}: unknown Claude tool name {name!r} (passed through as-is)"
            )
        return [name]

    def _group(self, entry: ToolEntry, group: str, *, deny: bool) -> list[str]:
        if self.provider_id == "claude":
            if entry.kind == "mcp":
                return [entry.claude_mcp_name]
            return list(CLAUDE_GROUP_TOOLS[group])
        if self.provider_id == "codex":
            # Codex has no per-tool policy; web search is a config switch.
            if group == "web" and deny:
                self.config["web_search"] = "disabled"
            return []
        return [entry.raw]  # stub / third-party: keep the neutral spelling


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


@overload
def translate_tools(
    allow: Iterable[str],
    deny: Iterable[str],
    provider_id: str,
    capabilities: ProviderCapabilities,
) -> ToolTranslation: ...


@overload
def translate_tools(
    spec: ToolsSpec | ToolPolicy,
    provider_id: str,
    /,
    *,
    capabilities: ProviderCapabilities | None = None,
) -> ToolTranslation: ...


def translate_tools(
    allow: Iterable[str] | ToolsSpec | ToolPolicy,
    deny: Iterable[str] | str,
    provider_id: str | ProviderCapabilities | None = None,
    capabilities: ProviderCapabilities | None = None,
) -> ToolTranslation:
    """Translate neutral allow/deny lists into provider-native names + config overrides.

    Two call forms: ``translate_tools(allow, deny, provider_id, capabilities)`` (adapters) and the
    legacy form ``translate_tools(ToolsSpec | ToolPolicy, provider_id[, capabilities])``; when
    ``capabilities`` is omitted the registry's declared table for ``provider_id`` is used.
    Unsupported entries land in ``errors`` (naming the violated capability); renamed/unknown raw
    names land in ``warnings``. Never raises for bad entries (only for an unknown provider id in
    the spec form).
    """
    if isinstance(allow, ToolsSpec | ToolPolicy):
        spec, pid = allow, deny
        caps = provider_id if provider_id is not None else capabilities
        if not isinstance(pid, str):
            raise TypeError("translate_tools(spec, provider_id, capabilities=None)")
        if caps is None:
            caps = get_registration(pid).capabilities
        elif not isinstance(caps, ProviderCapabilities):
            raise TypeError("translate_tools(spec, provider_id, capabilities=None)")
        return _translate(tuple(spec.allow), tuple(spec.deny), pid, caps)
    if isinstance(deny, str) or not isinstance(provider_id, str) or capabilities is None:
        raise TypeError("translate_tools(allow, deny, provider_id, capabilities)")
    return _translate(allow, deny, provider_id, capabilities)


def _translate(
    allow: Iterable[str],
    deny: Iterable[str],
    provider_id: str,
    capabilities: ProviderCapabilities,
) -> ToolTranslation:
    tr = _Translator(provider_id, capabilities)
    allow_native: list[str] = []
    deny_native: list[str] = []
    for raw in allow:
        allow_native.extend(tr.translate(parse_tool_entry(raw), deny=False))
    for raw in deny:
        deny_native.extend(tr.translate(parse_tool_entry(raw), deny=True))
    return ToolTranslation(
        allow_native=_dedupe(allow_native),
        deny_native=_dedupe(deny_native),
        config_overrides=dict(tr.config),
        warnings=tuple(tr.warnings),
        errors=tuple(tr.errors),
        allow_all_mcp=tr.allow_all_mcp,
        deny_all_mcp=tr.deny_all_mcp,
    )


def _entry_touches_write_group(entry: ToolEntry, provider_id: str) -> str | None:
    """Return the write group (``edit``/``shell``) an allow entry would enable, if any."""
    if entry.kind == "group" and entry.group in WRITE_GROUPS:
        return entry.group
    if entry.kind == "raw" and entry.provider == provider_id and provider_id == "claude":
        name = RENAMED_CLAUDE_TOOLS.get(entry.name or "", entry.name)
        for group in WRITE_GROUPS:
            if name in CLAUDE_GROUP_TOOLS[group]:
                return group
    return None


def validate_tools(
    spec: ToolsSpec | ToolPolicy,
    provider_id: str,
    capabilities: ProviderCapabilities,
    *,
    access: AccessLevel | str | None = None,
    known_providers: Collection[str] | None = None,
) -> list[str]:
    """Load-time check used by the loader: returns human-readable error messages (empty = ok).

    Beyond the translation errors it refuses ``access: read-only`` together with an allow entry
    that enables editing or shell (``edit``/``shell`` groups or their raw Claude names), and —
    when ``known_providers`` is given — raw entries addressed to an unknown provider id.
    """
    allow = tuple(spec.allow)
    deny = tuple(spec.deny)
    errors = list(_translate(allow, deny, provider_id, capabilities).errors)
    level: AccessLevel | None = None
    if access is not None:
        try:
            level = AccessLevel(access)
        except ValueError:
            errors.append(
                f"unknown access level {access!r}; expected one of "
                f"{', '.join(a.value for a in AccessLevel)} (read-only restricts tools)"
            )
    if level is AccessLevel.READ_ONLY:
        for raw in allow:
            entry = parse_tool_entry(raw)
            group = _entry_touches_write_group(entry, provider_id)
            if group is not None:
                errors.append(
                    f"access: read-only cannot allow {group!r} ({raw!r}); "
                    "use access: workspace-write or remove the entry"
                )
    if known_providers is not None:
        known = set(known_providers)
        for raw in (*allow, *deny):
            entry = parse_tool_entry(raw)
            if entry.kind == "raw" and entry.provider not in known:
                errors.append(
                    f"tool {raw!r}: unknown provider prefix {entry.provider!r} "
                    f"(known: {', '.join(sorted(known))})"
                )
    return errors


__all__ = [
    "CLAUDE_GROUP_TOOLS",
    "WRITE_GROUPS",
    "ToolEntry",
    "ToolTranslation",
    "parse_tool_entry",
    "translate_tools",
    "validate_tools",
]
