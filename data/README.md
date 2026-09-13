# Runtime monitor state (generated locally / in CI). Do not commit to `main`.

This directory holds:

- `repository_state.json` — last README SHA per source
- `seen_jobs.json` — jobs already handled
- `pending_jobs.json` — overflow / failed Discord deliveries

These files are **gitignored** on `main`. They are not part of the open-source source tree.

- **Local:** created automatically under `data/` when you run `check_jobs.py`
- **GitHub Actions:** restored/saved on a dedicated **`monitor-state`** branch (not Actions cache)

To fully reset Actions memory, delete the `monitor-state` branch, then run the workflow again (silent baseline unless you pass `--notify-existing`).
