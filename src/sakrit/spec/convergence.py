# SPDX-License-Identifier: Apache-2.0
"""SED ⇄ EffectDecl convergence — the Python binding of the format (P5-2).

``spec.md`` frames the Python decorator as *one binding that emits and consumes SED*; this
module is that binding. :func:`sed_to_decl` turns a parsed :class:`~sakrit.spec.v1.SedDocument`
into the in-code :class:`~sakrit.core.declaration.EffectDecl` the engine drives;
:func:`decl_to_sed` goes back; :func:`load_sed` is the one-shot ``mapping -> EffectDecl``.

**The discipline that keeps this honest (P5-4 / P5-2):** the *format* (``spec/v1.py``) is
complete, but the Python *runtime* implements a subset. A field the runtime cannot honor is
never silently dropped — it is one of two things:

- **Refused** (:class:`~sakrit.spec.v1.SpecError`) when ignoring it would be *unsafe* —
  the caller would get behavior weaker than they declared and a possible duplicate:
  ``result.replay`` other than ``value``, ``result.refetch``, ``level: l3`` / ``l0al``,
  a ``provider_key.encoding`` the injected key doesn't already satisfy, a ``"*"`` wildcard
  normalizer, or a ``fingerprint.version`` this build doesn't implement.
- **Accepted with a loud log** when ignoring it is *safe* — the caller gets the conservative
  default, never a duplicate: ``ambiguous.default: retry`` (→ halt, the safe default),
  ``ambiguous.escalate_after_s``, ``reconcile.window_s`` (advisory timing not yet enforced).

Each such field graduates from logged-to-honored when the core grows its consumer — never by
freezing a guess (the audit's P5-4 lesson).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable

from sakrit.core.declaration import ArgClass, EffectDecl
from sakrit.core.fingerprint import FINGERPRINT_VERSION
from sakrit.core.reconcile import Reconciliation
from sakrit.spec.v1 import SedDocument, SpecError, parse_sed

_log = logging.getLogger("sakrit.spec")

# The provider-key encoding the engine's injected key already satisfies: current_key() is a
# SHA-256 hex digest → 64 lowercase hex chars. A decl asking for a different encoding is asking
# for a transform the engine does not apply → refuse rather than dedup wrongly at the provider.
_SUPPORTED_ENCODINGS = frozenset({None, "hex64"})

# SED runtime-honored fingerprint scheme versions (v1 == the HMAC-SHA256/canonical scheme this
# build ships; docs/spec.md `fingerprint.version` == 1).
_SUPPORTED_FP_VERSION = 1

_ARG_CLASS = {
    "identity": ArgClass.IDENTITY,
    "content": ArgClass.CONTENT,
    "volatile": ArgClass.VOLATILE,
}

# EffectDecl.level (derived from capabilities) ⇄ the SED `level` string. l0al/l3 are refused
# by _refuse_unhonorable before this maps, so the derived "L0" maps to at-most-once (l0am), the
# L0 default this runtime implements.
_DERIVED_TO_SED = {"L0": "l0am", "L1": "l1", "L2": "l2", "L2R": "l2r"}


def _resolve_python_ref(ref: str) -> Callable[[str], Reconciliation]:
    """Resolve a SED ``python:<module>:<attr>`` code ref to a callable. Refuses a non-``python:``
    ref (this runtime cannot execute a ``node:`` ref) and a ref that does not import."""
    if not ref.startswith("python:"):
        raise SpecError(
            f"reconcile.ref {ref!r}: this Python runtime honors only 'python:<module>:<attr>' "
            "refs. A ref for another language binding cannot be executed here."
        )
    body = ref[len("python:") :]
    if ":" not in body:
        raise SpecError(f"reconcile.ref {ref!r}: expected 'python:<module>:<attr>'")
    module_path, _, attr = body.rpartition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise SpecError(f"reconcile.ref {ref!r}: cannot import {module_path!r}: {exc}") from exc
    try:
        fn = getattr(module, attr)
    except AttributeError as exc:
        raise SpecError(f"reconcile.ref {ref!r}: {module_path!r} has no {attr!r}") from exc
    if not callable(fn):
        raise SpecError(f"reconcile.ref {ref!r}: {attr!r} is not callable")
    return fn  # type: ignore[no-any-return]


def _refuse_unhonorable(doc: SedDocument) -> None:
    """Raise on any field whose non-honoring would be *unsafe* (weaker-than-declared)."""
    if doc.fingerprint.version != _SUPPORTED_FP_VERSION:
        raise SpecError(
            f"fingerprint.version={doc.fingerprint.version} is not implemented by this build "
            f"(only v{_SUPPORTED_FP_VERSION}, {FINGERPRINT_VERSION}). A different fingerprint "
            "scheme would silently mis-compare stored evidence."
        )
    if doc.level in ("l3", "l0al"):
        raise SpecError(
            f"level={doc.level!r} is not implemented by this runtime (l3 = same-DB transaction; "
            "l0al = at-least-once). Ignoring it would silently downgrade the guarantee."
        )
    if doc.result.replay != "value" or doc.result.refetch is not None:
        raise SpecError(
            f"result.replay={doc.result.replay!r} is not implemented (only 'value'; a non-JSON "
            "result already falls back to a Replayed marker automatically — P4-4). 'refetch' "
            "would return stale data as if fresh."
        )
    if doc.provider_key is not None and doc.provider_key.encoding not in _SUPPORTED_ENCODINGS:
        raise SpecError(
            f"provider_key.encoding={doc.provider_key.encoding!r} is not applied by the engine "
            "(it injects a 64-char hex key). A mismatched encoding could break provider dedup → "
            "a duplicate; refuse rather than dedup wrongly."
        )
    if doc.default_arg.normalize is not None:
        raise SpecError(
            "args.'*'.normalize (a wildcard normalizer) is not implemented; declare the "
            "normalizer on each identity arg by name instead."
        )


def _warn_unenforced(doc: SedDocument) -> None:
    """Log (loudly) fields recorded but not yet enforced — safe to ignore (conservative default,
    never a duplicate), so accepted rather than refused."""
    if doc.ambiguous.default != "halt":
        _log.warning(
            "SED %s: ambiguous.default=%r is not yet enforced; an AMBIGUOUS outcome will HALT "
            "for human resolution (the safe default), not auto-retry.",
            doc.tool,
            doc.ambiguous.default,
        )
    if doc.ambiguous.escalate_after_s is not None:
        _log.warning(
            "SED %s: ambiguous.escalate_after_s=%s is not yet enforced (no time-boxed auto-"
            "escalation); an AMBIGUOUS key waits for a human.",
            doc.tool,
            doc.ambiguous.escalate_after_s,
        )
    if doc.reconcile is not None and doc.reconcile.window_s is not None:
        _log.warning(
            "SED %s: reconcile.window_s=%s is not yet enforced; recovery reconciles immediately "
            "(guarded by provider_read).",
            doc.tool,
            doc.reconcile.window_s,
        )


def sed_to_decl(
    doc: SedDocument,
    *,
    resolve_ref: Callable[[str], Callable[[str], Reconciliation]] = _resolve_python_ref,
) -> EffectDecl:
    """Bind a validated SED document to an :class:`EffectDecl`, honoring the subset this runtime
    implements and refusing (or loudly logging) the rest. ``resolve_ref`` resolves a
    ``reconcile.ref`` to a callable (injectable for tests)."""
    _refuse_unhonorable(doc)
    _warn_unenforced(doc)

    classes = {name: _ARG_CLASS[spec.cls] for name, spec in doc.args.items()}
    normalizers = {name: spec.normalize for name, spec in doc.args.items() if spec.normalize}

    provider_key_param = doc.provider_key.param if doc.provider_key else None
    provider_ttl_s = doc.provider_key.ttl_s if doc.provider_key else None

    reconcile: Callable[[str], Reconciliation] | None = None
    provider_read = "eventual"
    if doc.reconcile is not None:
        reconcile = resolve_ref(doc.reconcile.ref)
        provider_read = doc.reconcile.provider_read

    decl = EffectDecl(
        tool=doc.tool,
        classes=classes,
        default=_ARG_CLASS[doc.default_arg.cls],
        normalizers=normalizers,
        provider_key_param=provider_key_param,
        provider_ttl_s=provider_ttl_s,
        reconcile=reconcile,
        provider_read=provider_read,
    )

    # A declared `level` must MATCH the level the capabilities derive — never silently bind to a
    # different rung. parse_sed checks the block-presence direction (l1/l2/l2r require their
    # block); this catches the reverse: e.g. `level: l0am` alongside a provider_key block would
    # derive L2 (a silent guarantee *upgrade*). Refuse the contradiction.
    if doc.level is not None and _DERIVED_TO_SED.get(decl.level) != doc.level:
        raise SpecError(
            f"declared level={doc.level!r} contradicts the capabilities, which derive "
            f"{decl.level!r} ({_DERIVED_TO_SED.get(decl.level)!r}). Declare the level that "
            "matches the provider_key/reconcile blocks present, or adjust the blocks."
        )
    return decl


def load_sed(
    doc: object,
    *,
    resolve_ref: Callable[[str], Callable[[str], Reconciliation]] = _resolve_python_ref,
) -> EffectDecl:
    """Parse + validate + bind a SED mapping to an :class:`EffectDecl` in one step. The caller
    owns YAML (``yaml.safe_load(text)`` then ``load_sed(...)``).

    **Trust:** a SED document is code-equivalent. A ``reconcile.ref`` of ``python:<module>:<attr>``
    is resolved by *importing the named module* — arbitrary code execution — so load only SED
    declarations you trust, exactly as you would a plugin or a ``setup.py``. To sandbox untrusted
    documents, pass a ``resolve_ref`` that resolves against an allow-list instead of importing."""
    from collections.abc import Mapping

    if not isinstance(doc, Mapping):
        raise SpecError(f"SED document must be a mapping, got {type(doc).__name__}")
    return sed_to_decl(parse_sed(doc), resolve_ref=resolve_ref)


def decl_to_sed(decl: EffectDecl) -> SedDocument:
    """Emit a SED document from an :class:`EffectDecl` (the reverse binding). A ``reconcile``
    callable is referenced as ``python:<module>:<qualname>``; a non-importable callable (e.g. a
    lambda) emits a best-effort ref that will not round-trip — declare a named function for a
    round-trippable SED.

    Not every EffectDecl field has a SED v1 home: ``on_absent`` and ``clean_failures`` have no
    SED field, so a decl built with ``on_absent="retry"`` or non-empty ``clean_failures`` does
    NOT round-trip through SED (those settings are dropped). Declare them in code, or carry them
    outside SED, until the format grows the fields."""
    from sakrit.spec.v1 import ArgSpec, ProviderKeySpec, ReconcileSpec

    _CLASS_NAME = {
        ArgClass.IDENTITY: "identity",
        ArgClass.CONTENT: "content",
        ArgClass.VOLATILE: "volatile",
    }
    args = {
        name: ArgSpec(cls=_CLASS_NAME[cls], normalize=decl.normalizers.get(name))
        for name, cls in decl.classes.items()
    }
    provider_key = (
        ProviderKeySpec(param=decl.provider_key_param, ttl_s=decl.provider_ttl_s)
        if decl.provider_key_param
        else None
    )
    reconcile = None
    if decl.reconcile is not None:
        fn = decl.reconcile
        ref = f"python:{getattr(fn, '__module__', '?')}:{getattr(fn, '__qualname__', '?')}"
        reconcile = ReconcileSpec(ref=ref, provider_read=decl.provider_read)
    return SedDocument(
        tool=decl.tool,
        level=_DERIVED_TO_SED[decl.level],
        args=args,
        default_arg=ArgSpec(cls=_CLASS_NAME[decl.default]),
        provider_key=provider_key,
        reconcile=reconcile,
    )
