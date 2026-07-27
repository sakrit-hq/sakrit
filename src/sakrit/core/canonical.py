# SPDX-License-Identifier: Apache-2.0
"""Canonical serialization — total, deterministic, and injective *up to a documented
set of container equivalences*.

The same logical value must always produce the same bytes, so a fingerprint over
canonicalized args is a reliable identity witness. Canonicalization is **core-owned**
(not per-adapter) or fingerprints wouldn't agree across adapters.

Rules (open-questions Q7): dict keys sorted by canonical key; sets sorted by
canonical element; ``str`` NFC-normalized; ``bytes`` verbatim; ``bool`` distinct
from ``int``; ``int`` exact; ``float`` shortest round-trip with ``-0.0``→``0.0`` and
NaN/inf as tokens; ``Decimal`` normalized; ``datetime`` UTC ISO (naive rejected);
``None`` distinct; every scalar length-framed and type-tagged so nothing collides.
Any unregistered type **fails closed** with :class:`CanonicalizationError`.

**Injectivity, precisely (P4-7).** Distinct *scalars* never collide (each is
type-tagged and length-framed; ``Decimal("1.00")`` and ``int 1`` differ by tag). But
canonicalization is injective on *logical shape*, not Python *type*: it deliberately
collapses these container equivalences —

- ``tuple`` ≡ ``list`` (both encode as ``L`` — JSON has no tuple, so the replay
  round-trip couldn't preserve the distinction anyway),
- ``set`` ≡ ``frozenset`` (both ``S``),
- any ``Mapping`` ≡ ``dict`` (both ``M``),
- a ``NamedTuple`` encodes as a positional ``L``, losing its field names.

These are benign for a fingerprint (a reworded ``list`` vs ``tuple`` of the same
identity args is the same intended action) and are the safe direction — they can only
make two calls look *more* alike, never mint a spurious divergence. They matter only
to the cross-language byte-identity story (Q15, ``spec.md``): a language binding MUST
adopt the same equivalences, or a shared fingerprint is meaningless. Documented, not
tightened — tightening ``tuple``/``list`` would break the JSON replay round-trip.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime, timezone
from decimal import Decimal

from sakrit.core.errors import CanonicalizationError


def canonicalize(value: object) -> bytes:
    """Return the canonical byte encoding of ``value`` (see module docstring)."""
    out = bytearray()
    _write(out, value)
    return bytes(out)


def _scalar(out: bytearray, tag: bytes, payload: bytes) -> None:
    out += tag
    out += len(payload).to_bytes(8, "big")
    out += payload


def _count(out: bytearray, tag: bytes, n: int) -> None:
    out += tag
    out += n.to_bytes(8, "big")


def _write(out: bytearray, v: object) -> None:
    if v is None:
        out += b"N"
    elif isinstance(v, bool):  # before int — bool is a subclass of int
        out += b"T" if v else b"F"
    elif isinstance(v, int):
        _scalar(out, b"i", str(v).encode("ascii"))
    elif isinstance(v, float):
        _scalar(out, b"f", _float(v).encode("ascii"))
    elif isinstance(v, Decimal):
        _scalar(out, b"d", _decimal(v).encode("ascii"))
    elif isinstance(v, str):
        _scalar(out, b"s", unicodedata.normalize("NFC", v).encode("utf-8"))
    elif isinstance(v, (bytes, bytearray)):
        _scalar(out, b"x", bytes(v))
    elif isinstance(v, datetime):
        _scalar(out, b"t", _datetime(v).encode("ascii"))
    elif isinstance(v, Mapping):
        items = sorted(v.items(), key=lambda kv: canonicalize(kv[0]))
        _count(out, b"M", len(items))
        for k, val in items:
            _write(out, k)
            _write(out, val)
    elif isinstance(v, AbstractSet):
        elems = sorted(canonicalize(e) for e in v)
        _count(out, b"S", len(elems))
        for e in elems:
            out += len(e).to_bytes(8, "big")
            out += e
    elif isinstance(v, Sequence):  # list/tuple/range — str & bytes handled above
        _count(out, b"L", len(v))
        for item in v:
            _write(out, item)
    else:
        raise CanonicalizationError(
            f"cannot canonicalize {type(v).__name__}; register a normalizer or "
            "pass a canonical-serializable value"
        )


def _float(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    if v == 0.0:  # collapse -0.0 and 0.0
        return "0.0"
    return repr(v)  # Python's repr is the shortest round-trip form


def _decimal(v: Decimal) -> str:
    if v.is_nan():
        return "NaN"
    if v.is_infinite():
        return "Infinity" if v > 0 else "-Infinity"
    return str(v.normalize())


def _datetime(v: datetime) -> str:
    if v.tzinfo is None:
        raise CanonicalizationError("naive datetime is ambiguous; attach a timezone")
    return v.astimezone(timezone.utc).isoformat()
