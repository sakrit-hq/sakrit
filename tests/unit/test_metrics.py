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
