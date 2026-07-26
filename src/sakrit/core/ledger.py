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
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sakrit.core.errors import EffectInFlightError, SakritError
from sakrit.core.seams import seam

try:
    import fcntl  # POSIX advisory locking
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]


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


@dataclass(frozen=True)
class Replayed:
    """Returned on replay of an effect whose result could not be serialized.

    The effect *happened*; its return value was not storable, so the ledger kept a
    marker instead. Execution truth outranks result fidelity — the caller handles
    the marker (the future rehydrate/marker machinery, arriving early). See
    ``docs/design.md`` §10.
    """

    key: str
    note: str


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
  reconcilable   INTEGER NOT NULL DEFAULT 0,
  result         TEXT,
  result_ref     TEXT,
  error          TEXT,
  created_at     TEXT NOT NULL,
  settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_scope_state ON effects (scope, state);
"""


class SqliteLedger:
    """A durable, single-writer ledger backed by SQLite."""

    def __init__(self, path: str | Path = ":memory:", *, i_accept_data_loss: bool = False) -> None:
        self._path = str(path)
        self._lock_fd: int | None = None
        # Q13 — enforce single-worker before opening: the SQLite backend is
        # single-worker, and the guarantee depends on that being a *property*, not a
        # hope. The kernel releases this lock on process death, however rude.
        if self._path != ":memory:":
            self._acquire_single_worker_lock()
        # isolation_level=None → autocommit; we drive BEGIN IMMEDIATE / COMMIT
        # ourselves so a claim is one atomic statement group.
        self.conn = sqlite3.connect(self._path, isolation_level=None)
        self._configure_durability(i_accept_data_loss)  # Q12
        self.conn.executescript(_SCHEMA)

    def _acquire_single_worker_lock(self) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX; best-effort, documented
            return
        lock_path = self._path + ".lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SakritError(
                f"another worker already holds {lock_path}. The SQLite backend is "
                "single-worker (the lock is a local-filesystem flock, kernel-released "
                "on death); use the Postgres backend to run multiple workers."
            ) from exc
        self._lock_fd = fd

    def _configure_durability(self, i_accept_data_loss: bool) -> None:
        # :memory: is ephemeral by nature — no durability claim to enforce.
        if self._path == ":memory:":
            return
        if i_accept_data_loss:
            self.conn.execute("PRAGMA synchronous=OFF")  # fast, unsafe — explicitly opted in
            return
        # WAL + FULL: durable against a process crash *and* power loss. (WAL+NORMAL
        # is process-crash-safe but not power-loss-safe — see fault_model.)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")

    def fault_model(self) -> str:
        """The durability tier this ledger certifies — for docs and observability."""
        if self._path == ":memory:":
            return "ephemeral (:memory:) — not durable"
        sync = self.conn.execute("PRAGMA synchronous").fetchone()[0]
        journal = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
        if sync == 0:
            return "NONE (synchronous=OFF; i_accept_data_loss)"
        if journal == "WAL" and sync == 1:  # NORMAL
            return "process-crash-safe (WAL+NORMAL); power-loss requires FULL"
        return "process-and-power-crash-safe (WAL+FULL)"

    def close(self) -> None:
        self.conn.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # releases the flock
            self._lock_fd = None

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
        self,
        key: str,
        scope: str,
        tool: str,
        fingerprint: str,
        *,
        provider_dedup: bool = False,
        reconcilable: bool = False,
    ) -> Claim:
        """Atomically decide what to do with ``key`` and take ownership if we run it."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, result, fingerprint, result_ref FROM effects WHERE key = ?",
                (key,),
            ).fetchone()

            if row is None:
                self.conn.execute(
                    "INSERT INTO effects "
                    "(key, scope, tool, fingerprint, state, provider_dedup, reconcilable, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        scope,
                        tool,
                        fingerprint,
                        EffectState.CLAIMED.value,
                        int(provider_dedup),
                        int(reconcilable),
                        _now(),
                    ),
                )
                claim = Claim(ClaimKind.PROCEED)

            else:
                state = EffectState(row[0])
                if state is EffectState.SUCCEEDED:
                    result: object
                    if row[3] is not None:  # result_ref → the result was unserializable
                        result = Replayed(key=key, note=row[3])
                    else:
                        result = None if row[1] is None else json.loads(row[1])
                    claim = Claim(ClaimKind.REPLAY, result=result, fingerprint=row[2])
                elif state is EffectState.AMBIGUOUS:
                    claim = Claim(ClaimKind.AMBIGUOUS)
                elif state is EffectState.EXECUTING:
                    # No death-evidence in the claim path (single-worker ≠ single-
                    # thread) → refuse to transition. Recovery is the sole owner of
                    # EXECUTING → {AMBIGUOUS | re-claim}. (Applies to L2 too: one rule.)
                    raise EffectInFlightError(
                        f"{key}: row is EXECUTING — a concurrent guard of this key, or a "
                        "missed recovery. This transition belongs to recover() (run at "
                        "startup), not to claim."
                    )
                else:
                    # Re-own and retry: CLAIMED (crash before dispatch) or a declared
                    # clean FAILED — the effect provably never completed.
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
        # Execution truth outranks result fidelity: the effect happened. If the
        # result won't serialize, record SUCCEEDED with a marker (replay returns the
        # marker) — never raise here, which would leave a lie or a retriable state.
        encoded: str | None
        marker: str | None
        try:
            encoded, marker = json.dumps(result), None
        except (TypeError, ValueError):
            encoded, marker = None, f"unserializable:{type(result).__name__}"
        self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, result_ref = ?, settled_at = ? "
            "WHERE key = ?",
            (EffectState.SUCCEEDED.value, encoded, marker, _now(), key),
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

        L0 rows → `AMBIGUOUS` (surfaced). L2 rows (provider-deduplicating, not
        reconcilable) → `CLAIMED`, so the next attempt safely re-dispatches.
        **Reconcilable rows (L1/L2R) are left `EXECUTING`** — only the engine has
        their reconcile function; it resolves them via :meth:`pending_reconcile`.
        Returns the keys left `AMBIGUOUS`.
        """
        rows = self.conn.execute(
            "SELECT key, provider_dedup, reconcilable FROM effects WHERE state = ?",
            (EffectState.EXECUTING.value,),
        ).fetchall()
        ambiguous: list[str] = []
        for key, dedup, reconcilable in rows:
            if reconcilable:
                pass  # engine-driven; see pending_reconcile()
            elif dedup:
                self.conn.execute(
                    "UPDATE effects SET state = ? WHERE key = ?", (EffectState.CLAIMED.value, key)
                )
            else:
                self.conn.execute(
                    "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                    (EffectState.AMBIGUOUS.value, _now(), key),
                )
                ambiguous.append(key)
            seam("during_recovery")  # kill mid-scan; recovery must be idempotent over itself
        return ambiguous

    # --- engine-driven reconciliation (L1 / L2R) --------------------------
    def pending_reconcile(self) -> list[tuple[str, str]]:
        """`(key, tool)` for `EXECUTING` reconcilable rows awaiting the engine."""
        rows = self.conn.execute(
            "SELECT key, tool FROM effects WHERE state = ? AND reconcilable = 1",
            (EffectState.EXECUTING.value,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def settle_reconciled(self, key: str, result: object) -> None:
        """Reconcile found the effect SETTLED — adopt the provider's record."""
        self.record_success(key, result)

    def reclaim(self, key: str) -> None:
        """Reconcile found the effect ABSENT (or it's a re-dispatchable leftover) —
        make it re-claimable."""
        self.conn.execute(
            "UPDATE effects SET state = ? WHERE key = ?", (EffectState.CLAIMED.value, key)
        )

    def ambiguate(self, key: str) -> None:
        """Reconcile could not determine the outcome (UNKNOWN) — surface it."""
        self.conn.execute(
            "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
            (EffectState.AMBIGUOUS.value, _now(), key),
        )

    def keys_in(self, state: EffectState) -> Iterable[str]:
        rows = self.conn.execute(
            "SELECT key FROM effects WHERE state = ?", (state.value,)
        ).fetchall()
        return [r[0] for r in rows]
