---
name: Agent-ready task
about: Work an agent can pick up and finish without asking questions
labels: 'type: task, agent: ready'
---

## Outcome

What will be true or possible when this is done?

## Context

Why this matters now. Link the milestone, related issues, ADRs, or ideas.

## Design guidance

Files and functions to touch, interfaces to add, and approaches to prefer or avoid. Enough that an agent does not have to guess.

## Acceptance criteria

- [ ] Observable result, stated so a test or the verifier role can check it.

## Validation

Commands to run in addition to `./scripts/validate.sh`.

## Dependencies

- **Blocked by:** #
- **Blocks:** #

Also set the native "blocked by" links on GitHub so `scripts/agents/next_issues.py` sees them. Add `review: second-pass` if the change touches policy, the executor, approvals, decision merging, isolation, or deployment.
