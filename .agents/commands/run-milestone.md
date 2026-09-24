---
description: Drive a milestone forward with parallel role threads
argument-hint: [milestone name or number, optional]
---

Act as the `orchestrator` role. Read `AGENTS.md` and `.agents/roles/orchestrator.md` first, then follow the role's steps exactly.

Target: $ARGUMENTS

If no milestone is given, use the earliest open milestone that has unblocked `agent: ready` issues. Spawn the other roles as subagents (for example with the Agent tool and `subagent_type` set to the role name) or as separate sessions in their own worktrees.
