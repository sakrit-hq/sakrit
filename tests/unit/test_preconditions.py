# SPDX-License-Identifier: Apache-2.0
"""Act III preconditions: durability probe, single-worker refusal, engine recovery."""

import sys
import threading
from pathlib import Path

import pytest

from sakrit import Sakrit, SqliteLedger
from sakrit.core import ArgClass, Coordinate, EffectDecl, EffectState, SakritError, positional_key

SECRET = b"deployment-secret"


# --- Q12: durability probe ------------------------------------------------
def test_file_ledger_is_durable_by_default(tmp_path: Path) -> None:
    with SqliteLedger(tmp_path / "l.sqlite") as led:
        assert led.fault_model() == "process-and-power-crash-safe (WAL+FULL)"


def test_i_accept_data_loss_is_explicit_and_reported(tmp_path: Path) -> None:
    with SqliteLedger(tmp_path / "l.sqlite", i_accept_data_loss=True) as led:
        assert "NONE" in led.fault_model()


def test_memory_ledger_is_ephemeral() -> None:
    with SqliteLedger() as led:
        assert "ephemeral" in led.fault_model()


# --- Q13: single-worker refusal (flock) -----------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock")
def test_second_worker_on_same_file_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "l.sqlite"
    first = SqliteLedger(db)
    try:
        with pytest.raises(SakritError, match="single-worker"):
            SqliteLedger(db)
    finally:
        first.close()
    # After the first releases the lock, a new worker may open it.
    SqliteLedger(db).close()


def test_memory_ledgers_do_not_lock() -> None:
    a, b = SqliteLedger(), SqliteLedger()  # no file → no lock → both fine
    a.close()
    b.close()


# --- Q14: engine-guaranteed recovery before the first claim ---------------
def test_guard_runs_recovery_once_before_first_effect() -> None:
    led = SqliteLedger()
    # A crash leftover from a prior process: an EXECUTING row (L0).
    stale = positional_key(Coordinate("run-0", b"stale-step"), "email.send")
    led.claim(stale, "run-0", "email.send", "fp")
    led.mark_executing(stale)
    assert led.state_of(stale) is EffectState.EXECUTING

    sk = Sakrit(led, secret=SECRET)
    calls: list[str] = []

    @sk.effect(EffectDecl("email.send", {"to": ArgClass.IDENTITY}), key="new-effect")
    def send(to: str) -> str:
        calls.append(to)
        return "ok"

    send(to="a@x.com")

    # The first guarded call ran recovery first → the leftover is resolved…
    assert led.state_of(stale) is EffectState.AMBIGUOUS
    # …and the new effect executed exactly once.
    assert calls == ["a@x.com"]


# --- P3-3 / P1-11: machine-checked multi-worker preconditions --------------
def test_multi_worker_memory_is_refused() -> None:
    # An in-memory DB is private per connection → N workers, N isolated ledgers.
    with pytest.raises(SakritError, match="shared database"):
        SqliteLedger(multi_worker=True)


def test_mode_stamp_refuses_multi_open_of_single_worker_db(tmp_path: Path) -> None:
    db = tmp_path / "l.sqlite"
    single = SqliteLedger(db)  # stamps single-worker, holds the flock
    try:
        with pytest.raises(SakritError, match="single-worker mode"):
            SqliteLedger(db, multi_worker=True)
    finally:
        single.close()


def test_mode_stamp_refuses_single_open_of_multi_worker_db(tmp_path: Path) -> None:
    db = tmp_path / "l.sqlite"
    SqliteLedger(db, multi_worker=True).close()  # stamps multi-worker
    with pytest.raises(SakritError, match="multi-worker mode"):
        SqliteLedger(db)


def test_same_mode_reopen_is_allowed(tmp_path: Path) -> None:
    db = tmp_path / "l.sqlite"
    SqliteLedger(db, multi_worker=True).close()
    SqliteLedger(db, multi_worker=True).close()  # same mode → fine


def test_engine_accepts_a_multi_worker_ledger(tmp_path: Path) -> None:
    # P3-8: the engine now drives the leased protocol against a multi-worker ledger.
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    try:
        sk = Sakrit(led, secret=SECRET)
        assert sk._leased is True
    finally:
        led.close()


def test_engine_recover_refuses_in_multi_worker_mode(tmp_path: Path) -> None:
    # P3-4: the lease-blind startup scan would poison live peers → refuse an explicit call.
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    try:
        sk = Sakrit(led, secret=SECRET)
        with pytest.raises(SakritError, match="multi_worker"):
            sk.recover()
    finally:
        led.close()


# --- V-3: one multi_worker connection per worker --------------------------
def test_shared_multi_worker_ledger_across_threads_is_refused(tmp_path: Path) -> None:
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    try:
        led.claim_leased("k", "s", "t", "fp", owner="A", lease_seconds=30.0)  # binds this thread
        errs: list[str] = []

        def other() -> None:
            try:
                led.claim_leased("k2", "s", "t", "fp", owner="B", lease_seconds=30.0)
            except SakritError as e:
                errs.append(str(e))

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert len(errs) == 1
        assert "per worker" in errs[0]
    finally:
        led.close()


def test_separate_connections_per_worker_thread_are_fine(tmp_path: Path) -> None:
    # The sanctioned model: each worker its own connection to the shared file → no trip.
    db = tmp_path / "l.sqlite"
    ok: list[object] = []

    def worker(name: str) -> None:
        led = SqliteLedger(db, multi_worker=True)  # this worker's own connection
        try:
            c = led.claim_leased(f"k-{name}", "s", "t", "fp", owner=name, lease_seconds=30.0)
            ok.append(c.kind)
        finally:
            led.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 3  # each on its own connection+thread → all claim fine


def test_durability_pragmas_are_verified_not_just_set(tmp_path: Path) -> None:
    # P1-14: construction asserts WAL+FULL actually took effect (it would raise otherwise),
    # so fault_model() reports a checked property, not a hope.
    led = SqliteLedger(tmp_path / "l.sqlite")
    try:
        assert str(led.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert led.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert led.fault_model() == "process-and-power-crash-safe (WAL+FULL)"
    finally:
        led.close()


# --- V-6b: owner= is a label, not an identity (uuid-suffixed) --------------
def test_duplicate_owner_label_gets_distinct_ids(tmp_path: Path) -> None:
    db = tmp_path / "l.sqlite"
    a = SqliteLedger(db, multi_worker=True, owner="pod-3")
    b = SqliteLedger(db, multi_worker=True, owner="pod-3")
    try:
        assert a.owner != b.owner  # uniqueness is unconditional — no silent collision
        assert a.owner.startswith("pod-3-") and b.owner.startswith("pod-3-")  # label preserved
    finally:
        a.close()
        b.close()


def test_same_owner_label_two_workers_do_not_both_proceed(tmp_path: Path) -> None:
    from sakrit.core.ledger import ClaimKind

    db = tmp_path / "l.sqlite"
    a = SqliteLedger(db, multi_worker=True, owner="pod")
    b = SqliteLedger(db, multi_worker=True, owner="pod")
    try:
        ca = a.claim_leased("k", "s", "t", "fp", owner=a.owner, lease_seconds=30.0, now=100.0)
        cb = b.claim_leased("k", "s", "t", "fp", owner=b.owner, lease_seconds=30.0, now=110.0)
        assert ca.kind is ClaimKind.PROCEED
        assert cb.kind is ClaimKind.BUSY  # the live lease is respected — no collision-PROCEED
    finally:
        a.close()
        b.close()
