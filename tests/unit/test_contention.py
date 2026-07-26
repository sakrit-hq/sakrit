# SPDX-License-Identifier: Apache-2.0
"""Multi-worker contention protocol: leases, fencing, takeover-by-ladder, late evidence.

Verified deterministically (owner ids, clock, and tokens are controlled). Wiring
this into a concurrent settle loop over Postgres, plus true-concurrency chaos, is
the remaining Act III-M work.
"""

from sakrit.core import EffectState, SqliteLedger
from sakrit.core.ledger import ClaimKind

LEASE = 30.0


def _led() -> SqliteLedger:
    return SqliteLedger()  # :memory: — no flock, fine for the protocol test


def test_fresh_claim_acquires_lease_and_token() -> None:
    led = _led()
    claim = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    assert claim.kind is ClaimKind.PROCEED
    assert claim.fencing_token == 1


def test_live_lease_is_busy_for_another_worker() -> None:
    led = _led()
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    # B claims while A's lease is live (now < 100+30).
    b = led.claim_leased("k", "s", "t", "fp", owner="B", now=110.0, lease_seconds=LEASE)
    assert b.kind is ClaimKind.BUSY


def test_expired_lease_takeover_bumps_the_fence() -> None:
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    # A stalls; its lease expires; B takes over after 100+30.
    b = led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)
    assert b.kind is ClaimKind.PROCEED
    assert b.fencing_token == a.fencing_token + 1


def test_fencing_rejects_a_zombie_write() -> None:
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)  # takeover
    # A wakes up and tries to record with its stale token — rejected.
    assert led.fence("k", a.fencing_token, EffectState.SUCCEEDED, result="from-A") is False
    # B's current-token write applies.
    b_token = a.fencing_token + 1
    assert led.fence("k", b_token, EffectState.SUCCEEDED, result="from-B") is True
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_l0_expired_takeover_is_forbidden_and_surfaces_ambiguous() -> None:
    led = _led()
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", 1, EffectState.EXECUTING)  # A marked executing, then stalled
    # B takes over an EXECUTING L0 row (no provider dedup, not reconcilable).
    b = led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)
    assert b.kind is ClaimKind.AMBIGUOUS
    assert led.state_of("k") is EffectState.AMBIGUOUS


def test_l2_expired_takeover_is_allowed() -> None:
    led = _led()
    led.claim_leased(
        "k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE, provider_dedup=True
    )
    led.fence("k", 1, EffectState.EXECUTING)
    b = led.claim_leased(
        "k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE, provider_dedup=True
    )
    assert b.kind is ClaimKind.PROCEED  # L2 re-dispatch is safe (provider dedups)


def test_heartbeat_extends_a_live_lease() -> None:
    led = _led()
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    assert led.heartbeat("k", "A", now=125.0, lease_seconds=LEASE) is True
    # After the heartbeat, B at t=140 (< 125+30) still sees a live lease.
    b = led.claim_leased("k", "s", "t", "fp", owner="B", now=140.0, lease_seconds=LEASE)
    assert b.kind is ClaimKind.BUSY


def test_late_evidence_self_heals_ambiguity() -> None:
    led = _led()
    led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", 1, EffectState.EXECUTING)
    led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)  # → AMBIGUOUS
    assert led.state_of("k") is EffectState.AMBIGUOUS
    # A (the GC-paused worker) returns knowing it succeeded → accepted as evidence.
    assert led.accept_late_evidence("k", result="A-succeeded") is True
    assert led.state_of("k") is EffectState.SUCCEEDED
    # A second late-evidence write is a no-op (row no longer AMBIGUOUS).
    assert led.accept_late_evidence("k", result="again") is False
