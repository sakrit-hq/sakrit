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
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from sakrit.core.adapter import RuntimeAdapter, resolve_coordinate
from sakrit.core.declaration import EffectDecl
from sakrit.core.errors import SakritError
from sakrit.core.fingerprint import fingerprint, matches_fingerprint, secret_id
from sakrit.core.keys import positional_key
from sakrit.core.leased import settle_leased, settle_leased_async
from sakrit.core.protocols import LeasedLedger
from sakrit.core.reconcile import Verdict
from sakrit.core.settle import Verifier, settle, settle_async

logger = logging.getLogger("sakrit")

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class _StepCtx:
    """Coordinate overrides active for the duration of a ``sk.step(...)`` block."""

    step: str | None
    occurrence: int
    scope: str | None


# The active sk.step() context, if any. Per-task via contextvars, so concurrent
# branches don't clobber each other's occurrence.
_step_ctx: ContextVar[_StepCtx | None] = ContextVar("sakrit_step_ctx", default=None)


def _decl_signature(decl: EffectDecl) -> tuple[object, ...]:
    """The structural, callable-identity-free signature of a decl (P5-8c).

    Two decls with this signature equal would reconcile and fingerprint identically, so a
    re-registration of one over the other is benign (first-registered wins its callables).
    Captures every behavior-affecting field *by value* and the ``reconcile`` callable only by
    *presence* (L0 vs L1/L2R) — never by object identity, so equivalent inline lambdas on the
    ``guard`` hot path don't spuriously conflict."""
    return (
        decl.tool,
        tuple(sorted(decl.classes.items(), key=lambda kv: kv[0])),
        decl.default,
        decl.provider_key_param,
        decl.provider_ttl_s,
        decl.clean_failures,
        decl.on_absent,
        decl.provider_read,
        decl.reconcile is not None,
    )


def _is_lazy_iterator_fn(fn: Callable[..., object]) -> bool:
    """A generator or async-generator function: *calling* it returns a lazy iterator and
    runs none of the body. Guarding it would record SUCCEEDED before any effect ran
    (V-1) — the same record-before-effect inversion as an un-awaited coroutine."""
    return inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn)


def _reject_lazy(fn: Callable[..., object]) -> None:
    """Refuse a generator / async-generator tool (V-1). Its body runs only on iteration,
    so Sakrit would record SUCCEEDED without the effect. Collect the values inside a
    plain (or async) function and return the result — Sakrit records after it runs."""
    if _is_lazy_iterator_fn(fn):
        raise SakritError(
            f"{getattr(fn, '__name__', fn)!r} is a generator function; its body runs only "
            "when iterated, so guarding it would record SUCCEEDED before any effect ran. "
            "Collect the values inside a plain function and return the result."
        )


def _reject_async(fn: Callable[..., object]) -> None:
    """Refuse, on the *sync* guard path, anything whose effect wouldn't have run by the
    time Sakrit records: an ``async def`` (use guard_async) or a generator (V-1)."""
    if inspect.iscoroutinefunction(fn):
        raise SakritError(
            f"{getattr(fn, '__name__', fn)!r} is async; guard it with guard_async / "
            "@sk.effect (which routes async defs), not the sync guard path — guarding it "
            "synchronously would record success before the effect runs."
        )
    _reject_lazy(fn)


