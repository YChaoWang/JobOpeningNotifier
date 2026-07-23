# Runtime monitor state (generated locally / in CI). Do not commit.

This directory holds:

- `repository_state.json` — last README SHA per source
- `seen_jobs.json` — jobs already handled
- `pending_jobs.json` — overflow / failed Discord deliveries

These files are **gitignored**. They are not part of the open-source tree.

- **Local:** created automatically under `data/` when you run `check_jobs.py`
- **GitHub Actions:** restored/saved via the Actions cache (not committed to git)

If cache is lost, the next run behaves like a fresh baseline (silent unless you pass `--notify-existing`).
