<!-- Thanks for contributing to Sakrit. Keep PRs small and focused. -->

## What & why

<!-- What does this change, and why? Link the issue it closes (e.g. "Closes #123"). -->

## How it was verified

<!-- The bar is correctness. Tick what applies and say what you ran. -->

- [ ] `uv run pytest tests/unit` green (bare `uv run pytest` also runs integration + chaos + the
      network **corpus** suite)
- [ ] `uv run pytest tests/chaos -m chaos` green (if you touched the ledger/settle/recovery paths)
- [ ] `uv run mypy` (strict) and `uv run ruff check .` clean
- [ ] New behavior has a test; a bug fix has a test that fails without the fix

## Effect-safety checklist (if you touched the guarantee)

<!-- Delete this section if your change doesn't touch the ledger, engine, or adapters. -->

- [ ] No new transition without its evidence (no state change that can lose the reason it happened)
- [ ] Failure modes are **loud** — nothing is silently swallowed
- [ ] If you added a `DivergentRetry` raise site, it commits its `divergence` evidence before raising

## Notes for the reviewer

<!-- Anything non-obvious: a tradeoff, a follow-up, a thing you're unsure about. -->

---

By submitting this PR you agree to the project's CLA (see CONTRIBUTING.md).
