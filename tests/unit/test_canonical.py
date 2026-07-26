# SPDX-License-Identifier: Apache-2.0
"""Canonicalization: deterministic, order-independent, type-distinct, injective."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sakrit.core import CanonicalizationError, canonicalize


def test_deterministic() -> None:
    v = {"b": [1, 2, 3], "a": "x"}
    assert canonicalize(v) == canonicalize({"a": "x", "b": [1, 2, 3]})


def test_dict_key_order_independent() -> None:
    assert canonicalize({"a": 1, "b": 2}) == canonicalize({"b": 2, "a": 1})


def test_set_order_independent() -> None:
    assert canonicalize({1, 2, 3}) == canonicalize({3, 1, 2})


def test_types_are_distinct() -> None:
    encs = {
        canonicalize(1),
        canonicalize(1.0),
        canonicalize("1"),
        canonicalize(True),
        canonicalize(Decimal("1")),
    }
    assert len(encs) == 5  # int, float, str, bool, Decimal all differ


def test_bool_not_int() -> None:
    assert canonicalize(True) != canonicalize(1)
    assert canonicalize(False) != canonicalize(0)


def test_negative_zero_collapses() -> None:
    assert canonicalize(-0.0) == canonicalize(0.0)


def test_nan_is_stable() -> None:
    # float NaN != NaN, but the canonical form must be stable.
    assert canonicalize(float("nan")) == canonicalize(float("nan"))


def test_containers_injective() -> None:
    assert canonicalize(["a", "b"]) != canonicalize(["ab"])
    assert canonicalize({"a": "bc"}) != canonicalize({"ab": "c"})
    assert canonicalize(["a", ["b"]]) != canonicalize([["a"], "b"])


def test_none_distinct_from_empty_and_false() -> None:
    encs = {canonicalize(None), canonicalize(""), canonicalize(False), canonicalize(0)}
    assert len(encs) == 4


def test_datetime_utc_normalized() -> None:
    from datetime import timedelta, tzinfo

    class _Plus1(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            return timedelta(hours=1)

        def tzname(self, dt: datetime | None) -> str:
            return "+01"

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

    aware_utc = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    same_moment_plus1 = datetime(2026, 7, 26, 13, 0, tzinfo=_Plus1())
    assert canonicalize(aware_utc) == canonicalize(same_moment_plus1)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="naive datetime"):
        canonicalize(datetime(2026, 7, 26, 12, 0))


def test_unknown_type_fails_closed() -> None:
    class Weird:
        pass

    with pytest.raises(CanonicalizationError, match="cannot canonicalize"):
        canonicalize(Weird())
