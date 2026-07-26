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
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sakrit.core.errors import DivergentRetry, EffectInFlightError, SakritError
from sakrit.core.seams import seam

logger = logging.getLogger("sakrit")

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


# Terminal states are write-once — a settled outcome must not be overwritten (P3-6a).
_TERMINAL_STATES = (EffectState.SUCCEEDED, EffectState.FAILED, EffectState.AMBIGUOUS)
_TERMINAL_VALUES = tuple(s.value for s in _TERMINAL_STATES)


class ClaimKind(Enum):
    PROCEED = "proceed"  # we own a fresh/re-claimable row — execute it
    REPLAY = "replay"  # already SUCCEEDED — return the saved result (check fp)
    AMBIGUOUS = "ambiguous"  # crashed in the window — must surface, do not execute
    BUSY = "busy"  # a live lease holds it (multi-worker) — wait for the owner's result
    RECONCILE = "reconcile"  # took over a reconcilable in-flight row — ask "did it happen?"
    # (multi-worker L1/L2R takeover) before deciding to adopt, re-dispatch, or surface


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


def _within_ttl(created_at: str, ttl_s: float | None) -> bool:
    """Whether a row claimed at ``created_at`` is still inside its provider key-TTL.

    ``ttl_s=None`` means unbounded (always within). Uses wall-clock, matching how
    ``created_at`` was written — a same-machine restart's skew is negligible against a
    provider TTL measured in hours (P1-5)."""
    if ttl_s is None:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds()
    return age <= ttl_s


_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
  key            TEXT PRIMARY KEY,
  scope          TEXT NOT NULL,
  tool           TEXT NOT NULL,
  fingerprint    TEXT NOT NULL,
  state          TEXT NOT NULL,
  provider_dedup INTEGER NOT NULL DEFAULT 0,
  provider_ttl_s REAL,
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


# Coordination-mode stamp, written to the SQLite header (PRAGMA user_version). 0 means
# "unstamped" (fresh or legacy); a mismatched open is refused (P3-3).
_MODE_SINGLE = 1
_MODE_MULTI = 2


