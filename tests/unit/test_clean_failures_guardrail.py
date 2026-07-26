# SPDX-License-Identifier: Apache-2.0
"""P1-12: clean_failures is the one knob that can reopen a duplicate — declaring a type that
also catches a *timeout* (the ambiguous window) marks it FAILED → re-claimable → a duplicate
of a possibly-landed effect. Refused at construction (§3 anti-reflex friction)."""

import pytest

from sakrit.core import ArgClass, EffectDecl, SakritError

DECL_ARGS = ("email.send", {"to": ArgClass.IDENTITY})


@pytest.mark.parametrize("bad", [TimeoutError, OSError, Exception, BaseException])
def test_timeout_encompassing_clean_failure_is_refused(bad: type[BaseException]) -> None:
    with pytest.raises(SakritError, match="timeout"):
        EffectDecl(*DECL_ARGS, clean_failures=(bad,))


def test_named_timeout_type_is_refused() -> None:
    class ProviderTimeout(Exception):  # a third-party-style *Timeout* type, matched by name
        pass

    with pytest.raises(SakritError, match="timeout"):
        EffectDecl(*DECL_ARGS, clean_failures=(ProviderTimeout,))


def test_narrow_clean_failure_is_allowed() -> None:
    class ValidationError(Exception):  # proves the effect did not run (rejected before I/O)
        pass

    decl = EffectDecl(*DECL_ARGS, clean_failures=(ValidationError,))
    assert decl.clean_failures == (ValidationError,)


def test_one_bad_type_among_good_is_refused() -> None:
    class Rejected(Exception):
        pass

    with pytest.raises(SakritError, match="timeout"):
        EffectDecl(*DECL_ARGS, clean_failures=(Rejected, TimeoutError))
