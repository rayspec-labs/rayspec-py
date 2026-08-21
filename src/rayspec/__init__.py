# SPDX-License-Identifier: Apache-2.0
"""rayspec — declarative agent workflows for coding agents (CLI only)."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("rayspec")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
