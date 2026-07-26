# SPDX-License-Identifier: Apache-2.0
"""The ledger — the durable write-ahead record of every effect.

Answers "has this action already happened, and what did it return?" It is a
**write-ahead intent log**, not a check-then-act cache: the ``EXECUTING`` mark
commits *before* the effect dispatches, so a crash in the window leaves evidence
(``docs/design.md`` §8).

Act II subset of the state machine::

    (new) --claim--> CLAIMED --mark--> EXECUTING --record--> SUCCEEDED
      ^                 |                   |                    |
      │ claim           │ crash             │ crash             ├── clean-fail ──> FAILED
      │ re-owns         v                   v                   └── crash ──> EXECUTING stays
    INTENDED <──recover── (leftover)   recovery: L2 → INTENDED,
    (recovery-blessed;                 L0 → AMBIGUOUS, L1/L2R → reconcile
     the only re-claimable state — claim refuses live CLAIMED/EXECUTING, P3-1)

L1/L2R reconciliation and the multi-worker contention protocol (leases, fencing,
late evidence) are implemented below. What remains ("Act III-M"): a Postgres
backend and a concurrent settle loop that drives that protocol under real load.
Deferred to Act IV: ``BUFFERED`` (the outbox / approval gating).
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
    BUSY = "busy"  # a live lease holds it (multi-worker) — wait for the owner's result


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    result: object | None = None
    fingerprint: str | None = None
    fencing_token: int = 0  # multi-worker: guards this owner's writes against a zombie


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
  settled_at     TEXT,
  fencing_token  INTEGER NOT NULL DEFAULT 0,
  lease_owner    TEXT,
  lease_expires  REAL,
  resolved_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_scope_state ON effects (scope, state);
"""


