# SPDX-License-Identifier: Apache-2.0
"""The golden money demo, executed verbatim (roadmap Stage 2 · Phase 0 · deliverable 6).

The demo under ``examples/money_agent/`` is reused as the Phase 1 end-to-end fixture and the Phase 3
approval reference, so it must keep proving its claim: a naive agent double-charges on a retry, and
a Sakrit-guarded one charges exactly once — through a plain retry and through a crash in the
dual-write window. Framework-free, so it always runs here.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_MONEY = Path(__file__).resolve().parents[2] / "examples" / "money_agent"
_CRASH_WORKER = _MONEY / "crash_worker.py"
_KILL_CODE = 137  # os._exit(137) at the seam — 128 + SIGKILL(9)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _MONEY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_golden_money_demo_runs() -> None:
    _load("demo").main()  # asserts the three charge counts (2 / 1 / 1) internally


def _run_crash_worker(db: Path, world: Path, crash_at: str | None) -> int:
    """Run the money charge in a real child process; return its exit code so the caller can assert
    the kill actually fired. `crash_at` set → os._exit at that seam; None → a clean recovery run."""
    env = dict(os.environ)
    env["MONEY_DB"] = str(db)
    env["MONEY_WORLD"] = str(world)
    env.pop("SAKRIT_TESTING", None)
    env.pop("SAKRIT_CRASH_AT", None)
    if crash_at is not None:
        env["SAKRIT_TESTING"] = "1"
        env["SAKRIT_CRASH_AT"] = crash_at
    proc = subprocess.run(
        [sys.executable, str(_CRASH_WORKER)], env=env, capture_output=True, text=True
    )
    return proc.returncode


def _charges(world: Path) -> int:
    if not world.exists():
        return 0
    return sum(1 for line in world.read_text().splitlines() if line.strip())


def _row_state(db: Path, key: str = "order-4471-charge") -> str | None:
    con = sqlite3.connect(str(db))
    try:
        # the ledger stores a positional key hash, not the business key — read the one row's state
        row = con.execute("SELECT state FROM effects").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def test_money_charge_survives_a_real_process_kill(tmp_path: Path) -> None:
    # The honest version of demo.py scenario 3: a real child process, hard-killed in the dual-write
    # window. Backs the "crashed"/"process death" headline claim with an actual os._exit.
    db, world = tmp_path / "ledger.sqlite", tmp_path / "world.jsonl"

    # 1. Charge lands, then the process is hard-killed BEFORE the ledger records it.
    rc = _run_crash_worker(db, world, crash_at="after_dispatch")
    assert rc == _KILL_CODE, f"the kill did not fire (seam not reached); exit={rc}"
    assert _charges(world) == 1, "the charge should have landed exactly once before the kill"
    assert _row_state(db) == "EXECUTING", "the row should be left ambiguous (mid-window)"

    # 2. Restart: recovery reconciles the ambiguous row against the provider world; no new charge.
    rc2 = _run_crash_worker(db, world, crash_at=None)
    assert rc2 == 0, "the restart run should exit cleanly"
    assert _charges(world) == 1, "still exactly one charge after crash + recovery"
    assert _row_state(db) == "SUCCEEDED", "recovery should have resolved the row to SUCCEEDED"


def test_provider_dedups_on_idempotency_key() -> None:
    provider = _load("provider").FakePaymentProvider()
    first = provider.charge(amount_cents=4999, currency="USD", idempotency_key="k1")
    again = provider.charge(amount_cents=4999, currency="USD", idempotency_key="k1")
    assert first == again  # same charge returned for a repeated key
    assert provider.charge_count == 1  # money moved once


def test_provider_reconcile_reports_absent_then_settled() -> None:
    provider_mod = _load("provider")
    provider = provider_mod.FakePaymentProvider()
    assert provider.reconcile("k2").verdict.value == "absent"
    provider.charge(amount_cents=100, currency="USD", idempotency_key="k2")
    assert provider.reconcile("k2").verdict.value == "settled"
