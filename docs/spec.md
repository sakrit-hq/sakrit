# The Sakrit format (human-readable)

*Companion to the machine-readable schema in [`src/sakrit/spec/`](../src/sakrit/spec/).*

This document describes how a tool declares itself **safe-to-retry** — the stamp
and effect tag — in language a human can read. Code gets forked; formats get
adopted. Publishing the format as its own versioned specification, independent of
our implementation, is the line between shipping a library and becoming
infrastructure (Act IV, step 15).

> **Status: not yet drafted.** The format is settled on paper during Act II
> (interface questions) and published as a standalone spec in Act IV. This file
> is the placeholder for that write-up.

## What belongs here (when written)

- **The stamp** — how an action's identity is determined (derived vs. declared).
- **The effect tag** — how a tool announces it has a real-world consequence.
- **Versioning rules** — what may change within a major version, what may not.
- **The unwrapped-tool default** — what happens to a consequential tool that
  wasn't stamped (a safe default, or a loud failure?).

## What does not belong here

Implementation detail. This is the *format*, not how Sakrit honors it.
