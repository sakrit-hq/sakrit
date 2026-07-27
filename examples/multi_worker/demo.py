# SPDX-License-Identifier: Apache-2.0
"""Multi-worker exactly-once — N agents racing the same charge, one charge lands.

Sakrit's multi-worker mode (leases + fencing tokens) makes concurrent workers safe: several agents
— threads here, separate processes in production — can hit the **same** effect at the same instant,
and it fires **exactly once**. One worker wins the lease and executes; the rest see the live lease,
wait, and replay the winner's recorded result. A worker that stalls and returns late is *fenced* —
its stale write is rejected — so a zombie can't double-charge.

Two rules that matter for multi-worker:

1. **One ledger connection per worker.** Each worker opens its *own* `SqliteLedger(shared_path,
   multi_worker=True)` and its own `Sakrit` engine. Don't share one engine/connection across
   threads — the atomic claim relies on a per-worker connection.
2. **The coordinate must be shared across workers on the same logical effect.** Here that's a
   business `key` (globally unique — the order). With a framework adapter instead, the shared piece
   is `scope` = your stable, persisted run identity, so every worker on that run agrees.

Run it::

    python examples/multi_worker/demo.py

Executed verbatim in CI (tests/integration/test_multi_worker_demo.py).
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass

CHARGE = EffectDecl(
    "payment.charge",
    {"customer": ArgClass.IDENTITY, "amount_cents": ArgClass.IDENTITY},
)
SECRET = b"<per-deployment-secret>"
N_WORKERS = 6


def run() -> tuple[int, list[object]]:
    """Race N workers on one charge against a shared multi-worker ledger. Returns
    (times the effect fired, each worker's returned result)."""
    charges: list[str] = []
    charges_lock = threading.Lock()
    results: dict[int, object] = {}

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sakrit.db"
        SqliteLedger(db, multi_worker=True).close()  # provision the shared file once
        start = threading.Barrier(N_WORKERS)

        def worker(i: int) -> None:
            # Rule 1: this worker's OWN connection + engine to the shared ledger.
            ledger = SqliteLedger(db, multi_worker=True)
            sk = Sakrit(ledger, secret=SECRET)

            # Rule 2: the same business key = the same logical effect, shared across all workers.
            @sk.effect(CHARGE, key="order-4471-charge")
            def charge_card(customer: str, amount_cents: int) -> dict[str, str]:
                with charges_lock:
                    charges.append(customer)  # a real charge only happens here
                return {"charge_id": "ch_1"}

            try:
                start.wait()  # release all workers together — maximize the race
                results[i] = charge_card(customer="cus_8815", amount_cents=4999)
            finally:
                ledger.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    return len(charges), list(results.values())


def main() -> None:
    n_charges, results = run()
    assert n_charges == 1, f"expected exactly one charge, got {n_charges}"
    assert len(results) == N_WORKERS and all(r == {"charge_id": "ch_1"} for r in results)
    print(f"{N_WORKERS} workers raced the same charge → {n_charges} charge landed")
    print(f"all {N_WORKERS} workers returned the same receipt: {results[0]}")


if __name__ == "__main__":
    main()
