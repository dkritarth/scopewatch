# Contributing

We are still deciding what to build. Use an idea issue for proposals and record accepted decisions in `docs/`. Attribute model-authored proposals and keep them distinct from team decisions.

## Workflow

1. Discuss substantial work in an issue. Pick a narrow scope and an owner.
2. Branch from current `main`, using `feature/`, `fix/`, `docs/`, or `chore/` followed by a short description.
3. Make focused commits with messages such as `docs: explain permission boundaries`.
4. Open a pull request. Explain the problem, changes, validation, and remaining limitations. Link any related issue.
5. Obtain at least one approval from another collaborator with write access. The latest push must be approved by someone other than its pusher. New changes dismiss stale approvals.
6. Resolve review conversations, then squash-merge through GitHub. Delete the merged branch.

Never push directly to `main`, force-push it, delete it, or bypass review. These rules also apply to administrators and automation. Do not weaken protection to merge your own PR.

## Before review

- Read the diff and check that it contains only the intended changes.
- Verify documentation links and configuration syntax. For executable changes, run relevant tests and report the commands and results.
- Keep credentials, private workplace data, and raw production traces out of the repository and public issues.
- Use synthetic data and controlled local targets for demonstrations of unsafe agent behavior.
- Identify model-authored suggestions by model name. Do not present generated suggestions as team agreement.

Required CI checks will be added with the first executable implementation. Until then, PR authors record manual validation. See [governance](docs/repository-governance.md) for settings and labels.
