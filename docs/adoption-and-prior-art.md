# Adoption and prior art

Scopewatch is for platform and security teams running coding agents that need to
review tool actions before they affect a workspace. A team supplies the task's
allowed paths, tools, and operations. The schema also records network destinations,
but the current policy denies every network operation; listing a destination
does not enable outbound access. The gateway records its
decision before dispatching an action and presents held actions to a reviewer.
This is a student hackathon implementation, with synthetic fixtures and mock
providers available locally. It has no measured deployment ROI or adoption-time
claim.

## Why review actions before execution?

Two real vulnerability disclosures show why an assistant's access and its action
boundary matter. These are vendor-confirmed vulnerabilities, not evidence of
attacks against Scopewatch or claims of exploitation in the wild:

- Microsoft's [M365 Copilot disclosure, CVE-2025-32711](https://msrc.microsoft.com/update-guide/vulnerability/CVE-2025-32711)
  describes AI command injection that lets an unauthorized attacker disclose
  information over a network. The [Microsoft-authored CVE record](https://github.com/CVEProject/cvelistV5/blob/main/cves/2025/32xxx/CVE-2025-32711.json)
  supplies the machine-readable description. This motivates explicit outbound
  permissions and separating untrusted input from authorization.
- Microsoft's [GitHub Copilot and Visual Studio disclosure, CVE-2025-53773](https://msrc.microsoft.com/update-guide/vulnerability/CVE-2025-53773)
  describes command injection that lets an unauthorized attacker execute code
  locally. The [Microsoft-authored CVE record](https://github.com/CVEProject/cvelistV5/blob/main/cves/2025/53xxx/CVE-2025-53773.json)
  confirms that impact. This motivates checking a concrete action before
  execution and limiting the executor's authority.

Scopewatch has not reproduced these vulnerabilities or demonstrated that it
would stop their exploit chains. It does not wrap M365 Copilot or Visual Studio
by default. A local command or network request outside its API bypasses its
policy, auditor, and evidence store.

OWASP's [LLM01:2025 prompt injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
explains how instructions in external content can alter model behavior, including
through obfuscated inputs. [LLM06:2025 excessive agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
recommends minimal functionality and permissions, approval of high-impact
actions, and authorization in downstream code. Scopewatch implements a bounded
example of these recommendations. It does not establish OWASP compliance or
complete mediation for an agent that retains another execution path.

## What overlaps with existing work?

The sources below are maintained by the respective projects, reviewed on
2026-10-28. This is a comparison of documented approaches, not a benchmark or an
exhaustive feature inventory. We did not install or test the other systems.

| Prior art and primary source | Documented approach | Scopewatch's focus and overlap |
| --- | --- | --- |
| [Meta LlamaFirewall](https://github.com/meta-llama/PurpleLlama/blob/main/LlamaFirewall/README.md) | Composes PromptGuard, AlignmentCheck reasoning/trace auditing, CodeShield, and custom scanners. | Reasoning-based goal-alignment auditing already exists. Scopewatch combines an escalate-only auditor with deterministic task scope, stored decisions, exact approvals, a controlled executor, and reviewer evidence. No claim to invent reasoning audits. |
| [NVIDIA NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails/blob/develop/README.md) | Programmable input, dialog, retrieval, execution, and output rails; execution rails can constrain tool interactions. | Tool/action controls overlap. Scopewatch concentrates on the gateway's policy-before-audit order and the action's decision, approval, and execution record. It does not replace conversational rails or content moderation. |
| [Invariant Guardrails](https://github.com/invariantlabs-ai/invariant/blob/main/README.md) | Python-inspired rules over messages and tool-call sequences, usable programmatically or through an LLM/MCP gateway that intercepts calls. | Gateway mediation and rule-based enforcement overlap directly. Scopewatch adds its particular task-scope schema, escalate-only audit contract, provenance labels, grounded excerpts, and reviewer workflow. These are implementation choices, not an exclusive capability claim. |
| [NVIDIA AEGIS 1.0 defensive model card](https://huggingface.co/nvidia/Aegis-AI-Content-Safety-LlamaGuard-Defensive-1.0) | A content-safety classifier trained with a safety taxonomy; the card now names it Llama Nemotron Safety Guard Defensive V1. | Content safety and task authorization answer different questions. A benign-looking code edit may still be outside the task. Scopewatch's auditor assesses scope concerns; it does not provide AEGIS's content-safety taxonomy. This row concerns the linked AEGIS 1.0 model, not every product using the name AEGIS. |

Scopewatch's useful combination is an enforcement order and review record:
deterministic `DENY` remains final; reasoning can only escalate `ALLOW` to
`HOLD`; an attempted failed audit also holds. The executor requires a stored
decision and, for a held action, a valid single-use approval bound to that action.
The capture credential gates verified provenance labels, not truthfulness.
A caller without that credential cannot promote its own text to a verified
provider trace. The auditor's stored excerpts must be grounded in submitted text;
bounded repair can replace a miscopied excerpt with the trace's own characters.
Neither provenance nor grounding proves intent or grants permission.

These contracts are inspectable in the [service](../backend/scopewatch/service.py),
[auditor](../backend/scopewatch/reasoning_audit.py), and focused regression suites
for [provenance](../backend/tests/test_caller_provenance.py),
[required reasoning](../backend/tests/test_required_reasoning.py),
[complete evidence](../backend/tests/test_complete_reasoning.py), and
[approvals](../backend/tests/test_approvals.py). Their presence is repository
evidence, not a new test-run result from this documentation change.

## What adoption requires

The following is an integration work inventory, not a measured cost estimate.
No operator study, elapsed integration time, reviewer-workload measurement,
throughput comparison, or cost-per-protected-action measurement is available.

| Work the adopting team owns | Current implementation and limit |
| --- | --- |
| Route tool calls and remove alternate access | The bundled agent loop dispatches through the gateway. An external agent needs an adapter and permissions that prevent direct filesystem, shell, or network bypass. A hook or preview decision alone is not execution containment. See [Claude Code preflight integration](../integrations/claude-code/README.md). |
| Declare task scope | Configure paths, operations, tools, approval requirements, and allowed commands. A broad scope can permit harmful edits inside an allowed file. Policy does not determine whether an arbitrary code change implements the user's intent. |
| Choose evidence and provider profiles | Agent and auditor profiles are separate. Missing reasoning stays visible. Tasks can use `require_reasoning_for` to hold selected operations when required evidence is absent; optional missing evidence is not a successful audit. Attempted incomplete or malformed audits fail closed. Provider traces, summaries, and caller assertions keep separate labels. |
| Operate approvals and execution | Assign reviewers, protect reviewer/capture credentials, handle approval expiry, and select the executor. Local mock invoice replay needs no model key; coding `run_command` requires Docker. A Docker executor constrains dispatched commands, not the agent's unrelated host processes. |
| Budget for inference and review | Auditing adds provider calls when enabled and evidence is available; held actions require review. Historical isolated auditor latency is not total agent-turn latency or end-to-end adoption cost. No savings or acceptable review volume has been established. |

The [architecture](ARCHITECTURE.md), [provider profiles](agents/provider-profiles.md),
and [building guide](../BUILDING.md) describe current configuration. The
[architecture proposal](architecture/proposed-scopewatch-architecture.md) describes
direction; proposals are not shipped integrations.

## Evidence limits and the shipped profile gate

The tracked [October 3 verification report](spikes/2026-10-nemotron-provider-verification.md)
records historical auditor runs for `nebius-demo`, served as
`nvidia/Nemotron-3_5-Lightning`, and `openrouter-dev`, served as
`nvidia/nemotron-3.5-lightning`. Each live run had 48 cases, including 12 clean
cases. Zero false holds means 0/12 per profile, or 0/24 clean case evaluations
combined, not 0/96 total audits. Both profiles missed the false-negative target.
These historical model/date labels come from the tracked report. They do not
establish performance after subsequent prompt, grounding, and evidence changes.
This work did not re-run inference or verify the original report artifacts.
Evaluation methodology and artifact stewardship remain in
[evaluation documentation](evaluation.md) and
[#299](https://github.com/dkritarth/scopewatch/issues/299).

[#214](https://github.com/dkritarth/scopewatch/issues/214) still gates the shipped
agent/auditor profile pair and dated live evidence for headline scenarios 06 and
12. A configured NVIDIA model on Nebius and an observed historical auditor run
are distinct from a verified live escalation demo with that pair. The offline
scripted scenario demonstrates the gateway workflow on synthetic evidence.

A monitored agent may omit incriminating reasoning, give a reassuring summary,
or encode an instruction to evade an auditor. Monitoring awareness and
obfuscation are pressures to test, not defenses Scopewatch has established.
Independent auditing does not make agent-written evidence faithful. Required
evidence and completeness checks can hold missing or truncated input, but cannot
prove that a complete-looking trace discloses the whole plan. Deterministic
policy still applies to the submitted action; a policy-legal harmful edit may
pass if its intent is concealed. No current evidence supports a guarantee against
such adaptive attacks.
