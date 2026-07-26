# SPDX-License-Identifier: Apache-2.0
"""Idempotency key derivation.

Assigns each action a stable, unique identity so that "the same action" can be
recognized across a crash, a resume, or a parallel branch. The open interface
question (Act II, step 6) — is the key derived automatically from the call, or
declared by the developer? — is resolved here.

TODO(Act II): implement key derivation.
"""
