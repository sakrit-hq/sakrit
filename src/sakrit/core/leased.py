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

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Callable, Mapping

from sakrit.core.context import _current_key
from sakrit.core.errors import AmbiguousOutcome, DivergentRetry, SakritError
from sakrit.core.ledger import ClaimKind, EffectState
from sakrit.core.protocols import LeasedLedger
from sakrit.core.reconcile import Reconciliation, Verdict
from sakrit.core.seams import seam

logger = logging.getLogger("sakrit")

# Sentinel: reconcile-on-takeover decided the effect did NOT land → re-dispatch it.
_DISPATCH = object()


def _record_success_fenced(
    ledger: LeasedLedger, key: str, token: int, result: object, fingerprint: str | None = None
) -> object:
    """Record SUCCEEDED under our fence; if the fenced write is rejected, self-heal (V-7).

    A rejected terminal fence means we lost ownership mid-flight (the heartbeat failed and
    a peer took over) — but we *ran the effect and know its result*. Rather than drop that
    evidence: if the row was ambiguated (a forbidden L0 takeover), heal it via
    ``accept_late_evidence``; if a peer already settled it, adopt the recorded result; any
    other state, surface. This is the sole sanctioned use of the late-evidence path — and
    ``accept_late_evidence`` is token-less precisely because it is *only* reached here, where
    the caller provably executed the effect."""
    if ledger.fence(key, token, EffectState.SUCCEEDED, result=result):
        return result
    state = ledger.state_of(key)
    if state is EffectState.AMBIGUOUS:
        if ledger.accept_late_evidence(key, result):
            logger.warning("sakrit: %s was ambiguated mid-flight; healed by late evidence", key)
            return result
        # Hygiene: the accept no-op'd → between our read and write a *competing* accept
        # settled it (AMBIGUOUS's only legal successor is SUCCEEDED). Adopt the recorded
        # truth rather than blindly returning ours.
        logger.info("sakrit: %s late-evidence lost the race; adopting the recorded result", key)
        return ledger.recorded_result(key)
    if state is EffectState.SUCCEEDED:
        # A peer settled it → adopt the recorded truth. V-10: the takeover fp-gate already
        # bars a divergent peer from recording, but defend the "fingerprint is a guarantee on
        # every path" invariant here too — never adopt a *different* action's result.
        if fingerprint is not None and ledger.fingerprint_of(key) != fingerprint:
            raise DivergentRetry(
                f"{key}: a peer settled this key with a different action; refusing to adopt a "
                "divergent result"
            )
        return ledger.recorded_result(key)
    raise AmbiguousOutcome(
        f"{key}: our result was rejected and the row is {state.value if state else None} — "
        "lost the lease mid-flight and cannot safely record; resolve it"
    )


def _reconcile_on_takeover(
    ledger: LeasedLedger,
    key: str,
    token: int,
    reconcile: Callable[[str], Reconciliation] | None,
    on_absent: str,
    fingerprint: str | None = None,
) -> object:
    """Resolve a RECONCILE claim (P1-2): a worker took over a reconcilable row a
    presumed-dead owner left in flight. Query the provider (read-only ⇒ crash-safe) and:
    ``SETTLED`` → adopt the record; ``ABSENT`` + ``on_absent="retry"`` → return ``_DISPATCH``
    (re-dispatch with the token we hold); else (ABSENT+surface / UNKNOWN / no reconcile) →
    fence ``AMBIGUOUS`` and raise. Shared by the sync and async leased loops."""
    rec = reconcile(key) if reconcile is not None else Reconciliation.unknown()
    if rec.verdict is Verdict.SETTLED:
        return _record_success_fenced(ledger, key, token, rec.result, fingerprint)  # V-7 self-heal
    if rec.verdict is Verdict.ABSENT and on_absent == "retry":
        return _DISPATCH
    ledger.fence(key, token, EffectState.AMBIGUOUS)
    raise AmbiguousOutcome(
        f"{key}: took over a reconcilable in-flight row; provider verdict is "
        f"{rec.verdict.value} — outcome unknown, resolve it"
    )


def _start_heartbeat(
    ledger: LeasedLedger, key: str, lease_seconds: float, interval: float | None
) -> tuple[threading.Event, threading.Thread] | None:
    """Extend our lease while ``fn`` runs, so a slow-but-alive owner is not presumed
    dead (P3-5). Only in multi-worker mode: single-worker has no lease contention, and
    its connection is thread-bound so a background writer would be illegal anyway.
    Returns ``(stop_event, thread)`` or ``None`` if no heartbeat was started."""
    if not ledger.multi_worker:
        return None
    period = interval if interval is not None else max(lease_seconds / 3.0, 0.001)
    stop = threading.Event()

    def _beat() -> None:
        # Renew until told to stop. wait() returns True the instant stop is set, so the
        # thread joins promptly the moment dispatch completes. V-8: a heartbeat that
        # dies silently lets the lease expire → a live owner is presumed dead and a peer
        # takes over mid-dispatch. Never die silently: log and keep trying; a False
        # renewal (row gone / lease lost) is a warning too.
        while not stop.wait(period):
            try:
                if not ledger.heartbeat(key, ledger.owner, lease_seconds):
                    logger.warning("sakrit: heartbeat for %s did not renew (lease lost?)", key)
            except Exception:  # noqa: BLE001 — a transient DB error must not kill the beat
                logger.exception("sakrit: heartbeat for %s raised; will retry", key)

    thread = threading.Thread(target=_beat, name=f"sakrit-heartbeat-{key}", daemon=True)
    thread.start()
    return stop, thread


