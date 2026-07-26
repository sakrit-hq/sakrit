# SPDX-License-Identifier: Apache-2.0
"""The effect declaration — the in-code binding of SED (``docs/spec.md``).

A tool declares which of its arguments are **identity** (divergence means a
different action → ``DivergentRetry``), **content** (regeneratable; divergence
tolerated), or **volatile** (ignored). Undeclared arguments default to
**identity** — the safe direction (a loud halt, never a silent duplicate). Only the
subset the narrow Act II core needs is here; the full SED surface lands with its
Act.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from sakrit.core.errors import SakritError
from sakrit.core.reconcile import Reconciliation


def _encompasses_timeout(exc_type: type[BaseException]) -> bool:
    """Whether declaring ``exc_type`` a clean failure would also swallow a *timeout* — the
    ambiguous window where the effect may already have landed (P1-12).

    True when ``except exc_type`` catches a builtin ``TimeoutError`` (so ``exc_type`` is
    ``TimeoutError`` / ``OSError`` / ``Exception`` / ``BaseException`` — the broad catch-alls),
    or when ``exc_type`` (or a base) is itself a *Timeout* type (a third-party network timeout,
    matched by name since we cannot import every provider SDK). It does *not* catch every
    over-broad third-party base (e.g. ``requests.exceptions.RequestException`` does not subclass
    the builtin ``TimeoutError`` and isn't named "timeout"); the docstring on ``clean_failures``
    states the developer's remaining responsibility."""
    if issubclass(TimeoutError, exc_type):
        return True
    return any("timeout" in base.__name__.lower() for base in exc_type.__mro__)


class ArgClass(Enum):
    IDENTITY = "identity"
    CONTENT = "content"
    VOLATILE = "volatile"


@dataclass(frozen=True)
class EffectDecl:
    """A tool's identity classification (a minimal SED binding).

    ``provider_key_param`` names the call parameter into which Sakrit injects the
    derived key as a provider idempotency key (L2). Its presence marks the tool as
    provider-deduplicating: a retry carrying the same key is safe, so a crash in the
    window re-dispatches rather than surfacing as ``AMBIGUOUS``. Absent → L0 (the
    default): the crash-in-window floor is at-most-once + surfaced ambiguity. The
    fuller ladder (L1/L2R/L3) lands in Act III.
    """

    tool: str
    classes: Mapping[str, ArgClass] = field(default_factory=dict)
    default: ArgClass = ArgClass.IDENTITY
    provider_key_param: str | None = None
    clean_failures: tuple[type[BaseException], ...] = ()
    """Exception types the author asserts imply the effect did NOT execute (e.g.
    validation errors before any I/O, a provider 4xx meaning "rejected, nothing
    done"). Only these record ``FAILED`` (safely re-claimable). Every other
    exception is treated as *ambiguous* — the effect may have landed.

    **The most dangerous declaration in the system** (P1-12): it is the one knob that can
    reopen a duplicate. A type that also catches a *timeout* (the ambiguous window) is refused
    at construction — but Sakrit cannot inspect every SDK's exception tree, so **you** must
    ensure the types you name genuinely prove non-execution: never an over-broad network base
    (e.g. ``requests.exceptions.RequestException``, which *includes* ``Timeout``)."""
    provider_ttl_s: float | None = None
    """How long the provider remembers an idempotency key (L2). Beyond this horizon the
    provider has forgotten the key, so a crash-recovery re-dispatch would *not* dedup —
    a silent duplicate. Recovery surfaces ``AMBIGUOUS`` for an L2 leftover older than the
    TTL instead of re-claiming it (design §6: "within provider TTL; AMBIGUOUS beyond").
    ``None`` means unbounded (the current, over-optimistic behavior — set it for real
    providers, e.g. Stripe's 24h)."""
    reconcile: Callable[[str], Reconciliation] | None = None
    """A read-only query answering "did this effect happen?" for crash recovery.
    Its presence makes the tool L1 (or L2R, with ``provider_key_param``)."""
    on_absent: str = "surface"
    """What recovery does when reconcile says ABSENT: ``surface`` (safe default for
    irreversible effects — a lagging read can lie) or ``retry`` (re-claimable)."""
    provider_read: str = "eventual"
    """The consistency of the reconcile read: ``strong`` (a miss means the effect truly
    did not land) or ``eventual`` (a lagging replica may report ABSENT for an effect that
    *did* land). Only a ``strong`` read may back ``on_absent="retry"`` (P1-7): an eventual
    miss would re-execute a landed effect → duplicate. Default ``eventual`` — the safe
    assumption; assert ``strong`` explicitly to unlock auto-retry."""

    def __post_init__(self) -> None:
        for exc_type in self.clean_failures:
            if _encompasses_timeout(exc_type):
                # P1-12 / §3 anti-reflex: clean_failures is the single knob that reopens Q2 —
                # the most dangerous declaration in the system, and (unlike arg classes) it had
                # no friction. A type that also catches a *timeout* classifies the ambiguous
                # window (the effect MAY have landed) as "clean" → FAILED → re-claimable → a
                # duplicate of a possibly-completed effect. Refuse it loudly.
                raise SakritError(
                    f"{self.tool}: clean_failures includes {exc_type.__name__!r}, which also "
                    "catches a *timeout* — the ambiguous window where the effect may have "
                    "landed. Marking it 'clean' records FAILED (re-claimable) and re-dispatches "
                    "a possibly-completed effect → duplicate. Declare only failures that *prove* "
                    "the effect did not run (a validation error before any I/O; a provider 4xx "
                    "meaning 'rejected, nothing done'), never a timeout- or broad-I/O-encompassing "
                    "type."
                )
        if self.on_absent not in ("surface", "retry"):
            raise SakritError(f"on_absent must be 'surface' or 'retry', not {self.on_absent!r}")
        if self.provider_read not in ("strong", "eventual"):
            raise SakritError(
                f"provider_read must be 'strong' or 'eventual', not {self.provider_read!r}"
            )
        if self.on_absent == "retry" and self.provider_read != "strong":
            # §3 anti-reflex: the most dangerous recovery declaration must carry loud
            # friction. Auto-retry on ABSENT re-executes an irreversible effect; it is
            # only sound if the read cannot report a false ABSENT.
            raise SakritError(
                f"{self.tool}: on_absent='retry' auto-re-executes an effect the reconcile "
                "read reported ABSENT — sound only with provider_read='strong'. An "
                "eventually-consistent read can report ABSENT for an effect that landed, "
                "minting a duplicate. Declare provider_read='strong' to assert the read "
                "cannot lie, or use on_absent='surface'."
            )

    @property
    def provider_dedup(self) -> bool:
        return self.provider_key_param is not None

    @property
    def level(self) -> str:
        """The provider-cooperation rung, derived from declared capabilities."""
        if self.provider_key_param and self.reconcile:
            return "L2R"
        if self.reconcile:
            return "L1"
        if self.provider_key_param:
            return "L2"
        return "L0"

    def class_of(self, arg: str) -> ArgClass:
        return self.classes.get(arg, self.default)

    def identity_args(self, named: Mapping[str, object]) -> dict[str, object]:
        """The subset of supplied args that bear identity — what the fingerprint sees."""
        return {k: v for k, v in named.items() if self.class_of(k) is ArgClass.IDENTITY}
