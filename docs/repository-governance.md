# Repository governance

## Main branch

`main` is the default branch. GitHub branch protection requires a pull request and resolution of review conversations. Separate approvals are optional; collaborators with write access may merge their own PRs. Stale approvals are dismissed when reviews are used. Protections apply to administrators; force pushes and branch deletion are disabled. Squash merging is the only enabled merge method, and merged topic branches are deleted automatically. Project policy adds one rule on top: PRs for issues labelled `review: second-pass` need an approving review from a thread or person other than the author before merge.

The initial empty commit establishes the base for the first PR. It contains no project files. All project content is introduced through the foundation PR.

GitHub does not allow authors to approve their own PRs, so the required approval count is zero and latest-push approval is disabled. Merging is still limited to collaborators with write access or higher. Administrators retain the technical ability to edit settings, but project policy forbids disabling PR-only protection to push directly to main.

The desired settings are stored in [.github/branch-protection.json](../.github/branch-protection.json). This file documents configuration; GitHub enforces the applied server settings. CI has three workflows with distinct job names: `backend` (Backend tests), `cot-auditing` (CoT auditing tests), and `reviewer-ui` (Reviewer UI tests, which also runs `./scripts/validate.sh`). They are not yet required status checks; issue #22 tracks requiring them. This change does not modify branch protection.

An administrator can restore the recorded settings with:

```bash
gh api --method PUT repos/dkritarth/scopewatch/branches/main/protection --input .github/branch-protection.json
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
| `type: spike` | Timeboxed investigation that ends in a written finding |
| `type: decision` | Decision record needed before dependent work starts |
| `area: agent` | Agent loop, model providers, and reasoning capture |
| `area: submission` | Hackathon hosting, demo video, and submission package |
| `agent: ready` | Agents may claim once every blocking issue is closed |
| `agent: in-progress` | Claimed by an agent or person; see the claim comment |
| `agent: needs-human` | Needs a human: decision, credentials, billing, settings, or recording |
| `review: second-pass` | Security-critical; needs an approving review from a thread or person other than the author before merge |

Use the issue templates to propose ideas, define tasks (the `agent-task` template produces issues agents can pick up), or report bugs. Keep secrets and private workplace traces out of issues.
