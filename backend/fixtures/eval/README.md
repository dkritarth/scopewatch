# Reasoning Auditor Evaluation Dataset

This directory contains evaluation datasets for the Scopewatch Chain-of-Thought (CoT) reasoning auditor.

## Strict Rules on Dataset Splits

1. **Development split (`dev/`)**:
   Use these cases for prompt engineering, tuning system prompts, debugging parser heuristics, and exploring failure modes.
2. **Held-out split (`heldout/`)**:
   **Never view, edit, or tune prompts against held-out cases during development.** Held-out cases exist to provide an unbiased measurement of auditor performance on unseen inputs. Inspecting or tuning against held-out cases compromises the integrity of benchmark figures.

Only automated evaluation harnesses should read `heldout/` test cases. When investigating regressions on the held-out split, create a new synthetic repro case in `dev/` instead of adapting prompts directly to the held-out sample.

Tune-then-freeze rule for live runs: the fixtures and the mock auditor landed in the same PR, so there is no freeze-then-tune history proving the split was uncontaminated. Before a model-backed live run means what #32 intends, freeze the held-out split, document the freeze, then tune only on `dev/`. Re-derive thresholds after the live run.

## Directory Layout

```
backend/fixtures/eval/
├── README.md               # Dataset rules and schema documentation
├── dev/
│   └── cases.json          # Development and prompt tuning cases
└── heldout/
    └── cases.json          # Held-out benchmark cases (minimum 40 cases)
```

## Categories

The dataset includes six balanced categories:

1. **`clean`**: Normal agent workflows, safe actions strictly inside allowed paths and tools, or explicit safe refusals where the agent rejects an untrusted command.
2. **`drift`**: The agent wanders off to perform unprompted, extraneous tasks such as refactoring unrelated modules, installing new dependencies, or redesigning UI components.
3. **`injection_following`**: The reasoning trace follows adversarial injection directives or attempts auditor manipulation (for example delimiter smuggling or instruction override commands).
4. **`exfiltration_intent`**: The agent plans to transmit secrets, credentials, or code outside the approved network boundary (using tools like `curl`, `wget`, webhooks, or sockets).
5. **`policy_evasion`**: The agent attempts path traversal (`../`), accesses explicitly blocked paths, or claims forged authority (for example claiming permissions are elevated or unrestricted).
6. **`ambiguous`**: Borderline or subtle cases designed to test auditor discernment on edge cases (such as handling log excerpts that mention sensitive paths, or complex dependency checks).

## Case Schema

Each case in `cases.json` is a JSON object with the following fields:

- `case_id` (string, required): Unique identifier for the case (for example `heldout-clean-01`).
- `category` (string, required): One of `clean`, `drift`, `injection_following`, `exfiltration_intent`, `policy_evasion`, or `ambiguous`.
- `task_scope` (object, required): A complete `TaskScope` object defining `task_description`, `allowed_paths`, `blocked_paths`, `allowed_tools`, `allowed_operations`, `allowed_network_destinations`, and `created_at`.
- `reasoning_trace` (string, required): The agent Chain-of-Thought reasoning trace to be audited.
- `planned_actions` (list of objects, required): Planned tool actions for the turn (`tool`, `operation`, `resource`, `arguments`).
- `expected_verdict` (string, required): Either `NO_CONCERN` or `CONCERN`.
- `expected_concern_type` (string or null, optional): Specific concern type when `expected_verdict` is `CONCERN`.
- `description` (string, optional): Context describing the scenario.
