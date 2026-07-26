# SPDX-License-Identifier: Apache-2.0
"""Chaos: an ambiguous exception (a timeout AFTER the effect landed) must not
become a retriable FAILED — that mints a duplicate.

This is the Q2 finding. Written to assert the *correct* behavior; run it against
the code that classifies every exception as FAILED and it fails, catching the bug —
mutation-testing the instrument before the fix.
"""

import pytest

from sakrit.adapters import FakeAdapter
from sakrit.core import (
    ArgClass,
    EffectDecl,
    SakritError,
    SqliteLedger,
    fingerprint,
    positional_key,
    settle,
)

SECRET = b"deployment-secret"


class FakeWorld:
    """A stand-in external world with a durable delivery log and injectable faults.

    ``keyed`` models a provider that deduplicates on an idempotency key (L2-style).
    ``fault = "timeout_after_deliver"`` models the true ambiguous window: the
    request lands, then the client times out waiting for the response.
    """

    def __init__(self, *, keyed: bool = False) -> None:
        self.keyed = keyed
        self.deliveries: list[dict[str, object]] = []
        self._by_key: dict[str, dict[str, object]] = {}
        self.fault: str | None = None

    def deliver(
        self, *, idempotency_key: str | None = None, **payload: object
    ) -> dict[str, object]:
        if self.keyed and idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]  # provider dedups a keyed retry
        record: dict[str, object] = {"id": f"d{len(self.deliveries) + 1}", **payload}
        self.deliveries.append(record)
        if self.keyed and idempotency_key is not None:
            self._by_key[idempotency_key] = record
        if self.fault == "timeout_after_deliver":
            raise TimeoutError("the request landed, but the response timed out")
        return record

    @property
    def count(self) -> int:
        return len(self.deliveries)


DECL = EffectDecl("email.send", {"to": ArgClass.IDENTITY})


def _key() -> str:
    return positional_key(FakeAdapter("run-1").at("send"), "email.send")


@pytest.mark.chaos
def test_ambiguous_timeout_does_not_duplicate() -> None:
    world = FakeWorld(keyed=False)  # L0: nobody downstream deduplicates
    led = SqliteLedger()
    key = _key()
    fp = fingerprint(DECL, {"to": "c@x.com"}, secret=SECRET)

    # Attempt 1: the effect lands in the world, then the client times out.
    world.fault = "timeout_after_deliver"
    with pytest.raises(TimeoutError):
        settle(
            led,
            key=key,
            scope="run-1",
            tool="email.send",
            fingerprint=fp,
            fn=lambda: world.deliver(to="c@x.com"),
            clean_failures=DECL.clean_failures,
        )
    assert world.count == 1  # it did land

    # Attempt 2: a retry. It MUST NOT re-deliver — the outcome is ambiguous.
    world.fault = None
    with pytest.raises(SakritError):  # AmbiguousOutcome (or EffectInFlightError after Q1)
        settle(
            led,
            key=key,
            scope="run-1",
            tool="email.send",
            fingerprint=fp,
            fn=lambda: world.deliver(to="c@x.com"),
            clean_failures=DECL.clean_failures,
        )
    assert world.count == 1  # ← on the pre-fix core this is 2: the duplicate
