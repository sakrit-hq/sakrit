# SPDX-License-Identifier: Apache-2.0
"""SED ⇄ EffectDecl convergence (P5-2): the format binds to the in-code declaration, the
normalizer flows into the fingerprint, and a field the runtime can't honor is refused loudly
(never silently dropped) or accepted with a loud log — never a silent no-op."""

import logging

import pytest

from sakrit.core import ArgClass, Reconciliation, Verdict, fingerprint
from sakrit.spec import SpecError, decl_to_sed, load_sed, parse_sed, sed_to_decl

SECRET = b"deployment-secret"


def _reconcile(_key: str) -> Reconciliation:
    return Reconciliation(Verdict.ABSENT)


def test_sed_binds_to_effectdecl() -> None:
    decl = load_sed(
        {
            "sed": 1,
            "tool": "crm.create_ticket",
            "level": "l2r",
            "args": {
                "customer_id": {"class": "identity", "normalize": "trim"},
                "body": {"class": "content"},
            },
            "provider_key": {"param": "idempotency_key", "ttl_s": 86400},
            "reconcile": {"ref": "python:x:y", "provider_read": "strong"},
        },
        resolve_ref=lambda ref: _reconcile,
    )
    assert decl.tool == "crm.create_ticket"
    assert decl.classes["customer_id"] is ArgClass.IDENTITY
    assert decl.classes["body"] is ArgClass.CONTENT
    assert decl.normalizers == {"customer_id": "trim"}
    assert decl.provider_key_param == "idempotency_key"
    assert decl.provider_ttl_s == 86400.0
    assert decl.provider_read == "strong"
    assert decl.reconcile is _reconcile
    assert decl.level == "L2R"  # derived level agrees with the declared one


# --- the normalizer actually flows into the fingerprint -------------------
def test_declared_normalizer_changes_the_fingerprint_path() -> None:
    decl = load_sed(
        {"sed": 1, "tool": "t", "args": {"email": {"class": "identity", "normalize": "email"}}},
    )
    # Two calls differing only in incidental case/whitespace normalize to one identity...
    fp_a = fingerprint(decl, {"email": " Alice@Example.COM "}, secret=SECRET)
    fp_b = fingerprint(decl, {"email": "alice@example.com"}, secret=SECRET)
    assert fp_a == fp_b
    # ...but a genuinely different recipient still diverges.
    fp_c = fingerprint(decl, {"email": "bob@example.com"}, secret=SECRET)
    assert fp_c != fp_a


def test_normalizer_preserves_int_vs_str_type_distinction() -> None:
    # A declared normalizer must NOT collapse int 1 and str "1" onto one identity (that would
    # drop a genuinely distinct effect). Normalizers apply only to str values; a non-str keeps
    # its type tag through canonicalization.
    decl = load_sed(
        {"sed": 1, "tool": "t", "args": {"n": {"class": "identity", "normalize": "trim"}}}
    )
    fp_int = fingerprint(decl, {"n": 1}, secret=SECRET)
    fp_str = fingerprint(decl, {"n": "1"}, secret=SECRET)
    assert fp_int != fp_str  # type distinction survives the declared normalizer


def test_structured_value_under_a_normalizer_does_not_crash_or_coerce() -> None:
    # A dict/list identity arg with a declared normalizer must not be str()-coerced to a bogus
    # identity — it is left to the canonicalizer, which handles it by structure.
    decl = load_sed(
        {"sed": 1, "tool": "t", "args": {"n": {"class": "identity", "normalize": "trim"}}}
    )
    fp_a = fingerprint(decl, {"n": [1, 2]}, secret=SECRET)
    fp_b = fingerprint(decl, {"n": [1, 3]}, secret=SECRET)
    assert fp_a != fp_b  # structure preserved, not coerced to "12"/"13"-ish collisions


def test_no_normalizer_is_byte_identical_to_a_plain_decl() -> None:
    # The non-breaking guarantee: a decl with no normalizer fingerprints exactly as a
    # hand-built EffectDecl (no FINGERPRINT_VERSION bump was needed).
    from sakrit.core import EffectDecl

    sed_decl = load_sed({"sed": 1, "tool": "t", "args": {"to": {"class": "identity"}}})
    plain = EffectDecl("t", {"to": ArgClass.IDENTITY})
    args = {"to": "a@x.com"}
    assert fingerprint(sed_decl, args, secret=SECRET) == fingerprint(plain, args, secret=SECRET)


