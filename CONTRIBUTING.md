# Contributing

Humans and agents follow the same workflow. The full guide, including the domain invariants and validation commands, is [AGENTS.md](AGENTS.md). The short version:

1. Pick an issue (`python3 scripts/agents/next_issues.py`) or open one with a template. Ideas that are not ready for the backlog can go straight into a prototype under `poc/<name>/`.
2. Claim it with a comment and the `agent: in-progress` label.
3. Branch from current `main` as `feature/`, `fix/`, `docs/`, `test/`, or `chore/` plus `<issue>-<slug>`. `scripts/agents/worktree.sh` does this in a separate worktree.
4. Commit in small steps with conventional messages such as `feat: add provider profiles`.
5. Run `./scripts/validate.sh` and open a PR with the template: problem, change, validation, and what you could not verify.
6. Squash-merge once CI passes and conversations are resolved. Issues labelled `review: second-pass` need an approving review from someone (or some agent thread) other than the author first.

We are a student team with a deadline. Be ambitious, try things, and fix forward. The few hard lines are in AGENTS.md: no secrets or private data, synthetic data only for model providers, never push to `main` directly, report results honestly, and keep reasoning evidence labelled.

Model-authored proposals go in `docs/ideas/<model>-<date>-<topic>.md` and name the model. Accepted decisions go in `docs/adr/`.

Branch protection details and labels: [docs/repository-governance.md](docs/repository-governance.md).
