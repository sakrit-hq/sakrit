# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for Sakrit.

A single base type so callers can ``except SakritError`` and catch anything the
library raises, while still being able to narrow to specific failures.
"""


class SakritError(Exception):
    """Base class for all errors raised by Sakrit."""


class NoCoordinateError(SakritError):
    """No coordinate could be established for a consequential effect.

    The last rung of the coordinate ladder (``docs/design.md`` §4): where identity
    cannot be established, the answer is a loud refusal, not a wrong identity. The
    message names the ways to supply one (an adapter, ``step=``, or ``key=``).
    """


class DivergentRetry(SakritError):
    """A replay's identity args differ from the recorded action.

    Never merge, never re-execute — raise. The safe failure direction (a loud halt
    instead of a silent duplicate or swallow). See ``docs/design.md`` §2.
    """


class RegeneratedDuplicate(SakritError):
    """A settled irreversible effect from an earlier plan epoch matches this one.

    Raised by the cross-epoch tripwire (R3, ``docs/design.md`` §7): prevent-and-ask,
    never silently replay. Deferred past the Act II narrow core.
    """


class AmbiguousOutcome(SakritError):
    """An effect's outcome cannot be determined after a crash in the window.

    The honest floor for an L0 tool: at-most-once with a loud flag. Resolved out of
    band (reconcile, late evidence, or human ``sk.resolve``).
    """


class CanonicalizationError(SakritError):
    """An argument value has no defined canonical form.

    Fail-closed at fingerprint time rather than silently stringify — an unstable
    canonical form would make the fingerprint (and thus divergence detection)
    unreliable. See ``docs/design.md`` §11 and open-questions Q7.
    """
