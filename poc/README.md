# Experiments and prototypes

The place to try ideas quickly. Anything goes here: throwaway scripts, alternative designs, model comparisons, UI sketches. Code in `poc/` does not need to meet the backend's bar, and it may be deleted once its lesson is learned.

Each experiment gets its own folder, `poc/<topic>/`, with a `README.md` that says:

- **Question:** what are we trying to learn?
- **How to run:** exact commands, dependencies, and environment variables (never the values).
- **Result:** what happened, with dates and model IDs. Negative results count.
- **Status:** active, concluded, promoted (moved into `backend/` or `frontend/`, with the PR), or abandoned.

If an experiment has tests, keep them runnable offline. The reasoning-audit PoC's tests run in CI and in `./scripts/validate.sh`; add a new experiment to CI only if other code depends on it.

Rules that still apply: synthetic data only, no secrets, and label reasoning evidence honestly.

| Experiment | Status |
| --- | --- |
| [cot-auditing](cot-auditing/README.md) | Active. Hardened auditor being ported into the backend (#28) |
