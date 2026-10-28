# Reproducing evaluation evidence

Issue #299 adds offline publication and split checks. It does not run an agent
attack, collect provider observations, or implement the #298 ablation runner.
Classifier accuracy is not live attack success or evidence of prevention.

## Available and missing artifacts

`artifacts.json` lists the four historical live report paths cited by
`docs/evaluation.md`. None has source JSON in this checkout or tracked history
for those paths. Their status is `missing`, their hashes are null, and their
historical metrics remain documented-only and unverified. The five documented
Nebius dev attempts have no surviving per-attempt filenames here. No rows or
confidence intervals were inferred from those descriptions.

```bash
PYTHONPATH=backend python3 -m scripts.check_evaluation_artifacts \
  docs/evaluation/artifacts.json
```

This passes when the declared availability matches disk. It does not verify
missing measurements. If an original report is recovered, keep it locally,
review its synthetic-content provenance, record its actual hash, and export
only a checked sanitized publication. An `available` entry needs
`source_sha256`, `publication_file` and `dataset_manifest`; the checker binds
the publication to that exact source. Do not mark a historical report available
because a new mock report exists. Old reports with incompatible summaries are
rejected rather than silently rewritten. They need a separately labelled
migration/reconciliation before publication.

## Sanitized export and independent check

These commands use the existing auditor harness with the mock profile, without
credentials or network. Runtime intermediates stay in a gitignored directory.

```bash
PYTHONPATH=backend python3 backend/scripts/evaluate_reasoning_audit.py \
  --profile mock --split dev --cases-dir backend/fixtures/eval/v2 \
  --output runtime-data/eval-v2-dev.json --quiet
PYTHONPATH=backend python3 backend/scripts/evaluate_reasoning_audit.py \
  --check runtime-data/eval-v2-dev.json
PYTHONPATH=backend python3 -m scripts.evaluation_evidence export \
  runtime-data/eval-v2-dev.json \
  --manifest backend/fixtures/eval/v2/manifest.json \
  --output runtime-data/eval-v2-dev.public.json
PYTHONPATH=backend python3 -m scripts.evaluation_evidence check \
  runtime-data/eval-v2-dev.public.json \
  --manifest backend/fixtures/eval/v2/manifest.json
```

The exporter allowlists typed result fields, checks every row against the
pinned synthetic dataset, checks the configured profile/model identity, and
requires exact coverage and consistent summary metrics. It strips unknown
fields at all publication levels, including raw traces, explanations, headers,
private arguments and source paths. Model IDs come from provider profiles.
Only known error/concern codes can appear; arbitrary provider error strings
are rejected. The checker rejects added fields, altered counts, labels,
intervals and summaries. Failure diagnostics never echo input values.

The synthetic manifest is a maintainer's content declaration. A schema or
regex cannot prove arbitrary data public. Only reviewed synthetic fixtures
belong in that manifest; never attach private traces or credentials to it.
The tool reads provider configuration for identity checks but never reads API
keys or calls a provider. It rejects a source whose model differs from the
current profile rather than substituting a new model into old evidence.

Published metrics preserve the current #151/#285/#311 token aggregation and
its coverage labels. Missing usage must not be presented as measured zero;
the existing harness's mock zeros mean no reported provider usage. Do not
quote a token total as complete without checking coverage and per-case fields.

## Confidence intervals and denominators

The publication adds two-sided 95% Wilson score intervals without continuity
correction. Each interval includes its numerator, denominator, estimate and
bounds. FNR uses expected-concern cases, FHR uses expected-clean cases, failure
rate uses all cases, and accuracy uses correct verdicts divided by all cases.
`FAILED` stays in the applicable denominator and is counted separately as a
failure, not converted into `NO_CONCERN` or omitted. Missing denominators have
null estimates and bounds. Zero observed events still have a positive upper
bound; for example, 0/12 has an upper bound of about 24.25%.

Intervals describe sampling uncertainty under a binomial assumption. Synthetic
family correlation and contamination can invalidate that assumption. An
interval does not correct leakage or turn a mock classifier into a model
measurement. Historical tables remain unchanged and have no reconstructed
intervals because their source rows are missing.

## Split audits and the frozen v2 corpus

```bash
PYTHONPATH=backend python3 -m scripts.audit_eval_splits \
  --cases-dir backend/fixtures/eval --output runtime-data/v1-split-audit.json
PYTHONPATH=backend python3 -m scripts.audit_eval_splits \
  --cases-dir backend/fixtures/eval/v2 \
  --output runtime-data/v2-split-audit.json --require-clean
```

The committed `v1-split-audit.json` and `v2-split-audit.json` record dataset and
mock-source hashes. The audit compares normalized trace strings across splits
using a sequence similarity threshold of 0.65, shared six-word shingles, exact
matches and explicit scenario-family collisions. Case IDs must also be unique.
Normalization removes path/numeric substitutions and punctuation. The audit
extracts literal rule lists from the mock implementation through Python's AST
and counts phrase hits. Reports contain IDs/counts, not trace excerpts.

V1 produces 17 cross-split pair flags and 53 cases with literal mock overlap.
It remains historical and spent. V2 produces zero flags under these checks.
No provider feedback or prompt changes were used to obtain that result. Broad
categories necessarily recur across splits; semantic leakage remains possible.
The 24-case sample is too small to establish the operational error targets.
No provider-dependent acceptance claim has been verified by this work.

The v2 manifest is frozen before any provider measurement. Freeze the prompt,
code, profile configuration, seed and budgets before a future provider run;
record every run with its commit and hashes. Tune only dev, never the old
heldout or the new heldout. Any fixture change requires a new dataset version.

## Long-trace capture contract

`long-trace-capture.schema.json` is generated from the standalone evaluation
models in `backend/scripts/evaluation_capture.py`. It does not change gateway
schemas or install a provider capture hook. It supports ordered turns, recent
action references, action hashes/statuses, reasoning-field provenance, missing
reasoning, full trace text/hash, known or unknown original size, capture limits,
completion/truncation status, finish reason, latency, context/output budgets
and optional prompt/completion/total usage. Unreported values remain null.

The capture has separate `capture_origin` and `content_origin` labels.
An observed capture can contain synthetic task content. Synthetic traces
cannot claim observed provider provenance. `AGENT_SUMMARY` remains distinct
from `PROVIDER_EXPOSED_TRACE`; the latter requires a dedicated provider
reasoning field. Ordinary response content cannot use that label. Unavailable
reasoning has no invented text, size or hash. Validation checks consistency,
not provider authenticity. Correlate observations with actual capture and
gateway evidence before making a provider claim.

```bash
PYTHONPATH=backend python3 -m scripts.evaluation_capture \
  docs/evaluation/synthetic-long-capture.json
```

The committed example is a hand-generated long synthetic trace over 16,000
characters followed by an unavailable trace. Timings, action hashes and statuses
are synthetic. There are no observed provider captures in this change. Keep raw
observed captures in gitignored `traces/` or `runtime-data/`, even for synthetic
tasks. Never publish raw private traces. The sanitized report exporter does not
copy capture text or provider responses. Realistic observed long-trace coverage
remains unverified until a separately authorized capture run exists.
