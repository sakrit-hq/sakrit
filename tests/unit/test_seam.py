# SPDX-License-Identifier: Apache-2.0
"""The core ↔ adapter seam: coordinate ladder, positional keys, FakeAdapter rule.

This module imports only ``sakrit`` — no framework. If the core's dedup can be
exercised here, the seam holds (docs/design.md §11).
"""

import pytest

from sakrit.adapters import FakeAdapter
from sakrit.core import (
    Capabilities,
    Coordinate,
    NoCoordinateError,
    RuntimeAdapter,
    Stability,
    positional_key,
    resolve_coordinate,
)


def test_fakeadapter_satisfies_runtime_adapter() -> None:
    assert isinstance(FakeAdapter(), RuntimeAdapter)


def test_coordinate_is_immutable_and_hashable() -> None:
    c = Coordinate(scope="run-1", call_site=b"send")
    assert c.occurrence == 1 and c.plan_epoch == 0
    assert hash(c) == hash(Coordinate("run-1", b"send"))
    with pytest.raises(AttributeError):
        c.scope = "other"  # type: ignore[misc]


# --- the coordinate ladder (design.md §4) ---------------------------------
def test_ladder_rung1_explicit_key_wins_over_adapter() -> None:
    # P4-2: an explicit key names one action, so it outranks the runtime coordinate.
    adapter = FakeAdapter(scope="run-1")
    adapter.at("send-email")
    coord = resolve_coordinate(adapter, key="invoice-8841-charge")
    assert coord.scope == "global"
    assert coord.call_site == b"invoice-8841-charge"


def test_ladder_rung2_adapter_coordinate_when_no_key() -> None:
    adapter = FakeAdapter(scope="run-1")
    adapter.at("send-email")
    # No key → the adapter's runtime coordinate wins over a step.
    coord = resolve_coordinate(adapter, step="ignored")
    assert coord.scope == "run-1"
    assert coord.call_site == b"send-email"


def test_ladder_key_and_step_together_refuses() -> None:
    with pytest.raises(NoCoordinateError, match="two different identities"):
        resolve_coordinate(key="invoice-8841-charge", step="welcome-email")


def test_ladder_rung2_developer_step() -> None:
    coord = resolve_coordinate(scope="run-1", step="welcome-email", occurrence=2)
    assert coord == Coordinate("run-1", b"welcome-email", occurrence=2)


def test_ladder_rung2_step_without_scope_refuses() -> None:
    with pytest.raises(NoCoordinateError, match="needs a scope"):
        resolve_coordinate(step="welcome-email")


def test_ladder_rung3_business_key_is_global() -> None:
    coord = resolve_coordinate(key="invoice-8841-charge")
    assert coord.scope == "global"
    assert coord.call_site == b"invoice-8841-charge"


def test_ladder_rung4_refuses_loudly() -> None:
    with pytest.raises(NoCoordinateError, match="no coordinate"):
        resolve_coordinate()


def test_adapter_with_no_current_coordinate_falls_through() -> None:
    adapter = FakeAdapter()  # never placed at a step -> current_coordinate() is None
    coord = resolve_coordinate(adapter, key="biz-key")
    assert coord.call_site == b"biz-key"


# --- positional keys: stable across replay, unique per step ---------------
def test_key_stable_across_reexecution() -> None:
    adapter = FakeAdapter(scope="run-1")
    first = positional_key(adapter.at("send"), "email.send")
    # A replay re-places the adapter at the same step -> same coordinate -> same key.
    replay = positional_key(adapter.at("send"), "email.send")
    assert first == replay


def test_key_unique_per_step_and_per_tool_and_per_occurrence() -> None:
    a = FakeAdapter(scope="run-1")
    k_send = positional_key(a.at("send"), "email.send")
    k_other_site = positional_key(a.at("notify"), "email.send")
    k_other_tool = positional_key(a.at("send"), "sms.send")
    k_other_occ = positional_key(a.at("send", occurrence=2), "email.send")
    k_other_scope = positional_key(FakeAdapter(scope="run-2").at("send"), "email.send")
    assert len({k_send, k_other_site, k_other_tool, k_other_occ, k_other_scope}) == 5


def test_key_is_hex_sha256() -> None:
    k = positional_key(Coordinate("s", b"c"), "t")
    assert len(k) == 64 and all(ch in "0123456789abcdef" for ch in k)


def test_key_framing_is_injective() -> None:
    # Concatenation-ambiguous inputs must not collide (length-prefix framing).
    a = positional_key(Coordinate("ab", b"c"), "tool")
    b = positional_key(Coordinate("a", b"bc"), "tool")
    assert a != b


# --- adapter self-report ---------------------------------------------------
def test_fakeadapter_reports_stability_and_capabilities() -> None:
    adapter = FakeAdapter()
    assert adapter.stability_domain() is Stability.REPLAY | Stability.RETRY
    assert adapter.capabilities() is Capabilities.NONE
    assert adapter.scope_terminal("fake-run") is False
    adapter.mark_terminal()
    assert adapter.scope_terminal("fake-run") is True


def test_reserved_adapter_methods_are_off_the_stable_surface() -> None:
    # P5-4: RuntimeAdapter (v1 stable) is exactly current_coordinate(); the reserved methods
    # (semantics unfixed) satisfy ReservedAdapter but are not part of the frozen contract, and
    # on_recovery was removed entirely (the engine owns recovery).
    from sakrit.core import ReservedAdapter, RuntimeAdapter

    a = FakeAdapter()
    assert isinstance(a, RuntimeAdapter)  # provides the one consumed method
    assert isinstance(a, ReservedAdapter)  # still offers the reserved appendix
    assert not hasattr(a, "on_recovery")  # the false contract is gone
