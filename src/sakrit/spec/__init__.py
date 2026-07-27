# SPDX-License-Identifier: Apache-2.0
"""The published, versioned Sakrit format.

The way a tool declares itself safe-to-retry — the stamp and effect tag — is a
small, versioned standard, documented independently of this implementation
(Act IV, step 15). Code gets forked; formats get adopted. Keep this package
free of implementation detail: it defines the format, not how we honor it.

``spec.v1`` parses/validates/emits a SED v1 document; ``spec.normalizers`` is the
builtin-normalizer binding (conformance-gated by ``tests/fixtures/`` golden vectors);
``spec.convergence`` binds a document to the in-code ``EffectDecl``.
"""

from sakrit.spec.normalizers import BUILTIN_NORMALIZERS, NORMALIZER_NAMES, normalize
from sakrit.spec.v1 import (
    SED_MAJOR,
    ArgSpec,
    SedDocument,
    SpecError,
    emit_sed,
    parse_sed,
)

__all__ = [
    "BUILTIN_NORMALIZERS",
    "NORMALIZER_NAMES",
    "SED_MAJOR",
    "ArgSpec",
    "SedDocument",
    "SpecError",
    "emit_sed",
    "normalize",
    "parse_sed",
]