class SqliteLedger:
    """A durable, single-writer ledger backed by SQLite.

    ``path`` is **required** (P1-13): there is no default. For a durability library the unsafe
    thing is the zero-argument path — ``SqliteLedger()`` used to default to an ephemeral
    in-memory DB that passes every in-process test and then loses everything on the first real
    crash, silently reinstating the dual-write hole. Choose explicitly: a file path for
    durability, or the literal ``":memory:"`` for a deliberately ephemeral dev/test ledger.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        i_accept_data_loss: bool = False,
        multi_worker: bool = False,
        owner: str | None = None,
        on_ambiguous: Callable[[str], None] | None = None,
        on_replay: Callable[[str], None] | None = None,
    ) -> None:
        self._path = str(path)
        self._lock_fd: int | None = None
        self._multi_worker = multi_worker
        # P3-10(e) / V-6b: the owner id must be globally unique — a *collision* is a silent
        # duplicate (two workers sharing an id both pass the `lease_owner != owner` BUSY test
        # → both PROCEED on one live row). `owner=` is therefore a **label, not an identity**:
        # we always suffix a uuid, so ops keep a readable prefix (`pod-3-a1b2c3d4`) and
        # uniqueness is unconditional — even against a copied config, a pod name, or a
        # `gethostname()`. Heartbeat / BUSY compare the stored value, so the suffix is
        # transparent to the protocol.
        self.owner = f"{owner}-{uuid.uuid4().hex[:8]}" if owner else f"worker-{uuid.uuid4().hex}"
        # V-3: a multi_worker ledger is one connection == one worker. It runs with
        # check_same_thread=False (leases coordinate instead), so SQLite won't catch a
        # connection shared across worker threads — which defeats BEGIN IMMEDIATE's
        # per-connection serialization. We bind the connection to its first *claiming*
        # thread and refuse a claim from any other (the heartbeat thread only heartbeats).
        self._claim_thread: int | None = None
        self._claim_thread_lock = threading.Lock()
        # P1-6: the differentiating half of the guarantee is "…or tells you it couldn't."
        # Every transition *into* AMBIGUOUS routes through _tell_ambiguous, which logs a
        # warning (never silent) and fires this optional alert/metric hook.
        self._on_ambiguous = on_ambiguous
        # V-2 rider: a served replay (recorded result returned, effect NOT re-fired) is
        # routine on resume/retry — but it must be *told*, so an operator asking "why didn't
        # the repeat fire?" finds the answer. Logged at INFO; fires this optional hook.
        self._on_replay = on_replay
        # P3-3: multi-worker coordination is *between* workers over one shared database.
        # An in-memory DB is private to its connection, so N multi-worker processes would
        # get N isolated ledgers — zero dedup, N× execution, silently. Refuse it.
        if multi_worker and self._path == ":memory:":
            raise SakritError(
                "multi_worker=True needs a shared database, but path is ':memory:' — each "
                "connection would get a private, empty ledger (no dedup across workers). "
                "Pass a file path (or a Postgres backend, when it lands)."
            )
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
        if multi_worker:
            # Set busy_timeout FIRST, before any write. Setup itself contends — several
            # workers opening one fresh file race on `PRAGMA journal_mode=WAL` and the
            # schema/mode-stamp writes; without a busy timeout the loser gets an immediate
            # "database is locked" at construction. With it, concurrent writers wait.
            self.conn.execute("PRAGMA busy_timeout=5000")
        self._configure_durability(i_accept_data_loss)  # Q12
        self.conn.executescript(_SCHEMA)
        self._enforce_mode_stamp(multi_worker)

    def _enforce_mode_stamp(self, multi_worker: bool) -> None:
        """Stamp the database with its coordination mode and refuse a mismatched open
        (P3-3). The fenced (multi-worker) and unfenced (single-worker) protocols disagree
        on what EXECUTING *means*; letting them share one file corrupts exactly-once. The
        stamp lives in the SQLite header (``user_version``), so the check is machine-made,
        not caller-promised."""
        want = _MODE_MULTI if multi_worker else _MODE_SINGLE
        stamped = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if stamped == 0:  # freshly created (or a legacy pre-stamp DB) → claim it
            self.conn.execute(f"PRAGMA user_version = {want}")
        elif stamped != want:
            have = "multi-worker" if stamped == _MODE_MULTI else "single-worker"
            need = "multi-worker" if multi_worker else "single-worker"
            raise SakritError(
                f"ledger at {self._path!r} was created in {have} mode; refusing to open it "
                f"{need}. The fenced and unfenced protocols must not share a database."
            )

    @property
    def multi_worker(self) -> bool:
        """Whether this ledger coordinates concurrent workers (leases + fencing). Off →
        a single-thread-bound connection; a background writer (heartbeat) is illegal.

        The coordination model is **one ledger (connection) per worker** — leases and
        fencing coordinate *across* connections, not within one. Sharing a single
        ``multi_worker`` ledger across worker threads is refused (V-3); give each worker
        its own ``SqliteLedger(path, multi_worker=True)``."""
        return self._multi_worker

    def _bind_claim_thread(self) -> None:
        """Bind this connection to its first claiming thread; refuse a claim from another
        (V-3). No-op for a single-worker ledger (SQLite's own same-thread guard covers it)."""
        if not self._multi_worker:
            return
        tid = threading.get_ident()
        with self._claim_thread_lock:
            if self._claim_thread is None:
                self._claim_thread = tid
            elif self._claim_thread != tid:
                raise SakritError(
                    "one SqliteLedger(multi_worker=True) connection per worker: this ledger "
                    f"was first claimed from thread {self._claim_thread} and is now used from "
                    f"{tid}. Sharing a single connection across workers defeats the atomic "
                    "claim (BEGIN IMMEDIATE serializes per connection). Give each worker its "
                    "own SqliteLedger(path, multi_worker=True)."
                )

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
        self._set_wal_with_retry()
        # P1-14: don't just *set* the pragmas — verify they took. A SET that silently
        # didn't apply (a read-only mount, a driver quirk) would leave the durability claim
        # a lie. synchronous FULL == 2 (OFF=0, NORMAL=1, FULL=2, EXTRA=3).
        self.conn.execute("PRAGMA synchronous=FULL")
        sync = self.conn.execute("PRAGMA synchronous").fetchone()[0]
        if sync not in (2, 3):  # FULL or the even-stricter EXTRA
            raise SakritError(
                f"could not set synchronous=FULL (got {sync}) — power-loss durability is not "
                "guaranteed; pass i_accept_data_loss=True to opt out explicitly."
            )

    def _set_wal_with_retry(self, attempts: int = 50, delay: float = 0.02) -> None:
        """Enable WAL, tolerating concurrent cold-start. Converting a fresh file to WAL
        needs a brief exclusive moment, and SQLite's WAL conversion does *not* honor
        busy_timeout — so N workers opening one new file race it and the losers get an
        immediate "database is locked". Retry a bounded number of times; fail loud if WAL
        never takes (a durability claim we couldn't keep must not be silent)."""
        for i in range(attempts):
            try:
                mode = self.conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if str(mode).lower() == "wal":
                    return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or i == attempts - 1:
                    raise
            time.sleep(delay)
        mode = str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            raise SakritError(
                f"could not enable WAL journal mode (got {mode!r}) under contention — "
                "durability is not guaranteed; retry, or provision the database once first."
            )

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

    def fingerprint_of(self, key: str) -> str | None:
        """The identity fingerprint stored on ``key`` (``None`` if absent) — for the adopt
        path's divergence check (V-10)."""
        row = self.conn.execute("SELECT fingerprint FROM effects WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def recorded_result(self, key: str) -> object:
        """The result stored on ``key`` — the same decoding as a REPLAY (a ``Replayed``
        marker for an unserializable result, else the decoded value). Raises if absent."""
        row = self.conn.execute(
            "SELECT result, result_ref FROM effects WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        result, result_ref = row
        if result_ref is not None:
            return Replayed(key, result_ref)
        return None if result is None else json.loads(result)

    # --- the atomic claim -------------------------------------------------
    def claim(
        self,
        key: str,
        scope: str,
        tool: str,
        fingerprint: str,
        *,
        provider_dedup: bool = False,
        provider_ttl_s: float | None = None,
        reconcilable: bool = False,
    ) -> Claim:
        """Atomically decide what to do with ``key`` and take ownership if we run it."""
        self._bind_claim_thread()  # V-3
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, result, fingerprint, result_ref FROM effects WHERE key = ?",
                (key,),
            ).fetchone()

            if row is None:
                self.conn.execute(
                    "INSERT INTO effects "
                    "(key, scope, tool, fingerprint, state, provider_dedup, provider_ttl_s, "
                    "reconcilable, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        scope,
                        tool,
                        fingerprint,
                        EffectState.CLAIMED.value,
                        int(provider_dedup),
                        provider_ttl_s,
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
        #
        # P4-4: what's stored is ``json.dumps(result)``, so a *replay* returns
        # ``json.loads`` of it — a lossy transform: a tuple comes back a list, int dict-keys
        # come back strings, a dataclass/object falls to the ``Replayed`` marker. The
        # exactly-once invariant is untouched (nothing re-fires), but "replay the recorded
        # result" is really "replay a JSON reconstruction of it". Never silent: when the
        # result won't round-trip cleanly, tell the operator (a full result-type declaration +
        # rehydration is Act IV). See the ``guard`` return contract.
        encoded: str | None
        marker: str | None
        try:
            encoded, marker = json.dumps(result), None
            if json.loads(encoded) != result:
                logger.info(
                    "sakrit: %s result (%s) will not JSON-round-trip — a replay returns a lossy "
                    "reconstruction (e.g. tuple→list, int keys→str)",
                    key,
                    type(result).__name__,
                )
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
            "SELECT key, provider_dedup, provider_ttl_s, reconcilable, created_at "
            "FROM effects WHERE state = ?",
            (EffectState.EXECUTING.value,),
        ).fetchall()
        ambiguous: list[str] = []
        for key, dedup, ttl_s, reconcilable, created_at in rows:
            if reconcilable:
                pass  # engine-driven; see pending_reconcile()
            elif dedup and _within_ttl(created_at, ttl_s):
                # L2 within the provider's key-TTL → the provider still dedups a retry.
                self.conn.execute(
                    "UPDATE effects SET state = ? WHERE key = ?", (EffectState.INTENDED.value, key)
                )
            elif dedup:
                # L2 past the horizon (P1-5): the provider has forgotten the key, so a
                # re-dispatch would NOT dedup → surface rather than silently duplicate.
                self.conn.execute(
                    "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                    (EffectState.AMBIGUOUS.value, _now(), key),
                )
                ambiguous.append(key)
                self._tell_ambiguous(key, "L2 leftover older than the provider key TTL")
            else:
                self.conn.execute(
                    "UPDATE effects SET state = ?, settled_at = ? WHERE key = ?",
                    (EffectState.AMBIGUOUS.value, _now(), key),
                )
                ambiguous.append(key)
                self._tell_ambiguous(key, "L0 effect crashed in the dispatch window")
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
        self._tell_ambiguous(key, "reconcile could not determine the outcome")

    def _tell_ambiguous(self, key: str, reason: str) -> None:
        """Announce that a row is now AMBIGUOUS (P1-6). Ambiguity is never silent: a
        crash left an effect that may or may not have landed, and only a human/operator
        can resolve it. Always logs; also fires the ``on_ambiguous`` hook if configured.
        A misbehaving hook must not corrupt the ledger, so its exceptions are logged and
        swallowed — the row is already durably AMBIGUOUS regardless."""
        logger.warning("sakrit: effect %s is AMBIGUOUS (%s) — resolve it", key, reason)
        if self._on_ambiguous is not None:
            try:
                self._on_ambiguous(key)
            except Exception:  # noqa: BLE001 — never let alerting break the ledger
                logger.exception("sakrit: on_ambiguous hook raised for %s", key)

    def _tell_replay(self, key: str) -> None:
        """Announce that a recorded result was served instead of re-running the effect
        (V-2 rider). Routine on resume/retry — INFO, not an alert — but *told*, so a
        repeat that was intentionally not re-fired (P4-1's pinned swallow) is visible to an
        operator, not silent. Fires the optional ``on_replay`` hook; a raising hook is
        logged and swallowed (a replay is not a failure)."""
        logger.info("sakrit: %s served a recorded result — effect not re-fired (replay)", key)
        if self._on_replay is not None:
            try:
                self._on_replay(key)
            except Exception:  # noqa: BLE001
                logger.exception("sakrit: on_replay hook raised for %s", key)

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
        provider_ttl_s: float | None = None,
        reconcilable: bool = False,
    ) -> Claim:
        """Atomic lease-based claim (multi-worker).

        A live lease held by another worker → ``BUSY`` (wait for its result). An
        expired lease is a presumed-dead owner → **takeover by ladder**: a pre-
        dispatch (``CLAIMED``) row or an L2/L1 ``EXECUTING`` row is re-owned with a
        bumped fencing token; an L0 ``EXECUTING`` row cannot be safely retried, so it
        surfaces ``AMBIGUOUS`` (forbidden takeover). An L2 ``EXECUTING`` row past its
        provider key-TTL also surfaces ``AMBIGUOUS`` — a re-dispatch would no longer
        dedup (P1-5, leased variant). Fencing makes a returning zombie harmless: its
        stale-token writes are rejected (see :meth:`fence`).
        """
        self._bind_claim_thread()  # V-3: one connection per worker
        if now is None:
            now = self._db_now()  # DB clock only (Q21)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT state, result, fingerprint, result_ref, fencing_token, lease_owner, "
                "lease_expires, created_at, provider_ttl_s FROM effects WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                token = 1
                self.conn.execute(
                    "INSERT INTO effects (key, scope, tool, fingerprint, state, provider_dedup, "
                    "provider_ttl_s, reconcilable, created_at, fencing_token, lease_owner, "
                    "lease_expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        scope,
                        tool,
                        fingerprint,
                        EffectState.CLAIMED.value,
                        int(provider_dedup),
                        provider_ttl_s,
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
                created_at, row_ttl_s = row[7], row[8]
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
                elif row[2] != fingerprint:
                    # V-10: divergence detection on the takeover path. Every takeover below
                    # transfers the lease and may re-run the action; a taker presenting a
                    # *different* fingerprint is a different logical action for the same key,
                    # and the key names one action (positional identity). Refuse loudly BEFORE
                    # any lease transfer — never run B's action under A's identity, never let B
                    # "resolve" A's row. (REPLAY checks fp caller-side; this is the in-flight
                    # gate the takeover path was missing.)
                    raise DivergentRetry(
                        f"{key}: identity args differ from the in-flight action; the key names "
                        "one action, not one tool — refusing takeover by a divergent caller"
                    )
                elif state is EffectState.EXECUTING and not (provider_dedup or reconcilable):
                    # L0 expired-lease takeover is forbidden — no safe retry exists.
                    # P3-6b: bump the fence so the presumed-dead owner's stale-token write
                    # is rejected if it returns — "zombie writes are rejected" must hold on
                    # this path too, not rely on the accidental un-bumped token.
                    self.conn.execute(
                        "UPDATE effects SET state = ?, fencing_token = fencing_token + 1, "
                        "settled_at = ? WHERE key = ?",
                        (EffectState.AMBIGUOUS.value, _now(), key),
                    )
                    self._tell_ambiguous(key, "L0 lease expired mid-dispatch (forbidden takeover)")
                    claim = Claim(ClaimKind.AMBIGUOUS)
                elif (
                    state is EffectState.EXECUTING
                    and provider_dedup
                    and not reconcilable
                    and not _within_ttl(created_at, row_ttl_s)
                ):
                    # P1-5 (leased variant): an L2 row taken over *past its provider
                    # key-TTL* — the provider has forgotten the key, so re-dispatch would
                    # NOT dedup → silent duplicate. Surface instead (same shape as the L0
                    # forbidden takeover: bump the fence, tell). Within TTL falls through
                    # to PROCEED; a reconcilable (L2R) row reconciles regardless.
                    self.conn.execute(
                        "UPDATE effects SET state = ?, fencing_token = fencing_token + 1, "
                        "settled_at = ? WHERE key = ?",
                        (EffectState.AMBIGUOUS.value, _now(), key),
                    )
                    self._tell_ambiguous(key, "L2 leftover past provider key TTL (leased takeover)")
                    claim = Claim(ClaimKind.AMBIGUOUS)
                elif state is EffectState.EXECUTING:
                    # V-5: an EXECUTING row was mid-flight — something may have run. PRESERVE
                    # that evidence: transfer the lease + bump the fence only. Do NOT
                    # downgrade to CLAIMED (which asserts "nothing ran") and do NOT overwrite
                    # the in-flight fingerprint. If *this* taker dies, or its reconcile
                    # raises, the next takeover still sees EXECUTING and re-decides by ladder
                    # — never a blind PROCEED on a landed effect. (Downgrading first is the
                    # same evidence-erasure bug as Q1/Q2/P3-1: no transition without evidence.)
                    new_token = token + 1
                    self.conn.execute(
                        "UPDATE effects SET fencing_token = ?, lease_owner = ?, "
                        "lease_expires = ? WHERE key = ?",
                        (new_token, owner, now + lease_seconds, key),
                    )
                    # Reconcilable (L1/L2R) → RECONCILE ("did it happen?"); a bare L2 row
                    # (within TTL — past-TTL was surfaced above) → PROCEED (re-dispatch, the
                    # provider dedups). The EXECUTING state is untouched, so the L2-TTL and
                    # forbidden-takeover branches re-fire correctly on any later takeover.
                    kind = ClaimKind.RECONCILE if reconcilable else ClaimKind.PROCEED
                    claim = Claim(kind, fencing_token=new_token)
                else:
                    # CLAIMED (crash *before* dispatch) / INTENDED / FAILED — nothing ran.
                    # Re-own to CLAIMED with a fresh fingerprint and PROCEED.
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
        terminal = state in _TERMINAL_STATES
        encoded: str | None = None
        marker: str | None = None
        if state is EffectState.SUCCEEDED:
            # Q6 (P1-4): execution truth outranks result fidelity. If the result won't
            # serialize, fence SUCCEEDED with a marker (replay returns it) — never raise,
            # which would leave a *succeeded* effect EXECUTING to be re-fired on takeover.
            try:
                encoded = json.dumps(result)
            except (TypeError, ValueError):
                marker = f"unserializable:{type(result).__name__}"
        # P3-6a: terminal states are write-once. The token guard alone let a current-token
        # holder overwrite SUCCEEDED with FAILED (un-settling a landed effect) or un-terminal
        # an AMBIGUOUS row; require the *source* state to be non-terminal (mid-flight). The
        # sanctioned AMBIGUOUS → SUCCEEDED healing goes through accept_late_evidence, not here.
        # P3-10(b): a terminal fence also releases the lease. A clean-FAILED row is
        # re-claimable, but with a live lease a peer would BUSY-wait a whole lease before
        # re-claiming it; clearing the lease lets the next worker take over immediately.
        cur = self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, result_ref = ?, settled_at = ?, "
            "lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END, "
            "lease_expires = CASE WHEN ? THEN NULL ELSE lease_expires END "
            "WHERE key = ? AND fencing_token = ? AND state NOT IN (?, ?, ?)",
            (state.value, encoded, marker, _now() if terminal else None)
            + (terminal, terminal, key, token)
            + _TERMINAL_VALUES,
        )
        applied = cur.rowcount > 0
        if applied and state is EffectState.AMBIGUOUS:
            self._tell_ambiguous(key, "reconcile on takeover could not confirm the outcome")
        return applied

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

    def accept_late_evidence(
        self, key: str, result: object = None, *, failed: bool = False
    ) -> bool:
        """A returning owner's terminal outcome, recorded onto a row it no longer fences.

        The **sole sanctioned path** that resolves an AMBIGUOUS row (P3-6/P3-7) and the one
        deliberate exception to fence's write-once rule. Honor-system: it trusts the caller to
        be the genuine executor — sound because its only caller is ``_record_success_fenced`` /
        ``_record_failure_fenced``, reached only by a worker that actually ran (or provably
        did *not* run) the effect; a bumped fence has already invalidated any zombie's token.

        - ``failed=False`` (success): the effect *happened*. Heal ``AMBIGUOUS`` **or** a peer's
          ``FAILED``→``SUCCEEDED`` — success outranks a clean-failure claim (V-11): a peer's
          "already exists" 4xx that fenced FAILED is corrected by the owner that truly landed it.
        - ``failed=True`` (clean failure): the effect provably did *not* run. Free a spuriously
          ``AMBIGUOUS`` row → ``FAILED`` (re-claimable), so a clean-failed zombie no longer
          strands the row forever (item 6). Never overwrites a ``SUCCEEDED`` (success wins).
        """
        if failed:
            cur = self.conn.execute(
                "UPDATE effects SET state = ?, resolved_by = ?, settled_at = ? "
                "WHERE key = ? AND state = ?",
                (
                    EffectState.FAILED.value,
                    "late_evidence",
                    _now(),
                    key,
                    EffectState.AMBIGUOUS.value,
                ),
            )
            return cur.rowcount > 0
        cur = self.conn.execute(
            "UPDATE effects SET state = ?, result = ?, resolved_by = ?, settled_at = ? "
            "WHERE key = ? AND state IN (?, ?)",
            (
                EffectState.SUCCEEDED.value,
                json.dumps(result),
                "late_evidence",
                _now(),
                key,
                EffectState.AMBIGUOUS.value,
                EffectState.FAILED.value,
            ),
        )
        return cur.rowcount > 0

    def keys_in(self, state: EffectState) -> Iterable[str]:
        rows = self.conn.execute(
            "SELECT key FROM effects WHERE state = ?", (state.value,)
        ).fetchall()
        return [r[0] for r in rows]
