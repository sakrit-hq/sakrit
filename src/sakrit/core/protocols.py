# SPDX-License-Identifier: Apache-2.0
"""The backend seam — ``Ledger`` and ``LeasedLedger`` Protocols (P5-5).

The core drives the ledger through these Protocols, never a concrete class, so a second
backend (Postgres) can satisfy the contract structurally instead of subclassing a class
named ``Sqlite`` (a lie in public types) or forcing a breaking signature change. Each
Protocol carries **exactly** the surface the core consumes — grep-verified against
``settle``, ``settle_leased[_async]``, and the engine — split into the single-worker core
(``Ledger``) and the multi-worker extension (``LeasedLedger``).

**This is also the Postgres-port checklist.** "Postgres is a storage swap" is only true of
the *primitives*; several methods must be implemented *differently* under MVCC, and the
obligation lives on the method that carries it (see ``docs/dev-notes/ledger-protocol.md``):

- ``claim`` / ``claim_leased`` — SQLite's ``BEGIN IMMEDIATE`` (writer-lock-at-BEGIN) has no
  Postgres analogue: use ``SELECT … FOR UPDATE`` + ``INSERT … ON CONFLICT`` (two workers
  inserting one new key otherwise raises a unique violation under MVCC), plus
  serialization-failure retries. The claim MUST remain atomic-per-key. ``claim_leased``'s
  takeover divergence gate MUST compare through the ``verify`` callable when it is supplied
  (the dual-secret rotation window, C-1), not a raw byte-compare of the stored fingerprint —
  else a rotation bricks every old-signed non-terminal row.
- ``recover`` / ``pending_reconcile`` — the scan wants ``SKIP LOCKED`` thinking, and MUST
  read committed state.
- ``fence`` — the conditional UPDATE-by-token ports cleanly, but MUST stay a single
  conditional write (write-once on terminal states; the token gate is the fence).

``SqliteLedger`` satisfies both structurally — mypy checks conformance wherever a concrete
``SqliteLedger`` reaches a Protocol-typed parameter (a ``Sakrit(SqliteLedger(...))`` construction,
the settle calls). A backend that satisfies the *signatures* but not these *semantics* is a
silent exactly-once break — the semantics are the real contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from sakrit.core.ledger import Claim, EffectState


@runtime_checkable
class Ledger(Protocol):
    """The single-worker durable ledger surface — what ``settle`` and the engine's recovery
    consume. Atomic ``claim``, the write-ahead transitions, and the recovery scan."""

    # Coordination mode (the engine feature-gates single-worker vs leased on this).
    @property
    def multi_worker(self) -> bool: ...

    # --- the atomic claim + write-ahead transitions ---
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
        secret_id: str | None = None,
    ) -> Claim: ...

    def mark_executing(self, key: str) -> None: ...

    def record_success(self, key: str, result: object) -> None: ...

    def record_failure(self, key: str, error: BaseException) -> None: ...

    # --- recovery (engine-driven L1/L2R reconciliation lives above this) ---
    def recover(self) -> list[str]: ...

    def pending_reconcile(self) -> list[tuple[str, str]]: ...

    def settle_reconciled(self, key: str, result: object) -> None: ...

    def reclaim(self, key: str) -> None: ...

    def ambiguate(self, key: str) -> None: ...

    # --- observability: "…or tells you it couldn't" / a served replay is told ---
    def _tell_replay(self, key: str) -> None: ...


@runtime_checkable
class LeasedLedger(Ledger, Protocol):
    """The multi-worker extension — leases, fencing, heartbeat, late evidence — consumed by
    ``settle_leased`` / ``settle_leased_async`` on top of the single-worker ``Ledger``."""

    # This worker's identity, for lease ownership.
    owner: str

    def state_of(self, key: str) -> EffectState | None: ...

    def recorded_result(self, key: str) -> object: ...

    def fingerprint_of(self, key: str) -> str | None: ...

    def secret_id_of(self, key: str) -> str | None: ...

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
        secret_id: str | None = None,
        verify: Callable[[str, str | None], bool] | None = None,
    ) -> Claim: ...

    def fence(self, key: str, token: int, state: EffectState, *, result: object = None) -> bool: ...

    def heartbeat(
        self, key: str, owner: str, lease_seconds: float, now: float | None = None
    ) -> bool: ...

    def accept_late_evidence(
        self, key: str, result: object = None, *, failed: bool = False
    ) -> bool: ...
