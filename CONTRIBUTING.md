# Contributing

Thanks for improving JobOpeningNotifier.

## Pull requests

- Keep changes focused (one concern per PR when practical).  
- Do **not** commit personal `DISCORD_WEBHOOK_URL`, tokens, or `data/*.json` state.  
- Upstream keeps `data/` documentation only (`data/README.md`); runtime JSON is gitignored.  
- Do not open PRs from the auto-generated `monitor-state` branch (Actions-only memory).  
- Run tests before opening a PR:

```bash
uv sync --group dev
uv run pytest -q
```

## Adding a source repository

Document the README format (Markdown table vs HTML table) in the PR description. Prefer extending the rule parsers; use AI fallback only when needed.

## Questions

Prefer an [issue template](https://github.com/YChaoWang/JobOpeningNotifier/issues/new/choose): missing job, Discord not posting, or new source repo. Include the listing URL, a README snippet, and what you expected vs what happened.
