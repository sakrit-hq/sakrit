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
from sakrit.core.fingerprint import fingerprint
from sakrit.core.keys import positional_key
from sakrit.core.ledger import SqliteLedger
from sakrit.core.settle import settle

F = TypeVar("F", bound=Callable[..., Any])


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
        self._ledger = ledger
        self._secret = secret
        self._adapter = adapter

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
        )

    def effect(
        self,
        decl: EffectDecl,
        *,
        step: str | None = None,
        key: str | None = None,
        scope: str | None = None,
    ) -> Callable[[F], F]:
        """Decorator form: wrap a tool so every call is guarded."""

        def deco(fn: F) -> F:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.guard(
                    decl, fn, args=args, kwargs=kwargs, step=step, key=key, scope=scope
                )

            return wrapper  # type: ignore[return-value]

        return deco
