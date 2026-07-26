# SPDX-License-Identifier: Apache-2.0
"""P5-5: the backend seam. SqliteLedger satisfies the Ledger / LeasedLedger Protocols, so
the core types against the Protocol and a future backend (Postgres) conforms structurally
— no Sqlite subclass lie, no breaking signature change. (mypy enforces the signature-level
conformance at every call site; this is the runtime smoke check.)"""

from sakrit.core import LeasedLedger, Ledger, SqliteLedger


def test_sqlite_ledger_satisfies_the_ledger_protocols() -> None:
    led = SqliteLedger(":memory:")
    try:
        assert isinstance(led, Ledger)  # single-worker core surface
        assert isinstance(led, LeasedLedger)  # + the leased extension (LeasedLedger ⊇ Ledger)
    finally:
        led.close()
