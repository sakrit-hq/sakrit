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

from sakrit.core.reconcile import Reconciliation


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
    exception is treated as *ambiguous* — the effect may have landed."""
    reconcile: Callable[[str], Reconciliation] | None = None
    """A read-only query answering "did this effect happen?" for crash recovery.
    Its presence makes the tool L1 (or L2R, with ``provider_key_param``)."""
    on_absent: str = "surface"
    """What recovery does when reconcile says ABSENT: ``surface`` (safe default for
    irreversible effects — a lagging read can lie) or ``retry`` (re-claimable)."""

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
