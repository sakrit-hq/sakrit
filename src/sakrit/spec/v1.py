# SPDX-License-Identifier: Apache-2.0
"""SED — the Sakrit Effect Declaration format, version 1 (roadmap Act IV, step 15).

The versioned, language-neutral format by which a tool declares how it settles
exactly-once. Code gets forked; formats get adopted — so this is a *format*, documented
independently of the Python binding (``docs/spec.md``). This module parses, validates, and
emits a v1 document; :mod:`sakrit.spec.convergence` binds it to the in-code
:class:`~sakrit.core.declaration.EffectDecl`.

Design rules honored here (so we do not re-introduce the audit's failure modes):

- **Version gates fail closed on the axis that matters.** ``sed`` *major* is breaking: a
  consumer MUST reject a major it does not implement. *Minor* is additive-optional: unknown
  optional fields are ignored (forward-compat), so a v1 consumer reads a v1.x document.
- **The safe direction is the default.** An undeclared arg is ``identity`` (a loud
  ``DivergentRetry`` on divergence, never a silent duplicate). Unknown enum values are
  refused, not coerced.
- **The anti-reflex guardrail (design §3).** An identity-*typed* field (``money``, an
  ``*_id`` name, an account/email/card/iban type) declared ``content`` is refused unless the
  author writes ``force: true`` — which then emits a loud, permanent lint. Downgrading the
  most consequential fields must never be reflexive.
- **This is the format, not the runtime.** Every v1 field parses and round-trips here. Which
  fields the *Python runtime* actually enforces is decided in ``convergence.py`` — and a
  field the runtime cannot yet honor is **refused loudly there**, never silently dropped
  (the P5-4 "no ornamental surface" discipline).

Core stays dependency-free: this parses a pre-loaded ``Mapping`` (the caller owns YAML, e.g.
``yaml.safe_load``); there is no YAML dependency in the package.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from sakrit.core.errors import SakritError
from sakrit.core.normalizers import NORMALIZER_NAMES

_log = logging.getLogger("sakrit.spec")

SED_MAJOR = 1

LEVELS = frozenset({"l0am", "l0al", "l1", "l2", "l2r", "l3"})
ARG_CLASSES = frozenset({"identity", "content", "volatile"})
REPLAY_MODES = frozenset({"value", "marker", "refetch"})
PROVIDER_READS = frozenset({"eventual", "strong"})
AMBIGUOUS_DEFAULTS = frozenset({"halt", "retry"})

# Semantic types that are identity-bearing by default (the anti-reflex guardrail, §3). A
# field of one of these types classed ``content`` must carry ``force: true``.
IDENTITY_TYPES = frozenset({"money", "account", "email", "phone", "card", "iban"})


class SpecError(SakritError):
    """A SED document is malformed, uses an unknown enum value, declares an unsupported
    ``sed`` major, or trips the anti-reflex guardrail. Raised at parse time — a bad
    declaration fails at registration, not at 2am."""


# --- typed sub-documents ---------------------------------------------------------------
@dataclass(frozen=True)
class ArgSpec:
    cls: str = "identity"
    normalize: str | None = None
    type: str | None = None
    force: bool = False


@dataclass(frozen=True)
class ProviderKeySpec:
    param: str
    ttl_s: float | None = None
    encoding: str | None = None


@dataclass(frozen=True)
class ReconcileSpec:
    ref: str
    window_s: float | None = None
    provider_read: str = "eventual"


@dataclass(frozen=True)
class RefetchSpec:
    ref: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResultSpec:
    replay: str = "value"
    refetch: RefetchSpec | None = None


@dataclass(frozen=True)
class AmbiguousSpec:
    default: str = "halt"
    escalate_after_s: float | None = None


@dataclass(frozen=True)
class FingerprintSpec:
    version: int = 1


@dataclass(frozen=True)
class SedDocument:
    """A parsed, validated SED v1 document — the complete format surface."""

    tool: str
    sed: int = SED_MAJOR
    level: str | None = None
    args: Mapping[str, ArgSpec] = field(default_factory=dict)
    default_arg: ArgSpec = field(default_factory=lambda: ArgSpec(cls="identity"))
    provider_key: ProviderKeySpec | None = None
    reconcile: ReconcileSpec | None = None
    result: ResultSpec = field(default_factory=ResultSpec)
    ambiguous: AmbiguousSpec = field(default_factory=AmbiguousSpec)
    fingerprint: FingerprintSpec = field(default_factory=FingerprintSpec)


# --- typed field readers (fail closed on the wrong shape) ------------------------------
def _as_mapping(v: object, where: str) -> Mapping[str, object]:
    if not isinstance(v, Mapping):
        raise SpecError(f"{where}: expected a mapping, got {type(v).__name__}")
    return v


def _req_str(m: Mapping[str, object], key: str, where: str) -> str:
    if key not in m:
        raise SpecError(f"{where}: missing required field {key!r}")
    v = m[key]
    if not isinstance(v, str) or not v:
        raise SpecError(f"{where}.{key}: expected a non-empty string, got {v!r}")
    return v


def _opt_str(m: Mapping[str, object], key: str, where: str) -> str | None:
    if key not in m or m[key] is None:
        return None
    v = m[key]
    if not isinstance(v, str):
        raise SpecError(f"{where}.{key}: expected a string, got {type(v).__name__}")
    return v


def _opt_num(m: Mapping[str, object], key: str, where: str) -> float | None:
    if key not in m or m[key] is None:
        return None
    v = m[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SpecError(f"{where}.{key}: expected a number, got {v!r}")
    if v < 0:
        raise SpecError(f"{where}.{key}: must be >= 0, got {v}")
    return float(v)


def _opt_bool(m: Mapping[str, object], key: str, where: str) -> bool:
    if key not in m or m[key] is None:
        return False
    v = m[key]
    if not isinstance(v, bool):
        raise SpecError(f"{where}.{key}: expected a boolean, got {v!r}")
    return v


def _enum(value: str, allowed: frozenset[str], where: str) -> str:
    if value not in allowed:
        raise SpecError(f"{where}: {value!r} is not one of {sorted(allowed)}")
    return value


# --- the parser ------------------------------------------------------------------------
def _is_identity_typed(name: str, spec: ArgSpec) -> bool:
    """Whether an arg is identity-bearing *by type or name* (the anti-reflex trigger). A
    declared ``type`` in :data:`IDENTITY_TYPES`, or a name that reads like an identifier
    (``*_id`` / bare ``id``)."""
    if spec.type is not None and spec.type in IDENTITY_TYPES:
        return True
    return name == "id" or name.endswith("_id")


def _parse_arg(name: str, raw: object) -> ArgSpec:
    m = _as_mapping(raw, f"args.{name}")
    cls = _enum(
        _opt_str(m, "class", f"args.{name}") or "identity", ARG_CLASSES, f"args.{name}.class"
    )
    normalize = _opt_str(m, "normalize", f"args.{name}")
    if normalize is not None and normalize not in NORMALIZER_NAMES:
        raise SpecError(
            f"args.{name}.normalize: unknown normalizer {normalize!r}; "
            f"builtins are {sorted(NORMALIZER_NAMES)}"
        )
    type_ = _opt_str(m, "type", f"args.{name}")
    force = _opt_bool(m, "force", f"args.{name}")
    spec = ArgSpec(cls=cls, normalize=normalize, type=type_, force=force)

    # Anti-reflex guardrail (§3): an identity-typed field classed content needs force=true.
    if cls == "content" and _is_identity_typed(name, spec):
        if not force:
            raise SpecError(
                f"args.{name}: an identity-typed field (type={type_!r}, name={name!r}) is "
                "declared class=content. Downgrading a consequential field out of identity "
                "silently reopens duplicates — declare force: true to assert you mean it "
                "(and know a reworded value will then dedup as the same action)."
            )
        _log.warning(
            "SED anti-reflex lint: args.%s is an identity-typed field forced to class=content "
            "(force: true). A divergent value at this arg will NOT trip DivergentRetry.",
            name,
        )
    return spec


def _parse_provider_key(raw: object) -> ProviderKeySpec:
    m = _as_mapping(raw, "provider_key")
    return ProviderKeySpec(
        param=_req_str(m, "param", "provider_key"),
        ttl_s=_opt_num(m, "ttl_s", "provider_key"),
        encoding=_opt_str(m, "encoding", "provider_key"),
    )


def _parse_reconcile(raw: object) -> ReconcileSpec:
    m = _as_mapping(raw, "reconcile")
    return ReconcileSpec(
        ref=_req_str(m, "ref", "reconcile"),
        window_s=_opt_num(m, "window_s", "reconcile"),
        provider_read=_enum(
            _opt_str(m, "provider_read", "reconcile") or "eventual",
            PROVIDER_READS,
            "reconcile.provider_read",
        ),
    )


def _parse_result(raw: object) -> ResultSpec:
    m = _as_mapping(raw, "result")
    replay = _enum(_opt_str(m, "replay", "result") or "value", REPLAY_MODES, "result.replay")
    refetch: RefetchSpec | None = None
    if "refetch" in m and m["refetch"] is not None:
        rm = _as_mapping(m["refetch"], "result.refetch")
        raw_fields = rm.get("fields", [])
        if not isinstance(raw_fields, (list, tuple)) or not all(
            isinstance(f, str) for f in raw_fields
        ):
            raise SpecError("result.refetch.fields: expected a list of strings")
        refetch = RefetchSpec(ref=_req_str(rm, "ref", "result.refetch"), fields=tuple(raw_fields))
    if replay == "refetch" and refetch is None:
        raise SpecError("result.replay=refetch requires a result.refetch block")
    return ResultSpec(replay=replay, refetch=refetch)


def _parse_ambiguous(raw: object) -> AmbiguousSpec:
    m = _as_mapping(raw, "ambiguous")
    return AmbiguousSpec(
        default=_enum(
            _opt_str(m, "default", "ambiguous") or "halt", AMBIGUOUS_DEFAULTS, "ambiguous.default"
        ),
        escalate_after_s=_opt_num(m, "escalate_after_s", "ambiguous"),
    )


def _parse_fingerprint(raw: object) -> FingerprintSpec:
    m = _as_mapping(raw, "fingerprint")
    v = m.get("version", 1)
    if isinstance(v, bool) or not isinstance(v, int):
        raise SpecError(f"fingerprint.version: expected an integer, got {v!r}")
    return FingerprintSpec(version=v)


_TOP_KEYS = frozenset(
    {
        "sed",
        "tool",
        "level",
        "args",
        "provider_key",
        "reconcile",
        "result",
        "ambiguous",
        "fingerprint",
    }
)


def parse_sed(doc: Mapping[str, object]) -> SedDocument:
    """Parse and validate a SED v1 document (a pre-loaded mapping). Raises :class:`SpecError`
    on any malformation, unknown enum value, unsupported ``sed`` major, or anti-reflex
    violation. Unknown *optional* top-level fields are ignored with a log line (minor
    forward-compat), never silently swallowed without trace."""
    if not isinstance(doc, Mapping):
        raise SpecError(f"SED document must be a mapping, got {type(doc).__name__}")

    # Version gate — fail closed on an unimplemented major.
    if "sed" not in doc:
        raise SpecError("SED document missing required 'sed' major version")
    major = doc["sed"]
    if isinstance(major, bool) or not isinstance(major, int):
        raise SpecError(f"'sed' must be an integer major version, got {major!r}")
    if major != SED_MAJOR:
        raise SpecError(
            f"unsupported SED major version {major}; this build implements sed {SED_MAJOR}. "
            "A consumer must reject a major it does not implement."
        )

    for k in doc:
        if k not in _TOP_KEYS:
            _log.info("SED: ignoring unknown optional field %r (minor forward-compat)", k)

    tool = _req_str(doc, "tool", "SED")

    level = _opt_str(doc, "level", "SED")
    if level is not None:
        level = _enum(level.lower(), LEVELS, "level")

    args: dict[str, ArgSpec] = {}
    default_arg = ArgSpec(cls="identity")
    if "args" in doc and doc["args"] is not None:
        am = _as_mapping(doc["args"], "args")
        for name, raw in am.items():
            if name == "*":
                default_arg = _parse_arg("*", raw)
            else:
                args[name] = _parse_arg(name, raw)

    provider_key = _parse_provider_key(doc["provider_key"]) if doc.get("provider_key") else None
    reconcile = _parse_reconcile(doc["reconcile"]) if doc.get("reconcile") else None
    result = _parse_result(doc["result"]) if doc.get("result") else ResultSpec()
    ambiguous = _parse_ambiguous(doc["ambiguous"]) if doc.get("ambiguous") else AmbiguousSpec()
    fingerprint = (
        _parse_fingerprint(doc["fingerprint"]) if doc.get("fingerprint") else FingerprintSpec()
    )

    # Level ↔ capability consistency: a declared rung must match declared capabilities.
    if level in ("l2", "l2r") and provider_key is None:
        raise SpecError(f"level={level!r} declares provider-key dedup but no provider_key block")
    if level in ("l1", "l2r") and reconcile is None:
        raise SpecError(f"level={level!r} declares reconcile recovery but no reconcile block")

    return SedDocument(
        tool=tool,
        sed=major,
        level=level,
        args=args,
        default_arg=default_arg,
        provider_key=provider_key,
        reconcile=reconcile,
        result=result,
        ambiguous=ambiguous,
        fingerprint=fingerprint,
    )


# --- emit (round-trip) -----------------------------------------------------------------
def _arg_to_dict(spec: ArgSpec) -> dict[str, object]:
    out: dict[str, object] = {"class": spec.cls}
    if spec.normalize is not None:
        out["normalize"] = spec.normalize
    if spec.type is not None:
        out["type"] = spec.type
    if spec.force:
        out["force"] = True
    return out


def emit_sed(doc: SedDocument) -> dict[str, object]:
    """Emit ``doc`` as a plain dict (round-trips through :func:`parse_sed`). The caller owns
    YAML/JSON serialization."""
    out: dict[str, object] = {"sed": doc.sed, "tool": doc.tool}
    if doc.level is not None:
        out["level"] = doc.level
    args: dict[str, object] = {name: _arg_to_dict(spec) for name, spec in doc.args.items()}
    if doc.default_arg != ArgSpec(cls="identity"):
        args["*"] = _arg_to_dict(doc.default_arg)
    if args:
        out["args"] = args
    if doc.provider_key is not None:
        pk: dict[str, object] = {"param": doc.provider_key.param}
        if doc.provider_key.ttl_s is not None:
            pk["ttl_s"] = doc.provider_key.ttl_s
        if doc.provider_key.encoding is not None:
            pk["encoding"] = doc.provider_key.encoding
        out["provider_key"] = pk
    if doc.reconcile is not None:
        rc: dict[str, object] = {
            "ref": doc.reconcile.ref,
            "provider_read": doc.reconcile.provider_read,
        }
        if doc.reconcile.window_s is not None:
            rc["window_s"] = doc.reconcile.window_s
        out["reconcile"] = rc
    if doc.result != ResultSpec():
        res: dict[str, object] = {"replay": doc.result.replay}
        if doc.result.refetch is not None:
            res["refetch"] = {
                "ref": doc.result.refetch.ref,
                "fields": list(doc.result.refetch.fields),
            }
        out["result"] = res
    if doc.ambiguous != AmbiguousSpec():
        amb: dict[str, object] = {"default": doc.ambiguous.default}
        if doc.ambiguous.escalate_after_s is not None:
            amb["escalate_after_s"] = doc.ambiguous.escalate_after_s
        out["ambiguous"] = amb
    if doc.fingerprint != FingerprintSpec():
        out["fingerprint"] = {"version": doc.fingerprint.version}
    return out
