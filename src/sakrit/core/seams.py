# SPDX-License-Identifier: Apache-2.0
"""Named crash seams for the chaos suite.

A **no-op in production** — the check is a constant branch gated on ``SAKRIT_TESTING``.
Under test, a subprocess is launched with ``SAKRIT_TESTING=1`` and
``SAKRIT_CRASH_AT=<seam>``; when execution reaches that seam the process dies with
``os._exit`` — modelling a hard kill at that exact boundary. The injection point
must live in the code because chaos runs in a **subprocess** (you're killing the
process) and monkeypatches don't cross ``exec``. This is how serious systems do
fault injection (etcd/TiKV failpoints, Postgres). The same hook points become
tracing attachment points later.
"""

from __future__ import annotations

import os

_ENABLED = os.environ.get("SAKRIT_TESTING") == "1"
_CRASH_AT = os.environ.get("SAKRIT_CRASH_AT")


def seam(name: str) -> None:
    """Die here (``os._exit``) iff testing is enabled and this seam is the target."""
    if _ENABLED and name == _CRASH_AT:
        os._exit(137)  # hard kill, no cleanup — as a SIGKILL would leave things
