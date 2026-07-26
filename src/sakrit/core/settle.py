# SPDX-License-Identifier: Apache-2.0
"""settle — execute an effect exactly once, or replay its recorded result.

The heart of the engine. Given a resolved key + fingerprint, it drives the
write-ahead protocol: **claim → (replay | surface ambiguity | proceed)**, and on
proceed, **mark EXECUTING durably → dispatch → record**. See ``docs/design.md`` §8.

The public ``guard`` (resolve coordinate → key → fingerprint → settle) composes
this with the adapter and the SED declaration; it lands with the LangGraph adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sakrit.core.errors import AmbiguousOutcome, DivergentRetry
from sakrit.core.ledger import ClaimKind, SqliteLedger


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
    clean_failures: tuple[type[BaseException], ...] = (),
) -> object:
    """Run ``fn`` exactly once for ``key`` (durably), or return its saved result.

    When ``provider_key_param`` is set (an L2 tool), the derived key is injected
    into the call as the provider's idempotency key, so a re-dispatch after a
    crash-in-window deduplicates at the provider instead of double-firing.
    """
    claim = ledger.claim(
        key, scope, tool, fingerprint, provider_dedup=provider_key_param is not None
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
    call_kwargs = dict(kwargs or {})
    if provider_key_param is not None:
        call_kwargs[provider_key_param] = key  # inject Sakrit's key as the provider idempotency key
    ledger.mark_executing(key)  # durable BEFORE dispatch
    try:
        result = fn(*args, **call_kwargs)
    except BaseException as exc:
        # A *declared* clean failure proves the effect did not execute → FAILED
        # (safely re-claimable). Every other exception is AMBIGUOUS: the effect may
        # have landed (a timeout on a POST *is* the ambiguous window). Leave the row
        # EXECUTING and let recovery resolve it per ladder — never launder an
        # unclassified exception into a retriable FAILED, which mints a duplicate.
        if isinstance(exc, clean_failures):
            ledger.record_failure(key, exc)
        raise
    ledger.record_success(key, result)
    return result
