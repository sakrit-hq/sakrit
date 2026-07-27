# SPDX-License-Identifier: Apache-2.0
"""Plain-Python quickstart — exactly-once with no agent framework at all.

Sakrit is a dependency, not a destination: with no framework and no adapter, positional identity
comes from a business ``key=`` you supply. Run this file directly to see a guarded effect fire once
across a simulated restart::

    python examples/quickstarts/plain_quickstart.py

This same script is executed verbatim in CI (tests/integration/test_quickstarts.py), so the
copy-paste code on the docs site can never rot.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sakrit import EffectDecl, Sakrit, SqliteLedger
from sakrit.core import ArgClass

# Declare which arguments are IDENTITY (a different value = a different action) and which are
# CONTENT (the model may reword them on a retry — still the same action).
SEND = EffectDecl(
    "email.send",
    {
        "to": ArgClass.IDENTITY,  # a different recipient is a different email
        "subject": ArgClass.IDENTITY,
        "body": ArgClass.CONTENT,  # a reworded body is the *same* email
    },
)


def main() -> None:
    sent: list[str] = []

    def send_email(to: str, subject: str, body: str) -> str:
        sent.append(to)  # stand-in for a real SMTP/API call
        return f"delivered to {to}"

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sakrit.db"

        def run_once() -> object:
            # A fresh engine over the same ledger file models a separate process/attempt.
            ledger = SqliteLedger(db)
            sk = Sakrit(ledger, secret=b"<per-deployment-secret>")
            try:
                return sk.guard(
                    SEND,
                    send_email,
                    kwargs={"to": "ops@example.com", "subject": "Welcome", "body": "Hello!"},
                    key="welcome-ops",  # the business key = this action's identity
                )
            finally:
                ledger.close()  # release the single-worker lock, as a crash would

        first = run_once()
        second = run_once()  # a retry / resume: this REPLAYS, it does not re-send

    assert sent == ["ops@example.com"], f"expected one send, got {sent}"
    assert first == second == "delivered to ops@example.com"
    print(f"fired exactly once: {sent}")
    print(f"the retry replayed the recorded result: {second!r}")


if __name__ == "__main__":
    main()
