# Repository instructions for coding agents

- Read README.md and CONTRIBUTING.md before making changes.
- Work on a topic branch. Never push directly to main, force-push main, or bypass its pull request requirements.
- Collaborators with write access may merge their own PRs. A separate approving review is optional. Keep PR-only protection enabled.
- Distinguish observed actions, exposed reasoning traces, summaries, and inferred intent. Never invent missing traces or claim guaranteed detection.
- Keep secrets and private traces out of commits. Use synthetic fixtures and authorized test targets.
- Validate changes by running `./scripts/validate.sh` before submitting pull requests to ensure all backend, frontend, browser, and security tests pass. Report what was checked and what remains unverified.
