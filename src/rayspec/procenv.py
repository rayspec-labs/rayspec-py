# SPDX-License-Identifier: Apache-2.0
"""Which environment variables rayspec itself put into this process, and which the operator did.

A ``.env`` file is a *convenience*: rayspec reads ``<home>/.env`` and, for the commands that
execute a workflow, ``<project>/.rayspec/.env``, and copies what it finds into ``os.environ``.
After that copy the process environment holds two kinds of variable that look identical:

* what the **operator** exported in the shell that launched the command, and
* what a **file** said — and both of those files are files a running workflow can write. A
  ``shell:`` step runs with the user's own ``$HOME`` and with ``$RAYSPEC_HOME`` exported, so
  ``printf 'RAYSPEC_ACTOR=…' > "$RAYSPEC_HOME/.env"`` is one line in one step; the checkout's
  ``.rayspec/.env`` is simply a file in the tree the run works in.

Configuration may come from either — that is what the files are for. An **identity** may not:
an identity is only evidence if the audited code could not have chosen it. This module is the
seam that keeps the two apart. :func:`note_env_file_values` is called by the one function that
applies a ``.env`` (:func:`rayspec.config.settings.load_env`), and :func:`operator_env` returns
the process environment with exactly those variables removed again — so anything resolved from
:func:`operator_env` is resolved from what the operator set, whatever a run wrote to disk.

The rule is about the *source*, not about a list of attacks, which is what makes it hold for a
``.env`` file rayspec learns to read tomorrow: a variable is untrusted for identity because
rayspec copied it out of a file, not because of what it is called.

No rayspec module is imported here on purpose: this has to be importable from the configuration
layer and from the identity layer without either depending on the other.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

#: ``variable → (value rayspec applied, the file it came from)``, for the current process.
#: Only variables :func:`note_env_file_values` was told about are in here, and each is dropped
#: again the moment the live value stops matching what was applied (see :func:`env_file_origin`).
_ENV_FILE_VALUES: dict[str, tuple[str, str]] = {}


def note_env_file_values(applied: Mapping[str, str], *, origin: str) -> None:
    """Record that ``applied`` was copied from the ``.env`` file described by ``origin``.

    Called for every variable a ``.env`` load actually wrote to the process environment.
    Repeated loads simply overwrite, so the last file to supply a variable is the one named.
    """
    for key, value in applied.items():
        _ENV_FILE_VALUES[key] = (value, origin)


def forget_env_file_values() -> None:
    """Drop the record — for a host that re-enters the CLI in one process (tests, embedders)."""
    _ENV_FILE_VALUES.clear()


def env_file_origin(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The ``.env`` file that supplied ``name``'s *current* value, or ``None``.

    ``None`` covers every case where the value is the operator's: the variable was never in a
    ``.env``; it was, but the shell had already set it (a ``.env`` never overrides an exported
    variable, so nothing was applied and nothing recorded); or something set it afterwards to
    something else. The comparison is against the live value, so the answer stays right no
    matter what happened to the environment after the file was read.
    """
    environ = os.environ if environ is None else environ
    known = _ENV_FILE_VALUES.get(name)
    if known is None:
        return None
    value, origin = known
    return origin if environ.get(name) == value else None


def env_file_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """``name``'s current value when a ``.env`` supplied it, else ``None``.

    What a project *declared*. It is never an identity — but it is worth recording that the
    claim was made, so a reader of the ledger sees the attempt rather than nothing at all.
    """
    if env_file_origin(name, environ) is None:
        return None
    environ = os.environ if environ is None else environ
    return environ.get(name)


def operator_env(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """``environ`` minus every variable rayspec copied out of a ``.env`` file.

    What is left is the environment as the operator handed it over. Resolve an identity from
    this and from the operating system, never from the live environment: a run can write the
    files that feed the live one.
    """
    environ = os.environ if environ is None else environ
    supplied = {name for name in _ENV_FILE_VALUES if env_file_origin(name, environ) is not None}
    if not supplied:
        return environ
    return {key: value for key, value in environ.items() if key not in supplied}


def is_process_environ(target: MutableMapping[str, str]) -> bool:
    """Whether ``target`` IS this process's environment (and not a caller's own mapping)."""
    return target is os.environ


__all__ = [
    "env_file_origin",
    "env_file_value",
    "forget_env_file_values",
    "is_process_environ",
    "note_env_file_values",
    "operator_env",
]
