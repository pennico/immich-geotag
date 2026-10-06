# Contributing

Thanks for your interest in improving immich-geotag.

## Ground rules

- **Add-only.** The whole point of this tool is that it never modifies or
  deletes existing photo locations. Any change must keep that promise.
- **Stdlib only.** Python 3.9+, single-file scripts, no new dependencies.
- **No live API calls in tests.** Tests use fake payloads and mocked
  HTTP; they must pass without credentials or network access.
- **Never commit personal data.** `.env`, `*.csv`, `testdata/`, and
  `geotag-data/` are gitignored — keep them out of your PRs.

## Development

```bash
python3 -m unittest discover -p 'test_*.py'   # or: pytest
ruff check .                                  # lint (CI runs this too)
```

CI must be green before a PR can merge.

## Pull requests

1. Fork, create a branch, make your change with a minimal diff.
2. Add or update tests for the behavior you changed.
3. Open a PR against `main` describing what changed and how you tested
   it. If a change affects what results mean (what is counted,
   excluded, grouped, or named), say so explicitly in the description.

Bug reports and feature requests are welcome — use the issue templates.
