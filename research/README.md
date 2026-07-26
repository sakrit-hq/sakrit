# research/ — not shipped

This directory is the project's R&D trail. **Nothing here is packaged or
published** — the built wheel ships only `src/sakrit/`. Keep it that way; this is
where rough reproductions, notes, and the Act I evidence dossier live before
(and instead of) being productized.

- `repro/` — the raw failure reproduction (`agent.py`, `run.py`, `resume.py`,
  `check_outbox.py`). This is where we prove the problem to ourselves before a
  line of the library exists (Act I, step 1). The cleaned-up version graduates
  to [`examples/`](../examples/).
- `evidence/` — the Act I dossier: war stories, the hand-rolled partial fixes
  found in the wild, and interview notes (Act I, steps 2–3).
