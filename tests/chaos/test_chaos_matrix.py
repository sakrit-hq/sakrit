# SPDX-License-Identifier: Apache-2.0
"""The chaos matrix — the do-not-launch gate.

Kill a single worker at every dangerous boundary, restart, and assert exactly-once
(or exactly-AMBIGUOUS) holds against both the ledger and the durable world. Each
cell proves a distinct claim; the one deliberately red cell (the unguarded control)
proves the bug is real. This table green is the Act III gate.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.chaos

_WORKER = Path(__file__).parent / "worker.py"


_KILL_CODE = 137  # os._exit(137) at the seam — models 128 + SIGKILL(9)


def _run(tmp: Path, scenario: str, crash_at: str | None, *, leased: bool = False) -> int:
    """Run one worker process. Returns its exit code so the caller can assert the kill
    actually fired (P2-1): a seam run must exit _KILL_CODE, a clean run must exit 0 —
    otherwise the boundary was never reached and the "kill" was vacuous. ``leased`` drives
    the multi-worker leased protocol (short lease, settle_leased)."""
    env = dict(os.environ)
    env["CHAOS_SCENARIO"] = scenario
    env["CHAOS_DB"] = str(tmp / "ledger.sqlite")
    env["CHAOS_WORLD"] = str(tmp / "world.jsonl")
    env.pop("SAKRIT_TESTING", None)
    env.pop("SAKRIT_CRASH_AT", None)
    env.pop("CHAOS_LEASED", None)
    if leased:
        env["CHAOS_LEASED"] = "1"
    if crash_at is not None:
        env["SAKRIT_TESTING"] = "1"
        env["SAKRIT_CRASH_AT"] = crash_at
    proc = subprocess.run([sys.executable, str(_WORKER)], env=env, capture_output=True, text=True)
    return proc.returncode


def _world_count(tmp: Path) -> int:
    world = tmp / "world.jsonl"
    if not world.exists():
        return 0
    return sum(1 for line in world.read_text(encoding="utf-8").splitlines() if line.strip())


def _ledger_states(tmp: Path) -> list[str]:
    db = tmp / "ledger.sqlite"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        return [row[0] for row in con.execute("SELECT state FROM effects").fetchall()]
    finally:
        con.close()


# id, scenario, phase crash-seams (in order), expected world deliveries, ledger state
_CELLS = [
    ("L0 x after_world_write -> AMBIGUOUS", "L0", ["after_world_write", None], 1, "AMBIGUOUS"),
    (
        "L2 x after_world_write -> re-dispatch dedups",
        "L2",
        ["after_world_write", None],
        1,
        "SUCCEEDED",
    ),
    ("L0 x after_claim -> clean retry", "L0", ["after_claim", None], 1, "SUCCEEDED"),
    ("L0 x after_record -> replay", "L0", ["after_record", None], 1, "SUCCEEDED"),
    (
        "mid_recovery double-kill -> idempotent",
        "L0",
        ["after_world_write", "during_recovery", None],
        1,
        "AMBIGUOUS",
    ),
    # P2-2: the post-intent boundary — killed *after* the durable EXECUTING mark but
    # *before* dispatch (nothing delivered). L0 can't prove it didn't fire → AMBIGUOUS,
    # world stays 0 (at-most-once). L2 re-dispatches safely (provider dedups) → 1 send.
    (
        "L0 x after_mark_executing -> AMBIGUOUS",
        "L0",
        ["after_mark_executing", None],
        0,
        "AMBIGUOUS",
    ),
    (
        "L2 x after_mark_executing -> re-dispatch",
        "L2",
        ["after_mark_executing", None],
        1,
        "SUCCEEDED",
    ),
    # P2-3: L1 (reconcilable, non-deduplicating). The ambiguous window that L0 must
    # surface, L1 *resolves*: reconcile reads the world by key and adopts the delivery.
    (
        "L1 x after_world_write -> reconcile settles",
        "L1",
        ["after_world_write", None],
        1,
        "SUCCEEDED",
    ),
    # L1 killed before the world write: reconcile truthfully says ABSENT; on_absent
    # defaults to surface (an eventual read may lie), so it surfaces rather than re-fire.
    (
        "L1 x after_mark_executing -> reconcile absent -> AMBIGUOUS",
        "L1",
        ["after_mark_executing", None],
        0,
        "AMBIGUOUS",
    ),
]


@pytest.mark.parametrize(
    ("scenario", "phases", "world", "state"),
    [(c[1], c[2], c[3], c[4]) for c in _CELLS],
    ids=[c[0] for c in _CELLS],
)
def test_chaos_cell(
    tmp_path: Path, scenario: str, phases: list[str | None], world: int, state: str
) -> None:
    for crash_at in phases:
        code = _run(tmp_path, scenario, crash_at)
        if crash_at is None:
            assert code == 0, f"clean run should exit 0, got {code}"
        else:
            # P2-1: prove the kill was real. If the seam wasn't reached the process would
            # exit 0, and the "crash at {crash_at}" cell would be silently vacuous.
            assert code == _KILL_CODE, f"crash at {crash_at!r} should hard-kill, got exit {code}"
    assert _world_count(tmp_path) == world, f"world deliveries: {_world_count(tmp_path)}"
    assert state in _ledger_states(tmp_path), f"ledger states: {_ledger_states(tmp_path)}"
    # Belt-and-braces: no delivery record ever repeats (dedup and replay are exact).
    world_file = tmp_path / "world.jsonl"
    if world_file.exists():
        lines = [line for line in world_file.read_text().splitlines() if line.strip()]
        assert len(lines) == len({json.dumps(json.loads(x), sort_keys=True) for x in lines})


def test_unguarded_control_duplicates(tmp_path: Path) -> None:
    # The deliberately red cell: without Sakrit, a crash after the world write then a
    # restart re-delivers. The bug is real — this is the "before" picture.
    assert _run(tmp_path, "control", "after_world_write") == _KILL_CODE  # deliver, then die
    assert _run(tmp_path, "control", None) == 0  # restart -> deliver again
    assert _world_count(tmp_path) == 2


# The 1-second lease is measured on the whole-second DB clock, so its effective life is up
# to ~2s; sleep 3s (plus process-startup slack) before a takeover to clear it on slow CI.
_LEASE_EXPIRY_SLEEP = 3.0


def test_leased_l1_takeover_survives_a_real_kill(tmp_path: Path) -> None:
    # P2-4 / V-4: the leased path, killed for real. Worker A (L1, multi-worker, 1s lease)
    # delivers to the world, then a hard os._exit kills it mid-dispatch — its row is left
    # EXECUTING under a now-orphaned lease. After the lease expires, worker B takes over by
    # ladder: RECONCILE finds A's delivery → adopts it. Exactly-once holds across a real
    # kill + a real lease-expiry takeover — the P1-2 Critical, finally killed.
    assert _run(tmp_path, "L1", "after_world_write", leased=True) == _KILL_CODE  # deliver, die
    time.sleep(_LEASE_EXPIRY_SLEEP)  # let A's lease expire
    assert _run(tmp_path, "L1", None, leased=True) == 0  # B takes over, reconciles, adopts

    assert _world_count(tmp_path) == 1, f"world deliveries: {_world_count(tmp_path)}"
    assert "SUCCEEDED" in _ledger_states(tmp_path), f"ledger states: {_ledger_states(tmp_path)}"


def test_leased_l0_forbidden_takeover_under_kill(tmp_path: Path) -> None:
    # L0-leased: A delivers then dies mid-dispatch (EXECUTING, no provider cooperation). B
    # takes over an L0 in-flight row — forbidden (no safe retry) → AMBIGUOUS, world stays 1.
    assert _run(tmp_path, "L0", "after_world_write", leased=True) == _KILL_CODE
    time.sleep(_LEASE_EXPIRY_SLEEP)
    assert _run(tmp_path, "L0", None, leased=True) == 0  # B surfaces AMBIGUOUS, exits clean

    assert _world_count(tmp_path) == 1, f"world deliveries: {_world_count(tmp_path)}"
    assert "AMBIGUOUS" in _ledger_states(tmp_path), f"ledger states: {_ledger_states(tmp_path)}"


def test_leased_l2_takeover_dedups_under_kill(tmp_path: Path) -> None:
    # L2-leased: A delivers (keyed) then dies. B takes over within TTL → re-dispatches; the
    # provider dedups on the same key → no second delivery, row SUCCEEDED, world 1.
    assert _run(tmp_path, "L2", "after_world_write", leased=True) == _KILL_CODE
    time.sleep(_LEASE_EXPIRY_SLEEP)
    assert _run(tmp_path, "L2", None, leased=True) == 0

    assert _world_count(tmp_path) == 1, f"world deliveries: {_world_count(tmp_path)}"
    assert "SUCCEEDED" in _ledger_states(tmp_path), f"ledger states: {_ledger_states(tmp_path)}"


def test_leased_taker_killed_mid_reconcile_still_adopts(tmp_path: Path) -> None:
    # V-5a's crash variant, finally killable: A (L1) delivers then dies. B takes over
    # (RECONCILE) but is hard-killed *inside the reconcile-takeover window* — the exact spot
    # the V-5 preserve-EXECUTING fix defends. The row must stay EXECUTING (not downgraded),
    # so a third worker C still RECONCILEs and adopts A's delivery. World stays 1.
    assert _run(tmp_path, "L1", "after_world_write", leased=True) == _KILL_CODE  # A delivers, dies
    time.sleep(_LEASE_EXPIRY_SLEEP)
    # B takes over (RECONCILE) then dies inside the reconcile window.
    assert _run(tmp_path, "L1", "during_reconcile_takeover", leased=True) == _KILL_CODE
    time.sleep(_LEASE_EXPIRY_SLEEP)
    assert _run(tmp_path, "L1", None, leased=True) == 0  # C reconciles, adopts

    assert _world_count(tmp_path) == 1, f"world deliveries: {_world_count(tmp_path)}"
    assert "SUCCEEDED" in _ledger_states(tmp_path), f"ledger states: {_ledger_states(tmp_path)}"
