# SPDX-License-Identifier: Apache-2.0
"""settle — execute an effect exactly once, or replay its recorded result.

The heart of the engine. Given a resolved key + fingerprint, it drives the
write-ahead protocol: **claim → (replay | surface ambiguity | proceed)**, and on
proceed, **mark EXECUTING durably → dispatch → record**. See ``docs/design.md`` §8.

``settle`` runs a sync tool; ``settle_async`` awaits an async one. Both share the
same claim/record protocol — only the dispatch differs (call vs ``await``). The ledger
writes are synchronous SQLite (fast, local); crucially, ``settle_async`` never wraps
them in an event loop, and ``settle`` never opens one (``no asyncio.run`` in the sync
path), so neither corrupts the other's execution model.

The public ``guard`` / ``guard_async`` compose this with the adapter and the SED
declaration.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping

from sakrit.core.context import _current_key
from sakrit.core.errors import AmbiguousOutcome, DivergentRetry, SakritError
from sakrit.core.ledger import ClaimKind
from sakrit.core.protocols import Ledger
from sakrit.core.seams import seam

# Sentinel: the claim decided PROCEED (vs a replayed result, which may itself be None).
_PROCEED = object()

# A dual-secret verifier: given a stored (fingerprint, secret_id), does the *incoming* call
# reproduce it under the secret that signed the row? None → plain byte-compare (no rotation
# window; the single-secret behavior, unchanged). See ``fingerprint.matches_fingerprint``.
Verifier = Callable[[str, str | None], bool]


def _fingerprint_ok(
    stored_fp: str, stored_secret_id: str | None, incoming_fp: str, verify: Verifier | None
) -> bool:
    """Whether a stored fingerprint matches the incoming action. With a rotation ``verify`` in
    play, recompute under the row's signing secret (dual-secret window); else byte-compare."""
    if verify is not None:
        return verify(stored_fp, stored_secret_id)
    return stored_fp == incoming_fp


def _decide(
    ledger: Ledger,
    *,
    key: str,
    scope: str,
    tool: str,
    fingerprint: str,
    provider_key_param: str | None,
    provider_ttl_s: float | None,
    reconcilable: bool,
    secret_id: str | None,
    verify: Verifier | None,
) -> object:
    """Claim the key and decide: return a replayed result, raise on ambiguity, or
    return ``_PROCEED`` to signal 'we own it — dispatch'. Shared by sync and async."""
    claim = ledger.claim(
        key,
        scope,
        tool,
        fingerprint,
        provider_dedup=provider_key_param is not None,
        provider_ttl_s=provider_ttl_s,
        reconcilable=reconcilable,
        secret_id=secret_id,
    )
    if claim.kind is ClaimKind.REPLAY:
        assert claim.fingerprint is not None  # a SUCCEEDED row always carries its fingerprint
        if not _fingerprint_ok(claim.fingerprint, claim.secret_id, fingerprint, verify):
            raise DivergentRetry(
                f"{key}: identity args differ from the recorded action; refusing to "
                "merge or re-execute (should a reworded field be declared content?)"
            )
        ledger._tell_replay(key)  # V-2 rider: served-not-re-fired is told, not silent
        return claim.result
    if claim.kind is ClaimKind.AMBIGUOUS:
        if claim.fingerprint is not None and not _fingerprint_ok(
            claim.fingerprint, claim.secret_id, fingerprint, verify
        ):
            # P4-8: this is not a retry of the ambiguous action — it is a *different* action
            # colliding on its key. Name that, rather than send the operator to investigate
            # "did my effect land?" for an effect they never issued.
            raise DivergentRetry(
                f"{key}: a different action collides on the key of a prior attempt whose "
                "outcome is unresolved (it crashed after dispatch). This is not a retry of "
                "that action — refusing. Use a distinct key, or resolve the ambiguous prior."
            )
        raise AmbiguousOutcome(
            f"{key}: a prior attempt crashed after dispatch; outcome unknown — resolve it"
        )
    # P3-10(a): only PROCEED reaches here. Never *assume* it — a future ClaimKind (BUSY,
    # RECONCILE are leased-only) must not silently execute on the single-worker path.
    if claim.kind is not ClaimKind.PROCEED:
        raise SakritError(f"{key}: unexpected claim kind {claim.kind.value} on the sync path")
    return _PROCEED


