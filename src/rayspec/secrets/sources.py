# SPDX-License-Identifier: Apache-2.0
"""The built-in secret sources behind ``config.secrets``: ``env``, ``file``, ``cmd``.

Module boundary: turning one :class:`~rayspec.config.model.SecretSourceSpec` into a string, and
nothing else. Nothing here knows about runs, stores or redaction — the caller (the CLI, at run
start) decides what to do with the values.

Rules that are part of the contract:

* a ``file`` source is refused unless the file is ``0600`` or tighter — a secret that any local
  user can read is not a secret, and silently reading it would be the worst of both worlds;
* a ``cmd`` source is run without a shell (a string is ``shlex.split``), inherits the caller's
  environment, is bounded by :data:`CMD_TIMEOUT_S` and contributes only its **stdout**; when it
  fails, its stderr is NOT put in the error message (helpers print sensitive material there) —
  it goes to the debug log, and into the message only under ``RAYSPEC_DEBUG``;
* a value is stripped of surrounding whitespace (a trailing newline is what ``echo``, ``pass``
  and ``op read`` all produce) and an empty result is an error, not an empty secret;
* every error message names ``secrets.<NAME>`` and the source, never the value.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

from rayspec.config.model import SecretSourceSpec
from rayspec.secrets.base import SecretError

#: Seconds a ``cmd:`` source may take before it is refused.
CMD_TIMEOUT_S = 30.0
#: Permission bits a ``file:`` source must not have (group/other anything).
_LOOSE_BITS = 0o077
#: How much of a failing ``cmd``'s last stderr line ``RAYSPEC_DEBUG`` may show.
_STDERR_TAIL_CHARS = 200

_log = logging.getLogger(__name__)


def _fail(name: str, message: str, *, hint: str | None = None) -> SecretError:
    return SecretError(f"secrets.{name}: {message}", hint=hint)


def resolve_source(
    name: str,
    spec: SecretSourceSpec,
    *,
    env: Mapping[str, str],
    base_dir: Path,
) -> str | None:
    """Resolve one secret, or ``None`` when it is absent and ``spec.required`` is false.

    Raises :class:`SecretError` for a missing required value, a loose ``file`` mode, an
    unreadable file or a failing command.
    """
    value = _read(name, spec, env=env, base_dir=base_dir)
    if value is not None:
        value = value.strip()
    if not value:
        if not spec.required:
            return None
        raise _fail(
            name,
            f"{_describe(spec)} produced no value"
            if value is None
            else f"{_describe(spec)} produced an empty value",
            hint=f"set it, or declare secrets.{name}.required: false",
        )
    return value


def _read(
    name: str, spec: SecretSourceSpec, *, env: Mapping[str, str], base_dir: Path
) -> str | None:
    if spec.env is not None:
        return env.get(spec.env)
    if spec.file is not None:
        return _read_file(name, spec.file, base_dir=base_dir, required=spec.required)
    return _read_cmd(name, spec.cmd, env=env, base_dir=base_dir)


def secret_file_path(raw: str, *, base_dir: Path) -> Path:
    """``~`` expanded; a relative path is taken relative to ``base_dir`` (the project root)."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else base_dir / path


def _read_file(name: str, raw: str, *, base_dir: Path, required: bool) -> str | None:
    path = secret_file_path(raw, base_dir=base_dir)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        if not required:
            return None
        raise _fail(name, f"secret file {path} does not exist") from None
    except OSError as exc:
        raise _fail(name, f"cannot stat secret file {path}: {exc.strerror or exc}") from None
    mode = stat.S_IMODE(info.st_mode)
    if mode & _LOOSE_BITS:
        raise _fail(
            name,
            f"secret file {path} is mode {mode:04o}; it must not be readable by group or others",
            hint=f"chmod 600 {path}",
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(name, f"cannot read secret file {path}: {exc}") from None


def _read_cmd(
    name: str, cmd: str | list[str] | None, *, env: Mapping[str, str], base_dir: Path
) -> str | None:
    assert cmd is not None
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    if not argv:
        raise _fail(name, "cmd is empty")
    try:
        proc = subprocess.run(  # the argv comes from the user's own config
            argv,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_S,
            cwd=str(base_dir),
            env={**os.environ, **env},
            check=False,
        )
    except FileNotFoundError:
        raise _fail(name, f"cmd {argv[0]!r} not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise _fail(name, f"cmd {argv[0]!r} timed out after {CMD_TIMEOUT_S:.0f}s") from None
    except OSError as exc:
        raise _fail(name, f"cmd {argv[0]!r} could not be run: {exc.strerror or exc}") from None
    if proc.returncode != 0:
        raise _cmd_failed(name, argv[0], proc, env=env)
    return proc.stdout


def _cmd_failed(
    name: str, program: str, proc: subprocess.CompletedProcess[str], *, env: Mapping[str, str]
) -> SecretError:
    """The error for a helper that exited non-zero — WITHOUT its output by default.

    A helper's stderr is not safe to print: real ones write a partially decrypted blob, an auth
    URL carrying a token, or the value itself before a checksum fails. The message therefore
    names only the program and the exit code; the last line goes to the debug log, and into the
    message only when the user opted in with ``RAYSPEC_DEBUG``.
    """
    lines = proc.stderr.strip().splitlines()
    tail = lines[-1][:_STDERR_TAIL_CHARS] if lines else ""
    if tail:
        _log.debug("secrets.%s: cmd %r stderr tail: %s", name, program, tail)
    message = f"cmd {program!r} failed with exit code {proc.returncode}"
    if tail and (env.get("RAYSPEC_DEBUG") or os.environ.get("RAYSPEC_DEBUG")):
        message = f"{message}: {tail}"
        hint = None
    else:
        hint = (
            f"run {program!r} yourself to see why; set RAYSPEC_DEBUG=1 to add the last stderr "
            "line to this message (it may contain sensitive output)"
        )
    return _fail(name, message, hint=hint)


def _describe(spec: SecretSourceSpec) -> str:
    """A source description safe to print (names the *source*, never the value)."""
    if spec.env is not None:
        return f"env {spec.env}"
    if spec.file is not None:
        return f"file {spec.file}"
    cmd = spec.cmd if isinstance(spec.cmd, str) else shlex.join(spec.cmd or [])
    return f"cmd {cmd}"


def describe_sources(specs: Mapping[str, SecretSourceSpec]) -> tuple[tuple[str, str], ...]:
    """``((name, "env GH_TOKEN"), …)`` for ``rayspec doctor`` — sources only, never values."""
    return tuple((name, _describe(spec)) for name, spec in specs.items())


__all__ = ["CMD_TIMEOUT_S", "describe_sources", "resolve_source", "secret_file_path"]
