# SPDX-License-Identifier: Apache-2.0
"""settle_leased — the multi-worker settle loop.

Drives the contention protocol (``docs/design.md`` §8 / Fable Q17): acquire a lease
+ fencing token, mark ``EXECUTING`` (fenced), dispatch, record (fenced). A worker
that loses the claim to a live lease **waits** for the owner's result; if the owner
dies (its lease expires), the waiter takes over by ladder. Fencing makes a
returning zombie harmless — its stale-token writes are rejected.

Backend-agnostic over the ledger's lease/fence methods; verified against a shared
SQLite ledger under real thread concurrency (Postgres is a storage swap for scale).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from sakrit.core.context import _current_key
from sakrit.core.errors import AmbiguousOutcome, DivergentRetry
from sakrit.core.ledger import ClaimKind, EffectState, SqliteLedger
from sakrit.core.seams import seam


def settle_leased(
    ledger: SqliteLedger,
    *,
    key: str,
    scope: str,
    tool: str,
    fingerprint: str,
    fn: Callable[..., object],
    args: tuple[object, ...] = (),
    kwargs: Mapping[str, object] | None = None,
    provider_key_param: str | None = None,
    clean_failures: tuple[type[BaseException], ...] = (),
    reconcilable: bool = False,
    lease_seconds: float = 30.0,
    wait_timeout: float = 30.0,
    poll: float = 0.01,
) -> object:
    """Run ``fn`` exactly once for ``key`` across concurrent workers, or return the
    winner's recorded result."""
    deadline = time.time() + wait_timeout
    while True:
        claim = ledger.claim_leased(
            key,
            scope,
            tool,
            fingerprint,
            owner=ledger.owner,
            lease_seconds=lease_seconds,
            provider_dedup=provider_key_param is not None,
            reconcilable=reconcilable,
        )

        if claim.kind is ClaimKind.REPLAY:
            if claim.fingerprint != fingerprint:
                raise DivergentRetry(f"{key}: identity args differ from the recorded action")
            return claim.result
        if claim.kind is ClaimKind.AMBIGUOUS:
            raise AmbiguousOutcome(f"{key}: a prior attempt's outcome is unknown — resolve it")
        if claim.kind is ClaimKind.BUSY:
            # Another worker holds a live lease — wait for its result (or its death,
            # after which the next claim takes over).
            if time.time() > deadline:
                raise AmbiguousOutcome(f"{key}: timed out waiting for the lease owner")
            time.sleep(poll)
            continue

        # PROCEED — we hold the lease and a fencing token.
        token = claim.fencing_token
        if not ledger.fence(key, token, EffectState.EXECUTING):  # write-ahead, fenced
            # P1-3: the fence no-op'd — our token is stale, so a peer took the lease over
            # between claim and here (the exact stop-the-world window fencing exists for).
            # We have dispatched *nothing*; abort before the effect and re-resolve. The
            # new owner holds a live lease → the next claim BUSY-waits for its result.
            if time.time() > deadline:
                raise AmbiguousOutcome(f"{key}: lost the lease before dispatch and timed out")
            continue
        seam("after_mark_executing")
        set_token = _current_key.set(key)
        try:
            result = fn(*args, **dict(kwargs or {}))
            seam("after_dispatch")
        except BaseException as exc:
            if isinstance(exc, clean_failures):
                ledger.fence(key, token, EffectState.FAILED)
            raise
        finally:
            _current_key.reset(set_token)
        ledger.fence(key, token, EffectState.SUCCEEDED, result=result)
        seam("after_record")
        return result
