# SPDX-License-Identifier: Apache-2.0
"""The fingerprint — evidence, not identity.

The fingerprint answers *"does what I'm being asked to do match what was
recorded?"* It is an HMAC over the canonicalized **identity args** only, so a
reworded ``content`` arg does not change it (R2 tolerance) while a changed identity
arg does (→ ``DivergentRetry``). Stored on the ledger row, never in the key.

Keyed with a per-deployment secret so the stored fingerprint is non-reversible —
the ledger holds no raw argument bytes by default. See ``docs/design.md`` §2–§3.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from sakrit.core.canonical import canonicalize
from sakrit.core.declaration import EffectDecl


def fingerprint(decl: EffectDecl, named_args: Mapping[str, object], *, secret: bytes) -> str:
    """HMAC-SHA256 over the canonicalized identity args of a call."""
    identity = decl.identity_args(named_args)
    return hmac.new(secret, canonicalize(identity), hashlib.sha256).hexdigest()
