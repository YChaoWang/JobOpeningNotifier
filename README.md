# JobOpeningNotifier

Open-source code that watches public internship/job README lists on GitHub and posts matching roles to **your** Discord.

You run the code (fork or clone + GitHub Actions). You provide your own Discord webhook. Your job history stays in your repo’s `data/` folder.

**Default sources** (edit in `config.yaml`):

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) (HTML tables)
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) (Markdown tables)

## Architecture

```
config.yaml
  → fetch README + SHA
  → skip unchanged repos (pending queue still drains)
  → Markdown / HTML parsers (GitHub Models AI fallback if needed)
  → normalize + filter + dedupe by application URL
  → Discord notify (batched, retried)
  → atomic state under data/
```

## Quick start

1. Fork this repo  
2. Create a Discord webhook (channel → Integrations → Webhooks)  
3. Add secret `DISCORD_WEBHOOK_URL` in your fork  
4. Enable Actions → run **Internship Monitor** once  

**First run is silent by default:** existing jobs are stored as seen, Discord is not flooded. Later README changes notify only new matches. Use `--notify-existing` (or the workflow input) to send the current matching set.

## Behavior notes

| Topic | Behavior |
| --- | --- |
| Pending queue | Jobs beyond `max_jobs_per_run` go to `data/pending_jobs.json` and are sent on later runs |
| Partial Discord failure | Successfully accepted jobs are checkpointed; the rest stay pending (no duplicates) |
| Dedup | Same normalized apply URL = same job across sources |
| Fallback ID | Without a URL: company + role + location + source repo |
| Filters | Case-insensitive on company/role/location; closed never notified; keyword rejects are not permanently forgotten |
| SHA skip | Unchanged README skips parse; pending notifications still process |
| Public forks | `DISCORD_WEBHOOK_URL` stays encrypted as an Actions secret; committed `data/*.json` is visible on public forks |

## Local commands

```bash
uv sync --group dev
uv run pytest -q

export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
uv run python check_jobs.py --test-discord
uv run python check_jobs.py --dry-run
uv run python check_jobs.py --notify-existing
uv run python check_jobs.py
```

## Config

```yaml
repositories:
  - name: my-list
    repo: owner/repository
    branch: main
    readme_path: README.md
    enabled: true
```

Also configure `filters`, `ai`, and `notifications` in `config.yaml`.

## GitHub Actions

- Schedule: every 30 minutes + `workflow_dispatch`
- Inputs: `test_discord`, `notify_existing`
- Permissions: `contents: write`, `models: read`
- Secret: `DISCORD_WEBHOOK_URL`
- Commits state with `chore: update internship monitor state` only when files change

## Security

- Never commit webhook URLs or tokens  
- Logs redact webhook URLs  
- Arbitrary README formats and AI extraction are best-effort  

## License

[MIT](LICENSE)
