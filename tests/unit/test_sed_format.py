# SPDX-License-Identifier: Apache-2.0
"""SED v1 parse / validate / emit (roadmap Act IV #15).

Covers the format's guarantees: version gating fails closed, unknown enum values are
refused, undeclared args default to identity, the anti-reflex guardrail refuses an
unforced identity-typed downgrade, and a document round-trips parse→emit→parse."""

import logging

import pytest

from sakrit.spec import SedDocument, SpecError, emit_sed, parse_sed
from sakrit.spec.v1 import ArgSpec

FULL_DOC = {
    "sed": 1,
    "tool": "crm.create_ticket",
    "level": "l2r",
    "args": {
        "customer_id": {"class": "identity", "normalize": "trim"},
        "amount_cents": {"class": "identity", "type": "money"},
        "subject": {"class": "identity", "normalize": "nfc-trim"},
        "body": {"class": "content"},
        "request_ts": {"class": "volatile"},
        "*": {"class": "identity"},
    },
    "provider_key": {"param": "idempotency_key", "ttl_s": 86400, "encoding": "hex64"},
    "reconcile": {
        "ref": "python:myapp.recon:find_ticket",
        "window_s": 30,
        "provider_read": "strong",
    },
    "result": {"replay": "value"},
    "ambiguous": {"default": "halt", "escalate_after_s": 604800},
    "fingerprint": {"version": 1},
}


def test_parse_full_document() -> None:
    doc = parse_sed(FULL_DOC)
    assert doc.tool == "crm.create_ticket"
    assert doc.level == "l2r"
    assert doc.args["customer_id"] == ArgSpec(cls="identity", normalize="trim")
    assert doc.args["amount_cents"].type == "money"
    assert doc.args["body"].cls == "content"
    assert doc.default_arg.cls == "identity"
    assert doc.provider_key is not None and doc.provider_key.param == "idempotency_key"
    assert doc.provider_key.ttl_s == 86400.0
    assert doc.reconcile is not None and doc.reconcile.provider_read == "strong"


def test_round_trips_through_emit() -> None:
    doc = parse_sed(FULL_DOC)
    reparsed = parse_sed(emit_sed(doc))
    assert reparsed == doc


# --- version gating fails closed ------------------------------------------
def test_unsupported_major_is_refused() -> None:
    with pytest.raises(SpecError, match="unsupported SED major"):
        parse_sed({"sed": 2, "tool": "t"})


def test_missing_sed_is_refused() -> None:
    with pytest.raises(SpecError, match="missing required 'sed'"):
        parse_sed({"tool": "t"})


def test_unknown_optional_top_field_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sakrit.spec"):
        doc = parse_sed({"sed": 1, "tool": "t", "future_field": {"x": 1}})
    assert doc.tool == "t"  # forward-compat: unknown optional TOP-LEVEL field ignored, not fatal
    rec = [r for r in caplog.records if "future_field" in r.message]
    assert rec and rec[0].levelno == logging.WARNING  # G-4: WARNING, not invisible INFO


def test_nested_unknown_arg_field_is_refused() -> None:
    # G-4 (Fable's cell): a typo'd nested key (`normalise` for `normalize`) was silently
    # swallowed → the declared dedup tolerance was absent, surfacing later as a confusing
    # DivergentRetry storm on case-variant values. A nested block's shape is fixed within a
    # major, so refuse the typo loudly at declaration.
    with pytest.raises(SpecError, match="unknown field 'normalise'"):
        parse_sed({"sed": 1, "tool": "t", "args": {"email": {"normalise": "email"}}})


@pytest.mark.parametrize(
    "doc",
    [
        {"sed": 1, "tool": "t", "provider_key": {"param": "k", "ttls": 60}},
        {"sed": 1, "tool": "t", "reconcile": {"ref": "python:m:f", "windows_s": 30}},
        {"sed": 1, "tool": "t", "result": {"replays": "value"}},
        {"sed": 1, "tool": "t", "result": {"refetch": {"ref": "python:m:f", "field": []}}},
        {"sed": 1, "tool": "t", "ambiguous": {"defaults": "halt"}},
        {"sed": 1, "tool": "t", "fingerprint": {"ver": 1}},
    ],
)
def test_nested_unknown_field_in_a_block_is_refused(doc: dict[str, object]) -> None:
    with pytest.raises(SpecError, match="unknown field"):
        parse_sed(doc)


def test_x_prefixed_extension_key_is_allowed_in_a_block() -> None:
    # An `x-`-prefixed key is a reserved private-extension namespace — allowed through, not fatal.
    doc = parse_sed({"sed": 1, "tool": "t", "args": {"a": {"class": "identity", "x-note": "hi"}}})
    assert doc.args["a"].cls == "identity"


# --- unknown enum values refused ------------------------------------------
def test_unknown_level_is_refused() -> None:
    with pytest.raises(SpecError, match="level"):
        parse_sed({"sed": 1, "tool": "t", "level": "l9"})


def test_unknown_arg_class_is_refused() -> None:
    with pytest.raises(SpecError, match="class"):
        parse_sed({"sed": 1, "tool": "t", "args": {"a": {"class": "bogus"}}})


def test_unknown_normalizer_is_refused() -> None:
    with pytest.raises(SpecError, match="unknown normalizer"):
        parse_sed({"sed": 1, "tool": "t", "args": {"a": {"normalize": "no-such"}}})


# --- the anti-reflex guardrail (§3) ---------------------------------------
def test_identity_typed_field_forced_to_content_is_refused() -> None:
    with pytest.raises(SpecError, match="force: true"):
        parse_sed(
            {"sed": 1, "tool": "t", "args": {"amount": {"class": "content", "type": "money"}}}
        )


def test_name_based_identity_typing_is_refused_without_force() -> None:
    with pytest.raises(SpecError, match="identity-typed"):
        parse_sed({"sed": 1, "tool": "t", "args": {"customer_id": {"class": "content"}}})


def test_forced_content_downgrade_is_allowed_and_lints_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sakrit.spec"):
        doc = parse_sed(
            {
                "sed": 1,
                "tool": "t",
                "args": {"amount": {"class": "content", "type": "money", "force": True}},
            }
        )
    assert doc.args["amount"].cls == "content"
    assert any("anti-reflex lint" in r.message for r in caplog.records)


def test_content_field_that_is_not_identity_typed_is_fine() -> None:
    doc = parse_sed({"sed": 1, "tool": "t", "args": {"body": {"class": "content"}}})
    assert doc.args["body"].cls == "content"  # no type, no id-name → no friction


# --- level ↔ capability consistency ---------------------------------------
def test_level_l2_without_provider_key_is_refused() -> None:
    with pytest.raises(SpecError, match="provider_key"):
        parse_sed({"sed": 1, "tool": "t", "level": "l2"})


def test_level_l2r_without_reconcile_is_refused() -> None:
    with pytest.raises(SpecError, match="reconcile"):
        parse_sed({"sed": 1, "tool": "t", "level": "l2r", "provider_key": {"param": "k"}})


# --- result.replay=refetch needs a refetch block --------------------------
def test_replay_refetch_requires_refetch_block() -> None:
    with pytest.raises(SpecError, match="refetch"):
        parse_sed({"sed": 1, "tool": "t", "result": {"replay": "refetch"}})


def test_defaults_are_the_safe_direction() -> None:
    doc = parse_sed({"sed": 1, "tool": "t"})
    assert doc.default_arg.cls == "identity"  # undeclared args default to identity
    assert doc.result.replay == "value"
    assert doc.ambiguous.default == "halt"
    assert isinstance(doc, SedDocument)
