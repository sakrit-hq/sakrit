# SPDX-License-Identifier: Apache-2.0
"""Effect-outcome metrics — a dependency-free tally (roadmap #21).

The number that proves *dependency rather than curiosity* is **total effects settled**
(roadmap #21). :class:`Metrics` is a small, thread-safe counter the ledger updates at each
terminal outcome; you pass one to :class:`~sakrit.core.ledger.SqliteLedger` and read
:meth:`Metrics.snapshot` (or copy it into whatever telemetry you run). There is no exporter
and no dependency — a hosted metrics service is Act V §19, deliberately not this.

The four events, and exactly what each counts:

- ``settled``  — an effect **executed and recorded SUCCEEDED** (a fresh fire, not a replay);
  the headline "total effects settled". Counted on both the single-worker success write and
  the leased fenced success write.
- ``replayed`` — a **dedup hit**: a recorded result was returned and the effect was NOT
  re-fired (resume/retry). Both paths (single-worker and leased) route through it.
- ``ambiguous``— an outcome that **could not be determined** after a crash in the window and
  was surfaced for a human. Every transition into AMBIGUOUS routes through it.
- ``failed``   — a **declared-clean failure** recorded FAILED (re-claimable). Both paths.

**Coverage caveat (honest v1 scope):** counts are **per process** (an in-memory object — a
multi-worker fleet aggregates each worker's snapshot), and a late-evidence *heal* of an
AMBIGUOUS row (``accept_late_evidence``) is not re-counted as a fresh ``settled`` — it already
counted as ``ambiguous`` when it surfaced.
"""

from __future__ import annotations

import threading
from collections import Counter

# The terminal outcomes the ledger observes. Public string constants so a caller can key its
# own dashboards off them without importing internals.
SETTLED = "settled"
REPLAYED = "replayed"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
_EVENTS = (SETTLED, REPLAYED, AMBIGUOUS, FAILED)


class Metrics:
    """A thread-safe tally of effect outcomes. See the module docstring for what each event
    counts. Construct one, pass it as ``SqliteLedger(path, metrics=...)``, read ``snapshot()``."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def record(self, event: str) -> None:
        """Tally one occurrence of ``event`` (thread-safe — the leased ledger heartbeats and
        claims from different threads).

        If you override this to push to statsd/OTel, note the ledger calls it inside a
        write transaction: a raise here is **caught and swallowed** (logged) by the ledger so
        telemetry being down can never roll back an effect transition or fail a settled call
        (C-2) — so a push that must not be lost should buffer/retry, not rely on this call."""
        with self._lock:
            self._counts[event] += 1

    def snapshot(self) -> dict[str, int]:
        """A point-in-time copy of all four counts (unseen events read as 0)."""
        with self._lock:
            return {event: self._counts.get(event, 0) for event in _EVENTS}

    @property
    def settled(self) -> int:
        """The headline count: effects that executed and recorded SUCCEEDED (roadmap #21)."""
        with self._lock:
            return self._counts.get(SETTLED, 0)
