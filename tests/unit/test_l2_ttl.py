# SPDX-License-Identifier: Apache-2.0
"""P1-5: L2 crash recovery has a TTL horizon. A provider only remembers an idempotency
key for so long; beyond that, a re-dispatch would not dedup — a silent duplicate. So an
L2 leftover older than its declared provider_ttl_s surfaces AMBIGUOUS instead of being
re-claimed. (design §6: "within provider TTL; AMBIGUOUS beyond".)"""

from datetime import datetime, timedelta, timezone

from sakrit import Sakrit, SqliteLedger
from sakrit.core import ArgClass, EffectDecl, EffectState

SECRET = b"deployment-secret"


def _age_row(led: SqliteLedger, key: str, delta: timedelta) -> None:
    old = (datetime.now(timezone.utc) - delta).isoformat()
    led.conn.execute("UPDATE effects SET created_at = ? WHERE key = ?", (old, key))


def test_l2_leftover_within_ttl_is_reclaimable() -> None:
    led = SqliteLedger()
    led.claim("k", "s", "t", "fp", provider_dedup=True, provider_ttl_s=3600)
    led.mark_executing("k")
    assert led.recover() == []
    assert led.state_of("k") is EffectState.INTENDED  # provider still dedups


def test_l2_leftover_beyond_ttl_surfaces_ambiguous() -> None:
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    led.claim("k", "s", "t", "fp", provider_dedup=True, provider_ttl_s=3600)  # 1h
    led.mark_executing("k")
    _age_row(led, "k", timedelta(hours=2))  # past the horizon
    assert led.recover() == ["k"]
    assert led.state_of("k") is EffectState.AMBIGUOUS  # provider has forgotten the key
    assert told == ["k"]  # and it is told, never silent


def test_l2_leftover_unbounded_ttl_stays_reclaimable() -> None:
    # provider_ttl_s=None → unbounded (the prior, over-optimistic behavior is preserved
    # for anyone who has not declared a TTL).
    led = SqliteLedger()
    led.claim("k", "s", "t", "fp", provider_dedup=True)  # no TTL
    led.mark_executing("k")
    _age_row(led, "k", timedelta(days=30))
    assert led.recover() == []
    assert led.state_of("k") is EffectState.INTENDED


def test_decl_ttl_is_captured_at_claim() -> None:
    # End-to-end: the TTL flows decl → guard → settle → claim and lands on the row.
    led = SqliteLedger()
    decl = EffectDecl(
        "pay.charge",
        {"amt": ArgClass.IDENTITY},
        provider_key_param="idk",
        provider_ttl_s=86400,
    )
    sk = Sakrit(led, secret=SECRET)

    def charge(amt: int) -> dict[str, str]:
        return {"id": "c1"}

    guarded = sk.effect(decl, key="order-1")(charge)
    guarded(amt=100)
    stored = led.conn.execute("SELECT provider_ttl_s FROM effects").fetchone()[0]
    assert stored == 86400
