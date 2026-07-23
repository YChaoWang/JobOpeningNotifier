# Contributing

Thanks for improving JobOpeningNotifier.

## Pull requests

- Keep changes focused (one concern per PR when practical).  
- Do **not** commit personal `DISCORD_WEBHOOK_URL`, tokens, or production `data/` state from your fork.  
- Upstream `data/*.json` should stay empty placeholders.  
- Run tests before opening a PR:

```bash
uv sync --group dev
uv run pytest -q
```

## Adding a source repository

Document the README format (Markdown table vs HTML table) in the PR description. Prefer extending the rule parsers; use AI fallback only when needed.

## Questions

Open an issue with the repo URL, a README snippet, and what you expected vs what happened.
