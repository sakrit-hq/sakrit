# SPDX-License-Identifier: Apache-2.0
"""P4-4: a replay returns json.loads(json.dumps(result)) — a lossy transform (tuple→list,
int keys→str). Exactly-once is untouched (nothing re-fires), but the fidelity loss is now
declared and, when a result won't round-trip cleanly, told (never silent)."""

import logging

import pytest

from sakrit.core import SqliteLedger, positional_key
from sakrit.core.coordinate import Coordinate


def _executing(led: SqliteLedger, key: str) -> None:
    led.claim(key, "global", "t", "fp")
    led.mark_executing(key)


def test_lossy_result_is_told_and_replays_as_reconstruction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    led = SqliteLedger(":memory:")
    key = positional_key(Coordinate("global", b"k"), "t")
    _executing(led, key)
    with caplog.at_level(logging.INFO, logger="sakrit"):
        led.record_success(key, (1, 2, 3))  # a tuple → won't JSON-round-trip
    assert "round-trip" in caplog.text.lower()
    replay = led.claim(key, "global", "t", "fp")
    assert replay.result == [1, 2, 3]  # the tuple came back a list (documented)


def test_json_native_result_is_not_flagged(caplog: pytest.LogCaptureFixture) -> None:
    led = SqliteLedger(":memory:")
    key = positional_key(Coordinate("global", b"k"), "t")
    _executing(led, key)
    with caplog.at_level(logging.INFO, logger="sakrit"):
        led.record_success(key, {"ok": True, "n": 3})
    assert "round-trip" not in caplog.text.lower()
    replay = led.claim(key, "global", "t", "fp")
    assert replay.result == {"ok": True, "n": 3}
