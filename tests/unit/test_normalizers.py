# SPDX-License-Identifier: Apache-2.0
"""SED builtin-normalizer conformance (Q15).

The golden file ``tests/fixtures/sed-normalizer-vectors.json`` is the NORMATIVE,
language-neutral contract: every binding of the SED normalizers must reproduce its
output byte-for-byte. This asserts the Python binding against it — the same suite a
Node/TS binding would run against the same file."""

import json
from pathlib import Path

import pytest

from sakrit.spec.normalizers import NORMALIZER_NAMES, NormalizerError, normalize

_VECTORS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "sed-normalizer-vectors.json").read_text()
)


def _cases() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for name, rows in _VECTORS["vectors"].items():
        for row in rows:
            out.append((name, row["in"], row["out"]))
    return out


@pytest.mark.parametrize(("name", "value", "expected"), _cases())
def test_normalizer_matches_golden_vector(name: str, value: str, expected: str) -> None:
    assert normalize(name, value) == expected


def test_every_builtin_has_at_least_one_vector() -> None:
    # The golden file must cover every shipped builtin — a normalizer with no vector is an
    # unspecified cross-language contract (the exact Q15 hazard).
    covered = set(_VECTORS["vectors"])
    assert covered == set(NORMALIZER_NAMES), f"uncovered/extra: {covered ^ set(NORMALIZER_NAMES)}"


def test_normalizer_is_idempotent_on_its_own_output() -> None:
    # Applying a normalizer to already-normalized input is a no-op — a property any
    # canonicalizer must have (else a re-fingerprint of a stored identity could drift).
    for name, value, _expected in _cases():
        once = normalize(name, value)
        assert normalize(name, once) == once, f"{name} not idempotent on {once!r}"


def test_unknown_normalizer_fails_closed() -> None:
    with pytest.raises(NormalizerError, match="unknown normalizer"):
        normalize("no-such-normalizer", "x")


def test_money_rejects_non_numeric() -> None:
    with pytest.raises(NormalizerError, match="money"):
        normalize("money", "free")
