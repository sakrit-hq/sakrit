# SPDX-License-Identifier: Apache-2.0
"""Act III preconditions: durability probe, single-worker refusal, engine recovery."""

import sys
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


def test_engine_refuses_a_multi_worker_ledger(tmp_path: Path) -> None:
    led = SqliteLedger(tmp_path / "l.sqlite", multi_worker=True)
    try:
        with pytest.raises(SakritError, match="multi_worker"):
            Sakrit(led, secret=SECRET)
    finally:
        led.close()
