# JobOpeningNotifier

Watch public internship/job README lists on GitHub, merge many sources, and post matching roles to **your** Discord — without changing this upstream repo or the listing repos.

## How it works for users

1. **Fork** this repository (your copy is independent).  
2. Edit **`config.yaml`** in your fork (see [`config.example.yaml`](config.example.yaml)).  
3. Add secret **`DISCORD_WEBHOOK_URL`**.  
4. **Enable Actions** on the fork, then run **Internship Monitor**.  

Actions in your fork only update *your* `data/` state. The bot **reads** listing READMEs; it never writes to SimplifyJobs, sndsh404, or other sources.

**Starter sources** (edit freely):

- [SimplifyJobs/Summer2026-Internships](https://github.com/SimplifyJobs/Summer2026-Internships) — HTML tables  
- [sndsh404/summer-2027-internships](https://github.com/sndsh404/summer-2027-internships) — Markdown tables  

## Quick start

1. Fork this repo  
2. Discord → channel → **Integrations** → **Webhooks** → copy URL  
3. Fork → **Settings → Secrets and variables → Actions** → `DISCORD_WEBHOOK_URL`  
4. Edit `config.yaml` (repos + filters)  
5. **Actions** tab → enable workflows if prompted → **Internship Monitor** → Run workflow  

**First run is silent** (baselines existing jobs, no Discord flood). Later runs only notify **new** matches.

To post the current matching set anytime:

- Actions input: `notify_existing = true`  
- Or locally: `uv run python check_jobs.py --notify-existing`  

Batch limits still apply; overflow goes to `data/pending_jobs.json`.

## Configuration

| Goal | Where |
| --- | --- |
| Add/remove job lists | `config.yaml` → `repositories` |
| Keywords / locations / sponsorship | `config.yaml` → `filters` |
| AI fallback | `config.yaml` → `ai` |
| Discord batch size | `config.yaml` → `notifications` |
| Discord webhook | Secret / env `DISCORD_WEBHOOK_URL` (never commit) |
| Preview only | `uv run python check_jobs.py --dry-run` |
| Dump current matches | `--notify-existing` |

```bash
cp config.example.yaml config.yaml   # optional; then edit
```

## Pipeline

```text
config.yaml
  → MonitorPipeline (orchestrator)
      → NotificationService (pending + Discord checkpoints)
      → RepositoryProcessor (per source)
          → ReadmeFetcher (GitHub)
          → ParseCoordinator (Markdown / HTML / AI)
          → filters + URL dedupe + oldest→newest order
  → atomic state in data/
```

SOLID-oriented layout: `ports.py` (interfaces), `parsing.py`, `notifying.py`, `processing.py`, thin `pipeline.py`.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync --group dev
uv run pytest -q

cp .env.example .env   # optional; fill DISCORD_WEBHOOK_URL
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'

uv run python check_jobs.py --test-discord
uv run python check_jobs.py --dry-run
uv run python check_jobs.py --notify-existing
uv run python check_jobs.py
```

## GitHub Actions

| Item | Value |
| --- | --- |
| Schedule | Every 30 minutes |
| Manual | `workflow_dispatch` |
| Inputs | `test_discord`, `notify_existing` |
| Permissions | `contents: write`, `models: read` |
| Secret | `DISCORD_WEBHOOK_URL` |
| State commit | `chore: update internship monitor state` (only if files changed) |

## Behavior notes

| Topic | Behavior |
| --- | --- |
| Pending queue | Over `max_jobs_per_run` → `data/pending_jobs.json` |
| Partial Discord failure | Accepted jobs checkpointed; rest stay pending |
| Dedup | Same normalized apply URL = same job across sources |
| Notify order | Oldest → newest (newest job is the latest Discord message) |
| Fallback ID | No URL → company + role + location + source repo |
| Filters | Case-insensitive; closed never notified |
| Public forks | Webhook stays secret; committed `data/*.json` is public |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Workflow never runs | Enable Actions on the fork |
| No Discord messages on first run | Expected (silent baseline). Use `notify_existing` |
| No messages later | Set `DISCORD_WEBHOOK_URL`; check filters in `config.yaml` |
| Pending file keeps growing | Webhook missing or Discord errors — fix secret, re-run |
| Repo not found | Check `repo` / `branch` / `readme_path` |

## Security

- Never commit webhook URLs or tokens  
- Logs redact webhook URLs  
- Arbitrary README formats and AI extraction are best-effort  

## License

[MIT](LICENSE) — see also [CONTRIBUTING.md](CONTRIBUTING.md).
