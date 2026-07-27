# SPDX-License-Identifier: Apache-2.0
"""The golden money demo — an agent that charges a card, exactly once.

Three scenarios, each asserting the charge count:

1. **Naive** — no Sakrit. A retry (crash/resume) double-charges the customer. The bug, concretely.
2. **Guarded, retried** — the same tool under Sakrit. A retry replays the recorded charge; money
   moves once.
3. **Guarded, crashed in the window** — the provider captures the charge, then the response is lost
   (the dual-write window). After a restart, recovery **reconciles** the ambiguous row (asks the
   provider "did this land?") and the app's retry replays it — still one charge, no double bill.

Run it::

    python examples/money_agent/demo.py

Framework-free (positional identity from a business ``key=``) and offline. Reused as the Phase 1
end-to-end fixture and the Phase 3 approval-handling reference — build once, reuse everywhere.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from sakrit import EffectDecl, Sakrit, SqliteLedger, current_key
from sakrit.core import ArgClass, Reconciliation, Verdict

# Import the reusable fake provider as a sibling (works as a script and when loaded by path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider import (  # noqa: E402
    COMMIT_THEN_TIMEOUT,
    FakePaymentProvider,
    ProviderTimeout,
)

# A money tool at L2+R: an injected idempotency key (provider dedup) AND a reconcile read
# (recovery can ask "did it land?"). This is the level recommended for money.
CHARGE = EffectDecl(
    "payments.charge",
    {
        "customer_id": ArgClass.IDENTITY,
        "amount_cents": ArgClass.IDENTITY,  # a different amount is a different charge
        "currency": ArgClass.IDENTITY,
        "memo": ArgClass.CONTENT,  # a reworded statement memo is the same charge
    },
    provider_key_param="idempotency_key",
    provider_read="strong",  # a payment lookup is a strong read
)
SECRET = b"<per-deployment-secret>"
ORDER = {"customer_id": "cus_8815", "amount_cents": 4999, "currency": "USD", "memo": "Order #4471"}
CHARGE_KEY = "order-4471-charge"  # the business key = this charge's identity


def _guarded_charge(sk: Sakrit, provider: FakePaymentProvider):
    # L2+R needs the reconcile bound to this provider instance. It must return the SAME shape the
    # tool returns (a dict) — not the provider's internal Charge record — or a reconciled result
    # stores as unserializable and a later replay hands back a marker instead of {"charge_id": ...}.
    # Mapping the provider record to the tool's return shape is the app's job, done here.
    def reconcile(key: str) -> Reconciliation:
        rec = provider.reconcile(key)
        if rec.verdict is Verdict.SETTLED:
            return Reconciliation.settled({"charge_id": rec.result.id})  # type: ignore[union-attr]
        return rec

    decl = EffectDecl(
        CHARGE.tool,
        CHARGE.classes,
        provider_key_param="idempotency_key",
        provider_read="strong",
        reconcile=reconcile,
    )

    @sk.effect(decl, key=CHARGE_KEY)
    def charge_card(
        customer_id: str, amount_cents: int, currency: str, memo: str
    ) -> dict[str, str]:
        # The tool reads the Sakrit-derived idempotency key — no phantom parameter.
        charge = provider.charge(
            amount_cents=amount_cents, currency=currency, idempotency_key=current_key()
        )
        return {"charge_id": charge.id}

    return charge_card


def naive_double_charges_on_retry() -> int:
    """Scenario 1: no Sakrit. A retry re-runs the un-keyed charge → the customer is billed twice."""
    provider = FakePaymentProvider()

    def naive_charge() -> None:
        provider.charge(amount_cents=ORDER["amount_cents"], currency=ORDER["currency"])  # no key!

    naive_charge()
    naive_charge()  # the resume/retry
    return provider.charge_count


def guarded_charges_once_on_retry(db: Path) -> int:
    """Scenario 2: under Sakrit, a retry replays the recorded charge — money moves once."""
    provider = FakePaymentProvider()
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)
    charge_card = _guarded_charge(sk, provider)
    try:
        charge_card(**ORDER)
        charge_card(**ORDER)  # the retry — replays, does not re-charge
    finally:
        ledger.close()
    return provider.charge_count


def guarded_survives_crash_in_window(db: Path) -> int:
    """Scenario 3: the charge lands, the response is lost, the process restarts — one charge.

    This is an *in-process* model of the crash (we catch the timeout and reopen the ledger), so it
    demonstrates the recovery *mechanism* readably. A **real `os._exit` version of this exact money
    scenario** — a child process hard-killed in the dual-write window — lives in ``crash_worker.py``
    and is asserted by ``tests/integration/test_money_demo.py`` (the broader kill-at-every-boundary
    matrix is ``tests/chaos/``). At L2+R, recovery **reconciles**: it asks the provider "did this
    charge land?" and adopts the answer.
    """
    provider = FakePaymentProvider()
    provider.inject(COMMIT_THEN_TIMEOUT)  # first attempt captures, then the response vanishes

    # --- attempt, then "crash": the charge lands but the row is left EXECUTING (ambiguous) ---
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)
    charge_card = _guarded_charge(sk, provider)
    try:
        charge_card(**ORDER)
    except ProviderTimeout:
        pass  # the money moved, but we never learned it — the classic ambiguous window
    finally:
        ledger.close()  # the restart boundary (a real crash releases the OS lock the same way)

    # --- restart: recovery reconciles the ambiguous row, then the app re-invokes and REPLAYS ---
    ledger = SqliteLedger(db)
    sk = Sakrit(ledger, secret=SECRET)
    charge_card = _guarded_charge(sk, provider)  # re-register so recovery can reconcile the tool
    sk.recover()  # ask the provider whether the charge landed → adopt it, row → SUCCEEDED
    result = charge_card(**ORDER)  # the app retries after restart → replays the reconciled charge
    ledger.close()

    # Load-bearing: without working recovery this either re-charges (count 2) or the replay returns
    # a marker instead of the reconciled result. Both would fail one of these assertions.
    assert result == {"charge_id": "ch_1"}, f"expected the replayed reconciled charge, got {result}"
    return provider.charge_count


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        naive = naive_double_charges_on_retry()
        retried = guarded_charges_once_on_retry(Path(tmp) / "retry.db")
        crashed = guarded_survives_crash_in_window(Path(tmp) / "crash.db")

    assert naive == 2, f"naive should double-charge, got {naive}"
    assert retried == 1, f"guarded retry should charge once, got {retried}"
    assert crashed == 1, f"guarded crash-in-window should charge once, got {crashed}"

    print(f"1. naive agent, retried:            {naive} charges  ← the bug (double bill)")
    print(f"2. Sakrit-guarded, retried:         {retried} charge   ← replayed, not re-charged")
    print(
        f"3. Sakrit-guarded, crashed in window: {crashed} charge   ← recovery reconciled the charge"
    )


if __name__ == "__main__":
    main()
