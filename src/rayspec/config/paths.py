# SPDX-License-Identifier: Apache-2.0
"""``RAYSPEC_HOME`` resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_HOME = "RAYSPEC_HOME"


def rayspec_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the rayspec home directory: ``$RAYSPEC_HOME`` or ``~/.rayspec``."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_HOME)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".rayspec"


__all__ = ["ENV_HOME", "rayspec_home"]
