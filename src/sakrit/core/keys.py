# SPDX-License-Identifier: Apache-2.0
"""Positional key derivation.

The key answers "which step of which run is this?" — derived from the coordinate
and the tool's logical identity, **never from the arguments** (agents regenerate
arguments; args are evidence, checked via a fingerprint, not identity). See
``docs/design.md`` §2 and ``dev-notes/identity.md``.

Fields are length-prefixed before hashing so the derivation is injective — no two
distinct ``(coordinate, tool)`` inputs can collide through concatenation ambiguity,
even though ``call_site`` is arbitrary opaque bytes.
"""

from __future__ import annotations

import hashlib

from sakrit.core.coordinate import Coordinate

# Bumped if the derivation ever changes, so old and new keys never silently alias.
# v3→v4 (P4-6): the coordinate ladder now prefixes ``call_site`` with a one-byte rung tag
# (see ``adapter.py``), so a business ``key=``, a runtime coordinate, and a declared ``step=``
# with the same string can no longer mint a byte-identical key. That changes every
# ladder-produced key, so it is a key-scheme bump. A key-scheme change is a *re-key*: old
# rows are addressed under different keys and simply are not found (the row's stored
# ``key_version`` records which scheme wrote it, but — unlike a fingerprint-scheme change —
# it cannot be auto-detected on a key miss). Pre-freeze this is free (no production ledger
# exists); post-freeze it is the "drain in-flight runs" migration of design §13.5.
_KEY_SCHEME = b"sakrit-key-v4"

# The scheme id lifted out of the hash input onto the ledger row (P5-3), so a mixed-scheme
# ledger is *detectable* rather than silently orphaning. Must track ``_KEY_SCHEME``.
KEY_VERSION = "v4"


def positional_key(coord: Coordinate, tool: str) -> str:
    """Derive the stable, unique key for a logical step of a run.

    Same ``(coord, tool)`` → same key (dedup across a resume). Distinct logical
    steps → distinct keys (no collision). Independent of argument bytes.
    """
    parts: list[bytes] = [
        _KEY_SCHEME,
        coord.scope.encode("utf-8"),
        coord.call_site,
        tool.encode("utf-8"),
        str(coord.occurrence).encode("utf-8"),
        str(coord.plan_epoch).encode("utf-8"),
    ]
    buf = bytearray()
    for part in parts:
        buf += len(part).to_bytes(8, "big")  # length-prefix → injective framing
        buf += part
    return hashlib.sha256(bytes(buf)).hexdigest()
