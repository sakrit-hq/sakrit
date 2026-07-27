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

# The fingerprint scheme id (HMAC-SHA256 over the v1 canonical form). Stamped onto each ledger
# row (P5-3) so a change to *either* the HMAC construction or the canonicalizer is detectable —
# both change what a fingerprint means, so both must bump this. A stored fingerprint from a
# different scheme is incomparable; the ledger refuses to compare across versions.
FINGERPRINT_VERSION = "hmac-sha256-canon-v1"


def secret_id(secret: bytes) -> str:
    """A short, non-reversible id for the HMAC ``secret`` (P5-3).

    Recorded on each row so a *rotation* is detectable — the row remembers which secret signed
    its fingerprint, the precondition for a dual-secret verify window (verify against a small
    keyring, sign with the current key) instead of a fleet-wide ``DivergentRetry`` storm on
    rotation. It is a domain-separated digest of the secret, not the secret itself: the ledger
    never learns the raw bytes, and the id cannot be reversed to them for a real (high-entropy)
    secret. The keyring/verify machinery itself is Act IV; this is only the row stamp that makes
    it a config change later rather than a drain."""
    return hashlib.sha256(b"sakrit-secret-id-v1\x00" + secret).hexdigest()[:16]


def fingerprint(decl: EffectDecl, named_args: Mapping[str, object], *, secret: bytes) -> str:
    """HMAC-SHA256 over the canonicalized identity args of a call."""
    identity = decl.identity_args(named_args)
    return hmac.new(secret, canonicalize(identity), hashlib.sha256).hexdigest()
