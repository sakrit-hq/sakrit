# SPDX-License-Identifier: Apache-2.0
"""Runtime adapters — the framework-facing surface.

Each adapter sources a :class:`~sakrit.core.coordinate.Coordinate` from its
runtime and satisfies :class:`~sakrit.core.adapter.RuntimeAdapter`. Framework
imports belong *here*, never in ``sakrit.core``.

:class:`~sakrit.adapters.fake.FakeAdapter` is the in-memory reference the whole
core test suite runs against — see the FakeAdapter rule in ``docs/design.md`` §11.
"""

from sakrit.adapters.fake import FakeAdapter

__all__ = ["FakeAdapter"]