def settle_leased(
    ledger: LeasedLedger,
    *,
    key: str,
    scope: str,
    tool: str,
    fingerprint: str,
    fn: Callable[..., object],
    args: tuple[object, ...] = (),
    kwargs: Mapping[str, object] | None = None,
    provider_key_param: str | None = None,
    provider_ttl_s: float | None = None,
    clean_failures: tuple[type[BaseException], ...] = (),
    reconcilable: bool = False,
    reconcile: Callable[[str], Reconciliation] | None = None,
    on_absent: str = "surface",
    lease_seconds: float = 30.0,
    heartbeat_interval: float | None = None,
    wait_timeout: float = 30.0,
    poll: float = 0.01,
) -> object:
    """Run ``fn`` exactly once for ``key`` across concurrent workers, or return the
    winner's recorded result.

    ``reconcile`` (with ``reconcilable=True``) makes takeover honest for L1/L2R tools:
    when this worker takes over a row a presumed-dead owner left *in flight*, it asks
    the provider "did it happen?" before acting — adopting the record on ``SETTLED``,
    re-dispatching only on ``ABSENT`` + ``on_absent="retry"``, else surfacing. Without
    it, a reconcilable takeover cannot prove the effect didn't land, so it surfaces."""
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
            provider_ttl_s=provider_ttl_s,
            reconcilable=reconcilable,
        )

        if claim.kind is ClaimKind.REPLAY:
            if claim.fingerprint != fingerprint:
                raise DivergentRetry(f"{key}: identity args differ from the recorded action")
            ledger._tell_replay(key)  # V-2 rider: served-not-re-fired is told, not silent
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
        # P3-10(a): only PROCEED / RECONCILE dispatch. Guard against a future kind slipping
        # into the dispatch path unhandled.
        if claim.kind not in (ClaimKind.PROCEED, ClaimKind.RECONCILE):
            raise SakritError(f"{key}: unexpected claim kind {claim.kind.value} in settle_leased")

        # PROCEED / RECONCILE — we hold the lease and a fencing token.
        token = claim.fencing_token

        if claim.kind is ClaimKind.RECONCILE:
            outcome = _reconcile_on_takeover(ledger, key, token, reconcile, on_absent, fingerprint)
            if outcome is not _DISPATCH:
                return outcome
            # ABSENT + retry: safe to re-dispatch with the token we hold. Fall through.

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
        heartbeat = _start_heartbeat(ledger, key, lease_seconds, heartbeat_interval)
        try:
            try:
                result = fn(*args, **dict(kwargs or {}))
                seam("after_dispatch")
            finally:
                # Stop the heartbeat BEFORE any post-dispatch ledger write — the beat
                # runs on a background thread sharing this connection.
                if heartbeat is not None:
                    stop, thread = heartbeat
                    stop.set()
                    thread.join()
        except BaseException as exc:
            # Hygiene: a rejected FAILED write means we lost the lease mid-dispatch — a peer
            # already took over. Don't discard the signal; log it (the peer's resolution
            # stands, and this exception still propagates).
            if isinstance(exc, clean_failures) and not ledger.fence(key, token, EffectState.FAILED):
                logger.warning(
                    "sakrit: %s clean-failure FAILED write was rejected (lease lost)", key
                )
            raise
        finally:
            _current_key.reset(set_token)
        result = _record_success_fenced(ledger, key, token, result, fingerprint)  # V-7 heal
        seam("after_record")
        return result


def _start_heartbeat_async(
    ledger: LeasedLedger, key: str, lease_seconds: float, interval: float | None
) -> tuple[asyncio.Event, asyncio.Task[None]] | None:
    """The async twin of :func:`_start_heartbeat` (P3-5). Renews the lease from an asyncio
    task on the event loop — no thread, so the SQLite connection is never touched from a
    second OS thread. Returns ``(stop_event, task)`` or ``None`` (single-worker).

    V-9 caveat: this task shares the event loop with the effect. An await-less stretch
    longer than ``lease_seconds`` (a CPU-bound segment, a blocking sync call inside the
    coroutine) starves renewal while the owner is alive → the lease can expire → a live
    takeover. The tool MUST yield to the loop at least every ``lease_seconds/3``. A renewal
    that observes it ran overdue logs a loud WARNING (the starvation happened)."""
    if not ledger.multi_worker:
        return None
    period = interval if interval is not None else max(lease_seconds / 3.0, 0.001)
    stop = asyncio.Event()

    async def _beat() -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
                return  # stop set → done
            except asyncio.TimeoutError:
                # V-9: if the loop starved us past the lease, the renewal is already too
                # late — the lease may have expired and a peer taken over. Surface it.
                gap = loop.time() - last
                if gap > lease_seconds:
                    logger.warning(
                        "sakrit: async heartbeat for %s ran %.2fs late (> lease %.2fs) — the "
                        "effect is not yielding to the loop; the lease may have expired",
                        key,
                        gap,
                        lease_seconds,
                    )
                last = loop.time()
                # V-8: never let the renewal die silently. Log a raise / a False renewal.
                try:
                    if not ledger.heartbeat(key, ledger.owner, lease_seconds):
                        logger.warning("sakrit: heartbeat for %s did not renew (lease lost?)", key)
                except Exception:  # noqa: BLE001
                    logger.exception("sakrit: heartbeat for %s raised; will retry", key)

    return stop, asyncio.ensure_future(_beat())


