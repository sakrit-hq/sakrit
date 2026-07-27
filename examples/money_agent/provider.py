# SPDX-License-Identifier: Apache-2.0
"""A fake payment provider — the reusable seam behind the golden money demo.

Stripe-shaped: it **deduplicates on an idempotency key** (so a re-dispatched charge is harmless) and
it is **queryable** (a reconcile read answers "did this charge land?"). Both are keyed by the same
string Sakrit derives for the effect, which is what lets a money tool run at L2+R — provider dedup
*plus* a recovery read, the level recommended for money.

It also injects failures on demand — a timeout, a decline, and the nasty **commit-then-timeout**
that models the dual-write window (the charge is captured, then the response is lost). No network,
fully deterministic — built to be reused as the Phase 1 end-to-end fixture, not just this demo.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from sakrit.core import Reconciliation

# Injectable fault kinds.
TIMEOUT = "timeout"  # no response before the charge is captured — a clean retry is safe
DECLINE = "decline"  # the card was declined — a real, terminal business failure
COMMIT_THEN_TIMEOUT = (
    "commit_then_timeout"  # captured, THEN the response is lost — the ambiguous window
)


class ProviderTimeout(Exception):
    """The provider did not respond. May or may not have captured the charge."""


class ProviderDeclined(Exception):
    """The card was declined — a clean, terminal failure (no charge landed)."""


@dataclass(frozen=True)
class Charge:
    id: str
    amount_cents: int
    currency: str
    idempotency_key: str | None


class FakePaymentProvider:
    def __init__(self) -> None:
        self._by_key: dict[str, Charge] = {}  # idempotency key → the one charge for it
        self._landed: list[Charge] = []  # every charge that actually moved money
        self._faults: deque[str] = deque()

    def inject(self, *faults: str) -> None:
        """Schedule faults for the next N charge() calls, in order."""
        self._faults.extend(faults)

    def charge(
        self, *, amount_cents: int, currency: str, idempotency_key: str | None = None
    ) -> Charge:
        # Dedup FIRST — a real provider returns the same charge for a repeated key, before any
        # fault logic. This is the L2 guarantee: a re-issued key never double-charges.
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]

        fault = self._faults.popleft() if self._faults else None
        if fault == TIMEOUT:
            raise ProviderTimeout("no response from provider (charge NOT captured)")
        if fault == DECLINE:
            raise ProviderDeclined("card declined")

        charge = Charge(
            id=f"ch_{len(self._landed) + 1}",
            amount_cents=amount_cents,
            currency=currency,
            idempotency_key=idempotency_key,
        )
        self._landed.append(charge)  # the money moves HERE
        if idempotency_key is not None:
            self._by_key[idempotency_key] = charge

        if fault == COMMIT_THEN_TIMEOUT:
            # The charge is captured, but the caller never learns it — the dual-write window.
            raise ProviderTimeout("charge captured but the response was lost")
        return charge

    def reconcile(self, key: str) -> Reconciliation:
        """The recovery read: did the charge for this key land? (L1/L2+R). Read-only."""
        charge = self._by_key.get(key)
        return Reconciliation.settled(charge) if charge is not None else Reconciliation.absent()

    @property
    def charge_count(self) -> int:
        """How many times money actually moved — the number the whole demo is about."""
        return len(self._landed)
