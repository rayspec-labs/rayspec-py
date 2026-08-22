# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the property suites.

The scheduler properties need the same temp project + store + collecting sink that the engine
tests use, so the fixture is imported from ``tests/engine/conftest.py`` rather than copied: two
harnesses that drift apart would let a property pass against a runner the engine tests no longer
describe. Nothing is redefined here — importing the fixture function registers it for this
package only.
"""

from __future__ import annotations

from engine.conftest import Harness, harness, make_graph_harness  # noqa: F401
