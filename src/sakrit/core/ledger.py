# SPDX-License-Identifier: Apache-2.0
"""The ledger — a durable record of what has already happened.

Answers the one question the whole library turns on: "has this action already
happened, and if so, what did it return?" Re-running a completed action must
return its saved result instead of firing again.

The ledger is backed by a pluggable store (see ``sakrit.stores``): a simple
local store to begin with, a production database later.

TODO(Act II): define the Ledger protocol and the replay rule.
"""
