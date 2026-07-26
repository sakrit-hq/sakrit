# SPDX-License-Identifier: Apache-2.0
"""The public surface — ``Sakrit`` and ``guard``.

``guard`` composes the whole pipeline: resolve the coordinate (adapter → ladder) →
derive the positional key → fingerprint the identity args → settle (claim, replay,
or execute-and-record). The decorator ``@sk.effect(...)`` is sugar over it.

This is the "add three lines and it sends once" surface. See ``docs/design.md``
§11 and §14.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from sakrit.core.adapter import RuntimeAdapter, resolve_coordinate
from sakrit.core.declaration import EffectDecl
from sakrit.core.errors import SakritError
from sakrit.core.fingerprint import fingerprint
from sakrit.core.keys import positional_key
from sakrit.core.ledger import SqliteLedger
from sakrit.core.reconcile import Verdict
from sakrit.core.settle import settle

F = TypeVar("F", bound=Callable[..., Any])


def _reject_async(fn: Callable[..., object]) -> None:
    """Refuse to guard an async tool: calling it synchronously would return an
    unawaited coroutine, and Sakrit would record SUCCEEDED before the effect ran
    (record-before-effect → a silently-lost effect on replay). Fail closed until a
    real async settle path (guard_async) lands. See audit P1-1."""
    if inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        raise SakritError(
            f"{getattr(fn, '__name__', fn)!r} is async; guarding it synchronously would "
            "record success before the effect runs. Async tool support (guard_async) is "
            "coming — do not guard an async def with sk.effect/guard yet."
        )


def _bind(
    fn: Callable[..., object], args: tuple[object, ...], kwargs: Mapping[str, object]
) -> dict[str, object]:
    """Bind call arguments to their parameter names, for fingerprinting.

    Falls back to the kwargs alone for uninspectable callables (e.g. some builtins).
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return dict(kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


class Sakrit:
    """Holds the ledger, the fingerprint secret, and (optionally) a runtime adapter."""

    def __init__(
        self,
        ledger: SqliteLedger,
        *,
        secret: bytes,
        adapter: RuntimeAdapter | None = None,
    ) -> None:
        # P1-11: the engine drives the *single-worker* path only — guard → settle (the
        # unfenced claim) and a mandatory startup recover() that reads EXECUTING as
        # death-evidence. Against a multi-worker ledger that means ambiguating/re-owning
        # peers' *live* rows. Refuse the composition until the leased loop is wired in
        # (P3-8); the two protocols must not share the engine.
        if ledger.multi_worker:
            raise SakritError(
                "Sakrit(ledger=...) drives the single-worker settle path, but this ledger "
                "is multi_worker=True. The engine's startup recovery would poison live "
                "peers' rows. Use single-worker mode, or drive settle_leased directly."
            )
        self._ledger = ledger
        self._secret = secret
        self._adapter = adapter
        self._recovered = False
        self._registry: dict[str, EffectDecl] = {}  # tool identity → decl (for reconcile)

    def guard(
        self,
        decl: EffectDecl,
        fn: Callable[..., object],
        *,
        args: tuple[object, ...] = (),
        kwargs: Mapping[str, object] | None = None,
        step: str | None = None,
        key: str | None = None,
        scope: str | None = None,
        occurrence: int = 1,
    ) -> object:
        """Run ``fn`` exactly once for its logical step, or replay its saved result."""
        _reject_async(fn)
        self._registry.setdefault(decl.tool, decl)
        # Q14 — the engine guarantees recovery runs once per process, before the
        # first claim it issues. The Q1 fix removed claim's lazy safety net, so a
        # crash leftover must be resolved by recovery, not left to chance or the
        # integrator. No adapter obligation; a missed on_recovery hook is harmless.
        if not self._recovered:
            self._recovered = True
            self.recover()

        kw = dict(kwargs or {})
        named = _bind(fn, args, kw)
        coord = resolve_coordinate(
            self._adapter, scope=scope, step=step, key=key, occurrence=occurrence
        )
        return settle(
            self._ledger,
            key=positional_key(coord, decl.tool),
            scope=coord.scope,
            tool=decl.tool,
            fingerprint=fingerprint(decl, named, secret=self._secret),
            fn=fn,
            args=args,
            kwargs=kw,
            provider_key_param=decl.provider_key_param,
            clean_failures=decl.clean_failures,
            reconcilable=decl.reconcile is not None,
        )

    def recover(self) -> None:
        """Resolve crash-in-window rows: the ledger handles L0/L2; the engine drives
        L1/L2R reconciliation using each tool's read-only reconcile function."""
        self._ledger.recover()
        for key, tool in self._ledger.pending_reconcile():
            decl = self._registry.get(tool)
            if decl is None or decl.reconcile is None:
                self._ledger.ambiguate(key)  # no reconcile available → surface
                continue
            rec = decl.reconcile(key)
            if rec.verdict is Verdict.SETTLED:
                self._ledger.settle_reconciled(key, rec.result)
            elif rec.verdict is Verdict.ABSENT and decl.on_absent == "retry":
                self._ledger.reclaim(key)  # provably didn't happen → safe to retry
            else:  # ABSENT+surface (a lagging read may lie), or UNKNOWN
                self._ledger.ambiguate(key)

    def effect(
        self,
        decl: EffectDecl,
        *,
        step: str | None = None,
        key: str | None = None,
        scope: str | None = None,
    ) -> Callable[[F], F]:
        """Decorator form: wrap a tool so every call is guarded."""
        self._registry.setdefault(decl.tool, decl)  # available to recovery before first call

        def deco(fn: F) -> F:
            _reject_async(fn)  # fail at decoration (import) time, not at 2am

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.guard(
                    decl, fn, args=args, kwargs=kwargs, step=step, key=key, scope=scope
                )

            return wrapper  # type: ignore[return-value]

        return deco
