# Synthetic evaluation split v2

This corpus contains 12 dev and 12 heldout single-turn classifier cases, two
per category per split. Four cases in each split expect `NO_CONCERN`; eight
expect `CONCERN`. Each scenario family belongs to one split only. All text,
paths and actions are invented. Planned actions do not establish execution.

The author wrote these cases without historical heldout content, provider
feedback, or auditor-result-driven tuning. The deterministic leakage checks
report zero cross-split pairs and zero literal mock-rule overlaps. This is a
small public synthetic sample, not proof of semantic independence or realistic
agent behavior. Mock results test the tooling, not detection quality.

`manifest.json` pins bytes, counts, version, freeze time and measurement policy.
`frozen_unmeasured` means no provider heldout measurement is recorded. Offline
structural checks do not spend a provider measurement. Do not edit these bytes
after freezing. Future authored cases require a new version and new audit.

Tune only on dev. Before a provider heldout measurement, freeze the code,
prompt, provider configuration, seed and settings in a recorded commit. Keep
every result, including failures; never pick the best run. Never use historical
heldout to tune v2. Only automated tooling should read heldout case bodies.

Run the existing harness with `--cases-dir backend/fixtures/eval/v2`. The
exporter binds each row to these case IDs, categories and expected labels.
Instructions and limitations are in
[the evaluation workflow](../../../../docs/evaluation/reproducibility.md).
