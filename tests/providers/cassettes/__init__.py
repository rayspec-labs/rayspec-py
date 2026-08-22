# SPDX-License-Identifier: Apache-2.0
"""SDK drift cassettes: committed provider transcripts replayed through the event fold.

A cassette is one JSON file holding the messages a provider SDK delivers for a single agent turn,
in the SDK's own wire shapes, plus the :class:`~rayspec.providers.base.AgentEvent` stream and
:class:`~rayspec.providers.base.AgentResult` rayspec must fold them into. Replaying it exercises
the adapter end to end — the SDKs' own parsers turn the wire messages back into SDK objects, so a
provider that renames a field, drops a message type or stops recognising a notification fails a
test here instead of a production run.

Cassettes never touch the network and carry no credentials: the transcripts are replayed from
disk. They are scrubbed like the golden corpus — no path, host name, account or request
identifier, and no version of the machine that recorded them (``tests/providers/cassettes/
test_cassette_hygiene.py`` proves it). Every cassette says where it came from in its ``source``
field: ``recorded`` ones were captured from the bundled CLI / app-server with a deliberately
invalid API key (which is why the recorded turns are failures — no credential, no completion),
``authored`` ones were written against the installed SDK's message types and are validated by the
SDK's own parser on every replay.

Refreshing one after an SDK bump: run the turn again, dump the raw messages, scrub them the same
way, and update ``expect`` only where the SDK genuinely changed — an ``expect`` edited until the
test passed is worth nothing.
"""
