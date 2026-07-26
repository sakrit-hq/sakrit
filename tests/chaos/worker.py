# SPDX-License-Identifier: Apache-2.0
"""Chaos subprocess worker — runs one guarded effect, then exits (or is killed).

Config via env: ``CHAOS_SCENARIO`` (``L0`` | ``L2`` | ``control``), ``CHAOS_DB``
(ledger path), ``CHAOS_WORLD`` (durable delivery log, jsonl). A crash is injected
by ``SAKRIT_CRASH_AT=<seam>`` + ``SAKRIT_TESTING=1`` — including the world-side
``after_world_write`` seam, the true ambiguous window (the delivery is durably
committed, then the process dies before dispatch returns).

Invoked as a fresh process by ``test_chaos_matrix.py`` so ``os._exit`` kills really
kill (and ``flock`` is kernel-released on death).
"""

import json
import os
import sys
from pathlib import Path

from sakrit.core.seams import seam

SCENARIO = os.environ["CHAOS_SCENARIO"]
WORLD = Path(os.environ["CHAOS_WORLD"])


def _deliver(record: dict[str, object]) -> None:
    """Durably commit one delivery to the world, then hit the world-side seam."""
    with WORLD.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    seam("after_world_write")


def _already_delivered(idem: str) -> bool:
    if not WORLD.exists():
        return False
    return any(
        json.loads(line).get("idem") == idem
        for line in WORLD.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def run() -> None:
    if SCENARIO == "control":
        _deliver({"to": "c@x"})  # unguarded: no ledger, no dedup — the before-picture
        return

    from sakrit import EffectDecl, Sakrit, SqliteLedger, current_key
    from sakrit.core import ArgClass, Reconciliation

    ledger = SqliteLedger(os.environ["CHAOS_DB"])
    sk = Sakrit(ledger, secret=b"chaos-secret")

    # L1 (reconcilable): a non-deduplicating provider we can *query* on recovery. The
    # reconcile reads the durable world by the effect's key — "did this land?".
    def reconcile(key: str) -> Reconciliation:
        if _already_delivered(key):
            return Reconciliation.settled({"reconciled": True})
        return Reconciliation.absent()

    decl = EffectDecl(
        "chaos.send",
        {"to": ArgClass.IDENTITY},
        provider_key_param="idempotency_key" if SCENARIO == "L2" else None,
        reconcile=reconcile if SCENARIO == "L1" else None,
    )

    @sk.effect(decl, key="the-effect")
    def do_effect(to: str) -> dict[str, object]:
        if SCENARIO == "L2":
            idem = current_key()
            if _already_delivered(idem):  # the keyed provider deduplicates a retry
                return {"deduped": True}
            _deliver({"to": to, "idem": idem})
            return {"ok": True}
        if SCENARIO == "L1":
            # No self-dedup (the provider doesn't); recovery reconciles by key instead.
            _deliver({"to": to, "idem": current_key()})
            return {"ok": True}
        _deliver({"to": to})
        return {"ok": True}

    try:
        do_effect(to="c@x")
    except BaseException as exc:  # AmbiguousOutcome / EffectInFlightError are expected outcomes
        print(f"worker: {type(exc).__name__}", file=sys.stderr)
    finally:
        ledger.close()


if __name__ == "__main__":
    run()
