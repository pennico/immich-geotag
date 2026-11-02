## What changed and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- e.g. new/updated unit tests; `pytest -q` output. Never run against a live Immich library in a PR. -->

## Checklist

- [ ] `ruff check .` and `pytest -q` pass locally
- [ ] Add-only promise kept: no code path modifies or deletes existing locations, and Immich is only written through the reviewed apply flows
- [ ] Stdlib only — no new runtime dependencies
- [ ] No secrets, API keys, `.env`, CSVs, photos, or real personal data (locations, dates, filenames) in code, tests, or docs — use synthetic fixtures
- [ ] If this changes what results mean (what is counted, excluded, grouped, or named), it is called out above
