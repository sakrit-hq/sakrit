# SPDX-License-Identifier: Apache-2.0
"""settle — execute an effect exactly once, or replay its recorded result.

The heart of the engine. Given a resolved key + fingerprint, it drives the
write-ahead protocol: **claim → (replay | surface ambiguity | proceed)**, and on
proceed, **mark EXECUTING durably → dispatch → record**. See ``docs/design.md`` §8.

The public ``guard`` (resolve coordinate → key → fingerprint → settle) composes
this with the adapter and the SED declaration; it lands with the LangGraph adapter.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping

from sakrit.core.context import _current_key
from sakrit.core.errors import AmbiguousOutcome, DivergentRetry, SakritError
from sakrit.core.ledger import ClaimKind, SqliteLedger
from sakrit.core.seams import seam


def settle(
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
    provider_ttl_s: float | None = None,
    clean_failures: tuple[type[BaseException], ...] = (),
    reconcilable: bool = False,
) -> object:
    """Run ``fn`` exactly once for ``key`` (durably), or return its saved result.

    ``provider_key_param`` marks an L2 tool (provider-deduplicating): its crash
    recovery re-dispatches instead of surfacing ambiguity. The tool reads the key to
    hand its provider via ``sakrit.current_key()`` — a contextvar set here for the
    duration of the dispatch, so the tool's signature stays clean.
    """
    claim = ledger.claim(
        key,
        scope,
        tool,
        fingerprint,
        provider_dedup=provider_key_param is not None,
        provider_ttl_s=provider_ttl_s,
        reconcilable=reconcilable,
    )

    if claim.kind is ClaimKind.REPLAY:
        if claim.fingerprint != fingerprint:
            raise DivergentRetry(
                f"{key}: identity args differ from the recorded action; refusing to "
                "merge or re-execute (should a reworded field be declared content?)"
            )
        return claim.result

    if claim.kind is ClaimKind.AMBIGUOUS:
        raise AmbiguousOutcome(
            f"{key}: a prior attempt crashed after dispatch; outcome unknown — resolve it"
        )

    # PROCEED — we own the claim.
    seam("after_claim")
    ledger.mark_executing(key)  # durable BEFORE dispatch
    seam("after_mark_executing")
    token = _current_key.set(key)  # the tool may read this via sakrit.current_key()
    try:
        result = fn(*args, **dict(kwargs or {}))
        # Belt-and-braces (P1-1): a wrapper that dodged the decoration-time check may
        # still hand back a coroutine. It was never awaited → no effect ran → refuse
        # BEFORE recording, so we never record SUCCEEDED before the effect.
        if inspect.isawaitable(result):
            raise SakritError(
                f"{tool}: the guarded callable returned an awaitable; it was not run. "
                "Sakrit cannot guard async tools synchronously (record-before-effect)."
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