def settle(
    ledger: Ledger,
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
    secret_id: str | None = None,
    verify: Verifier | None = None,
) -> object:
    """Run ``fn`` exactly once for ``key`` (durably), or return its saved result.

    ``provider_key_param`` marks an L2 tool (provider-deduplicating): its crash
    recovery re-dispatches instead of surfacing ambiguity. The tool reads the key to
    hand its provider via ``sakrit.current_key()`` — a contextvar set here for the
    duration of the dispatch, so the tool's signature stays clean.

    ``secret_id`` is stamped on the row as provenance (P5-3) — which HMAC secret signed
    this fingerprint, for a future rotation window.
    """
    decided = _decide(
        ledger,
        key=key,
        scope=scope,
        tool=tool,
        fingerprint=fingerprint,
        provider_key_param=provider_key_param,
        provider_ttl_s=provider_ttl_s,
        reconcilable=reconcilable,
        secret_id=secret_id,
        verify=verify,
    )
    if decided is not _PROCEED:
        return decided

    # PROCEED — we own the claim.
    seam("after_claim")
    ledger.mark_executing(key)  # durable BEFORE dispatch
    seam("after_mark_executing")
    token = _current_key.set(key)  # the tool may read this via sakrit.current_key()
    try:
        result = fn(*args, **dict(kwargs or {}))
        # Belt-and-braces (P1-1, V-1): a wrapper that dodged the decoration-time check may
        # still hand back a lazy value whose body never ran — a coroutine (awaitable) or a
        # generator / async-generator (iterables). Recording any of them would record
        # SUCCEEDED before the effect. Refuse BEFORE recording.
        if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
            raise SakritError(
                f"{tool}: the guarded callable returned a {type(result).__name__}; its body "
                "did not run (a coroutine/generator is lazy). Sakrit records only after the "
                "effect executes — collect it to a value, or use guard_async for an async tool."
            )
        seam("after_dispatch")
    except BaseException as exc:
        # A *declared* clean failure proves the effect did not execute → FAILED
        # (safely re-claimable). Every other exception is AMBIGUOUS: the effect may
        # have landed (a timeout on a POST *is* the ambiguous window). Leave the row
        # EXECUTING and let recovery resolve it per ladder — never launder an
        # unclassified exception into a retriable FAILED, which mints a duplicate.
        if isinstance(exc, clean_failures):
            ledger.record_failure(key, exc)
            seam("after_record_failure")
        raise
    finally:
        _current_key.reset(token)
    ledger.record_success(key, result)
    seam("after_record")
    return result


async def settle_async(
    ledger: Ledger,
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
    secret_id: str | None = None,
    verify: Verifier | None = None,
) -> object:
    """Await ``fn`` exactly once for ``key`` (durably), or return its saved result.

    The async twin of :func:`settle`: identical claim/record protocol, but the effect is
    **awaited** rather than called. The record happens only *after* the await resolves —
    so, exactly as in the sync path, SUCCEEDED is never written before the effect runs.
    """
    decided = _decide(
        ledger,
        key=key,
        scope=scope,
        tool=tool,
        fingerprint=fingerprint,
        provider_key_param=provider_key_param,
        provider_ttl_s=provider_ttl_s,
        reconcilable=reconcilable,
        secret_id=secret_id,
        verify=verify,
    )
    if decided is not _PROCEED:
        return decided

    # PROCEED — we own the claim.
    seam("after_claim")
    ledger.mark_executing(key)  # durable BEFORE dispatch
    seam("after_mark_executing")
    token = _current_key.set(key)  # the tool may read this via sakrit.current_key()
    try:
        raw = fn(*args, **dict(kwargs or {}))
        # Await the effect. A sync tool that returns a plain value is tolerated (nothing
        # to await); a coroutine / awaitable is awaited before we record — never after.
        result = await raw if inspect.isawaitable(raw) else raw
        # V-1 belt: an async-generator is NOT awaitable, so it slips past the await above
        # with its body unrun. Refuse it (and a sync generator) before recording.
        if inspect.isgenerator(result) or inspect.isasyncgen(result):
            raise SakritError(
                f"{tool}: the guarded callable returned a {type(result).__name__}; its body "
                "did not run. An async generator is not awaitable — collect its items inside "
                "a coroutine and return the result."
            )
        seam("after_dispatch")
    except BaseException as exc:
        if isinstance(exc, clean_failures):
            ledger.record_failure(key, exc)
            seam("after_record_failure")
        raise
    finally:
        _current_key.reset(token)
    ledger.record_success(key, result)
    seam("after_record")
    return result