class SqliteLedger:
    """A durable, single-writer ledger backed by SQLite."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        i_accept_data_loss: bool = False,
        multi_worker: bool = False,
        owner: str | None = None,
    ) -> None:
        self._path = str(path)
        self._lock_fd: int | None = None
        self._multi_worker = multi_worker
        self.owner = owner or f"worker-{os.getpid()}-{id(self):x}"
        # Q13 — single-worker (default) is enforced with flock, so it's a *property*,
        # not a hope. In multi-worker mode, leases + fencing coordinate instead, so
        # the flock is intentionally skipped (a shared file DB, or Postgres for scale).
        if self._path != ":memory:" and not multi_worker:
            self._acquire_single_worker_lock()
        # isolation_level=None → autocommit; we drive BEGIN IMMEDIATE / COMMIT
        # ourselves so a claim is one atomic statement group.
        # Single-worker is single-thread-per-connection (SQLite's own guard raises if
        # a second thread touches this connection — the accidental protection the
        # CLAIMED-race fix, P3-1, no longer relies on but should not remove). Only the
        # multi-worker path, where each thread holds its own connection, opts out.
        self.conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=not multi_worker
        )
        self._configure_durability(i_accept_data_loss)  # Q12
        if multi_worker:
            self.conn.execute("PRAGMA busy_timeout=5000")  # concurrent writers wait, don't error
        self.conn.executescript(_SCHEMA)

    def _db_now(self) -> float:
        """Wall-clock seconds from the DB's own clock — the single source of truth
        for lease math (worker clock skew must not decide expiry; Q21)."""
        return float(self.conn.execute("SELECT strftime('%s','now')").fetchone()[0])

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
                elif state in (EffectState.EXECUTING, EffectState.CLAIMED):
                    # No death-evidence in the claim path (single-worker ≠ single-
                    # thread — parallel branches / tool-calls can hold a *live* row) →
                    # refuse to transition. Recovery is the sole owner of both
                    # EXECUTING → {AMBIGUOUS | re-claimable} and CLAIMED → re-claimable;
                    # it blesses a crash artifact as INTENDED, which we re-own below.
                    raise EffectInFlightError(
                        f"{key}: row is {state.value} — a concurrent guard of this key, or a "
                        "missed recovery. This transition belongs to recover() (run at "
                        "startup), not to claim."
                    )
                else:
                    # Re-own and retry: INTENDED (recovery blessed a crash-before-dispatch
                    # leftover) or a declared-clean FAILED — the effect provably never
                    # completed. Fresh fingerprint for the retry.
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
        """Startup scan (death-evidence: process start ⇒ nothing is in flight).

        - `CLAIMED` leftovers (crash *before* dispatch — nothing happened) → `INTENDED`
          (recovery-blessed, re-claimable). Claim itself refuses `CLAIMED` (P3-1),
          so recovery is the sole path to re-execution.
        - L0 `EXECUTING` (crash in the window) → `AMBIGUOUS` (surfaced).
        - L2 `EXECUTING` (provider-deduplicating) → `INTENDED`, so the next attempt
          safely re-dispatches.
        - Reconcilable `EXECUTING` (L1/L2R) → left for the engine (:meth:`pending_reconcile`).

        Returns the keys left `AMBIGUOUS`.
        """
        for (key,) in self.conn.execute(
            "SELECT key FROM effects WHERE state = ?", (EffectState.CLAIMED.value,)
        ).fetchall():
            self.conn.execute(
                "UPDATE effects SET state = ? WHERE key = ?", (EffectState.INTENDED.value, key)
            )
            seam("during_recovery")

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
                    "UPDATE effects SET state = ? WHERE key = ?", (EffectState.INTENDED.value, key)
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
        """Reconcile (inside recover, so death-evidence holds) found the effect ABSENT —
        bless it re-claimable as `INTENDED`. The next `claim` re-owns it (P3-1: `claim`
        refuses `CLAIMED`, so re-ownership must route through the INTENDED marker)."""
        self.conn.execute(
            "UPDATE effects SET state = ? WHERE key = ?", (EffectState.INTENDED.value, key)
        )

    def ambiguate(self, key: str) -> None:
        """Reconcile could not determine the outcome (UNKNOWN) — surface it."""
        self.conn.execute(
            "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
            (EffectState.AMBIGUOUS.value, _now(), key),
        )

    # --- multi-worker contention: leases, fencing, late evidence ----------
    #
    # The protocol below is the foundation for the multi-worker ("Act III-M")
    # path. It is verified deterministically here; wiring it into a concurrent
    # settle loop over a Postgres backend, plus true-concurrency chaos, is the
    # remaining Act III-M work. Single-worker deployments never touch it.
    def claim_leased(
        self,
        key: str,
        scope: str,
        tool: str,
        fingerprint: str,
        *,
        owner: str,
        lease_seconds: float,
        now: float | None = None,
        provider_dedup: bool = False,
        reconcilable: bool = False,
    ) -> Claim:
        """Atomic lease-based claim (multi-worker).

        A live lease held by another worker → ``BUSY`` (wait for its result). An
        expired lease is a presumed-dead owner → **takeover by ladder**: a pre-
        dispatch (``CLAIMED``) row or an L2/L1 ``EXECUTING`` row is re-owned with a
        bumped fencing token; an L0 ``EXECUTING`` row cannot be safely retried, so it
        surfaces ``AMBIGUOUS`` (forbidden takeover). Fencing makes a returning zombie
        harmless: its stale-token writes are rejected (see :meth:`fence`).
        """
        if now is None:
            now = self._db_now()  # DB clock only (Q21)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, result, fingerprint, result_ref, fencing_token, lease_owner, "
                "lease_expires FROM effects WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                token = 1
                self.conn.execute(
                    "INSERT INTO effects (key, scope, tool, fingerprint, state, provider_dedup, "
                    "reconcilable, created_at, fencing_token, lease_owner, lease_expires) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        scope,
                        tool,
                        fingerprint,
                        EffectState.CLAIMED.value,
                        int(provider_dedup),
                        int(reconcilable),
                        _now(),
                        token,
                        owner,
                        now + lease_seconds,
                    ),
                )
                claim = Claim(ClaimKind.PROCEED, fencing_token=token)
            else:
                state = EffectState(row[0])
                token, lease_owner, lease_expires = row[4], row[5], row[6]
                if state is EffectState.SUCCEEDED:
                    result = (
                        Replayed(key, row[3])
                        if row[3] is not None
                        else (None if row[1] is None else json.loads(row[1]))
                    )
                    claim = Claim(ClaimKind.REPLAY, result=result, fingerprint=row[2])
                elif state is EffectState.AMBIGUOUS:
                    claim = Claim(ClaimKind.AMBIGUOUS)
                elif lease_expires is not None and lease_expires > now and lease_owner != owner:
                    claim = Claim(ClaimKind.BUSY)  # a live owner holds it
                elif state is EffectState.EXECUTING and not (provider_dedup or reconcilable):
                    # L0 expired-lease takeover is forbidden — no safe retry exists.
                    self.conn.execute(
                        "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                        (EffectState.AMBIGUOUS.value, _now(), key),
                    )
                    claim = Claim(ClaimKind.AMBIGUOUS)
                else:
                    # Expired lease on a re-ownable row → take over, bump the fence.
                    new_token = token + 1
                    self.conn.execute(
                        "UPDATE effects SET state = ?, fingerprint = ?, fencing_token = ?, "
                        "lease_owner = ?, lease_expires = ? WHERE key = ?",
                        (
                            EffectState.CLAIMED.value,
                            fingerprint,
                            new_token,
                            owner,
                            now + lease_seconds,
                            key,
                        ),
                    )
                    claim = Claim(ClaimKind.PROCEED, fencing_token=new_token)
            self.conn.execute("COMMIT")
            return claim
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def fence(self, key: str, token: int, state: EffectState, *, result: object = None) -> bool:
        """A fenced state transition: applies only if ``token`` is still current.

        Returns whether it applied. A zombie worker whose lease was taken over
        carries a stale token, so its writes are rejected — it cannot corrupt a row
        it no longer owns.
        """
        terminal = state in (EffectState.SUCCEEDED, EffectState.FAILED, EffectState.AMBIGUOUS)
        encoded = json.dumps(result) if state is EffectState.SUCCEEDED else None
        cur = self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, settled_at = ? "
            "WHERE key = ? AND fencing_token = ?",
            (state.value, encoded, _now() if terminal else None, key, token),
        )
        return cur.rowcount > 0

    def heartbeat(
        self, key: str, owner: str, lease_seconds: float, now: float | None = None
    ) -> bool:
        """Extend our lease so a slow-but-alive worker is not presumed dead."""
        if now is None:
            now = self._db_now()
        cur = self.conn.execute(
            "UPDATE effects SET lease_expires = ? WHERE key = ? AND lease_owner = ?",
            (now + lease_seconds, key, owner),
        )
        return cur.rowcount > 0

    def accept_late_evidence(self, key: str, result: object) -> bool:
        """A returning zombie's terminal outcome, recorded onto an AMBIGUOUS row.

        AMBIGUOUS has no live owner, and the zombie is the only party that knows
        what happened — so the write strictly increases information and is accepted
        (self-healing spurious ambiguity). Applies only while the row is AMBIGUOUS.
        """
        cur = self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, resolved_by = ?, settled_at = ? "
            "WHERE key = ? AND state = ?",
            (
                EffectState.SUCCEEDED.value,
                json.dumps(result),
                "late_evidence",
                _now(),
                key,
                EffectState.AMBIGUOUS.value,
            ),
        )
        return cur.rowcount > 0

    def keys_in(self, state: EffectState) -> Iterable[str]:
        rows = self.conn.execute(
            "SELECT key FROM effects WHERE state = ?", (state.value,)
        ).fetchall()
        return [r[0] for r in rows]
