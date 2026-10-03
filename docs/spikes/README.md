# Spikes

Timeboxed investigations that answer one question with evidence. Written by `researcher` threads (see [.agents/roles/researcher.md](../../.agents/roles/researcher.md)).

File name: `<yyyy-mm>-<topic>.md`. Each spike states:

1. The question, and what answer would change the plan.
2. Sources read, with dates.
3. What was **observed** (run and seen), kept apart from what docs **claim** and what we **infer**.
4. Exact model IDs, versions, endpoints, and commands.
5. Recommendation and follow-up issues.

Negative results count. Never include keys, account identifiers, or raw responses that contain them.

| Spike | Topic | Status |
| --- | --- | --- |
| [2026-09-nemotron-provider-spike.md](2026-09-nemotron-provider-spike.md) | Nemotron reasoning, tool calling, and structured output on Nebius Token Factory and OpenRouter | Superseded in part by the 2026-10 verification |
| [2026-10-live-gateway-verification.md](2026-10-live-gateway-verification.md) | Live Nemotron runs through the full gateway path: invariants, adversarial lures, dashboard | Complete |
| [2026-10-nemotron-provider-verification.md](2026-10-nemotron-provider-verification.md) | Live latency/token measurements, the exact reasoning field, and the #136 live-agent reproduction | Complete; Nebius half blocked on a working key |
