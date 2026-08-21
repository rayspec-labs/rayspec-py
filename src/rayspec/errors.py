# SPDX-License-Identifier: Apache-2.0
"""Exception root for rayspec. Every rayspec-raised error derives from :class:`RayspecError`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class RayspecError(Exception):
    """Base class; ``hint`` is an optional actionable suggestion shown by the CLI."""

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class UnsupportedFeatureError(RayspecError):
    """A workflow uses a feature the resolved provider lacks (capability mismatch).

    Rendered in a fixed 4-line format so docs and tests can rely on it::

        unsupported: agents.implementer.max_turns = 60
          provider 'codex' does not support `max_turns` (capability max_turns=False)
          fix: remove it, use a provider that supports it (claude), or set
               defaults.on_unsupported: warn / --allow-unsupported
          at .rayspec/workflows/fix_issue.yaml:77
    """

    def __init__(
        self,
        *,
        path: str,
        value: object,
        provider: str,
        capability: str,
        capability_value: object = False,
        alternatives: Sequence[str] = (),
        location: str | None = None,
        field: str | None = None,
    ):
        self.path = path
        self.value = value
        self.provider = provider
        self.capability = capability
        self.capability_value = capability_value
        self.alternatives = list(alternatives)
        self.location = location
        #: what the provider lacks, as named in line 2 (defaults to the last path segment)
        self.field = field if field is not None else path.rsplit(".", 1)[-1]
        field = self.field
        alt = f" ({', '.join(self.alternatives)})" if self.alternatives else ""
        hint = (
            f"remove it, use a provider that supports it{alt}, or set "
            "defaults.on_unsupported: warn / --allow-unsupported"
        )
        lines = [
            f"unsupported: {path} = {_fmt(value)}",
            f"  provider {provider!r} does not support `{field}` "
            f"(capability {capability}={_fmt(capability_value)})",
            f"  fix: {hint}",
        ]
        if location:
            lines.append(f"  at {location}")
        super().__init__("\n".join(lines), hint=hint)


class LoaderError(RayspecError):
    """Loading a workflow / config file failed (YAML, missing file, include cycle, agent lookup…).

    ``location`` is ``<file>:<line>`` when known.
    """

    def __init__(self, message: str, *, hint: str | None = None, location: str | None = None):
        super().__init__(message, hint=hint)
        self.location = location


class InputError(RayspecError):
    """One or more workflow-input problems (unknown name, bad type, missing required…)."""

    def __init__(
        self,
        errors: Sequence[str] | str,
        *,
        hint: str | None = None,
        partial: Mapping[str, Any] | None = None,
        problems: Mapping[str, Sequence[str]] | None = None,
    ):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        #: inputs that did resolve (``rayspec plan`` shows them next to the bad ones)
        self.partial: dict[str, Any] = dict(partial or {})
        #: per-input problems (``name -> messages``) for the inputs that did not resolve
        self.problems: dict[str, list[str]] = {k: list(v) for k, v in (problems or {}).items()}
        super().__init__("\n".join(self.errors), hint=hint)


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return value
    return repr(value)


__all__ = ["InputError", "LoaderError", "RayspecError", "UnsupportedFeatureError"]
