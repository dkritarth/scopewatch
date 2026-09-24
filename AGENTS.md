# Repository instructions for coding agents

- Read README.md and CONTRIBUTING.md before making changes.
- Work on a topic branch. Never push directly to main, force-push main, or bypass its pull request requirements.
- Collaborators with write access may merge their own PRs. A separate approving review is optional. Keep PR-only protection enabled.
- Distinguish observed actions, exposed reasoning traces, summaries, and inferred intent. Never invent missing traces or claim guaranteed detection.
- Keep secrets and private traces out of commits. Use synthetic fixtures and authorized test targets.
- Validate changes by running `./scripts/validate.sh` before submitting pull requests to ensure all backend, frontend, browser, and security tests pass. Report what was checked and what remains unverified.

## Agent backlog workflow

The backlog is organized into GitHub milestones (M0 to M3, then Future and stretch). Each issue lists its blocking issues, acceptance criteria, and validation steps. Blocking links are also set natively on GitHub.

1. Pick the lowest-numbered open issue labelled `agent: ready` in the earliest open milestone whose blocking issues are all closed. Skip issues labelled `agent: needs-human`; those need decisions, credentials, billing, repository settings, or recordings.
2. Comment `Claimed by <agent/model>` on the issue before starting. A claim with no linked pull request after 24 hours may be taken over.
3. Branch from current `main` as `feature/<issue#>-<slug>` (or `fix/`, `docs/`, `chore/`). Open one pull request per issue with `Closes #<issue#>`.
4. Run `./scripts/validate.sh` and the issue's own checks. Paste the commands and results into the pull request, and state what remains unverified.
5. If the issue is labelled `review: human-required`, stop once checks pass and request review from a human. Otherwise squash-merge after required checks pass and conversations are resolved.
6. If the issue is blocked or its scope is wrong, comment on it and add `status: blocked`. File a new issue rather than widening scope.

Decisions behind the backlog are in [docs/ideas/claude-opus-5.5-2026-09-23-planning-session.md](docs/ideas/claude-opus-5.5-2026-09-23-planning-session.md) and, once merged, ADR-0001.
