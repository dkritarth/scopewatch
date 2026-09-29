# Building notes

This file is a project diary and build reference for Scopewatch, including environment setup and the model and token budget behind Codex or API-assisted runs.

The numbers below are shadow prices. Work performed through a ChatGPT or Codex subscription is not automatically an API charge. We use the published API rates only to make the compute easier to compare across runs.

## Local baseline setup and execution

### Prerequisites

- Python 3.12+
- Node.js 22+

### Quick start

```bash
# 1. Install backend dependencies in virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r backend/requirements.lock

# 2. Install frontend dependencies
npm ci --prefix frontend
npx --prefix frontend playwright install --with-deps chromium

# 3. Run all test suites and clean-room security verification
./scripts/validate.sh            # full, incl. Playwright browser suite
./scripts/validate.sh --quick   # same except the browser suite (use while iterating)

# 4. Docs consistency (links, model IDs, localhost allowlist, claims, secrets)
python3 scripts/check_docs_links.py

# 5. Launch the integrated demonstration
./scripts/run_demo.sh                       # scripted invoice scenarios 01-06
./scripts/run_demo.sh --agent               # agent loop, offline mock provider
./scripts/run_demo.sh --coding              # M2 coding scenarios 10-13
PORT=8001 HOST=127.0.0.1 ./scripts/run_demo.sh   # override defaults (8000/127.0.0.1)
```

`backend/requirements.txt` installs the same packages without hashes (used in the 5-minute judge path in `docs/operations/judge-runbook.md`); the lock file above is the reproducible build. Env names match `.env.example` (`SCOPEWATCH_AGENT_PROFILE`, `SCOPEWATCH_AUDITOR_PROFILE`, `OPENROUTER_API_KEY`, `NEBIUS_API_KEY`); `run_demo.sh` also honours `PORT`/`HOST` and writes to `runtime-data/` (gitignored) plus `demo/workspace/` fixtures (created only if missing).

### Python dependency lock files

`backend/requirements.txt` and `poc/cot-auditing/requirements.txt` are the
human-readable inputs (version ranges). The pinned, hashed outputs are
`backend/requirements.lock` and `poc/cot-auditing/requirements.lock`.
Always install from the lock files so every machine gets the same bytes:

```bash
pip install --require-hashes -r backend/requirements.lock
pip install --require-hashes -r poc/cot-auditing/requirements.lock
```

Regenerate the locks with Python 3.12.3 and uv 0.12.18 (`--python-version 3.12`
matches CI's `setup-python: '3.12'`):

```bash
uv pip compile backend/requirements.txt --generate-hashes --python-version 3.12 -o backend/requirements.lock --custom-compile-command "uv pip compile backend/requirements.txt --generate-hashes --python-version 3.12 -o backend/requirements.lock"
uv pip compile poc/cot-auditing/requirements.txt --generate-hashes --python-version 3.12 -o poc/cot-auditing/requirements.lock --custom-compile-command "uv pip compile poc/cot-auditing/requirements.txt --generate-hashes --python-version 3.12 -o poc/cot-auditing/requirements.lock"
```

Compiling onto an existing lock refreshes only what the inputs no longer
allow. To pull in newer versions of everything the range permits, delete the
lock first so uv resolves from scratch, then run the command above.

CI (`backend.yml`, `cot-auditing.yml`, `docker-executor.yml`, `reviewer-ui.yml`)
reinstalls pinned uv 0.12.18, copies the committed lock to a temp file, and
recompiles `requirements.txt` **into that seeded copy** with the same command,
then fails the run when the two differ (`cmp -s`) before installing with
`pip install --require-hashes -r <lockfile>`.

Seeding matters. `uv pip compile` prefers the pins already present in an
existing output file, so compiling into a seeded copy means "does the lock still
satisfy `requirements.txt`", which is what the lock is for. Compiling into a
brand-new path instead would re-resolve from scratch and turn every PyPI release
of an already-pinned transitive dependency into a red build. The check still
fails when it should: a new dependency, a dropped dependency, or a constraint
that the committed pin no longer satisfies all rewrite the seeded output and
break `cmp -s`.


### Running test suites individually

- **Backend tests:**
  ```bash
  PYTHONPATH=backend python3 -m pytest backend/tests -q
  ```

- **Docs link/claim checker (issue #46):**
  ```bash
  python3 scripts/check_docs_links.py
  # PASS on main 2026-09-28: links, model IDs (providers.toml), localhost
  # allowlist, claims, secrets clean — incl. read-only deploy/ check at
  # origin/feature/42-vm-deploy (11 files, no secrets, no docker.sock mount).
  # Checker never edits deploy/ (PR #79) or docs/submission/ (PR #72).
  ```

- **Frontend unit tests:**
  ```bash
  npm test --prefix frontend
  ```

- **Browser Playwright integration tests:**
  ```bash
  npm run test:browser --prefix frontend
  ```

---

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

September 16, 2026, Union Alpha via Codex extended the Python PoC. Codex token usage is unavailable. A minimal OpenRouter `stealth/union-alpha` call reported 30 prompt tokens, including 12 cached, 5 completion tokens, and cost 0. Two subsequent capture probes each reported cost 0. Auditor evaluation costs were not retained. See [validation notes](docs/ideas/union-alpha-2026-09-16-validation.md) for outcomes. These are provider-reported API observations, not subscription-cost estimates.

- Record the model name and interface for every run.
- Keep uncached input, cached input, cache writes, and output in separate columns.
- Link to a public usage export or commit when one exists. Never commit API keys, private prompts, workplace data, or raw private traces.
- Keep measured values separate from estimates. Label estimates and record the rate-card date beside them.
- Treat this as accounting for comparison, not proof of a subscription bill.
