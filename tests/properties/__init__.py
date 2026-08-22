# SPDX-License-Identifier: Apache-2.0
"""Generative ("property") tests: one promise, many generated cases, a minimal failing case.

These suites do not test a module's surface — that is what ``tests/<area>/`` is for. They test
the *promises* the documentation makes, over inputs nobody would think to write down: every
``{{ }}`` in a code body becomes an env slot whose value arrives verbatim; the join table holds
for every DAG shape under every failure policy, composites included.

A property that fails here is a defect report, not a licence to weaken the property. A promise
the code does not currently keep is recorded with ``@pytest.mark.xfail(strict=True)`` and the
reason spelled out, so the suite stays green *and* turns red the day the behaviour changes —
never by narrowing the generator until the counter-example stops appearing.

There is no generative-testing dependency: :mod:`tests.properties.generate` is a ~200 line
driver over :class:`random.Random`. Everything is seeded from a printed key, so a failure is
reproducible from its message alone.
"""
