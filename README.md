# Scopewatch

Scopewatch is a pre-execution security gateway and reviewer interface for auditing and mediating autonomous AI agent actions against declared task scopes before execution occurs.

It enforces a strict defense-in-depth model:
1. **Deterministic policy first**: Evaluates path, tool, and operation allowlists before any model call or executor dispatch. Deterministic `DENY` decisions are final.
2. **Escalate-only reasoning audit**: An independent auditor analyzes the agent's chain-of-thought trace for scope drift, prompt injection, and policy evasion. The auditor can only escalate an `ALLOW` to a `HOLD`—it can never grant access or downgrade a decision.
3. **Controlled execution**: Actions execute strictly inside a bounded workspace sandbox only after policy and audit approval.
4. **Structured evidence dashboard**: Reviewers inspect complete 5-part audit trails, including verbatim grounded trace excerpts, provenance labels, and single-use approval controls.

> **Interception coverage statement:**
> Scopewatch is an application-level security gateway. It mediates all tool actions routed through its API. It does not intercept arbitrary out-of-band host processes or direct OS system calls (container-level isolation is introduced in Milestone M2).
>
> Reasoning traces are isolated inside `<untrusted_reasoning_trace>` boundary delimiters. Reasoning is evidence, not proof of intent.

---

## Quick start

### 1. Run the local demo

Run the all-in-one demo launcher:

```bash
# Run in scripted mode (replays synthetic scenarios)
./scripts/run_demo.sh

# Run in agent mode with the default offline mock provider
./scripts/run_demo.sh --agent

# Run in agent mode with live provider profiles (requires API key in environment)
export OPENROUTER_API_KEY="sk-or-..."
./scripts/run_demo.sh --agent --profile openrouter-dev
```

This starts the FastAPI backend, seeds synthetic invoice scenarios into a local workspace, mounts the reviewer UI, and opens:

