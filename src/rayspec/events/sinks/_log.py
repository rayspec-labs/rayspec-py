# SPDX-License-Identifier: Apache-2.0
"""Shared logger for event sinks (``rayspec.events``); sinks log IO problems and carry on."""

from __future__ import annotations

import logging

log = logging.getLogger("rayspec.events")

__all__ = ["log"]
