# SPDX-License-Identifier: Apache-2.0
"""The :class:`SecretProvider` seam and its error type.

Module boundary: this module declares *what* a secret store must be able to do and nothing
about where values come from. :mod:`rayspec.secrets.sources` implements the built-in sources
(``env`` / ``file`` / ``cmd``); an out-of-tree store only has to satisfy the protocol.

A provider is asked by NAME and answers with a value or ``None``. It never logs, never prints
and never persists a value; every message it raises must be safe to show a user (it names the
secret and the source, never the value).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rayspec.errors import RayspecError


class SecretError(RayspecError):
    """A configured secret source cannot be used (missing value, loose file mode, failed cmd).

    The message names ``secrets.<NAME>`` and the source; it never contains a secret value. The
    CLI catches it at the command boundary and exits 2.
    """


@runtime_checkable
class SecretProvider(Protocol):
    """Read-only access to named secret values.

    :meth:`names` lists what the provider *could* supply (for ``rayspec doctor``, which prints
    the sources without their values); :meth:`get` resolves one name, returning ``None`` when
    the provider has nothing for it. ``get`` raises :class:`SecretError` when it *does* know the
    name but cannot obtain the value (a missing required variable, a world-readable file, a
    command that failed).
    """

    def names(self) -> tuple[str, ...]:
        """Every name this provider is configured for (declaration order)."""
        ...

    def get(self, name: str) -> str | None:
        """The value for ``name``, or ``None`` when this provider does not supply it."""
        ...


__all__ = ["SecretError", "SecretProvider"]
