# SPDX-License-Identifier: Apache-2.0
"""L2: the tool reads the key via current_key(); a keyed provider dedups a retry."""

from sakrit import Sakrit, SqliteLedger, current_key
from sakrit.adapters import FakeAdapter
from sakrit.core import (
    ArgClass,
    Coordinate,
    EffectDecl,
    EffectState,
    fingerprint,
    positional_key,
    settle,
)

SECRET = b"deployment-secret"
CHARGE = EffectDecl(
    "payment.charge",
    {"customer": ArgClass.IDENTITY, "amount_cents": ArgClass.IDENTITY},
    provider_key_param="idempotency_key",  # marks the tool L2 (provider-deduplicating)
)


class KeyedProvider:
    """A Stripe-style provider that deduplicates on the idempotency key."""

    def __init__(self) -> None:
        self.delivered: dict[str, dict[str, str]] = {}

    def charge(self, customer: str, amount_cents: int, idempotency_key: str) -> dict[str, str]:
        if idempotency_key in self.delivered:
            return self.delivered[idempotency_key]
        record = {"charge_id": f"ch_{len(self.delivered) + 1}"}
        self.delivered[idempotency_key] = record
        return record

    @property
    def count(self) -> int:
        return len(self.delivered)


def _key(scope: str = "run-1", site: str = "charge") -> str:
    return positional_key(FakeAdapter(scope).at(site), "payment.charge")


def test_l2_tool_reads_current_key() -> None:
    led = SqliteLedger()
    seen: dict[str, object] = {}

    # Clean signature — no phantom idempotency_key parameter; the tool reads the key.
    def charge(customer: str, amount_cents: int) -> dict[str, str]:
        seen["key"] = current_key()
        return {"charge_id": "ch_1"}

    sk = Sakrit(led, secret=SECRET)
    guarded = sk.effect(CHARGE, key="order-4471-charge")(charge)
    guarded(customer="c1", amount_cents=4000)

    expected = positional_key(Coordinate("global", b"order-4471-charge"), "payment.charge")
    assert seen["key"] == expected


def test_l2_crash_in_window_redispatches_and_dedups() -> None:
    led = SqliteLedger()
    provider = KeyedProvider()
    key = _key()
    fp = fingerprint(CHARGE, {"customer": "c1", "amount_cents": 4000}, secret=SECRET)

    # First attempt crashes in the window: claim + mark, the provider got the call
    # (with the key), but the result was never recorded.
    led.claim(key, "run-1", "payment.charge", fp, provider_dedup=True)
    led.mark_executing(key)
    provider.charge(customer="c1", amount_cents=4000, idempotency_key=key)
    assert provider.count == 1

    # Restart: recovery blesses an L2 leftover as INTENDED (re-claimable, not AMBIGUOUS).
    assert led.recover() == []
    assert led.state_of(key) is EffectState.INTENDED

    # Re-dispatch: the tool reads current_key() → same key → the provider dedups.
    def charge() -> dict[str, str]:
        return provider.charge(customer="c1", amount_cents=4000, idempotency_key=current_key())

    out = settle(
        led,
        key=key,
        scope="run-1",
        tool="payment.charge",
        fingerprint=fp,
        fn=charge,
        provider_key_param="idempotency_key",
    )
    assert out == {"charge_id": "ch_1"}
    assert provider.count == 1  # exactly one charge at the provider
    assert led.state_of(key) is EffectState.SUCCEEDED


def test_l0_crash_in_window_still_ambiguous() -> None:
    # Contrast: without a provider key (L0), the same crash surfaces AMBIGUOUS.
    led = SqliteLedger()
    key = _key()
    led.claim(key, "run-1", "payment.charge", "fp", provider_dedup=False)
    led.mark_executing(key)
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS
