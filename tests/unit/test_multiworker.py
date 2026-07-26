# SPDX-License-Identifier: Apache-2.0
"""Multi-worker exactly-once under REAL concurrency.

N worker threads, each with its own connection to a *shared* SQLite ledger, race
the same key. Exactly one executes the effect; the others wait on its lease and
return its recorded result. This drives the full leased settle loop (claim → lease
→ fence → dispatch → fenced record), not just the protocol primitives.

SQLite in WAL mode + `BEGIN IMMEDIATE` + `busy_timeout` serializes the claims; a
Postgres backend is a storage swap for scale, not a protocol change.
"""

import threading
from pathlib import Path

from sakrit.core import EffectState, SqliteLedger, settle_leased

SECRET = b"deployment-secret"


def test_concurrent_workers_execute_the_effect_exactly_once(tmp_path: Path) -> None:
    db = str(tmp_path / "shared.sqlite")
    key, tool = "the-key", "chaos.send"

    executions = 0
    exec_lock = threading.Lock()
    barrier = threading.Barrier(8)  # release all workers at once, maximizing the race
    results: list[object] = []
    results_lock = threading.Lock()

    def effect() -> str:
        nonlocal executions
        with exec_lock:
            executions += 1
        return "the-one-result"

    def worker() -> None:
        # Each thread is its own "worker" with its own ledger connection.
        ledger = SqliteLedger(db, multi_worker=True)
        try:
            barrier.wait()
            out = settle_leased(
                ledger,
                key=key,
                scope="run",
                tool=tool,
                fingerprint="fp",
                fn=effect,
                lease_seconds=30.0,
            )
            with results_lock:
                results.append(out)
        finally:
            ledger.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert executions == 1  # the effect fired exactly once across 8 racing workers
    assert results == ["the-one-result"] * 8  # every worker returned the winner's result

    with SqliteLedger(db, multi_worker=True) as check:
        assert check.state_of(key) is EffectState.SUCCEEDED


def test_second_worker_replays_after_first_completes(tmp_path: Path) -> None:
    db = str(tmp_path / "shared.sqlite")
    calls = 0

    def effect() -> str:
        nonlocal calls
        calls += 1
        return "done"

    with SqliteLedger(db, multi_worker=True) as w1:
        a = settle_leased(w1, key="k", scope="s", tool="t", fingerprint="fp", fn=effect)
    with SqliteLedger(db, multi_worker=True) as w2:  # a different worker, later
        b = settle_leased(w2, key="k", scope="s", tool="t", fingerprint="fp", fn=effect)

    assert a == b == "done"
    assert calls == 1  # the second worker replayed, did not re-execute
