# SPDX-License-Identifier: Apache-2.0
"""Multi-worker contention protocol: leases, fencing, takeover-by-ladder, late evidence.

Verified deterministically (owner ids, clock, and tokens are controlled). Wiring
this into a concurrent settle loop over Postgres, plus true-concurrency chaos, is
the remaining Act III-M work.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sakrit.core import EffectState, SqliteLedger
from sakrit.core.errors import AmbiguousOutcome
from sakrit.core.leased import settle_leased, settle_leased_async
from sakrit.core.ledger import ClaimKind, Replayed
from sakrit.core.reconcile import Reconciliation

LEASE = 30.0


def _age_row(led: SqliteLedger, key: str, delta: timedelta) -> None:
    old = (datetime.now(timezone.utc) - delta).isoformat()
    led.conn.execute("UPDATE effects SET created_at = ? WHERE key = ?", (old, key))


def _stale_l1_row(led: SqliteLedger) -> None:
    """An L1 (reconcilable, non-deduplicating) row left EXECUTING by a dead owner
    whose lease has long expired against the real clock."""
    led.claim_leased(
        "k", "s", "t", "fp", owner="A", now=1.0, lease_seconds=LEASE, reconcilable=True
    )
    led.fence("k", 1, EffectState.EXECUTING)


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


def test_fence_succeeded_unserializable_records_marker_not_raises() -> None:
    # P1-4: Q6 in the leased path — an unserializable result must not raise after the
    # effect ran (which would leave a *succeeded* row EXECUTING, re-fired on takeover).
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", a.fencing_token, EffectState.EXECUTING)

    class Weird:  # not JSON-serializable
        pass

    assert led.fence("k", a.fencing_token, EffectState.SUCCEEDED, result=Weird()) is True
    assert led.state_of("k") is EffectState.SUCCEEDED  # recorded, not left EXECUTING
    # Replay returns the marker, never a raise.
    replay = led.claim_leased("k", "s", "t", "fp", owner="B", now=110.0, lease_seconds=LEASE)
    assert replay.kind is ClaimKind.REPLAY
    assert isinstance(replay.result, Replayed)


def test_settle_leased_aborts_dispatch_when_lease_lost_before_fence() -> None:
    # P1-3: if fence(EXECUTING) no-ops (our token went stale — a peer took over between
    # claim and fence), settle_leased must NOT dispatch; it re-resolves and replays.
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "A-result"

    class PeerStealsBeforeFence(SqliteLedger):
        stolen = False

        def fence(self, key, token, state, *, result=None):  # type: ignore[no-untyped-def]
            if state is EffectState.EXECUTING and not self.stolen:
                self.stolen = True
                # A peer takes over and completes the effect before our fence lands.
                self.conn.execute(
                    "UPDATE effects SET fencing_token = fencing_token + 1, state = ?, "
                    "result = ?, settled_at = ? WHERE key = ?",
                    (EffectState.SUCCEEDED.value, json.dumps("peer-result"), 0, key),
                )
            return super().fence(key, token, state, result=result)

    led = PeerStealsBeforeFence()
    out = settle_leased(led, key="k", scope="s", tool="t", fingerprint="fp", fn=fn)
    assert out == "peer-result"  # we replayed the peer's result
    assert calls == []  # our fn never dispatched — exactly-once held


def test_settle_leased_times_out_if_lease_lost_and_never_resolves() -> None:
    # Belt: if the fence is lost but no peer result ever appears, we surface, not hang.
    class AlwaysLosesFence(SqliteLedger):
        def fence(self, key, token, state, *, result=None):  # type: ignore[no-untyped-def]
            if state is EffectState.EXECUTING:
                # Bump the token so our fence is forever stale, but record nothing.
                self.conn.execute(
                    "UPDATE effects SET fencing_token = fencing_token + 1 WHERE key = ?", (key,)
                )
            return super().fence(key, token, state, result=result)

    led = AlwaysLosesFence()
    try:
        settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=lambda: 1,
            wait_timeout=0.05,
            poll=0.01,
        )
        raise AssertionError("expected AmbiguousOutcome")
    except AmbiguousOutcome:
        pass


def test_settle_leased_reconciles_settled_takeover_no_redispatch() -> None:
    # P1-2: taking over a mid-flight L1 row must ask "did it happen?" — SETTLED adopts
    # the provider's record and never re-fires the effect.
    led = _led()
    _stale_l1_row(led)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "re-sent"

    out = settle_leased(
        led,
        key="k",
        scope="s",
        tool="t",
        fingerprint="fp",
        fn=fn,
        reconcilable=True,
        reconcile=lambda k: Reconciliation.settled("provider-record"),
    )
    assert out == "provider-record"  # adopted the provider's record
    assert calls == []  # the effect already happened → never re-dispatched
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_settle_leased_reconciles_absent_retry_redispatches_once() -> None:
    # ABSENT + on_absent="retry": the effect provably did not land → re-dispatch exactly once.
    led = _led()
    _stale_l1_row(led)
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "sent-now"

    out = settle_leased(
        led,
        key="k",
        scope="s",
        tool="t",
        fingerprint="fp",
        fn=fn,
        reconcilable=True,
        reconcile=lambda k: Reconciliation.absent(),
        on_absent="retry",
    )
    assert out == "sent-now"
    assert calls == [1]
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_settle_leased_reconciles_absent_surface_never_refires() -> None:
    # ABSENT + surface (the default — a lagging read may lie): surface, never re-fire.
    led = _led()
    _stale_l1_row(led)
    calls: list[int] = []

    with pytest.raises(AmbiguousOutcome):
        settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=lambda: calls.append(1),
            reconcilable=True,
            reconcile=lambda k: Reconciliation.absent(),
        )
    assert calls == []
    assert led.state_of("k") is EffectState.AMBIGUOUS


def test_settle_leased_reconcile_unknown_surfaces() -> None:
    led = _led()
    _stale_l1_row(led)
    with pytest.raises(AmbiguousOutcome):
        settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=lambda: 1,
            reconcilable=True,
            reconcile=lambda k: Reconciliation.unknown(),
        )
    assert led.state_of("k") is EffectState.AMBIGUOUS


def test_settle_leased_reconcilable_takeover_without_reconcile_fn_surfaces() -> None:
    # No reconcile function ⇒ we cannot prove the effect didn't land → surface, never
    # blind re-dispatch (the P1-2 failure mode).
    led = _led()
    _stale_l1_row(led)
    calls: list[int] = []
    with pytest.raises(AmbiguousOutcome):
        settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=lambda: calls.append(1),
            reconcilable=True,
        )
    assert calls == []
    assert led.state_of("k") is EffectState.AMBIGUOUS


def test_settle_leased_heartbeats_during_slow_dispatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # P3-5: a dispatch slower than the lease must renew the lease, or a live owner is
    # presumed dead and a peer double-dispatches. Count the renewals during a slow fn.
    import time as _time

    beats: list[int] = []

    class CountingLedger(SqliteLedger):
        def heartbeat(self, key, owner, lease_seconds, now=None):  # type: ignore[no-untyped-def]
            beats.append(1)
            return super().heartbeat(key, owner, lease_seconds, now=now)

    led = CountingLedger(str(tmp_path / "led.db"), multi_worker=True)

    def slow() -> str:
        _time.sleep(0.15)
        return "ok"

    out = settle_leased(
        led,
        key="k",
        scope="s",
        tool="t",
        fingerprint="fp",
        fn=slow,
        lease_seconds=0.09,
        heartbeat_interval=0.03,
    )
    assert out == "ok"
    assert len(beats) >= 1  # the lease was renewed while the slow effect ran
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_settle_leased_no_heartbeat_thread_in_single_worker() -> None:
    # Single-worker has no lease contention and a thread-bound connection — no beat.
    beats: list[int] = []

    class CountingLedger(SqliteLedger):
        def heartbeat(self, key, owner, lease_seconds, now=None):  # type: ignore[no-untyped-def]
            beats.append(1)
            return super().heartbeat(key, owner, lease_seconds, now=now)

    led = CountingLedger()  # single-worker, :memory:
    out = settle_leased(led, key="k", scope="s", tool="t", fingerprint="fp", fn=lambda: "x")
    assert out == "x"
    assert beats == []


def test_fence_cannot_overwrite_a_terminal_success() -> None:
    # P3-6a: SUCCEEDED is write-once — even the current-token holder can't un-settle it.
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", a.fencing_token, EffectState.EXECUTING)
    assert led.fence("k", a.fencing_token, EffectState.SUCCEEDED, result="done") is True
    # Same valid token, but the row is now terminal → the FAILED write is rejected.
    assert led.fence("k", a.fencing_token, EffectState.FAILED) is False
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_forbidden_takeover_bumps_fence_and_row_is_write_once() -> None:
    # P3-6b: the L0 forbidden takeover bumps the token (so a returning zombie is fenced),
    # and P3-6a makes the resulting AMBIGUOUS row write-once from any token.
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", a.fencing_token, EffectState.EXECUTING)
    led.claim_leased("k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE)  # forbidden
    assert led.state_of("k") is EffectState.AMBIGUOUS

    current = led.conn.execute("SELECT fencing_token FROM effects WHERE key = 'k'").fetchone()[0]
    assert current == a.fencing_token + 1  # bumped

    # Zombie A returns with its stale token → rejected (stale token AND write-once).
    assert led.fence("k", a.fencing_token, EffectState.SUCCEEDED, result="from-A") is False
    # Even the current token cannot un-terminal the AMBIGUOUS row.
    assert led.fence("k", current, EffectState.EXECUTING) is False
    assert led.state_of("k") is EffectState.AMBIGUOUS


def test_leased_l2_takeover_within_ttl_redispatches() -> None:
    # An L2 EXECUTING leftover taken over within the provider key-TTL → re-dispatch is
    # safe (the provider still dedups) → PROCEED.
    led = _led()
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=100.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    led.fence("k", 1, EffectState.EXECUTING)
    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    assert b.kind is ClaimKind.PROCEED


def test_leased_l2_takeover_beyond_ttl_surfaces_ambiguous() -> None:
    # P1-5 (leased): past the TTL the provider has forgotten the key → a re-dispatch would
    # NOT dedup → surface, don't silently duplicate. Token is bumped (zombie fenced).
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=100.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    led.fence("k", 1, EffectState.EXECUTING)
    _age_row(led, "k", timedelta(hours=2))  # past the 1h TTL
    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    assert b.kind is ClaimKind.AMBIGUOUS
    assert led.state_of("k") is EffectState.AMBIGUOUS
    assert told == ["k"]
    token = led.conn.execute("SELECT fencing_token FROM effects WHERE key = 'k'").fetchone()[0]
    assert token == 2  # bumped, so a returning zombie is fenced


def test_leased_l2r_takeover_beyond_ttl_still_reconciles() -> None:
    # A reconcilable row (L2R) past the TTL still RECONCILES — reconcile is the authority
    # ("did it happen?"), so the TTL does not force AMBIGUOUS here.
    led = _led()
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=100.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
        reconcilable=True,
    )
    led.fence("k", 1, EffectState.EXECUTING)
    _age_row(led, "k", timedelta(hours=2))
    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
        reconcilable=True,
    )
    assert b.kind is ClaimKind.RECONCILE


def test_leased_l2_claimed_leftover_beyond_ttl_still_proceeds() -> None:
    # A CLAIMED leftover (crash BEFORE dispatch) never ran — re-dispatch is safe regardless
    # of TTL, so it PROCEEDs even past the horizon.
    led = _led()
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=100.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )  # left CLAIMED (not fenced to EXECUTING)
    _age_row(led, "k", timedelta(hours=2))
    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    assert b.kind is ClaimKind.PROCEED


def test_leased_l2_unbounded_ttl_takeover_proceeds() -> None:
    # provider_ttl_s=None → unbounded → the prior behavior (always re-dispatch) is preserved.
    led = _led()
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=100.0,
        lease_seconds=LEASE,
        provider_dedup=True,
    )
    led.fence("k", 1, EffectState.EXECUTING)
    _age_row(led, "k", timedelta(days=30))
    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
    )
    assert b.kind is ClaimKind.PROCEED


def test_settle_leased_async_awaits_and_records_after() -> None:
    led = _led()
    order: list[str] = []

    async def effect() -> str:
        order.append("effect-ran")
        return "done"

    out = asyncio.run(
        settle_leased_async(led, key="k", scope="s", tool="t", fingerprint="fp", fn=effect)
    )
    assert out == "done"
    assert order == ["effect-ran"]
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_settle_leased_async_reconcile_settled_no_redispatch() -> None:
    led = _led()
    _stale_l1_row(led)  # L1 EXECUTING, lease long expired
    calls: list[int] = []

    async def fn() -> str:
        calls.append(1)
        return "re-sent"

    out = asyncio.run(
        settle_leased_async(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=fn,
            reconcilable=True,
            reconcile=lambda k: Reconciliation.settled("provider-record"),
        )
    )
    assert out == "provider-record"
    assert calls == []  # already happened → adopted, never re-awaited
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_settle_leased_async_clean_failure_records_failed() -> None:
    led = _led()

    class Rejected(Exception):
        pass

    async def fn() -> str:
        raise Rejected("provider 400 — nothing done")

    with pytest.raises(Rejected):
        asyncio.run(
            settle_leased_async(
                led,
                key="k",
                scope="s",
                tool="t",
                fingerprint="fp",
                fn=fn,
                clean_failures=(Rejected,),
            )
        )
    assert led.state_of("k") is EffectState.FAILED


def test_settle_leased_async_heartbeats_during_slow_dispatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    beats: list[int] = []

    class CountingLedger(SqliteLedger):
        def heartbeat(self, key, owner, lease_seconds, now=None):  # type: ignore[no-untyped-def]
            beats.append(1)
            return super().heartbeat(key, owner, lease_seconds, now=now)

    led = CountingLedger(str(tmp_path / "l.db"), multi_worker=True)

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "ok"

    out = asyncio.run(
        settle_leased_async(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=slow,
            lease_seconds=0.09,
            heartbeat_interval=0.03,
        )
    )
    assert out == "ok"
    assert len(beats) >= 1  # the lease was renewed on the event loop while the effect ran
    led.close()


def test_v5a_takeover_preserves_executing_across_crashed_reconcile() -> None:
    # V-5a: A (L1) dispatched, effect landed, A died (EXECUTING). B takes over → RECONCILE,
    # and the row MUST stay EXECUTING. B dies before reconciling; C takes over → still
    # RECONCILE, never a blind PROCEED on the landed effect.
    led = _led()
    led.claim_leased(
        "k", "s", "t", "fp", owner="A", now=1.0, lease_seconds=LEASE, reconcilable=True
    )
    led.fence("k", 1, EffectState.EXECUTING)

    b = led.claim_leased(
        "k", "s", "t", "fp", owner="B", now=200.0, lease_seconds=LEASE, reconcilable=True
    )
    assert b.kind is ClaimKind.RECONCILE
    assert led.state_of("k") is EffectState.EXECUTING  # preserved, NOT downgraded to CLAIMED

    c = led.claim_leased(
        "k", "s", "t", "fp", owner="C", now=400.0, lease_seconds=LEASE, reconcilable=True
    )
    assert c.kind is ClaimKind.RECONCILE  # not a blind PROCEED
    assert led.state_of("k") is EffectState.EXECUTING


def test_v5b_l2_ttl_horizon_survives_a_crashed_takeover() -> None:
    # V-5b: the L2-TTL horizon keys on state==EXECUTING; a crashed takeover must not erase
    # it to CLAIMED (which would let a later past-TTL taker PROCEED — an un-dedupable dup).
    told: list[str] = []
    led = SqliteLedger(on_ambiguous=told.append)
    led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="A",
        now=1.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    led.fence("k", 1, EffectState.EXECUTING)

    b = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="B",
        now=200.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    assert b.kind is ClaimKind.PROCEED  # within TTL → re-dispatch
    assert led.state_of("k") is EffectState.EXECUTING  # preserved

    _age_row(led, "k", timedelta(hours=2))  # B dies; the row ages past TTL
    c = led.claim_leased(
        "k",
        "s",
        "t",
        "fp",
        owner="C",
        now=400.0,
        lease_seconds=LEASE,
        provider_dedup=True,
        provider_ttl_s=3600,
    )
    assert c.kind is ClaimKind.AMBIGUOUS  # TTL horizon survived the crashed takeover
    assert told == ["k"]


def test_v5c_transient_reconcile_error_then_retry_still_reconciles() -> None:
    # V-5c: if reconcile raises (a transient network error), the row must stay EXECUTING so
    # the retry reconciles again — not a blind PROCEED.
    led = _led()
    _stale_l1_row(led)  # L1 EXECUTING, expired lease

    def boom(key: str) -> Reconciliation:
        raise RuntimeError("transient network error")

    with pytest.raises(RuntimeError):
        settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=lambda: 1,
            reconcilable=True,
            reconcile=boom,
        )
    assert led.state_of("k") is EffectState.EXECUTING  # evidence preserved across the raise

    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "re-sent"

    out = settle_leased(
        led,
        key="k",
        scope="s",
        tool="t",
        fingerprint="fp",
        fn=fn,
        reconcilable=True,
        reconcile=lambda k: Reconciliation.settled("provider-record"),
    )
    assert out == "provider-record"  # the retry reconciled → adopted
    assert calls == []  # never blind re-dispatched


def test_heartbeat_error_does_not_kill_the_beat_or_dispatch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # V-8: a heartbeat that raises must not die silently (the lease would expire → a live
    # owner presumed dead). The beat logs and keeps going; the dispatch still completes.
    class FlakyHeartbeat(SqliteLedger):
        raised = False

        def heartbeat(self, key, owner, lease_seconds, now=None):  # type: ignore[no-untyped-def]
            if not self.raised:
                self.raised = True
                raise RuntimeError("transient DB blip")
            return super().heartbeat(key, owner, lease_seconds, now=now)

    led = FlakyHeartbeat(str(tmp_path / "l.db"), multi_worker=True)

    def slow() -> str:
        time.sleep(0.12)
        return "ok"

    with caplog.at_level(logging.WARNING, logger="sakrit"):
        out = settle_leased(
            led,
            key="k",
            scope="s",
            tool="t",
            fingerprint="fp",
            fn=slow,
            lease_seconds=0.09,
            heartbeat_interval=0.03,
        )
    assert out == "ok"
    assert led.state_of("k") is EffectState.SUCCEEDED  # dispatch completed despite the blip
    assert "heartbeat" in caplog.text  # the error was surfaced, not silent
    led.close()


def test_settle_leased_late_evidence_heals_a_mid_flight_ambiguation() -> None:
    # V-7: the heartbeat failed and a forbidden takeover ambiguated our live row while we
    # ran. Our terminal fence is rejected — but we KNOW the effect happened, so
    # accept_late_evidence heals the spurious AMBIGUOUS to SUCCEEDED.
    led = _led()

    def fn() -> str:
        led.conn.execute(
            "UPDATE effects SET state = ? WHERE key = ?", (EffectState.AMBIGUOUS.value, "k")
        )
        return "did-happen"

    out = settle_leased(led, key="k", scope="s", tool="t", fingerprint="fp", fn=fn)
    assert out == "did-happen"
    assert led.state_of("k") is EffectState.SUCCEEDED  # healed by the returning owner


def test_settle_leased_adopts_peer_result_when_terminal_fence_rejected() -> None:
    # V-7: a peer settled the row (SUCCEEDED) before our terminal fence → adopt the
    # recorded truth, not our own copy.
    led = _led()

    def fn() -> str:
        led.conn.execute(
            "UPDATE effects SET state = ?, result = ?, fencing_token = fencing_token + 1 "
            "WHERE key = ?",
            (EffectState.SUCCEEDED.value, json.dumps("peer-result"), "k"),
        )
        return "our-result"

    out = settle_leased(led, key="k", scope="s", tool="t", fingerprint="fp", fn=fn)
    assert out == "peer-result"  # adopted the peer's recorded result
    assert led.state_of("k") is EffectState.SUCCEEDED


def test_async_heartbeat_warns_when_the_effect_starves_the_loop(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # V-9: the async heartbeat shares the event loop with the effect. A long await-less
    # stretch starves renewal; a renewal that then runs overdue must say so (it can't
    # prevent a same-loop total starvation — that's the tool's contract — but it surfaces).
    led = SqliteLedger(str(tmp_path / "l.db"), multi_worker=True)

    async def partially_starving() -> str:
        await asyncio.sleep(0)  # let the heartbeat task initialize (record its clock)
        time.sleep(0.2)  # block the loop for 0.2s (>> lease 0.05) — starves renewal
        await asyncio.sleep(0)  # yield again → the heartbeat runs and sees it's overdue
        return "ok"

    with caplog.at_level(logging.WARNING, logger="sakrit"):
        out = asyncio.run(
            settle_leased_async(
                led,
                key="k",
                scope="s",
                tool="t",
                fingerprint="fp",
                fn=partially_starving,
                lease_seconds=0.05,
                heartbeat_interval=0.02,
            )
        )
    assert out == "ok"
    assert "late" in caplog.text  # the starvation was surfaced
    led.close()


def test_clean_failed_fence_releases_the_lease() -> None:
    # P3-10(b): a clean-FAILED row is re-claimable, but a live lease would make a peer
    # BUSY-wait a whole lease before re-claiming. The terminal fence clears the lease so
    # the next worker takes over immediately.
    led = _led()
    a = led.claim_leased("k", "s", "t", "fp", owner="A", now=100.0, lease_seconds=LEASE)
    led.fence("k", a.fencing_token, EffectState.EXECUTING)
    led.fence("k", a.fencing_token, EffectState.FAILED)  # clean failure
    assert led.state_of("k") is EffectState.FAILED

    # B claims *while A's lease would still be live* (now=110 < 100+30) — but the terminal
    # fence released it, so B re-claims immediately instead of BUSY-waiting.
    b = led.claim_leased("k", "s", "t", "fp", owner="B", now=110.0, lease_seconds=LEASE)
    assert b.kind is ClaimKind.PROCEED


def test_unexpected_claim_kind_is_refused_not_executed() -> None:
    # P3-10(a): a leased-only kind must never silently execute on the sync settle path.
    from sakrit.core import SakritError, settle
    from sakrit.core.ledger import Claim, ClaimKind

    class BusyLedger(SqliteLedger):
        def claim(self, *a, **k):  # type: ignore[no-untyped-def]
            return Claim(ClaimKind.BUSY)  # a kind the sync path must not proceed on

    led = BusyLedger()
    with pytest.raises(SakritError, match="unexpected claim kind"):
        settle(led, key="k", scope="s", tool="t", fingerprint="fp", fn=lambda: 1)
