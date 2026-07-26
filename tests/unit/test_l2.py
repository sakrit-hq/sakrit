# SPDX-License-Identifier: Apache-2.0
"""L2 provider-key injection: a keyed provider deduplicates a re-dispatch."""

from collections.abc import Callable

from sakrit import Sakrit, SqliteLedger
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
    provider_key_param="idempotency_key",
)


def _provider() -> tuple[Callable[..., dict[str, str]], dict[str, dict[str, str]]]:
    """A Stripe-style provider that dedups on the idempotency key."""
    delivered: dict[str, dict[str, str]] = {}

    def charge(customer: str, amount_cents: int, idempotency_key: str) -> dict[str, str]:
        if idempotency_key in delivered:
            return delivered[idempotency_key]
        charge_id = f"ch_{len(delivered) + 1}"
        record = {"charge_id": charge_id}
        delivered[idempotency_key] = record
        return record

    return charge, delivered


def _key(scope: str = "run-1", site: str = "charge") -> str:
    return positional_key(FakeAdapter(scope).at(site), "payment.charge")


def test_guard_injects_provider_key() -> None:
    led = SqliteLedger()
    seen: dict[str, object] = {}

    def charge(customer: str, amount_cents: int, idempotency_key: str) -> dict[str, str]:
        seen["idempotency_key"] = idempotency_key
        return {"charge_id": "ch_1"}

    sk = Sakrit(led, secret=SECRET)
    guarded = sk.effect(CHARGE, key="order-4471-charge")(charge)
    # idempotency_key is injected by Sakrit, not passed by the caller — the
    # signature-preserving decorator can't express that (contextvar injection,
    # Fable Q43, is the clean fix; deferred).
    guarded(customer="c1", amount_cents=4000)  # type: ignore[call-arg]

    # Sakrit derived and injected its key (a business-key coordinate) as the
    # provider idempotency key.
    expected = positional_key(Coordinate("global", b"order-4471-charge"), "payment.charge")
    assert seen["idempotency_key"] == expected


def test_l2_crash_in_window_redispatches_and_dedups() -> None:
    led = SqliteLedger()
    charge, delivered = _provider()
    key = _key()
    fp = fingerprint(CHARGE, {"customer": "c1", "amount_cents": 4000}, secret=SECRET)

    # First attempt crashes in the window: claim + mark_executing + the provider
    # call happened, but the result was never recorded.
    led.claim(key, "run-1", "payment.charge", fp, provider_dedup=True)
    led.mark_executing(key)
    charge(customer="c1", amount_cents=4000, idempotency_key=key)  # provider got it
    assert len(delivered) == 1

    # Restart: recovery leaves an L2 leftover re-claimable (not AMBIGUOUS).
    assert led.recover() == []
    assert led.state_of(key) is EffectState.CLAIMED

    # Re-dispatch with the same injected key → the provider dedups → one charge.
    out = settle(
        led,
        key=key,
        scope="run-1",
        tool="payment.charge",
        fingerprint=fp,
        fn=charge,
        kwargs={"customer": "c1", "amount_cents": 4000},
        provider_key_param="idempotency_key",
    )
    assert out == {"charge_id": "ch_1"}
    assert len(delivered) == 1  # exactly one charge at the provider
    assert led.state_of(key) is EffectState.SUCCEEDED


def test_l0_crash_in_window_still_ambiguous() -> None:
    # Contrast: without a provider key (L0), the same crash surfaces AMBIGUOUS.
    led = SqliteLedger()
    key = _key()
    led.claim(key, "run-1", "payment.charge", "fp", provider_dedup=False)
    led.mark_executing(key)
    assert led.recover() == [key]
    assert led.state_of(key) is EffectState.AMBIGUOUS
