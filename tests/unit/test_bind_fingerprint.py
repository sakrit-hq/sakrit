# SPDX-License-Identifier: Apache-2.0
"""P4-5: an uninspectable callable's *positional* args can't be mapped to names, so they
would silently vanish from the fingerprint — two different actions share one fingerprint and
the second replays the first silently. Refuse fail-closed; kwargs-only is safe."""

import pytest

from sakrit.core import SakritError
from sakrit.engine import _bind


def test_uninspectable_callable_with_positional_args_is_refused() -> None:
    # getattr has no introspectable signature; two positional args would be dropped.
    with pytest.raises(SakritError, match="fingerprint"):
        _bind(getattr, ("obj", "attr"), {})


def test_uninspectable_callable_kwargs_only_is_allowed() -> None:
    # Kwargs are already named — the fallback captures them fully, no silent drop.
    assert _bind(getattr, (), {"name": "x", "default": 1}) == {"name": "x", "default": 1}


def test_inspectable_callable_binds_positional_to_names() -> None:
    def send(to: str, subject: str) -> None: ...

    assert _bind(send, ("a@x.com", "hi"), {}) == {"to": "a@x.com", "subject": "hi"}
