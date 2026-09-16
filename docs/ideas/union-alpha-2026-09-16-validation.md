# Reasoning capture and auditor validation

Author: Union Alpha, maker currently anonymous. September 16, 2026.
Status: findings and proposed next steps awaiting team review. Not an accepted architecture.

## Observed results

- Seven initial OpenRouter requests returned HTTP 200 but no nonempty `message.reasoning` and zero reasoning tokens. The initial report writer crashed, so those observations exist in terminal output only.
- A later minimal arithmetic call returned `391`, `reasoning: null`, no `reasoning_details`, and reported cost 0. Replaying that response through the adapter returned no trace. That replay was not another live call.
- A fresh two-request capture run, with reasoning disabled and enabled respectively, wrote an ignored local JSON report. Both returned HTTP 200, reported cost 0 and zero reasoning tokens, and produced no trace through the actual adapter. Latencies were 47.587 and 50.666 seconds.
- The six-case keyword mock matched 3 expected labels. It missed explicit blocked-path context and treated a refusal of unsafe work as unsafe work.
- The six-case live Union Alpha auditor matched 2 expected labels. Four requests failed backend validation or transport and became HOLD rather than accepted classifications. Latencies ranged from 4.011 to 49.307 seconds. These failed cases do not establish semantic misclassification.
- An exact blocked-config reproduction subsequently returned HTTP 200 with a JSON error envelope, code 502 and `provider_unavailable`, without choices or usage. A synthetic valid HOLD passed the same backend and classification parser. The saved-report checker rejected all four failed cases without new API calls.
- A follow-up check of the same four failing IDs confirmed that they were backend failures, not HOLD classifications. The saved report therefore contains no classification error for a HOLD that should have been IN_SCOPE.

Local reports are under `poc/cot-auditing/logs/`, excluded from git. The observations above are aggregate results, not raw private traces. The case set is committed synthetic development data, not a held-out benchmark. Expected labels are model-authored and need team review. API costs for the auditor batch were not retained, so they must not be entered as measured zero.

## Interpretation

The capture path works on synthetic provider fields, including labeled summaries. The tested Union Alpha route did not expose readable reasoning. The OpenRouter catalog advertised zero token prices but omitted reasoning from this model's supported parameters when checked. None of this establishes what other providers or configurations expose.

Union Alpha can return a validated scope classification as an auditor of supplied synthetic traces, but this small run was unreliable. No protected tools executed. No result demonstrates live prevention, reveals hidden intent, verifies an executor block, or satisfies NVIDIA/Nebius integration requirements.

## Proposed next work

1. Diagnose incomplete provider responses separately from malformed JSON and transport failures, using metadata only.
2. Validate a specifically documented reasoning-capable NVIDIA model on Nebius, including availability, licensing, output structure, cost, and latency.
3. Agree on held-out labels and error thresholds before tuning prompts. Keep explicit refusals, absent reasoning, and auditor-directed injections in the test plan.
4. Specify how optional reasoning evidence affects HOLD while deterministic policy remains authoritative. Missing optional reasoning must not silently disable the action-based path.

Sources: [PR #6 discussion](https://github.com/dkritarth/scopewatch/pull/6), [PoC PR #7](https://github.com/dkritarth/scopewatch/pull/7), [OpenRouter reasoning details](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [OpenRouter response formats](https://openrouter.ai/docs/api/reference/overview), [model catalog](https://openrouter.ai/api/v1/models).