async def _stop_heartbeat_async(
    beat: tuple[asyncio.Event, asyncio.Task[None]] | None,
) -> None:
    if beat is None:
        return
    stop, task = beat
    stop.set()
    await task  # awaited to completion before any post-dispatch ledger write


async def settle_leased_async(
    ledger: LeasedLedger,
    *,
    key: str,
    scope: str,
    tool: str,
    fingerprint: str,
    fn: Callable[..., object],
    args: tuple[object, ...] = (),
    kwargs: Mapping[str, object] | None = None,
    provider_key_param: str | None = None,
    provider_ttl_s: float | None = None,
    clean_failures: tuple[type[BaseException], ...] = (),
    reconcilable: bool = False,
    reconcile: Callable[[str], Reconciliation] | None = None,
    on_absent: str = "surface",
    lease_seconds: float = 30.0,
    heartbeat_interval: float | None = None,
    wait_timeout: float = 30.0,
    poll: float = 0.01,
) -> object:
    """Await an async ``fn`` exactly once for ``key`` across concurrent workers, or return
    the winner's recorded result. The async twin of :func:`settle_leased`: identical
    lease/fence/takeover protocol, but the effect is **awaited**, and the BUSY wait and the
    heartbeat run on the event loop (``asyncio.sleep`` / an asyncio task) rather than a
    thread — so the SQLite connection stays on one thread. The effect is awaited *before*
    the record, so SUCCEEDED is never written before it runs."""
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
            provider_ttl_s=provider_ttl_s,
            reconcilable=reconcilable,
        )

        if claim.kind is ClaimKind.REPLAY:
            if claim.fingerprint != fingerprint:
                raise DivergentRetry(f"{key}: identity args differ from the recorded action")
            ledger._tell_replay(key)  # V-2 rider: served-not-re-fired is told, not silent
            return claim.result
        if claim.kind is ClaimKind.AMBIGUOUS:
            raise AmbiguousOutcome(f"{key}: a prior attempt's outcome is unknown — resolve it")
        if claim.kind is ClaimKind.BUSY:
            if time.time() > deadline:
                raise AmbiguousOutcome(f"{key}: timed out waiting for the lease owner")
            await asyncio.sleep(poll)
            continue
        if claim.kind not in (ClaimKind.PROCEED, ClaimKind.RECONCILE):  # P3-10(a)
            raise SakritError(f"{key}: unexpected claim kind {claim.kind.value} in settle_leased")

        # PROCEED / RECONCILE — we hold the lease and a fencing token.
        token = claim.fencing_token
        if claim.kind is ClaimKind.RECONCILE:
            outcome = _reconcile_on_takeover(ledger, key, token, reconcile, on_absent, fingerprint)
            if outcome is not _DISPATCH:
                return outcome
            # ABSENT + retry: safe to re-dispatch with the token we hold. Fall through.

        if not ledger.fence(key, token, EffectState.EXECUTING):  # write-ahead, fenced (P1-3)
            if time.time() > deadline:
                raise AmbiguousOutcome(f"{key}: lost the lease before dispatch and timed out")
            continue
        seam("after_mark_executing")
        set_token = _current_key.set(key)
        beat = _start_heartbeat_async(ledger, key, lease_seconds, heartbeat_interval)
        try:
            try:
                raw = fn(*args, **dict(kwargs or {}))
                result = await raw if inspect.isawaitable(raw) else raw
                seam("after_dispatch")
            finally:
                await _stop_heartbeat_async(beat)  # stop before any post-dispatch write
        except BaseException as exc:
            # Hygiene: a rejected FAILED write means we lost the lease mid-dispatch — a peer
            # already took over. Don't discard the signal; log it (the peer's resolution
            # stands, and this exception still propagates).
            if isinstance(exc, clean_failures) and not ledger.fence(key, token, EffectState.FAILED):
                logger.warning(
                    "sakrit: %s clean-failure FAILED write was rejected (lease lost)", key
                )
            raise
        finally:
            _current_key.reset(set_token)
        result = _record_success_fenced(ledger, key, token, result, fingerprint)  # V-7 heal
        seam("after_record")
        return result
