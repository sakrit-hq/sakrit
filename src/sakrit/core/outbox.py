# SPDX-License-Identifier: Apache-2.0
"""The outbox — the waiting room for irreversible effects.

For the riskiest effects we do not fire immediately. We hold them, then release
on commit or discard on abort. When an agent explores two plans in parallel and
one loses, the losing branch releases nothing — it never touched the world at
all (Act III, step 12).

This module is also where the dual-write problem is confronted (Act III, step
9): write the *intention* down before acting, then reconcile on recovery.

TODO(Act III): implement buffer / commit / abort and recovery reconciliation.
"""