def _bind(
    fn: Callable[..., object], args: tuple[object, ...], kwargs: Mapping[str, object]
) -> dict[str, object]:
    """Bind call arguments to their parameter names, for fingerprinting.

    ``apply_defaults`` folds a parameter's default into the named map, so a default that is
    an *identity* arg participates in the fingerprint — changing that default across a deploy
    re-keys in-flight steps (a loud ``DivergentRetry`` on resume, not a silent swallow).
    """
    try:
        sig = inspect.signature(fn)
        bound = sig.bind_partial(*args, **kwargs)
    except (TypeError, ValueError) as exc:
        # P4-5: the signature is unavailable (a C-extension method, some builtins). We can't
        # map *positional* args to names, so they would silently vanish from the fingerprint
        # → two different actions share one fingerprint → the second replays the first
        # silently (the fingerprint's whole job, disabled). Refuse fail-closed. Kwargs-only
        # is safe — those args are already named.
        if args:
            raise SakritError(
                f"{getattr(fn, '__name__', fn)!r}: cannot inspect its signature to bind "
                f"{len(args)} positional argument(s) for fingerprinting — dropping them would "
                "silently replay a *different* action. Pass identity args by keyword, or wrap "
                "the tool in an inspectable function."
            ) from exc
        return dict(kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


class Sakrit:
    """Holds the ledger, the fingerprint secret, and (optionally) a runtime adapter."""

    def __init__(
        self,
        ledger: LeasedLedger,
        *,
        secret: bytes,
        verify_secrets: tuple[bytes, ...] = (),
        adapter: RuntimeAdapter | None = None,
        lease_seconds: float = 30.0,
        wait_timeout: float = 30.0,
    ) -> None:
        self._ledger = ledger
        self._secret = secret
        # P5-3: a stable, non-reversible id for this deployment's HMAC secret, stamped on
        # every row we claim so a *rotation* is detectable. Computed once.
        self._secret_id = secret_id(secret)
        # The software dual-secret rotation window: sign new writes with `secret`, but keep
        # `verify_secrets` (the just-retired secrets) in a keyring so an in-flight row signed by
        # an old secret still verifies during the drain — no fleet-wide DivergentRetry storm on
        # rotation. Keyed by secret_id (the row's provenance pivot). Empty → the single-secret
        # behavior (a plain byte-compare of fingerprints, unchanged). To rotate: deploy with
        # secret=<new>, verify_secrets=(<old>,); once no row is signed by <old>, drop it.
        #
        # A legacy pre-P5-3 row (secret_id NULL) verifies against `secret` (the primary), since
        # it carries no provenance to route by — so drain NULL-provenance rows (or keep the
        # secret that signed them as the primary) through their window, else a same-action retry
        # of one trips a loud DivergentRetry (never a silent duplicate).
        self._verify_secrets = verify_secrets
        # Build the keyring, refusing a secret_id collision loudly: two *different* secrets that
        # hash to one id would silently last-writer-win in a dict, then verify each other's rows
        # under the wrong secret. Birthday-infeasible for real secrets, but a silent footgun if
        # it ever happened — so detect it rather than drop one.
        self._keyring: dict[str, bytes] = {}
        for s in (secret, *verify_secrets):
            sid = secret_id(s)
            if sid in self._keyring and self._keyring[sid] != s:
                raise SakritError(
                    f"two distinct secrets in the rotation keyring collide on secret_id {sid!r} "
                    "— refusing rather than silently verify rows under the wrong secret."
                )
            self._keyring[sid] = s
        self._adapter = adapter
        self._recovered = False
        self._registry: dict[str, EffectDecl] = {}  # tool identity → decl (for reconcile)
        # "One decl per tool name per process" (P5-8c). The registry was first-decl-wins by
        # setdefault, so a *conflicting* second decl for one tool name was silently ignored —
        # recovery would then reconcile with the wrong (first-seen) decl. Refuse a conflict
        # loudly; an identical re-registration is idempotent (re-import, re-decoration).
        # P3-8: a multi-worker ledger drives the *leased* protocol (leases + fencing +
        # takeover-by-ladder); a single-worker ledger drives the unfenced settle path.
        # The mode is a property of the ledger, machine-checked at its construction (P3-3).
        self._leased = ledger.multi_worker
        self._lease_seconds = lease_seconds
        self._wait_timeout = wait_timeout

    def _register(self, decl: EffectDecl) -> None:
        """Register ``decl`` under its tool name, refusing a conflicting re-declaration.

        Idempotent for a structurally-equivalent decl (re-import / re-decoration, or the same
        inline decl passed to ``guard`` on every call); a decl that would *reconcile or
        fingerprint differently* for an already-registered tool name is a footgun (recovery
        keys on the first-seen decl), so it is refused loudly rather than silently dropped
        (P5-8c). Equivalence is **structural**, not ``==``: two decls carrying distinct but
        equivalent ``reconcile`` lambdas (frozen-dataclass ``==`` compares callables by
        identity) must not spuriously conflict — so the signature captures whether a reconcile
        is *present* (L0 vs L1/L2R), not which lambda object it is (first-registered wins, as
        the old ``setdefault`` did)."""
        existing = self._registry.get(decl.tool)
        if existing is not None and _decl_signature(existing) != _decl_signature(decl):
            raise SakritError(
                f"tool {decl.tool!r} is already registered with a different declaration. "
                "One decl per tool name per process — a second, conflicting decl would be "
                "silently ignored and recovery would reconcile with the first. Use one "
                "declaration, or a distinct tool name."
            )
        self._registry.setdefault(decl.tool, decl)  # first-registered wins (keeps its callables)

    def _prepare(
        self,
        decl: EffectDecl,
        fn: Callable[..., object],
        args: tuple[object, ...],
        kwargs: Mapping[str, object] | None,
        step: str | None,
        key: str | None,
        scope: str | None,
        occurrence: int | None,
    ) -> tuple[str, str, str, dict[str, object], Verifier | None]:
        """Shared prelude for guard/guard_async: run recovery once, merge the sk.step
        context, resolve the coordinate. Returns ``(key, scope, fingerprint, kwargs, verify)`` —
        ``verify`` is the dual-secret verifier (None unless a rotation window is configured)."""
        self._register(decl)
        # Q14 — the engine guarantees recovery runs once per process, before the
        # first claim it issues. The Q1 fix removed claim's lazy safety net, so a
        # crash leftover must be resolved by recovery, not left to chance or the
        # integrator. The *engine* owns recovery — no adapter hook (P5-4 removed the
        # false `on_recovery` "adapter decides when recovery runs" contract).
        # P3-4: the startup scan reads EXECUTING as death-evidence — TRUE only for a
        # single worker. Against a multi-worker ledger it would ambiguate/re-own live
        # peers' rows, so it MUST NOT run there; the leased protocol recovers lazily
        # (claim_leased takes over a row by ladder once its lease expires).
        # P1-15 sliver: set the flag only *after* recovery succeeds. Setting it first meant a
        # DB-level error in recover()/pending_reconcile() permanently disabled recovery for the
        # process. recover() is idempotent (it re-scans EXECUTING), so retrying on the next
        # guard is safe — and a persistently-broken ledger fails loud every call, not silently.
        if not self._recovered:
            if not self._leased:
                self.recover()
            self._recovered = True

        # Merge an active sk.step(...) block: an explicit arg wins; else the context
        # supplies it (P4-1 — distinct occurrences for repeats at one call site).
        ctx = _step_ctx.get()
        eff_step = step if step is not None else (ctx.step if ctx else None)
        eff_scope = scope if scope is not None else (ctx.scope if ctx else None)
        eff_occurrence = occurrence if occurrence is not None else (ctx.occurrence if ctx else 1)

        kw = dict(kwargs or {})
        named = _bind(fn, args, kw)
        coord = resolve_coordinate(
            self._adapter, scope=eff_scope, step=eff_step, key=key, occurrence=eff_occurrence
        )
        return (
            positional_key(coord, decl.tool),
            coord.scope,
            fingerprint(decl, named, secret=self._secret),
            kw,
            self._verifier(decl, named),
        )

    def _verifier(self, decl: EffectDecl, named: Mapping[str, object]) -> Verifier | None:
        """A dual-secret verifier bound to this call, or None when no rotation window is
        configured (settle then byte-compares — the single-secret behavior, unchanged). The
        closure recomputes the incoming args' fingerprint under the secret that signed a stored
        row (identified by the row's secret_id), so an in-flight row survives a rotation."""
        if not self._verify_secrets:
            return None
        keyring, primary = self._keyring, self._secret

        def verify(stored_fp: str, stored_secret_id: str | None) -> bool:
            return matches_fingerprint(
                decl, named, stored_fp, stored_secret_id, keyring=keyring, primary_secret=primary
            )

        return verify

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
        occurrence: int | None = None,
    ) -> object:
        """Run a **sync** ``fn`` exactly once for its logical step, or replay its result.

        Against a multi-worker ledger this drives the *leased* protocol (lease + fencing +
        takeover-by-ladder); against a single-worker ledger, the unfenced settle path.

        A deliberate repeat of the same identity args at one call site replays (does not
        re-fire) unless wrapped in :meth:`step` — see the sequential-repeat trap on
        :meth:`effect`.

        RETURN-VALUE CONTRACT (P4-4). On the *first* run you get the tool's real return value.
        On a *replay* (resume, retry) you get ``json.loads(json.dumps(result))`` — a JSON
        reconstruction: a tuple comes back a list, integer dict-keys come back strings, and a
        non-JSON object comes back as a :class:`~sakrit.core.Replayed` marker. Exactly-once is
        untouched (nothing re-fires); a lossy round-trip is logged. Return JSON-native values
        (or handle the ``Replayed`` marker) if downstream code runs on the resume path."""
        _reject_async(fn)  # an async tool must use guard_async, not the sync record path
        rkey, rscope, fp, kw, verify = self._prepare(
            decl, fn, args, kwargs, step, key, scope, occurrence
        )
        if self._leased:
            return settle_leased(
                self._ledger,
                key=rkey,
                scope=rscope,
                tool=decl.tool,
                fingerprint=fp,
                fn=fn,
                args=args,
                kwargs=kw,
                provider_key_param=decl.provider_key_param,
                provider_ttl_s=decl.provider_ttl_s,
                clean_failures=decl.clean_failures,
                reconcilable=decl.reconcile is not None,
                reconcile=decl.reconcile,
                on_absent=decl.on_absent,
                lease_seconds=self._lease_seconds,
                wait_timeout=self._wait_timeout,
                secret_id=self._secret_id,
                verify=verify,
            )
        return settle(
            self._ledger,
            key=rkey,
            scope=rscope,
            tool=decl.tool,
            fingerprint=fp,
            fn=fn,
            args=args,
            kwargs=kw,
            provider_key_param=decl.provider_key_param,
            provider_ttl_s=decl.provider_ttl_s,
            clean_failures=decl.clean_failures,
            reconcilable=decl.reconcile is not None,
            secret_id=self._secret_id,
            verify=verify,
        )

    async def guard_async(
        self,
        decl: EffectDecl,
        fn: Callable[..., object],
        *,
        args: tuple[object, ...] = (),
        kwargs: Mapping[str, object] | None = None,
        step: str | None = None,
        key: str | None = None,
        scope: str | None = None,
        occurrence: int | None = None,
    ) -> object:
        """Await an **async** ``fn`` exactly once for its logical step, or replay its
        result. Against a multi-worker ledger this drives the *async leased* protocol
        (settle_leased_async); against a single-worker ledger, the async settle path. The
        effect is awaited before Sakrit records — no record-before-effect.

        Multi-worker async caveat (V-9): the lease heartbeat runs on the event loop, so the
        tool must yield to it at least every ``lease_seconds/3``; an await-less CPU-bound
        stretch longer than the lease can let it expire and a peer take over mid-dispatch."""
        _reject_lazy(fn)  # V-1: an async-generator is not awaitable; its body never runs
        rkey, rscope, fp, kw, verify = self._prepare(
            decl, fn, args, kwargs, step, key, scope, occurrence
        )
        if self._leased:
            return await settle_leased_async(
                self._ledger,
                key=rkey,
                scope=rscope,
                tool=decl.tool,
                fingerprint=fp,
                fn=fn,
                args=args,
                kwargs=kw,
                provider_key_param=decl.provider_key_param,
                provider_ttl_s=decl.provider_ttl_s,
                clean_failures=decl.clean_failures,
                reconcilable=decl.reconcile is not None,
                reconcile=decl.reconcile,
                on_absent=decl.on_absent,
                lease_seconds=self._lease_seconds,
                wait_timeout=self._wait_timeout,
                secret_id=self._secret_id,
                verify=verify,
            )
        return await settle_async(
            self._ledger,
            key=rkey,
            scope=rscope,
            tool=decl.tool,
            fingerprint=fp,
            fn=fn,
            args=args,
            kwargs=kw,
            provider_key_param=decl.provider_key_param,
            provider_ttl_s=decl.provider_ttl_s,
            clean_failures=decl.clean_failures,
            reconcilable=decl.reconcile is not None,
            secret_id=self._secret_id,
            verify=verify,
        )

    def recover(self) -> None:
        """Resolve crash-in-window rows: the ledger handles L0/L2; the engine drives
        L1/L2R reconciliation using each tool's read-only reconcile function.

        Single-worker only. It reads ``EXECUTING`` as death-evidence, which is false when
        peers are alive — so on a multi-worker ledger it would poison live rows (P3-4).
        Leased recovery is lazy: ``claim_leased`` takes a row over by ladder once its
        lease expires."""
        if self._leased:
            raise SakritError(
                "recover() is the single-worker startup scan and would poison live peers' "
                "rows on a multi_worker ledger. The leased protocol recovers lazily via "
                "claim_leased takeover; do not call recover() in multi-worker mode."
            )
        self._ledger.recover()
        for key, tool in self._ledger.pending_reconcile():
            decl = self._registry.get(tool)
            if decl is None or decl.reconcile is None:
                self._ledger.ambiguate(key)  # no reconcile available → surface
                continue
            try:
                rec = decl.reconcile(key)
            except Exception:  # noqa: BLE001 — P1-15: one tool's failure must not strand
                # V-12: a *raised* reconcile means we couldn't *ask* — a transient failure
                # (DNS, timeout, a provider mid-deploy), NOT an answered "outcome unknown".
                # Do NOT ambiguate (AMBIGUOUS is terminal with no in-band exit, so a 30-second
                # startup blip would permanently strand this tool's whole backlog). Skip: leave
                # the row EXECUTING and warn — in-process claims refuse EXECUTING loudly, and the
                # next process restart re-attempts the reconcile, so transient failures self-heal.
                # Only an *answered* UNKNOWN (below) pays the ambiguity cost.
                logger.warning(
                    "sakrit: reconcile for %s raised (transient?) — left EXECUTING for the next "
                    "recovery, not ambiguated",
                    key,
                )
                continue
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
        occurrence: int | None = None,
    ) -> Callable[[F], F]:
        """Decorator form: wrap a tool so every call is guarded. Works on both sync and
        ``async def`` tools — an async tool yields an async wrapper (await it) driven by
        :meth:`guard_async`; a sync tool yields a sync wrapper driven by :meth:`guard`.

        A fixed ``occurrence`` here freezes one coordinate per call site. For repeats at
        one site (a loop, deliberate resend, or parallel same-tool calls), leave it and
        wrap the call in :meth:`step` so each iteration gets a distinct coordinate (P4-1).

        WARNING — the sequential-repeat trap (P4-1). Calling the same guarded tool **again
        at the same call site with the same identity args**, *not* wrapped in :meth:`step`,
        is treated as a retry: the recorded result is replayed and **the effect does not
        re-fire**. That is correct for a crash/resume retry but a silent swallow for a
        *deliberate* repeat (e.g. "send the same reminder twice"). It is logged at INFO and
        fires the ledger's ``on_replay`` hook, so it is told — but to actually fire twice,
        wrap each call in ``with sk.step(occurrence=i): …``. (A *concurrent* second call
        that overlaps a still-running first is not silent — it hits the live row and raises
        ``EffectInFlightError``; only the sequential same-args repeat swallows.) Automatic
        occurrence handling is deferred — see ``docs/dev-notes/occurrence.md``.
        """
        self._register(decl)  # available to recovery before first call; refuses a conflict

        def deco(fn: F) -> F:
            _reject_lazy(fn)  # V-1: refuse a generator / async-gen at decoration (import) time
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def awrapper(*args: Any, **kwargs: Any) -> Any:
                    return await self.guard_async(
                        decl,
                        fn,
                        args=args,
                        kwargs=kwargs,
                        step=step,
                        key=key,
                        scope=scope,
                        occurrence=occurrence,
                    )

                return awrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.guard(
                    decl,
                    fn,
                    args=args,
                    kwargs=kwargs,
                    step=step,
                    key=key,
                    scope=scope,
                    occurrence=occurrence,
                )

            return wrapper  # type: ignore[return-value]

        return deco

    @contextmanager
    def step(
        self, name: str | None = None, *, occurrence: int = 1, scope: str | None = None
    ) -> Iterator[None]:
        """Scope a block to a distinct coordinate position (P4-1).

        The same tool called repeatedly at one call site — a loop, a deliberate resend,
        or parallel same-tool calls in one node — otherwise collides on one key: the
        repeat is silently swallowed (identical args) or loudly refused (``DivergentRetry``,
        differing args). Wrap each repeat so it gets its own coordinate::

            for i, r in enumerate(recipients):
                with sk.step(occurrence=i):
                    send_email(to=r)

        WARNING: this is the manual mechanism. Sakrit does **not** yet fold repetition
        automatically (see ``docs/dev-notes/occurrence.md``); until it does, an unwrapped
        repeat at one call site is a latent swallow/halt.
        """
        token = _step_ctx.set(_StepCtx(step=name, occurrence=occurrence, scope=scope))
        try:
            yield
        finally:
            _step_ctx.reset(token)
