# SPDX-License-Identifier: Apache-2.0
"""Multi-worker contention protocol: leases, fencing, takeover-by-ladder, late evidence.

Verified deterministically (owner ids, clock, and tokens are controlled). Wiring
this into a concurrent settle loop over Postgres, plus true-concurrency chaos, is
the remaining Act III-M work.
"""

import json

import pytest

from sakrit.core import EffectState, SqliteLedger
from sakrit.core.errors import AmbiguousOutcome
from sakrit.core.leased import settle_leased
from sakrit.core.ledger import ClaimKind, Replayed
from sakrit.core.reconcile import Reconciliation

LEASE = 30.0


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
