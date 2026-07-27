# SPDX-License-Identifier: Apache-2.0
"""Subprocess worker for the real-kill money scenario — the honest version of demo.py's scenario 3.

`demo.py` models the crash in-process (catch a timeout, reopen the ledger). This runs the charge in
a **real child process** that a hard `os._exit(137)` kills in the dual-write window (seam
`after_dispatch`: the charge has landed, the process dies before the ledger records it). The landed
charges persist to a JSON "world" file, so the killed child's charge survives its death — the exact
state a real crashed process leaves behind.

Driven by `tests/integration/test_money_demo.py`. Env:
- `MONEY_DB`    — the ledger path
- `MONEY_WORLD` — the durable provider world (jsonl: one landed charge per line)
- `SAKRIT_TESTING=1` + `SAKRIT_CRASH_AT=after_dispatch` — inject the kill (omitted on restart)
"""

import json
import os
import sys
from pathlib import Path

from sakrit import EffectDecl, Sakrit, SqliteLedger, current_key
from sakrit.core import ArgClass, Reconciliation

WORLD = Path(os.environ["MONEY_WORLD"])
DB = os.environ["MONEY_DB"]
KEY = "order-4471-charge"
AMOUNT = 4999


def _landed() -> list[dict[str, str]]:
    if not WORLD.exists():
        return []
    return [json.loads(line) for line in WORLD.read_text().splitlines() if line.strip()]


def _find(idem: str) -> dict[str, str] | None:
    return next((c for c in _landed() if c["idem"] == idem), None)


def _land(idem: str) -> dict[str, str]:
    """Durably capture one charge, then fsync — the money moves here (the provider's commit)."""
    charge = {"idem": idem, "charge_id": f"ch_{len(_landed()) + 1}"}
    with WORLD.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(charge) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return charge


def run() -> None:
    ledger = SqliteLedger(DB)
    sk = Sakrit(ledger, secret=b"money-secret")

    # L2+R: recovery can ask the provider world "did this charge land?" and adopt it.
    def reconcile(key: str) -> Reconciliation:
        c = _find(key)
        return (
            Reconciliation.settled({"charge_id": c["charge_id"]}) if c else Reconciliation.absent()
        )

    decl = EffectDecl(
        "payments.charge",
        {"amount_cents": ArgClass.IDENTITY},
        provider_key_param="idempotency_key",
        provider_read="strong",
        reconcile=reconcile,
    )

    @sk.effect(decl, key=KEY)
    def charge_card(amount_cents: int) -> dict[str, str]:
        idem = current_key()
        existing = _find(idem)  # provider dedup: a re-dispatched key never double-charges
        if existing is not None:
            return {"charge_id": existing["charge_id"]}
        charge = _land(idem)  # money moves — then settle's after_dispatch seam may kill us here
        return {"charge_id": charge["charge_id"]}

    try:
        result = charge_card(amount_cents=AMOUNT)  # auto-recovers first on the restart run
        print(f"worker: charged {result}")
    except BaseException as exc:  # AmbiguousOutcome etc. are legitimate outcomes to report
        print(f"worker: {type(exc).__name__}", file=sys.stderr)
    finally:
        ledger.close()


if __name__ == "__main__":
    run()
