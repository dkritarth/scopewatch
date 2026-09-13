# Scopewatch

Working title for our Nebius x NVIDIA Global AI Hackathon project.

Scopewatch is an early-stage concept for reviewing whether an AI agent stays within the task and permissions it was given. It would collect observable actions, available reasoning summaries or traces, and authorization context, then give a human reviewer a clear evidence trail when an agent appears to move outside its scope.

**Status: initial concept and repository setup.** There is no runnable application yet. The interface, architecture, models, and feature scope remain open for team discussion.

## The problem

An employee asks an agent to work with a project folder. During execution, the agent tries to read another department's files, obtain broader credentials, or contact an external server. That action may follow the employee's instruction, a malicious instruction in retrieved content, or the agent's own attempt to finish the task. In each case, the organization needs to understand whether the action exceeds the authority granted to that user and agent.

The question we want to answer is: **Is this agent still operating within its authorized scope, and what evidence supports that judgment?**

## Initial direction

The concept centers on monitoring agent runs in a company environment. Relevant evidence could include the original task, effective permissions, tool calls and results, file and network access attempts, approval events, and reasoning traces exposed by the selected model or runtime.

| Example | What the monitor should help a reviewer establish |
| --- | --- |
| An agent reaches outside its assigned folder | Which resource it requested and whether the user and agent had permission |
| A user asks an agent to retrieve restricted information | Whether the request exceeds organizational policy, despite user intent |
| An agent contacts an unapproved external service | The destination, attempted operation, and authorization context |
| Retrieved content redirects the agent | Whether the action departs from the task after consuming untrusted instructions |

These are motivating scenarios, not implemented features or a finalized backlog. A website, desktop application, or another interface may be appropriate.

## Reasoning visibility and limits

Reasoning monitoring is central to the idea, but access must be verified for each integration. We will only use traces that the provider or runtime legitimately exposes. Reasoning summaries and agent-written explanations must be labeled separately from full reasoning traces. Missing traces must remain visibly missing.

A suspicious trace is evidence for investigation, not proof of malicious intent. A reassuring trace is not proof that an action is authorized. File permissions and network controls need independent enforcement. A monitor that observes completed actions cannot claim to prevent them; blocking would require integration before tool execution.

OpenAI's [internal coding-agent monitoring report](https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment/) describes analyzing reasoning and actions to flag potential misalignment. This is research inspiration; our project has no OpenAI affiliation or access to its internal monitoring implementation.

## Hackathon planning

See the [submission checklist](docs/hackathon.md) for source-linked requirements. The team still needs to select a track and decide how Nebius infrastructure and an NVIDIA open source model contribute to the working application.

## Start here

```bash
git clone https://github.com/dkritarth/scopewatch.git
cd scopewatch
```

Read this README and [CONTRIBUTING.md](CONTRIBUTING.md), then discuss a proposal through an issue. There are no dependencies to install or application commands to run yet. Add verified setup, configuration, run, and demo instructions with the first implementation.

| Location | Purpose |
| --- | --- |
| [docs/](docs/README.md) | Hackathon notes, repository governance, and team discussion |
| [docs/ideas/gpt-6-notes.md](docs/ideas/gpt-6-notes.md) | Suggestions written by GPT-6 via Codex, pending team review |
| [docs/ideas/gemini-3.8-flash-notes.md](docs/ideas/gemini-3.8-flash-notes.md) | Alternative ideation proposals written by Gemini 3.8 Flash, pending team review |
| [BUILDING.md](BUILDING.md) | Working notes and an API-equivalent model/token cost ledger |
| [.github/](.github/) | Issue templates, PR template, labels, and protection configuration |
| [AGENTS.md](AGENTS.md) | Instructions for coding agents contributing here |

Create implementation directories when the team chooses the stack and first deliverable.

## Contributing and license

All changes reach `main` through pull requests, including changes by administrators and coding agents. See [repository governance](docs/repository-governance.md) for enforcement details.

The repository uses the [MIT license](LICENSE). Third-party models, datasets, and dependencies retain their own licenses.
