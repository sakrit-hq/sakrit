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
from pathlib import Path

import pytest

pytestmark = pytest.mark.chaos

_WORKER = Path(__file__).parent / "worker.py"


def _run(tmp: Path, scenario: str, crash_at: str | None) -> None:
    env = dict(os.environ)
    env["CHAOS_SCENARIO"] = scenario
    env["CHAOS_DB"] = str(tmp / "ledger.sqlite")
    env["CHAOS_WORLD"] = str(tmp / "world.jsonl")
    env.pop("SAKRIT_TESTING", None)
    env.pop("SAKRIT_CRASH_AT", None)
    if crash_at is not None:
        env["SAKRIT_TESTING"] = "1"
        env["SAKRIT_CRASH_AT"] = crash_at
    subprocess.run([sys.executable, str(_WORKER)], env=env, capture_output=True, text=True)


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
        _run(tmp_path, scenario, crash_at)
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
    _run(tmp_path, "control", "after_world_write")  # deliver, then die
    _run(tmp_path, "control", None)  # restart -> deliver again
    assert _world_count(tmp_path) == 2