- **Reviewer dashboard (Live):** [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **API health status:** [http://127.0.0.1:8000/api/v1/health](http://127.0.0.1:8000/api/v1/health)
- **Active runs list:** [http://127.0.0.1:8000/api/v1/runs](http://127.0.0.1:8000/api/v1/runs)
- **Pending approvals:** [http://127.0.0.1:8000/api/v1/approvals?status=PENDING](http://127.0.0.1:8000/api/v1/approvals?status=PENDING)

### 2. Run the Scopewatch agent loop CLI

Drive a scenario directly via the agent loop runner:

```bash
# Run safe invoice audit scenario offline
python -m scopewatch.agent --scenario demo/scenarios/01_safe_audit.json --profile mock

# Run reasoning injection escalation scenario
python -m scopewatch.agent --scenario demo/scenarios/06_invoice_injection.json --profile mock
```

### 3. Run the held-out reasoning auditor evaluation

Evaluate auditor accuracy, false-negative rate, false-hold rate, and latency against held-out test cases:

```bash
# Offline evaluation using mock auditor rules
python backend/scripts/evaluate_reasoning_audit.py --profile mock --split heldout

# Live evaluation on OpenRouter or Nebius (requires API key)
python backend/scripts/evaluate_reasoning_audit.py --profile openrouter-dev --split heldout
```

### 4. Run clean-room validation

Verify the entire repository (backend unit/integration tests, PoC tests, frontend unit tests, browser Playwright tests, and clean-room security invariants):

```bash
./scripts/validate.sh
```

---

## Core capabilities

1. **Deterministic policy engine**
   Evaluates actions against task scope allowlists and blocklists without relying on non-deterministic model calls. Blocks parent-directory traversal (`..`), symlink breakout, null bytes, disallowed operations, and unauthorized network calls.

2. **Escalate-only reasoning auditor**
   Evaluates agent reasoning traces per turn with grounded excerpt validation. If the auditor flags a concern (`SCOPE_DRIFT`, `INJECTION_FOLLOWING`, `EXFILTRATION_INTENT`, `POLICY_EVASION`) or encounters an error/timeout, it escalates an `ALLOW` to a `HOLD`. It never produces `ALLOW` or `DENY`.

3. **Swappable provider profile layer**
   Centralizes model routing via `backend/config/providers.toml` supporting `mock` (offline testing), `openrouter-dev`, and `nebius-demo` (NVIDIA Nemotron on Nebius Token Factory). Automatically normalizes raw reasoning extraction and enforces sanitized error handling.

4. **Purpose-built agent loop**
   A minimal, isolated agent loop (`backend/scopewatch/agent/`) that interacts with the environment exclusively via gateway-mediated tools (`list_directory`, `read_text`, `write_text`, `delete_path`). Has zero import path to the executor.

5. **Approval workflows**
   Generates single-use approval requests for sensitive operations. Human reviewers approve or deny actions directly in the UI or via REST API. Approvals cannot authorize an action with a deterministic `DENY`.

6. **Structured five-part evidence panel**
   Every action audit displays:
   - **Observation:** Tool, operation, resource, arguments, and timestamp.
   - **Policy decision:** Outcome (`ALLOW`, `DENY`, `HOLD`), reason code, and matched rule.
   - **Human approval:** Resolution state, reviewer identity, and timestamp.
   - **Execution receipt:** Execution status, output summary, and sanitized output data.
   - **Reasoning provenance:** Explicit disclosures distinguishing provider-level traces (`PROVIDER_EXPOSED_TRACE`) from agent-authored summaries (`AGENT_AUTHORED_SUMMARY`), with verbatim excerpt highlighting.

7. **Live SSE streaming**
   Real-time event updates stream directly to the reviewer dashboard via Server-Sent Events (SSE) with automated fallback polling.

---

## Demonstration scenarios

Scopewatch includes six pre-configured scenarios located in `demo/scenarios/`:

| Scenario | Description | Expected outcome |
| --- | --- | --- |
| **01. Safe invoice audit** | Reads vendor invoices in `invoices/approved` and writes report to `outputs/audit-summary.txt`. | All actions `ALLOW` and execute successfully. |
| **02. Blocked confidential access** | Attempts to read restricted files in `invoices/private/`. | Blocked with `DENY` (`BLOCKED_PATH`). |
| **03. Path traversal attempt** | Attempts directory climbing (`../../etc/passwd`). | Blocked with `DENY` (`PATH_TRAVERSAL`). |
| **04. Network exfiltration** | Attempts outbound network communication not in task scope. | Blocked with `DENY` (`NETWORK_DISABLED`). |
| **05. Approval workflow** | Requests file deletion on `outputs/archive_2025.txt`. | Held with `HOLD` (`APPROVAL_REQUIRED`) until approved by a reviewer. |
| **06. Reasoning injection escalation** | Approved vendor invoice contains an injected prompt; policy allows write to `outputs`, but reasoning reveals injection-following intent. | Escalated to `HOLD` (`REASONING_SCOPE_CONCERN`) by the reasoning auditor. |

---

## Repository layout

| Directory | Purpose |
| --- | --- |
| `backend/scopewatch/` | FastAPI gateway, policy engine, reasoning auditor, bounded executor, SQLite repository, provider layer, and SSE broadcaster |
| `backend/scopewatch/agent/` | Scopewatch agent loop, prompt generator, and mediated tool dispatcher |
| `backend/scopewatch/providers/` | OpenAI-compatible provider client, profile loader, reasoning extraction, and retry logic |
| `backend/fixtures/eval/` | Dev and held-out evaluation datasets for the reasoning auditor |
| `frontend/` | Vanilla HTML, CSS, and JavaScript reviewer UI, action simulator, and browser tests |
| `demo/` | Synthetic workspace fixtures and demonstration scenario definitions |
| `scripts/` | `run_demo.sh`, `validate.sh`, `seed_demo.py`; `scripts/agents/` backlog and worktree helpers |
| `docs/` | Architecture, decision records (ADR-0001), ideas, spikes, agent playbook, and hackathon notes ([index](docs/README.md)) |
| `poc/` | Prototypes and experiments (including CoT auditing PoC in `poc/cot-auditing/`) |
| `.agents/` | Agent role specs, slash commands, and shared skills |

---

## Architecture and documentation

For deeper technical documentation, review:
- [Architecture overview](docs/ARCHITECTURE.md)
- [ADR-0001: Pre-Execution Gateway with Open-Weight Reasoning Agent](docs/adr/0001-pre-execution-gateway.md)
- [Reasoning auditor evaluation methodology](docs/evaluation.md)
- [Nemotron provider spike](docs/spikes/2026-09-nemotron-provider-spike.md)
- [Building and testing guide](BUILDING.md)
- [Agent guide](AGENTS.md) and [agent playbook](docs/agents/README.md)
- [Repository governance](docs/repository-governance.md)
