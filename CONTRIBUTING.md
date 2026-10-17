# Contributing

Thanks for your interest in improving immich-geotag. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) first.

## Ground rules

- **Add-only.** The whole point of this tool is that it never modifies or
  deletes existing photo locations. Any change must keep that promise.
- **Stdlib only.** Python 3.9+, single-file scripts, no new runtime
  dependencies.
- **No live API calls in tests.** Tests use fake payloads and mocked
  HTTP; they must pass without credentials or network access.
- **Never commit personal data.** `.env`, `*.csv`, photos, `testdata/`,
  and `geotag-data/` are gitignored — keep them out of your PRs. Test
  fixtures use synthetic places, coordinates, and dates, never values
  from a real library.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # pinned pytest + ruff (same pins as pyproject.toml [dev])
pytest -q                                     # tool + dispatcher tests
ruff check .                                  # lint (config in pyproject.toml)
```

CI runs ruff and the test suite on Python 3.9 (the supported floor) and
3.12; it must be green before a PR can merge.

## Pull requests

1. Fork, create a branch, make your change with a minimal diff.
2. Add or update tests for the behavior you changed.
3. Open a PR against `main` and fill in the template: what changed and
   how you tested it. If a change affects what results mean (what is
   counted, excluded, grouped, or named), say so explicitly.

Human contributions are reviewed by the maintainer. Much of the
day-to-day work here is done by AI coding agents (see "How this repo is
built" in the README); the agent automation only acts on PRs and
comments from the maintainer, so your PR will not be picked up or
modified by it.

Bug reports and feature requests are welcome — use the issue templates.
Security problems: see [SECURITY.md](SECURITY.md).
