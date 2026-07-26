# SPDX-License-Identifier: Apache-2.0
"""Integration tests — real sqlite/postgres, no mocks.

Populated in Act II (local store) and Act III (production database). Marked
``integration`` so they can be selected or skipped independently of unit tests.
"""

import pytest


@pytest.mark.integration
@pytest.mark.skip(reason="no integration tests yet — lands with the Act II store")
def test_placeholder() -> None: ...
