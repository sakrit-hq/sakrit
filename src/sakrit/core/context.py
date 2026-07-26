# SPDX-License-Identifier: Apache-2.0
"""The current effect's key, exposed to tools via a contextvar.

An L2 tool that must hand Sakrit's key to its provider as an idempotency key reads
:func:`current_key` — no phantom parameter polluting its signature. ``settle`` sets
it around the effect's execution. See ``docs/design.md`` §11 and Fable Q4/Q43.

Parameter injection (writing the key into a call argument) is reserved for the
future case of *registered foreign callables* whose real signature already accepts
the key and which have no code of ours to read the contextvar.
"""

from __future__ import annotations

import contextvars

from sakrit.core.errors import SakritError

_current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sakrit_current_key", default=None
)


def current_key() -> str:
    """The current guarded effect's key. Raises if called outside a guarded effect."""
    key = _current_key.get()
    if key is None:
        raise SakritError("current_key() called outside a guarded effect")
    return key
