# Provider feedback draft

Prepared by Codex / GPT-6.1 Sol on 2026-10-28 for
[issue #307](https://github.com/dkritarth/scopewatch/issues/307).
Ready to paste into a Devpost feedback form after human review. The human owns
publication. This draft summarizes existing synthetic probes, not new tests.

## Context and evidence limits

Scopewatch is a pre-execution gateway for AI agents. We use NVIDIA Nemotron
through configurable provider profiles. A dedicated reasoning field matters
because answer text is not evidence of provider-exposed reasoning.

The September spike credits "Subagent 1" and includes a 2026-10-01 result.
The October verification credits the `researcher` role and dates its measurements
2026-10-03. The later correction is reported in #175, opened 2026-10-04.
Its exact post-fix run time is missing. Those observations retain their original
attribution and dates here. None establishes current service behavior.

Raw request and response artifacts for these reports are missing from the
tracked evidence. The repository has probe code, sanitized measurements, and
issue text, not replayable captures. The September spike explicitly marks its
cited gitignored probe log absent. Reproduction below describes the recorded
method, not a request to run it or a claim that this draft reran it.

## Credential type and HTTP 401

**Observation.** The 2026-10-03 researcher record identifies the initial credential
as an AI Cloud service-account key. Token Factory rejected it with HTTP 401.
The AI Cloud endpoint returned 403, which the record interpreted as authentication
accepted but permission denied. A subsequently supplied Token Factory key
returned 200 for the model catalogue. This was a credential-product mismatch,
not evidence of a broken inference endpoint. [Source: verification §4][verification]

**Reproduction.** The recorded comparison used authenticated catalogue requests
against Token Factory and AI Cloud, plus missing-header and bogus-token controls.
Raw requests and responses are missing. No credential or account identifier is
included here.

**Expected.** Document the credential type required by each product beside the
endpoint examples. Where safe, make authentication errors distinguish a missing
credential from one the product cannot accept.

**Impact.** A shared 401 status initially led us to investigate endpoint and
profile configuration. Clear product-specific credential guidance would reduce
that setup work. #175 confirms the later working authentication state rather
than treating the initial rejection as an ongoing outage. [Correction][correction]

## Reasoning fields alongside tool calls

**Observation.** The 2026-10-03 researcher record found Nebius
`message.reasoning` and `message.reasoning_content` null on the inspected plain
completion. Thinking appeared in `content`. On paid OpenRouter
`nvidia/nemotron-3.5-lightning`, all four measured tool-call turns had nonempty
`choices[0].message.reasoning` alongside `tool_calls`. Field presence and lengths
were recorded, not raw thinking text. The earlier gateway verification claimed
Nebius `reasoning_content` worked, so the records must be read together with the
later field-specific finding. [Sources: verification §§4.3, 5.2][verification],
[earlier gateway observations][gateway].

**Reproduction.** The recorded method used a synthetic invoice prompt, a
`read_file` tool schema, and a follow-up synthetic tool result. It inspected
dedicated field names, lengths, token counts, and finish reasons separately from
answer content. Raw response captures are missing.

**Expected.** Publish a per-model contract for reasoning fields on plain and
tool-call turns, including suppression, truncation, and unavailable states.
We do not assume every model must expose reasoning.

**Impact.** Scopewatch records missing reasoning as `UNAVAILABLE` and cannot
promote answer text to a provider trace. The observed provider difference affects
which profile can demonstrate reasoning-based review. It does not establish that
Nebius never exposes reasoning under any configuration.

## Bounded structured JSON

**Observation.** The 2026-10-01 Nebius probes exhausted 256- and 512-token
completion limits without valid JSON when thinking was enabled. Disabling
thinking returned valid JSON in 28 completion tokens. On 2026-10-03, paid
OpenRouter's 256-token reasoning-enabled class returned valid JSON in 2 of 4
calls, versus 4 of 4 with reasoning disabled. [September result][september],
[verification §5.3][verification].

The old probe omitted the profile's `auditor_body`, so its malformed-output
rows do not describe the fixed probe. #175 reports 3/3 SUCCESS for both providers
after the fix. Its quoted Nebius JSON result is 526.3 ms, 28 completion tokens,
zero reasoning tokens, and `finish_reason: stop`. This is reported correction
evidence, not a new measurement. Strict `json_schema` output remains unverified
in the cited spike. [Correction and sanitized output][correction]

**Reproduction.** The recorded comparison held the synthetic auditor task and
token bound fixed while changing reasoning controls. The corrected
[probe][probe] applies the profile's `auditor_body` to the JSON request.
Original raw request and response captures are missing.

**Expected.** Explain that reasoning consumes the completion budget, and provide
a bounded JSON example with model-appropriate reasoning controls and finish-reason
checks. Distinguish JSON-object mode from strict schema enforcement.

**Impact.** An exhausted budget can leave no parseable verdict. Scopewatch holds
an action when an attempted audit fails. Correct controls avoid this observed
probe failure but do not guarantee schema validity or semantic accuracy.

## Documentation and API host migration

**Observation.** The researcher recorded on 2026-10-03 that
`docs.nebius.com/token-factory/` returned 404 and documentation had moved to
`docs.tokenfactory.nebius.com`. The recorded inference endpoint was
`api.tokenfactory.nebius.com/v1/chat/completions`. Existing submission notes also
report confusion from older AI Studio examples using a different API hostname.
This is documentation and setup feedback, not a measured inference outage.
[Verification §3][verification], [submission feedback][submission].

**Reproduction.** The recorded method compared the old documentation URL with
the new documentation index and quickstart. No saved HTTP capture, redirect
chain, or dated AI Studio example artifact is available in the cited records.
Current host behavior was not checked for this draft.

**Expected.** Redirect old documentation links where possible. Put migration
notices beside legacy examples with the current API host and credential product.

**Impact.** Stale examples make it harder to separate a host migration from a
credential mismatch. A short migration guide would simplify first-time setup.

[verification]: ../spikes/2026-10-nemotron-provider-verification.md
[september]: ../spikes/2026-09-nemotron-provider-spike.md
[gateway]: ../spikes/2026-10-live-gateway-verification.md
[correction]: https://github.com/dkritarth/scopewatch/issues/175
[probe]: ../../scripts/spikes/provider_probe.py
[submission]: nebius-nvidia.md#feedback-for-nebius
