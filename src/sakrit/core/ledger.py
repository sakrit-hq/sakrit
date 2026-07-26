# SPDX-License-Identifier: Apache-2.0
"""The ledger — the durable write-ahead record of every effect.

Answers "has this action already happened, and what did it return?" It is a
**write-ahead intent log**, not a check-then-act cache: the ``EXECUTING`` mark
commits *before* the effect dispatches, so a crash in the window leaves evidence
(``docs/design.md`` §8).

Act II subset of the state machine::

    (new) --claim--> CLAIMED --mark--> EXECUTING --record--> SUCCEEDED
                        ^                   |                    |
                        └── re-claim ───────┘                    ├── error ──> FAILED
                        (crash before dispatch: safe)            └── crash ──> (EXECUTING
                                                                    stays; recovery → AMBIGUOUS)

Deferred: ``BUFFERED`` (outbox), leases/fencing (multi-worker), ``RECONCILING``
(L1). This backend is SQLite single-writer (``BEGIN IMMEDIATE``); Postgres and the
ledger/adapter split come later.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sakrit.core.errors import SakritError


class EffectState(Enum):
    INTENDED = "INTENDED"
    CLAIMED = "CLAIMED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class ClaimKind(Enum):
    PROCEED = "proceed"  # we own a fresh/re-claimable row — execute it
    REPLAY = "replay"  # already SUCCEEDED — return the saved result (check fp)
    AMBIGUOUS = "ambiguous"  # crashed in the window — must surface, do not execute


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    result: object | None = None
    fingerprint: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
  key            TEXT PRIMARY KEY,
  scope          TEXT NOT NULL,
  tool           TEXT NOT NULL,
  fingerprint    TEXT NOT NULL,
  state          TEXT NOT NULL,
  provider_dedup INTEGER NOT NULL DEFAULT 0,
  result         TEXT,
  error          TEXT,
  created_at     TEXT NOT NULL,
  settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_scope_state ON effects (scope, state);
"""


class SqliteLedger:
    """A durable, single-writer ledger backed by SQLite."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        # isolation_level=None → autocommit; we drive BEGIN IMMEDIATE / COMMIT
        # ourselves so a claim is one atomic statement group.
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> SqliteLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- read -------------------------------------------------------------
    def state_of(self, key: str) -> EffectState | None:
        row = self.conn.execute("SELECT state FROM effects WHERE key = ?", (key,)).fetchone()
        return None if row is None else EffectState(row[0])

    # --- the atomic claim -------------------------------------------------
    def claim(
        self, key: str, scope: str, tool: str, fingerprint: str, *, provider_dedup: bool = False
    ) -> Claim:
        """Atomically decide what to do with ``key`` and take ownership if we run it."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, result, fingerprint FROM effects WHERE key = ?", (key,)
            ).fetchone()

            if row is None:
                self.conn.execute(
                    "INSERT INTO effects "
                    "(key, scope, tool, fingerprint, state, provider_dedup, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        scope,
                        tool,
                        fingerprint,
                        EffectState.CLAIMED.value,
                        int(provider_dedup),
                        _now(),
                    ),
                )
                claim = Claim(ClaimKind.PROCEED)

            else:
                state = EffectState(row[0])
                if state is EffectState.SUCCEEDED:
                    result = None if row[1] is None else json.loads(row[1])
                    claim = Claim(ClaimKind.REPLAY, result=result, fingerprint=row[2])
                elif state is EffectState.AMBIGUOUS:
                    claim = Claim(ClaimKind.AMBIGUOUS)
                elif state is EffectState.EXECUTING and not provider_dedup:
                    # L0: a crash landed in the ambiguous window. Surface as
                    # AMBIGUOUS — never silently re-execute a non-idempotent effect.
                    self.conn.execute(
                        "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                        (EffectState.AMBIGUOUS.value, _now(), key),
                    )
                    claim = Claim(ClaimKind.AMBIGUOUS)
                else:
                    # Re-own and retry: CLAIMED (crash before dispatch), FAILED, or
                    # an L2 EXECUTING leftover (a retry with the same provider key
                    # deduplicates, so re-dispatch is safe).
                    self.conn.execute(
                        "UPDATE effects SET state = ?, fingerprint = ? WHERE key = ?",
                        (EffectState.CLAIMED.value, fingerprint, key),
                    )
                    claim = Claim(ClaimKind.PROCEED)

            self.conn.execute("COMMIT")
            return claim
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # --- write-ahead transitions -----------------------------------------
    def mark_executing(self, key: str) -> None:
        """Durably record intent to dispatch. MUST commit before the effect runs."""
        self._transition(key, EffectState.EXECUTING)

    def record_success(self, key: str, result: object) -> None:
        try:
            encoded = json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise SakritError(
                f"result of {key} is not serializable ({exc}); declare a rehydrate "
                "or marker in the tool's SED"
            ) from exc
        self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, settled_at = ? WHERE key = ?",
            (EffectState.SUCCEEDED.value, encoded, _now(), key),
        )

    def record_failure(self, key: str, error: BaseException) -> None:
        self.conn.execute(
            "UPDATE effects SET state = ?, error = ?, settled_at = ? WHERE key = ?",
            (EffectState.FAILED.value, f"{type(error).__name__}: {error}", _now(), key),
        )

    def _transition(self, key: str, state: EffectState) -> None:
        self.conn.execute("UPDATE effects SET state = ? WHERE key = ?", (state.value, key))

    # --- recovery scan ----------------------------------------------------
    def recover(self) -> list[str]:
        """Startup scan over `EXECUTING` (crash-in-window) rows.

        L0 rows → `AMBIGUOUS` (surfaced). L2 rows (provider-deduplicating) →
        `CLAIMED`, so the next attempt safely re-dispatches with the same provider
        key. Returns the keys left `AMBIGUOUS` (the ones needing resolution).
        """
        rows = self.conn.execute(
            "SELECT key, provider_dedup FROM effects WHERE state = ?",
            (EffectState.EXECUTING.value,),
        ).fetchall()
        ambiguous: list[str] = []
        for key, dedup in rows:
            if dedup:
                self.conn.execute(
                    "UPDATE effects SET state = ? WHERE key = ?", (EffectState.CLAIMED.value, key)
                )
            else:
                self.conn.execute(
                    "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                    (EffectState.AMBIGUOUS.value, _now(), key),
                )
                ambiguous.append(key)
        return ambiguous

    def keys_in(self, state: EffectState) -> Iterable[str]:
        rows = self.conn.execute(
            "SELECT key FROM effects WHERE state = ?", (state.value,)
        ).fetchall()
        return [r[0] for r in rows]
