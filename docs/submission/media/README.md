# Submission media gallery

These are **scripted mock captures**, made from a running local gateway with the
six synthetic invoice scenarios and the mock auditor. They show the celestial UI
at commit `2bceb3e033c80892778557f0b798a250efcd9c88`, fetched from `origin/main`.
They do not establish live model choice, live scenario 06/12 success, Nebius
inference, or NVIDIA model usage. The UI's "Live gateway connected" badge means
an API connection, not live inference. All proposed payment content is synthetic.

The PNG captions are outside the unchanged UI screenshot. Keep each caption when
reusing an asset. The [manifest](manifest.json) records the UTC capture time,
commit, frontend tree, browser and viewport, source hashes, image hashes and
sizes, checks, and warnings. Three font URLs return 404 on this pinned gateway;
the screenshots show its fallback fonts. No runtime route or frontend was changed
to obtain these images. IDs and timestamps vary between reruns, so reproduction
means the same scripted state and checks, not byte-identical PNGs.

## Current gallery

![Scripted mock: moonlit home for the seeded invoice-injection run, with a pending request.](scripted-home-moonlit.png)

SCRIPTED MOCK: celestial home for the seeded invoice-injection run. Local gateway
data, no live model inference. Captured at the commit above with fallback fonts.

![Scripted mock: sunbeam activity timeline and selected reasoning hold, with a synthetic fixture trace.](scripted-hold-sunbeam.png)

SCRIPTED MOCK: policy-allowed synthetic write escalated to a reasoning hold by the
mock auditor. The visible trace is a synthetic fixture, not provider-exposed
reasoning. The held action has no successful execution event. Fallback fonts.

![Scripted mock: moonlit request page with exact proposed synthetic content and approve and deny buttons.](scripted-request-moonlit.png)

SCRIPTED MOCK: the pending request exposes the exact proposed write. Capture does
not approve, deny, or execute it. Fallback fonts.

![Architecture illustration: policy precedes audit, final deny stops, and exact single-use approvals gate held actions before execution.](architecture.png)

ARCHITECTURE: authored explanation of gateway-mediated execution, not a runtime
capture or sponsor-use claim. The [editable HTML source](architecture.html) has
semantic text in reading order, numbered steps, text decision labels, and a
caption. It requires no external renderer, fonts, or JavaScript. The source and
PNG are generated together by the capture command. The implementation reference
is [the architecture overview](../../ARCHITECTURE.md).

## Reproduce and verify

From the repository root, install the documented Python dependencies and the
frontend's pinned Playwright dependency, then run:

```bash
pip install -r backend/requirements.txt
npm ci --prefix frontend
npx --prefix frontend playwright install chromium
node scripts/media/capture.mjs --ref 2bceb3e033c80892778557f0b798a250efcd9c88
node scripts/media/verify.mjs
node --test scripts/media/verify.test.mjs
```

The tool requires the backend, frontend, fixtures, and launcher/seeder to match
`--ref`, including no untracked runtime files. Media tooling and documentation
may differ. It creates a scratch database, workspace, and per-run workspace
copies, launches the gateway on a loopback ephemeral port, explicitly selects
scripted mode and mock profiles, and removes scratch state on completion. It
does not inherit provider keys, proxy settings, or remote-executor configuration.
Browser requests outside that loopback origin are blocked and fail the capture.
No provider credential is needed. The launcher creates an ephemeral capture
credential internally and never prints it into the media artifacts.

The tool checks six runs, the scenario 06 reasoning hold, synthetic provenance,
no successful execution of that held action, the visible request content, browser
errors, and PNG signatures and dimensions. Only the three disclosed font 404s
are tolerated. It writes verified images and then the manifest. A failed run
writes no new manifest; always run the verifier before publishing. Successful
verification checks hashes and labels, not the truth of a future live claim.

To refresh after #290 lands, fetch main, use a checkout whose runtime matches that
commit, and run `node scripts/media/capture.mjs --ref origin/main`. An explicit
`--output runtime-data/media-preview` keeps trial output out of the committed
gallery. Inspect every PNG, run the verifier with the same `--output`, then update
this gallery's pinned commit and warnings to match the new manifest. Keep the
README image and Devpost asset list pointed at files that actually exist.

## Live capture and hero GIF checklist

These slots are **PENDING-HUMAN / PROVIDER VERIFICATION**. There are no live PNGs
or hero GIF in this gallery. Do not upload the scripted set as sponsor evidence.
The provider/scenario gate is [issue #214](https://github.com/dkritarth/scopewatch/issues/214).
The recording plan is [the demo script](../demo-script.md).

| Slot | Current status | Evidence needed before publication |
| --- | --- | --- |
| Scenario 06 live reasoning hold, or its actual observed outcome | Pending | Dated chosen profile pair, observed provenance, gateway decision and execution state |
| Scenario 12 live coding injection, or its actual observed outcome | Pending | Same metadata, plus executor configuration and command receipt when present |
| Hero GIF of request, evidence inspection, and reviewer response | Pending | Frames from one labelled run, readable caption, matching static poster and text transcript |
| Devpost gallery upload and public video | Pending human publication | Reviewed assets and captions; published URLs checked by a person |

For each real capture:

- [ ] Complete #214's three runs each for scenarios 06 and 12 under the candidate
  profile pairs, then record the chosen shipped pair and observed limits. Do not
  rerun the held-out evaluation to improve submission numbers.
- [ ] Record UTC date, commit, scenario file/hash, run/action IDs, agent and
  auditor profile names, actual served model fields if exposed, executor, and
  latency. Keep credentials and machine paths out of shared evidence.
- [ ] Use only synthetic workspace content. Inspect the browser and exported
  evidence for tokens, account details, host paths, and unrelated tabs before capture.
- [ ] Capture scope, action, policy decision, audit outcome, exact trace provenance,
  and execution receipt or its absence. Preserve unavailable reasoning visibly.
  Do not relabel summaries, content text, or scripted traces as provider traces.
- [ ] For a held action, inspect exact proposed content and demonstrate a reviewer
  response. Verify stored approval/denial events and the matching receipt or no
  execution. Do not assume a badge proves enforcement or exact single use.
- [ ] If the live run does not HOLD, show that observed outcome and disclose it.
  Do not substitute a scripted hold or edit a screenshot to imply success.
- [ ] Caption sponsor use only when linked evidence establishes the actual provider
  and model roles. Get a human to verify provider/account evidence; this mock
  capture command cannot supply it.
- [ ] For the hero GIF, keep the scripted/live label in every frame, avoid rapid
  flashing, retain readable timing, and supply a static poster, alt text, and a
  plain-text transcript. Inspect the GIF for sensitive text before publication.
- [ ] Review captions and generated hashes, add actual asset links only after the
  files exist, and check README/Devpost previews. Human owners publish the assets
  and record the public URLs. Leave these boxes open until actually verified.

## Notes

The gallery does not assert a shipped agent/auditor profile pair (issue #214). Older September screenshots and the `site/` assets are separate from this set.

The captures were taken from `main` before the hosted judge review controls (#290, #328) landed, so they do not show approval expiry or the reset and reseed controls. Rerun the capture and review the output before replacing this set. No model call is involved.
