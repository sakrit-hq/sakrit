# SPDX-License-Identifier: Apache-2.0
"""P1-7: on_absent="retry" auto-re-executes an effect a reconcile read reported ABSENT.
That is sound only if the read cannot report a false ABSENT — provider_read="strong".
The dangerous combination is refused at declaration time (§3 anti-reflex friction)."""

import pytest

from sakrit.core import ArgClass, EffectDecl, SakritError


def test_retry_without_strong_read_is_refused() -> None:
    with pytest.raises(SakritError, match="provider_read='strong'"):
        EffectDecl("crm.ticket", {"s": ArgClass.IDENTITY}, on_absent="retry")


def test_retry_with_eventual_read_is_refused() -> None:
    with pytest.raises(SakritError, match="provider_read='strong'"):
        EffectDecl(
            "crm.ticket", {"s": ArgClass.IDENTITY}, on_absent="retry", provider_read="eventual"
        )


def test_retry_with_strong_read_is_allowed() -> None:
    decl = EffectDecl(
        "crm.ticket", {"s": ArgClass.IDENTITY}, on_absent="retry", provider_read="strong"
    )
    assert decl.on_absent == "retry"
    assert decl.provider_read == "strong"


def test_surface_default_needs_no_strong_read() -> None:
    decl = EffectDecl("crm.ticket", {"s": ArgClass.IDENTITY})  # on_absent defaults to surface
    assert decl.on_absent == "surface"
    assert decl.provider_read == "eventual"


def test_invalid_on_absent_is_refused() -> None:
    with pytest.raises(SakritError, match="on_absent must be"):
        EffectDecl("crm.ticket", {"s": ArgClass.IDENTITY}, on_absent="refire")


def test_invalid_provider_read_is_refused() -> None:
    with pytest.raises(SakritError, match="provider_read must be"):
        EffectDecl("crm.ticket", {"s": ArgClass.IDENTITY}, provider_read="maybe")
