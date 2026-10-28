# Project documents

## Start here

- [AGENTS.md](../AGENTS.md): how work is done, domain invariants, validation, conventions.
- [Agent playbook](agents/README.md): how to run role threads (implementer, test-writer, verifier, test-runner, change-monitor, reviewer, researcher, docs-keeper, orchestrator).
- [Project status](STATUS.md): what works, what is unverified, and where it goes next (2026-10-28).
- [Hackathon checklist](hackathon.md): submission requirements and their status.
- [Workspace retention](operations/workspace-retention.md): operator dry-run and explicit reap for old terminal run workspaces.

## Architecture and decisions

- [Architecture overview](ARCHITECTURE.md): what the code does today.
- [Adoption and prior art](adoption-and-prior-art.md): who it is for, real disclosures, overlap with other guardrails.
- [Judge runbook](operations/judge-runbook.md) and [demo guards](operations/demo-guards.md): cold run and hosted-demo controls.
- [Evaluation methodology](evaluation.md) and [reproducibility](evaluation/reproducibility.md): auditor results and how to check them.
- [Monitor ablation PoC](../poc/monitor-ablation/README.md): synthetic replay comparing reasoning-aware and action-only monitoring; no live results.
- [Submission package](submission/description.md): Devpost description, Nebius/NVIDIA usage, testing instructions, provider feedback, [media gallery](submission/media/README.md).
- [Proposed architecture](architecture/proposed-scopewatch-architecture.md): the target design, amended by ADR-0001.
- [Architecture decision records](adr/README.md): accepted decisions, including [ADR-0001: Pre-execution gateway with an open-weight reasoning agent](adr/0001-pre-execution-gateway.md).
- [Planning session record, September 23](ideas/claude-opus-5.5-2026-09-23-planning-session.md): decisions, rejected options, and caveats behind the milestone backlog (issues #20–#52).

## Findings

- [Spikes](spikes/README.md): timeboxed investigations with observed evidence.
- [Tested baseline, September 17](baseline-2026-09-17.md): recovered prototypes, repeatable checks, and known limits at that date.
- [Agent run logs](agents/runs/README.md): what long agent sessions did.

## Ideas and history (model-authored, awaiting team review)

- [GPT-5 gap audit, September 19](ideas/gpt-5-2026-09-19-gap-audit.md): refreshed against main and backlog in issue #24.
- [Union Alpha validation notes](ideas/union-alpha-2026-09-16-validation.md): capture and synthetic auditor results.
- [GPT-6 discussion notes](ideas/gpt-6-notes.md).
- [Gemini 3.8 Flash discussion notes](ideas/gemini-3.8-flash-notes.md): active gateway, taint tracking, two-tier monitor.
- [PR #4 discussion record](ideas/pr-4-discussion-record.md): proposals, reviews, and open decisions from the ideation PR.

## Governance

- [Repository governance](repository-governance.md): branch protection, merge rules, and labels.