# --- refuse what the runtime can't honor (never a silent drop) ------------
@pytest.mark.parametrize(
    ("doc", "match"),
    [
        ({"sed": 1, "tool": "t", "result": {"replay": "marker"}}, "result.replay"),
        ({"sed": 1, "tool": "t", "level": "l3"}, "level='l3'"),
        (
            {
                "sed": 1,
                "tool": "t",
                "level": "l2",
                "provider_key": {"param": "k", "encoding": "base62"},
            },
            "encoding",
        ),
        ({"sed": 1, "tool": "t", "fingerprint": {"version": 9}}, "fingerprint.version"),
        ({"sed": 1, "tool": "t", "args": {"*": {"normalize": "trim"}}}, "wildcard normalizer"),
    ],
)
def test_unhonorable_fields_are_refused(doc: dict[str, object], match: str) -> None:
    with pytest.raises(SpecError, match=match):
        sed_to_decl(parse_sed(doc), resolve_ref=lambda ref: _reconcile)


def test_unenforced_but_safe_fields_are_accepted_with_a_loud_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sakrit.spec"):
        decl = load_sed(
            {
                "sed": 1,
                "tool": "t",
                "ambiguous": {"default": "retry", "escalate_after_s": 60},
            }
        )
    assert decl.tool == "t"  # accepted (safe default applies) ...
    msgs = " ".join(r.message for r in caplog.records)
    assert "ambiguous.default" in msgs and "escalate_after_s" in msgs  # ... but loudly logged


# --- reconcile.ref resolution --------------------------------------------
def test_non_python_ref_is_refused() -> None:
    with pytest.raises(SpecError, match="only 'python:"):
        load_sed({"sed": 1, "tool": "t", "level": "l1", "reconcile": {"ref": "node:x.y:z"}})


def test_python_ref_resolves_a_real_callable() -> None:
    # Exercises the real resolver against an importable target (mechanism proof).
    decl = load_sed(
        {"sed": 1, "tool": "t", "level": "l1", "reconcile": {"ref": "python:math:floor"}}
    )
    assert decl.reconcile is not None
    assert getattr(decl.reconcile, "__name__", None) == "floor"  # resolved the real builtin


def test_bad_python_ref_fails_closed() -> None:
    with pytest.raises(SpecError, match="cannot import|has no"):
        load_sed({"sed": 1, "tool": "t", "level": "l1", "reconcile": {"ref": "python:sakrit:nope"}})


def test_declared_level_contradicting_capabilities_is_refused() -> None:
    # level: l0am (no dedup) alongside a provider_key derives L2 — a silent guarantee upgrade.
    with pytest.raises(SpecError, match="contradicts the capabilities"):
        sed_to_decl(
            parse_sed({"sed": 1, "tool": "t", "level": "l0am", "provider_key": {"param": "k"}}),
        )


def test_matching_declared_level_is_accepted() -> None:
    decl = load_sed({"sed": 1, "tool": "t", "level": "l2", "provider_key": {"param": "k"}})
    assert decl.level == "L2"


# --- round-trip through decl_to_sed --------------------------------------
def test_l0_decl_round_trips_through_sed() -> None:
    # Regression: an L0 decl (no provider_key/reconcile) must emit a VALID sed level (l0am),
    # not bare "l0" (which is not in the enum) — the round-trip was broken for the common case.
    from sakrit.core import EffectDecl
    from sakrit.spec import emit_sed

    decl = EffectDecl("t", {"to": ArgClass.IDENTITY})
    reparsed = parse_sed(emit_sed(decl_to_sed(decl)))  # must not raise
    assert reparsed.level == "l0am"
    bound = sed_to_decl(reparsed)
    assert bound.level == "L0"


def test_decl_to_sed_round_trips_a_named_reconcile() -> None:
    decl = load_sed(
        {
            "sed": 1,
            "tool": "crm.create_ticket",
            "args": {"customer_id": {"class": "identity", "normalize": "trim"}},
            "provider_key": {"param": "idempotency_key"},
            "reconcile": {"ref": "python:ignored:by_injected_resolver"},
        },
        resolve_ref=lambda ref: _reconcile,
    )
    doc = decl_to_sed(decl)
    assert doc.tool == "crm.create_ticket"
    assert doc.args["customer_id"].normalize == "trim"
    assert doc.provider_key is not None and doc.provider_key.param == "idempotency_key"
    assert doc.reconcile is not None
    assert doc.reconcile.ref.endswith(":_reconcile")
