# SPDX-License-Identifier: Apache-2.0
"""SED builtin normalizers — cross-language, byte-identical *by specification*.

A normalizer is a pure ``str -> str`` transform applied to an identity-bearing argument
*before* it is fingerprinted, so two calls that differ only in an incidental way (a
trailing space, a mixed-case email) share one identity instead of tripping a spurious
``DivergentRetry``. These are the SED ``args.<name>.normalize`` builtins (``docs/spec.md``).

**They live in the core, not in ``sakrit.spec``.** A normalizer changes what a *fingerprint*
means — it is a moat primitive the fingerprint consumes — so the core owns it and the SED
format layer references it (``sakrit.spec`` re-exports these for the public format API). The
core never imports the format; the dependency runs one way. This module imports only the
standard library.

**The contract is the vectors, not this code (Q15).** A Python ``email`` normalizer and a
future Node ``email`` normalizer MUST produce identical output bytes, or a fingerprint
shared across a Python agent and a TS agent guarding the same tool is meaningless. So the
normative artifact is ``tests/fixtures/sed-normalizer-vectors.json`` — a language-neutral
input→output golden file every binding must reproduce. This module is the Python binding of
it; the rules below are stated precisely enough to re-implement elsewhere.

Byte-level (no normalization) is the default; a normalizer is opt-in per arg. Each rule is
deliberately conservative — it only removes incidental variation it can justify, never
guesses. Unicode operations pin an explicit normal form so they are reproducible.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

from sakrit.core.errors import SakritError


class NormalizerError(SakritError):
    """A value could not be normalized by the named builtin (e.g. non-numeric ``money``). A
    :class:`~sakrit.core.errors.SakritError` so a bad declared value surfaces as a first-class,
    catchable guard failure — raised while computing the fingerprint, before any ledger write,
    so it never leaves an ambiguous leftover."""


def _trim(s: str) -> str:
    """Strip leading/trailing Unicode whitespace (Python ``str.strip()`` — every code point
    with the Unicode White_Space property). Interior whitespace is untouched."""
    return s.strip()


def _nfc_trim(s: str) -> str:
    """NFC-normalize, then trim. NFC (canonical composition) is pinned so an accented
    character composed two different ways (``é`` vs ``e`` + combining acute) canonicalizes to
    one byte sequence across languages."""
    return unicodedata.normalize("NFC", s).strip()


def _email(s: str) -> str:
    """NFC + trim + ASCII-lowercase. Email is treated case-insensitively for dedup identity
    (virtually every provider does), so ``Alice@Example.COM`` and ``alice@example.com`` are
    one recipient. The whole address is lowercased (not just the domain) — simpler and
    cross-language-stable; the rare case-sensitive local-part is not worth the divergence."""
    return unicodedata.normalize("NFC", s).strip().lower()


def _phone_e164(s: str) -> str:
    """Reduce to ``[+]<digits>``: keep a single leading ``+`` if present, then every ASCII
    digit, dropping spaces, dashes, parentheses, and dots. A *normalizer*, not a validator —
    it does not verify country codes or length; it makes ``+1 (415) 555-0100`` and
    ``+14155550100`` one identity. Non-ASCII digits are not folded (kept out → dropped)."""
    plus = "+" if s.lstrip().startswith("+") else ""
    digits = "".join(c for c in s if c in "0123456789")
    return plus + digits


def _url_canonical(s: str) -> str:
    """Lowercase the scheme and host, drop a default port (80/http, 443/https), and remove
    the fragment. Path, query, and userinfo are preserved verbatim (case- and order-
    sensitive by spec). Whitespace is trimmed first. A conservative canonicalization — it
    does not reorder query params or decode percent-escapes (both are lossy/ambiguous)."""
    parts = urlsplit(s.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    netloc = host
    if parts.port is not None and parts.port != default_port:
        netloc = f"{host}:{parts.port}"
    if parts.username is not None:
        userinfo = parts.username + (f":{parts.password}" if parts.password is not None else "")
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _money(s: str) -> str:
    """Canonicalize a decimal money string so ``1.5``, ``1.50``, ``1.500`` and ``$1,500.00``
    normalize consistently. Rule: drop everything except digits, a single leading sign, and
    ``.`` (currency symbols, spaces, and ``,`` thousands separators are removed — **v1 assumes
    ``.`` is the decimal separator and ``,`` is a thousands separator**, the en-US/spec
    convention; a locale using ``,`` as the decimal point must declare a different type/
    normalizer). Parse as :class:`decimal.Decimal`, output the exponent-free canonical form
    (trailing zeros removed: ``1.50`` → ``1.5``; ``100`` stays ``100``, never ``1E+2``)."""
    cleaned = "".join(c for c in s if c in "0123456789.-")
    if not cleaned or cleaned in ("-", ".", "-."):
        raise NormalizerError(f"money normalizer: {s!r} has no parseable amount")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise NormalizerError(f"money normalizer: {s!r} is not a valid decimal amount") from exc
    return format(value.normalize(), "f")


# The builtin registry. Names are the SED ``normalize:`` values (docs/spec.md).
BUILTIN_NORMALIZERS: dict[str, Callable[[str], str]] = {
    "trim": _trim,
    "nfc-trim": _nfc_trim,
    "email": _email,
    "phone-e164": _phone_e164,
    "url-canonical": _url_canonical,
    "money": _money,
}

NORMALIZER_NAMES = frozenset(BUILTIN_NORMALIZERS)


def normalize(name: str, value: str) -> str:
    """Apply the named builtin normalizer to ``value``. Raises :class:`NormalizerError` on an
    unknown name (fail closed — never silently pass the value through unnormalized)."""
    fn = BUILTIN_NORMALIZERS.get(name)
    if fn is None:
        raise NormalizerError(
            f"unknown normalizer {name!r}; builtins are {sorted(NORMALIZER_NAMES)}"
        )
    return fn(value)
