# SPDX-License-Identifier: Apache-2.0
"""Reconciliation — asking the world "did this effect happen?" on recovery.

For an L1 tool (a provider that is queryable but not idempotent), recovery resolves
a crash-in-window row by *asking the provider*. The tool's reconcile function
returns a three-valued verdict; the doctrine (Fable Q13) is **the effect is the
submission, not the downstream journey** — an accepted-then-bounced email is
``SETTLED`` with a bounce in its record; a charge that exists but isn't captured is
``SETTLED``.

Reconcile functions are **read-only by contract** — which is what makes them
trivially crash-safe: a killed reconcile simply re-runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    SETTLED = "settled"  # the effect happened; adopt the provider's record as the result
    ABSENT = "absent"  # the effect did not happen
    UNKNOWN = "unknown"  # can't tell yet (e.g. a lagging read) → stays ambiguous


@dataclass(frozen=True)
class Reconciliation:
    verdict: Verdict
    result: object | None = None  # the canonical provider record, for SETTLED

    @classmethod
    def settled(cls, result: object) -> Reconciliation:
        return cls(Verdict.SETTLED, result)

    @classmethod
    def absent(cls) -> Reconciliation:
        return cls(Verdict.ABSENT)

    @classmethod
    def unknown(cls) -> Reconciliation:
        return cls(Verdict.UNKNOWN)
