# SPDX-License-Identifier: Apache-2.0
"""#21 metrics surface: a per-process tally of terminal outcomes — settled (the headline
"total effects settled"), replayed, ambiguous, failed — across the single-worker and leased
paths, updated at the same chokepoints as the on_replay/on_ambiguous hooks."""

from pathlib import Path

import pytest

from sakrit import EffectDecl, Metrics, Sakrit, SqliteLedger
from sakrit.core import ArgClass, Coordinate, EffectState, positional_key

SECRET = b"deployment-secret"
DECL = EffectDecl("mail.send", {"to": ArgClass.IDENTITY})


def test_settled_and_replayed_counts_single_worker() -> None:
    m = Metrics()
    sk = Sakrit(SqliteLedger(":memory:", metrics=m), secret=SECRET)
    sk.guard(DECL, lambda to: "ok", kwargs={"to": "a@x.com"}, key="k1")
    assert m.settled == 1 and m.snapshot() == {
        "settled": 1,
        "replayed": 0,
        "ambiguous": 0,
        "failed": 0,
    }
    # A second guard of the same key replays (does not re-fire) → replayed, settled unchanged.
    sk.guard(DECL, lambda to: "ok", kwargs={"to": "a@x.com"}, key="k1")
    assert m.snapshot() == {"settled": 1, "replayed": 1, "ambiguous": 0, "failed": 0}


def test_failed_count_on_declared_clean_failure() -> None:
    class Rejected(Exception):
        pass

    decl = EffectDecl("mail.send", {"to": ArgClass.IDENTITY}, clean_failures=(Rejected,))
    m = Metrics()
    sk = Sakrit(SqliteLedger(":memory:", metrics=m), secret=SECRET)

    def boom(to: str) -> str:
        raise Rejected("provider 4xx: rejected, nothing done")

    with pytest.raises(Rejected):
        sk.guard(decl, boom, kwargs={"to": "a@x.com"}, key="k1")
    assert m.snapshot()["failed"] == 1 and m.settled == 0


def test_ambiguous_count_on_l0_crash_recovery() -> None:
    m = Metrics()
    led = SqliteLedger(":memory:", metrics=m)
    key = positional_key(Coordinate("run-1", b"send"), "mail.send")
    led.claim(key, "run-1", "mail.send", "fp")
    led.mark_executing(key)
    assert led.recover() == [key]  # L0 crash in the window → surfaced AMBIGUOUS
    assert led.state_of(key) is EffectState.AMBIGUOUS
    assert m.snapshot()["ambiguous"] == 1


def test_settled_count_on_the_leased_path(tmp_path: Path) -> None:
    # The leased success write goes through fence(SUCCEEDED), a different chokepoint than the
    # single-worker record_success — the metric must cover it too (no double-count).
    m = Metrics()
    led = SqliteLedger(tmp_path / "l.db", multi_worker=True, metrics=m)
    try:
        sk = Sakrit(led, secret=SECRET)
        assert sk._leased is True
        sk.guard(DECL, lambda to: "ok", kwargs={"to": "a@x.com"}, key="k1")
        assert m.settled == 1
        sk.guard(DECL, lambda to: "ok", kwargs={"to": "a@x.com"}, key="k1")  # replay
        assert m.snapshot() == {"settled": 1, "replayed": 1, "ambiguous": 0, "failed": 0}
    finally:
        led.close()


def test_no_metrics_object_is_a_no_op() -> None:
    # The metrics param is optional; a ledger with none must behave identically (and not crash).
    sk = Sakrit(SqliteLedger(":memory:"), secret=SECRET)
    assert sk.guard(DECL, lambda to: "ok", kwargs={"to": "a@x.com"}, key="k1") == "ok"


def test_snapshot_is_a_copy() -> None:
    m = Metrics()
    snap = m.snapshot()
    snap["settled"] = 999  # mutating the snapshot must not corrupt the live counters
    assert m.snapshot()["settled"] == 0


# --- C-2: a raising Metrics override must never break the ledger ----------------------
class _RaisingMetrics(Metrics):
    def record(self, event: str) -> None:  # the obvious statsd/OTel push, but the backend is down
        raise ConnectionError("telemetry is down")


def test_raising_metrics_does_not_fail_a_settled_effect() -> None:
    # C-2 probe B: the effect ran and is durably SUCCEEDED; the caller must NOT see the error.
    led = SqliteLedger(":memory:", metrics=_RaisingMetrics())
    fired: list[str] = []

    def send(to: str) -> str:
        fired.append(to)
        return "ok"

    sk = Sakrit(led, secret=SECRET)
    assert sk.guard(DECL, send, kwargs={"to": "a@x.com"}, key="k1") == "ok"  # no ConnectionError
    assert fired == ["a@x.com"]
    succeeded = list(led.keys_in(EffectState.SUCCEEDED))
    assert len(succeeded) == 1 and led.state_of(succeeded[0]) is EffectState.SUCCEEDED


def test_raising_metrics_does_not_roll_back_the_ambiguous_transition(tmp_path: Path) -> None:
    # C-2 probe A (the serious one): _tell_ambiguous runs inside claim_leased's transaction.
    # A raising metrics must not roll the forbidden-takeover AMBIGUOUS transition back to
    # EXECUTING — the loud-surfacing half of the guarantee stays intact while telemetry is down.
    led = SqliteLedger(tmp_path / "l.db", multi_worker=True, metrics=_RaisingMetrics())
    try:
        # An L0 row left EXECUTING by a presumed-dead owner (lease expired) → forbidden takeover.
        c = led.claim_leased("k", "s", "t", "fp", owner="A", lease_seconds=30, now=100.0)
        assert led.fence("k", c.fencing_token, EffectState.EXECUTING)
        c2 = led.claim_leased("k", "s", "t", "fp", owner="B", lease_seconds=30, now=200.0)
        from sakrit.core.ledger import ClaimKind

        assert c2.kind is ClaimKind.AMBIGUOUS  # surfaced, not rolled back
        assert led.state_of("k") is EffectState.AMBIGUOUS  # durably AMBIGUOUS despite the raise
    finally:
        led.close()
