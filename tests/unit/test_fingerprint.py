# SPDX-License-Identifier: Apache-2.0
"""The fingerprint: over identity args only; reworded content tolerated."""

from sakrit.core import ArgClass, EffectDecl, fingerprint

SECRET = b"test-secret"

EMAIL = EffectDecl(
    tool="email.send",
    classes={
        "to": ArgClass.IDENTITY,
        "subject": ArgClass.IDENTITY,
        "body": ArgClass.CONTENT,
        "trace_id": ArgClass.VOLATILE,
    },
)


def _fp(**args: object) -> str:
    return fingerprint(EMAIL, args, secret=SECRET)


def test_deterministic() -> None:
    assert _fp(to="a@x.com", subject="Hi", body="hello") == _fp(
        to="a@x.com", subject="Hi", body="hello"
    )


def test_content_reword_tolerated() -> None:
    # body is content: the LLM rewording it must NOT change the fingerprint.
    assert _fp(to="a@x.com", subject="Hi", body="hello") == _fp(
        to="a@x.com", subject="Hi", body="HELLO, rephrased"
    )


def test_volatile_ignored() -> None:
    assert _fp(to="a@x.com", subject="Hi", body="x", trace_id="t1") == _fp(
        to="a@x.com", subject="Hi", body="x", trace_id="t2"
    )


def test_identity_change_diverges() -> None:
    base = _fp(to="a@x.com", subject="Hi", body="x")
    assert _fp(to="b@x.com", subject="Hi", body="x") != base  # recipient
    assert _fp(to="a@x.com", subject="Bye", body="x") != base  # subject


def test_undeclared_arg_defaults_to_identity() -> None:
    decl = EffectDecl(tool="t")  # no classes → default IDENTITY
    a = fingerprint(decl, {"amount": 40}, secret=SECRET)
    b = fingerprint(decl, {"amount": 41}, secret=SECRET)
    assert a != b


def test_secret_matters() -> None:
    a = fingerprint(EMAIL, {"to": "a@x.com"}, secret=b"secret-1")
    b = fingerprint(EMAIL, {"to": "a@x.com"}, secret=b"secret-2")
    assert a != b


def test_absent_identity_arg_differs_from_none() -> None:
    # "to" present-but-None must differ from "to" absent (None is distinct).
    with_none = _fp(to=None, subject="Hi", body="x")
    without = _fp(subject="Hi", body="x")
    assert with_none != without
