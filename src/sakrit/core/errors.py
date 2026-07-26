# SPDX-License-Identifier: Apache-2.0
"""Exception hierarchy for Sakrit.

A single base type so callers can ``except SakritError`` and catch anything the
library raises, while still being able to narrow to specific failures.
"""


class SakritError(Exception):
    """Base class for all errors raised by Sakrit."""
