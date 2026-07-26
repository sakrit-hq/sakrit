# SPDX-License-Identifier: Apache-2.0
"""Framework adapters — the integration surface.

Each module here plugs a specific agent framework into the framework-agnostic
core. This is the layer that decides whether Sakrit lives inside other people's
stacks (Act IV). Framework imports belong *here*, never in ``sakrit.core``.

We ship one adapter to begin with, not three (Act II, step 7). The others are
scaffolded so the shape is visible, but stay stubs until the core is proven.
"""
