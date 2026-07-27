# SPDX-License-Identifier: Apache-2.0
"""The no-telemetry promise, enforced (Fable F-10).

`docs-site/no-telemetry.md` and the README promise that `import sakrit` opens no network connection
and pulls in no framework. That is a *checkable* claim, so we check it — the project's own
"never silent" standard, applied to its own guarantees. A future `import httpx` in core, or a
socket opened at import time, turns this red.

Run in a fresh isolated interpreter (subprocess) so it measures a clean import, not this session's
already-loaded modules.
"""

from __future__ import annotations

import subprocess
import sys

# Network-client and agent-framework top-level packages the library must NOT import on its own.
_DISALLOWED = sorted(
    {
        "httpx",
        "requests",
        "urllib3",
        "aiohttp",
        "websockets",
        "http",  # stdlib http.client is a network client; sakrit core needs none of it
        "langgraph",
        "agents",
        "crewai",
        "openai",
        "boto3",
        "botocore",
    }
)

_PROBE = f"""
import sys, socket


class _NoSocket(socket.socket):
    def __init__(self, *a, **k):
        raise AssertionError("sakrit opened a socket at import time")


socket.socket = _NoSocket  # importing sakrit must not construct a socket
import sakrit  # noqa: F401

disallowed = {_DISALLOWED!r}
present = sorted(m for m in sys.modules if m.split(".")[0] in disallowed)
print("DISALLOWED_PRESENT:" + ",".join(present))
"""


def test_import_sakrit_opens_no_socket_and_imports_no_network_or_framework() -> None:
    result = subprocess.run([sys.executable, "-I", "-c", _PROBE], capture_output=True, text=True)
    # returncode != 0 means the socket guard fired (a socket was opened at import) or import failed.
    assert result.returncode == 0, f"import sakrit failed or opened a socket:\n{result.stderr}"
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("DISALLOWED_PRESENT:")
    )
    present = line.split(":", 1)[1]
    assert present == "", f"`import sakrit` pulled in disallowed modules: {present}"
