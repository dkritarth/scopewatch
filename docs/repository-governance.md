# Repository governance

## Main branch

`main` is the default branch. GitHub branch protection requires a pull request and resolution of review conversations. Separate approvals are optional; collaborators with write access may merge their own PRs. Stale approvals are dismissed when reviews are used. Protections apply to administrators; force pushes and branch deletion are disabled. Squash merging is the only enabled merge method, and merged topic branches are deleted automatically.

The initial empty commit establishes the base for the first PR. It contains no project files. All project content is introduced through the foundation PR.

GitHub does not allow authors to approve their own PRs, so the required approval count is zero and latest-push approval is disabled. Merging is still limited to collaborators with write access or higher. Administrators retain the technical ability to edit settings, but project policy forbids disabling PR-only protection to push directly to main.

The desired settings are stored in [.github/branch-protection.json](../.github/branch-protection.json). This file documents configuration; GitHub enforces the applied server settings. There are no required CI checks yet because the repository has no executable application. Add working checks with the implementation, then require their exact job names.

An administrator can restore the recorded settings with:

```bash
gh api --method PUT repos/dkritarth/Nebius-x-NVIDIA-Global-AI-Hackathon-Dummy-Name-/branches/main/protection --input .github/branch-protection.json
```

See GitHub's [protected branch documentation](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

## Issue labels

The definitions in [.github/labels.json](../.github/labels.json) are also applied to GitHub. Use one type label, relevant area labels, and a priority when agreed. GitHub's default labels remain available.

| Label | Use |
| --- | --- |
| `type: idea` | Product or architecture proposal awaiting discussion |
| `type: task` | Agreed work with completion criteria |
| `type: bug` | Reproducible incorrect behavior |
| `type: docs` | Documentation work |
| `area: monitoring` | Agent telemetry, reasoning, and detection |
| `area: permissions` | Access policy and authorization |
| `area: ui` | Reviewer experience |
| `area: infrastructure` | Hosting and runtime integration |
| `area: evaluation` | Scenarios and monitor quality |
| `priority: high` | Work the team considers urgent |
| `priority: normal` | Normal priority |
| `status: needs-discussion` | Decision needed before implementation |
| `status: blocked` | Cannot proceed; describe the dependency |

Use the issue templates to propose ideas, define tasks, or report bugs. Keep secrets and private workplace traces out of issues.
