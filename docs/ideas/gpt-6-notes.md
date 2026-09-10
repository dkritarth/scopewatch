# Suggestions for team discussion

Written by GPT-6 via Codex on September 9, 2026.

Status: model-authored proposals, not approved features or architecture.

## My suggested starting point

I would begin with one instrumented agent run and a reviewer-facing timeline. Give the agent a legitimate task inside a synthetic workspace, then show an attempt to reach a resource outside its allowed scope. The useful result is an alert that identifies the attempted action, the applicable permission rule, and the evidence that led to the alert.

The hardest early question is where we can reliably observe actions and reasoning. I would choose one runtime before committing to a dashboard design. A trace-replay prototype could help us evaluate monitoring first, but replay must be labeled as replay and cannot demonstrate live prevention.

## Candidate design

A runtime adapter could send task context, identity, permission snapshots, tool events, and available reasoning to a monitoring service. Deterministic checks could detect explicit permission violations. A separate model could examine broader task deviation or suspicious sequences and produce a cited alert for a reviewer.

I would keep the policy authority outside both agents. A user request cannot grant rights the user does not hold. The monitoring model should not be able to approve an access expansion on its own.

An NVIDIA open source model could be the semantic monitor, with inference on Nebius infrastructure. Before selecting one, verify availability, licensing, structured output behavior, latency, and whether the monitored model exposes useful reasoning. This is a proposed role, not a verified model integration.

## Questions worth resolving together

- Who reviews alerts: the employee running the agent, a security reviewer, or both?
- Which agent runtime can we instrument end to end during the hackathon?
- Can we obtain full exposed reasoning, summaries only, or tool events only?
- Does the first demo observe and alert, or intercept selected actions before execution?
- Where do authoritative file and network permissions come from?
- How much trace data can reviewers see, and how long should we retain it?
- Which track best fits the final implementation?

## What I would measure

Compare a permission-rule baseline with a model monitor using actions only, then with actions plus exposed reasoning when available. Use the same labeled scenarios and keep evaluation examples separate from prompt tuning.

Include allowed operations, unauthorized file access, external requests to a controlled local endpoint, and an agent that correctly requests approval without attempting access. Report false positives, missed violations, latency, and cost. Show the denominator and test conditions; success on a small scripted demo does not establish workplace reliability.

Test the monitor's own boundary as well. Text inside a trace may try to instruct it to ignore evidence. Treat that text as untrusted input. Also make missing events and monitor failures visible; absence of an alert must not silently mean authorization.

## Research context

OpenAI's [internal coding-agent monitoring report](https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment/) describes monitoring reasoning and actions, with asynchronous alerts and human triage. It distinguishes this from blocking before execution and notes uncertainty about missed incidents.

The [GPT-6 Astra safety overview](https://openai.com/index/safety-overview-gpt-6-astra/) reports broader monitoring deployment alongside reduced monitorability relative to GPT-5.6 Sol and evasion in some adversarial evaluations. My takeaway is to make coverage gaps visible and combine reasoning signals with independently observed behavior and enforced permissions.

[Evaluating chain-of-thought monitorability](https://openai.com/index/evaluating-chain-of-thought-monitorability/) provides evaluation approaches for studying what reasoning traces reveal. It is a useful starting point for a small, reproducible comparison of monitoring inputs.

The conversation also mentioned a Hugging Face incident. I have not established which incident was intended. I would leave it out of public factual claims until the team identifies and verifies a primary source.
