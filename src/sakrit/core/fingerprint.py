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
from sakrit.core.errors import SchemeMismatch
from sakrit.core.normalizers import normalize

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
    """HMAC-SHA256 over the canonicalized identity args of a call.

    A declared SED normalizer is applied to an identity arg *before* canonicalization, so
    incidental variation the author declared incidental (a trailing space, a mixed-case email)
    does not trip ``DivergentRetry``. An arg with no normalizer is canonicalized byte-level
    exactly as before — a normalizer-free decl is byte-identical to pre-normalizer fingerprints,
    so the scheme id above does not change.

    A normalizer is a ``str -> str`` transform, so it is applied **only to a string value**; a
    non-string identity arg (an ``int``, a structured value) is left untouched and canonicalized
    by its type as usual. This preserves the type distinction the canonicalizer makes — ``int 1``
    and ``str "1"`` stay distinct even under a declared normalizer, never colliding onto one
    identity — and a structured value never gets coerced to a bogus normalized string."""
    identity = decl.identity_args(named_args)
    if decl.normalizers:
        identity = {
            k: (
                normalize(decl.normalizers[k], v)
                if k in decl.normalizers and isinstance(v, str)
                else v
            )
            for k, v in identity.items()
        }
    return hmac.new(secret, canonicalize(identity), hashlib.sha256).hexdigest()


def matches_fingerprint(
    decl: EffectDecl,
    named_args: Mapping[str, object],
    stored_fp: str,
    stored_secret_id: str | None,
    *,
    keyring: Mapping[str, bytes],
    primary_secret: bytes,
) -> bool:
    """Whether ``named_args`` reproduces ``stored_fp`` under the secret that **signed the stored
    row** — the software dual-secret rotation window.

    Rotating the HMAC secret naively makes every in-flight fingerprint incomparable → a
    fleet-wide ``DivergentRetry`` storm, because the running code signs with the *new* secret
    while stored rows were signed with the *old* one. The window fixes that: sign new writes
    with the new (``primary_secret``) but keep old secrets in ``keyring`` (keyed by their
    :func:`secret_id`) for the drain window, and — the pivot — recompute the incoming args'
    fingerprint under the **same** secret that produced the stored one, identified by the row's
    ``stored_secret_id`` (the P5-3 provenance column). Same action → matches; different action →
    still diverges.

    - ``stored_secret_id is None`` (a pre-keyring/legacy row) → verify against ``primary_secret``:
      the single-secret behavior, unchanged.
    - ``stored_secret_id`` in ``keyring`` → verify against that secret.
    - ``stored_secret_id`` **not** in ``keyring`` → the signing secret has rotated *out* of the
      window. Refuse loudly (:class:`SchemeMismatch`) rather than return a misleading ``False``
      (a silent ``DivergentRetry``): the operator must widen the verify window or drain the runs
      it signed — never compare across an unknown secret.
    """
    secret: bytes
    if stored_secret_id is None:
        secret = primary_secret
    else:
        found = keyring.get(stored_secret_id)
        if found is None:
            raise SchemeMismatch(
                f"stored row was signed by HMAC secret id {stored_secret_id!r}, not in the "
                "verify keyring — it has rotated out of the window. Add the old secret to "
                "verify_secrets, or drain the in-flight runs it signed; never compare across an "
                "unknown secret."
            )
        secret = found
    return hmac.compare_digest(fingerprint(decl, named_args, secret=secret), stored_fp)
