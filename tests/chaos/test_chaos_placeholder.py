# SPDX-License-Identifier: Apache-2.0
"""Chaos tests — Act III kill-at-every-boundary suite.

Kill the process before the effect, during it, after it, before the record
saves, and after — asserting exactly-once holds through all of them. This suite
is itself a credibility artifact (Act III, step 10) and runs on a schedule via
``.github/workflows/chaos.yml``.
"""

import pytest


@pytest.mark.chaos
@pytest.mark.skip(reason="no chaos tests yet — lands in Act III")
def test_placeholder() -> None: ...
