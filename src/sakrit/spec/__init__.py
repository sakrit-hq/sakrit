# SPDX-License-Identifier: Apache-2.0
"""The published, versioned Sakrit format.

The way a tool declares itself safe-to-retry — the stamp and effect tag — is a
small, versioned standard, documented independently of this implementation
(Act IV, step 15). Code gets forked; formats get adopted. Keep this package
free of implementation detail: it defines the format, not how we honor it.
"""
