# Building notes

This file is a small project diary for the parts of the build that are easy to forget, including the model and token budget behind each Codex or API-assisted run.

The numbers below are shadow prices. Work performed through a ChatGPT or Codex subscription is not automatically an API charge. We use the published API rates only to make the compute easier to compare across runs.

## Astra thread ledger

The current workspace does not expose the Astra thread's raw usage export. The row is therefore an honest placeholder, not an estimate. Fill in the token columns from the thread or API usage record when available.

| Run | Date | Interface | Model | Uncached input | Cached input | Cache writes | Output | API-equivalent cost | Evidence/status |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| Astra thread for this repo | 2026-09-09 | Codex | GPT-6 Astra | unavailable | unavailable | unavailable | unavailable | pending usage export | User-reported run; no raw counters are present in this repository |

Do not replace `unavailable` with zero. Zero means the counter was measured and found to be zero.

## Current GPT-6 Astra rate card

Checked against the [official GPT-6 Astra API model page](https://developers.openai.com/api/docs/models/gpt-6-astra) on 2026-09-09. These are Standard text-token rates per 1 million tokens:

| Counter | Rate |
| --- | ---: |
| Uncached input | $10.00 |
| Cached input | $1.00 |
| Cache writes | $12.50 |
| Output | $50.00 |

For a completed row, calculate:

```text
cost = uncached_input / 1,000,000 * 10.00
     + cached_input   / 1,000,000 * 1.00
     + cache_writes   / 1,000,000 * 12.50
     + output         / 1,000,000 * 50.00
```

Use the usage fields supplied by the run. Do not add reasoning tokens separately if the provider already includes them in the reported output total. Record tool-call charges separately when they apply. The Astra page also notes higher rates for prompts above 272K input tokens, 50% rates for Batch and Flex, and 2x applicable rates for Fast mode. This ledger assumes Standard mode unless a row says otherwise.

## Logging rules

- Record the model name and interface for every run.
- Keep uncached input, cached input, cache writes, and output in separate columns.
- Link to a public usage export or commit when one exists. Never commit API keys, private prompts, workplace data, or raw private traces.
- Keep measured values separate from estimates. Label estimates and record the rate-card date beside them.
- Treat this as accounting for comparison, not proof of a subscription bill.
